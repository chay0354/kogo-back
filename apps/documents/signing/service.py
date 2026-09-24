"""
When a document is signed, where its original goes, and the helpers the email exits call.

The life of an original, while DOCUMENT_SIGNING_ENABLED is on:

1. **Issue** (`issue`). Inside the transaction that numbers the document, a
   SignedOriginal row is written for it — a plain insert in a savepoint, so
   nothing about it can fail the charge or the document around it. The
   signature itself is registered for after the commit.
2. **Sign** (`sign_original`). Drawn once with "מקור" and the signature line,
   signed through the backend, checked, and stored under a row lock. A row
   that is signed is never signed again. When the key is out of reach the row
   stays unsigned and held, and the sign-pending cron signs it later.
3. **Deliver** (`delivery_decision`). Paper when 18ב(ד) forbids mail (cash,
   an unmarked check); held while there is no signature, or — with
   COMPUTERIZED_CONSENT_ENFORCED — no recorded consent (18ב(ג)); none for a
   document kogo does not mail; otherwise email.
4. **Send** (`claim_email`). The mail exits ask for the stored bytes. They get
   them only when the decision is email, the file still matches its SHA-256,
   and nobody sent it before; the claim is marked on the row first, so the
   issuing request and the cron never both send it.

Money never waits on any of this: the row is a savepoint insert, and signing
and sending run after the commit, each in its own try — the pattern of
apps/customers/recurring_billing.py (the receipt follows the charge, and its
failure is logged, not raised).
"""
from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.documents.models import SignedOriginal
from apps.documents.signing import SigningUnavailable, consent_enforced, enabled
from apps.documents.signing.sources import Source, load_source, source_for

logger = logging.getLogger(__name__)

KIND_IR = SignedOriginal.KIND_IR
KIND_STORE = SignedOriginal.KIND_STORE
KIND_FORMAL = SignedOriginal.KIND_FORMAL

EMAIL = SignedOriginal.DELIVERY_EMAIL
PAPER = SignedOriginal.DELIVERY_PAPER
HELD = SignedOriginal.DELIVERY_HELD
NONE = SignedOriginal.DELIVERY_NONE

# What the office reads beside each original (the frontend shows it as is).
REASON_PENDING = 'ממתין לחתימה'
REASON_UNAVAILABLE = 'ממתין לחתימה — שירות החתימה לא זמין כרגע; המסמך ייחתם ויישלח אוטומטית'
REASON_NO_CONSENT = 'אין הסכמה רשומה לקבלת מסמכים ממוחשבים'
REASON_NO_CONSENT_REPORTED = 'אין הסכמה רשומה לקבלת מסמכים ממוחשבים — נשלח (האכיפה כבויה)'
REASON_NO_EMAIL = 'אין כתובת מייל ללקוח — המסמך לא נשלח'
REASON_ARCHIVE = 'המערכת אינה שולחת מסמך זה במייל — המקור החתום שמור בארכיון'
REASON_PRINTED = 'המקור הודפס ונמסר על נייר'
REASON_SENT = 'המקור החתום נשלח במייל'
REASON_TAMPERED = 'הקובץ השמור אינו תואם לטביעת האצבע שלו — לא נשלח; יש לפנות לתמיכה'

# A mail that failed this many times is left for a person; the row says why.
MAX_SEND_ATTEMPTS = 5
# The cron leaves a fresh row to the request that issued it.
CRON_GRACE = timedelta(minutes=2)


def _short_error(exc: BaseException) -> str:
    """What went wrong, for the row and the log — the exception's own words, never a token."""
    text = str(exc) if isinstance(exc, SigningUnavailable) else type(exc).__name__
    return text[:300]


# ── how many archive originals one request signs on the spot ────────────────

_inline_budget: ContextVar[list[int] | None] = ContextVar('kogo_signing_inline_budget', default=None)


def reset_inline_budget(**_kwargs) -> None:
    """Connected to request_started: each request signs up to SIGNING_INLINE_BUDGET archive originals."""
    _inline_budget.set([max(0, int(getattr(settings, 'SIGNING_INLINE_BUDGET', 5) or 0))])


def clear_inline_budget(**_kwargs) -> None:
    """Connected to request_finished: outside a request (a command, the shell) there is no limit."""
    _inline_budget.set(None)


def _take_inline_budget() -> bool:
    budget = _inline_budget.get()
    if budget is None:
        return True
    if budget[0] <= 0:
        return False
    budget[0] -= 1
    return True


# ── the row ─────────────────────────────────────────────────────────────────

class NumberClash(Exception):
    """Another document already holds this number's signed original."""


def _ensure_row(source: Source, *, channel: str = '', email_to: str = '', customer_name: str = '') -> SignedOriginal:
    row, created = SignedOriginal.objects.get_or_create(
        number=source.number,
        defaults={
            'kind': source.kind,
            'source_id': source.source_id,
            'channel': channel or '',
            'delivery': HELD,
            'delivery_reason': REASON_PENDING,
            'document_type_label': source.type_label[:50],
            'customer_name': (customer_name or source.customer_name or '')[:200],
            'document_date': source.document_date,
            'total': source.total,
            'email_to': (email_to or '')[:254],
        },
    )
    if created:
        return row
    if (row.kind, row.source_id) != (source.kind, source.source_id):
        raise NumberClash(f'{source.number} is already the original of {row.kind}:{row.source_id}')
    # A later caller may know what the first did not: that the document is mailed, and to whom.
    changed = []
    if channel and not row.channel:
        row.channel = channel
        changed.append('channel')
    if email_to and not row.email_to:
        row.email_to = email_to[:254]
        changed.append('email_to')
    if customer_name and not row.customer_name:
        row.customer_name = customer_name[:200]
        changed.append('customer_name')
    if changed:
        row.save(update_fields=[*changed, 'updated_at'])
    return row


def issue(kind: str, obj, *, channel: str = '', email_to: str = '', customer_name: str = '') -> None:
    """
    Called where a document is issued, inside its transaction: record it, sign it after the commit.

    `channel` is how kogo mails it ('' when it does not); `email_to` and
    `customer_name` where the mail goes when its caller, not the document,
    knows that (a refund's credit note). Never raises.
    """
    if not enabled():
        return
    try:
        source = source_for(kind, obj)
        if not source.issued():
            return
        with transaction.atomic():
            _ensure_row(source, channel=channel, email_to=email_to, customer_name=customer_name)
        source_id = source.source_id
    except Exception:
        logger.exception('Signing: could not record the original of %s %s (non-fatal)', kind, getattr(obj, 'pk', ''))
        return

    def _sign_after_commit():
        try:
            if not channel and not _take_inline_budget():
                # Archive-only and this request signed its share: the cron signs it.
                return
            sign_original(kind, load_source(kind, source_id).obj, channel=channel, email_to=email_to)
        except Exception:
            logger.exception('Signing: %s %s was not signed after issue (the cron retries)', kind, source_id)

    transaction.on_commit(_sign_after_commit)


# ── signing ─────────────────────────────────────────────────────────────────

def _sign_locked(row: SignedOriginal, source: Source) -> SignedOriginal:
    """Sign `row` (already locked). Leaves it held, with the reason, when it cannot."""
    from apps.documents.signing.backends import get_backend
    from apps.documents.signing.certificate import fingerprint_sha256
    from apps.documents.signing.signer import sign_pdf

    row.sign_attempts = min(row.sign_attempts + 1, 32767)
    try:
        with transaction.atomic():
            backend = get_backend()
            certificate = backend.certificate()
            if certificate is None:
                raise SigningUnavailable('No signing certificate is configured')
            signed = sign_pdf(source.render_original(), backend=backend, certificate=certificate)
    except Exception as exc:
        row.delivery = HELD
        row.delivery_reason = REASON_UNAVAILABLE
        row.last_error = _short_error(exc)
        row.save(update_fields=['sign_attempts', 'delivery', 'delivery_reason', 'last_error', 'updated_at'])
        logger.warning('Signing: %s held — %s', row.number, row.last_error)
        return row

    row.pdf = signed
    row.sha256 = hashlib.sha256(signed).hexdigest()
    row.size = len(signed)
    row.key_id = backend.key_id[:300]
    row.cert_fingerprint = fingerprint_sha256(certificate)
    row.signed_at = timezone.now()
    row.last_error = ''
    row.delivery, row.delivery_reason = _decide(source, row)
    row.save()
    logger.info('Signing: %s signed (%s bytes) → %s', row.number, row.size, row.delivery)
    return row


def sign_original(kind: str, obj, *, channel: str = '', email_to: str = '', customer_name: str = '') -> SignedOriginal | None:
    """
    The document's signed original: signed now, or the one it already has.

    Idempotent: the row is locked and looked at again, and a signed row is
    returned as it is — an original is never signed twice. None when signing
    is off or the document is not issued (a draft, a sale not yet paid).
    A failure to sign is recorded on the row (held), never raised.
    """
    if not enabled():
        return None
    source = source_for(kind, obj)
    if not source.issued():
        return None
    with transaction.atomic():
        row = _ensure_row(source, channel=channel, email_to=email_to, customer_name=customer_name)
    if row.is_signed:
        return row
    with transaction.atomic():
        row = SignedOriginal.objects.select_for_update().get(pk=row.pk)
        if row.is_signed:
            return row
        return _sign_locked(row, source)


# ── where the original goes ─────────────────────────────────────────────────

def _accepts(holder) -> bool:
    return holder is not None and bool(getattr(holder, 'accepts_computerized_documents', False))


def _decide(source: Source, row: SignedOriginal) -> tuple[str, str]:
    if row.paper_original_printed_at:
        return PAPER, REASON_PRINTED
    verdict = source.payment_verdict()
    if not verdict.allowed:
        return PAPER, verdict.reason
    if not row.channel:
        return NONE, REASON_ARCHIVE
    if not (row.email_to or source.default_email):
        return NONE, REASON_NO_EMAIL
    reported = ''
    if not _accepts(source.consent_holder):
        if consent_enforced():
            return HELD, REASON_NO_CONSENT
        from apps.core.computerized_docs import check_consent

        check_consent(source.consent_holder, row.number)  # logs the gap (report only)
        reported = REASON_NO_CONSENT_REPORTED
    if not row.is_signed:
        return HELD, row.delivery_reason if row.delivery_reason in (REASON_UNAVAILABLE,) else REASON_PENDING
    if row.sent_at:
        return EMAIL, REASON_SENT
    return EMAIL, reported


def delivery_decision(kind: str, obj, *, channel: str | None = None) -> tuple[str, str]:
    """
    (delivery, reason) for the document: email, paper, held or none, and why in Hebrew.

    18ב(ד) first (how it was paid), then whether kogo mails it at all, then
    18ב(ג) consent (reported, or enforced), then whether it is signed.
    """
    source = source_for(kind, obj)
    row = SignedOriginal.objects.filter(number=source.number).first()
    if row is None:
        row = SignedOriginal(number=source.number, kind=kind, source_id=source.source_id,
                             channel=channel or '', delivery=HELD, delivery_reason=REASON_PENDING)
    elif channel is not None and not row.channel:
        row.channel = channel
    return _decide(source, row)


# ── sending ─────────────────────────────────────────────────────────────────

@dataclass
class EmailClaim:
    """The right to mail one original, and its bytes. Report back with sent() or failed()."""
    row_id: object
    number: str
    pdf: bytes

    def sent(self) -> None:
        SignedOriginal.objects.filter(pk=self.row_id).update(last_error='', updated_at=timezone.now())

    def failed(self, exc: BaseException) -> None:
        # Given back, so the cron can try again (up to MAX_SEND_ATTEMPTS).
        SignedOriginal.objects.filter(pk=self.row_id).update(
            sent_at=None, last_error=f'שליחה נכשלה: {type(exc).__name__}'[:300], updated_at=timezone.now(),
        )


def claim_email(kind: str, obj, *, channel: str, email_to: str = '') -> EmailClaim | None:
    """
    The stored original to attach, or None when it must not be mailed now.

    Signs first when the document is not signed yet. None — with the reason
    written on the row — when the decision is not email (paper, held, none),
    when the stored file no longer matches its SHA-256, or when the original
    was already mailed. The claim marks the row as sent before the caller
    sends; the caller gives it back with failed() if the mail does not go.
    """
    row = sign_original(kind, obj, channel=channel, email_to=email_to)
    if row is None:
        return None
    source = source_for(kind, obj)
    with transaction.atomic():
        row = SignedOriginal.objects.select_for_update().get(pk=row.pk)
        if email_to and not row.email_to:
            row.email_to = email_to[:254]
        if channel and not row.channel:
            row.channel = channel
        if row.sent_at:
            return None
        delivery, reason = _decide(source, row)
        if delivery != EMAIL:
            row.delivery, row.delivery_reason = delivery, reason
            row.save(update_fields=['email_to', 'channel', 'delivery', 'delivery_reason', 'updated_at'])
            logger.info('Signing: %s not mailed — %s', row.number, delivery)
            return None
        if not row.pdf_intact():
            row.delivery, row.delivery_reason = HELD, REASON_TAMPERED
            row.save(update_fields=['email_to', 'channel', 'delivery', 'delivery_reason', 'updated_at'])
            logger.error('Signing: %s — the stored original does not match its SHA-256; not mailed', row.number)
            return None
        row.sent_at = timezone.now()
        row.send_attempts = min(row.send_attempts + 1, 32767)
        # Sent — and, while consent is only reported, the reason says it went without one.
        row.delivery, row.delivery_reason = EMAIL, reason or REASON_SENT
        row.save(update_fields=[
            'email_to', 'channel', 'sent_at', 'send_attempts', 'delivery', 'delivery_reason', 'updated_at',
        ])
        return EmailClaim(row_id=row.pk, number=row.number, pdf=bytes(row.pdf))


def _send_by_channel(row: SignedOriginal) -> bool:
    """Mail a held or unsent original through the exit that would have mailed it at issue."""
    if row.channel == SignedOriginal.CHANNEL_IR:
        from apps.customers.financial_models import Invoice
        from apps.customers.subscription_invoice_email import send_subscription_invoice_email

        return bool(send_subscription_invoice_email(Invoice.objects.get(pk=row.source_id)))
    if row.channel == SignedOriginal.CHANNEL_STORE:
        from apps.store.invoice_email import send_store_invoice_email
        from apps.store.models import StoreInvoice

        return bool(send_store_invoice_email(StoreInvoice.objects.get(pk=row.source_id)))
    if row.channel == SignedOriginal.CHANNEL_CREDIT_NOTE:
        from apps.documents.models import FormalDocument
        from apps.documents.service import _email_credit_note

        doc = FormalDocument.objects.select_related('business_customer', 'child__family', 'linked_document').get(
            pk=row.source_id,
        )
        return bool(_email_credit_note(doc, customer_name=row.customer_name or None, email=row.email_to or None))
    if row.channel == SignedOriginal.CHANNEL_RENTAL:
        from apps.rental_billing.receipt_email import send_rental_receipt_email

        return bool(send_rental_receipt_email(row.source_id))
    return False


def sign_pending(*, limit: int = 25) -> dict:
    """
    The cron: sign what is still unsigned, then mail what became mailable.

    Bounded by `limit` on each of the two passes; idempotent (a signed row is
    skipped, a sent one is never sent again). Rows are taken least recently
    touched first, so a row that stays held moves to the back and cannot
    starve the rest.
    """
    if not enabled():
        return {'disabled': True}
    limit = max(1, min(int(limit or 25), 200))
    summary = {'signed': 0, 'still_unsigned': 0, 'sent': 0, 'not_sent': 0, 'errors': 0}

    for row in SignedOriginal.objects.filter(signed_at__isnull=True).order_by('updated_at')[:limit]:
        try:
            source = load_source(row.kind, row.source_id)
            with transaction.atomic():
                locked = SignedOriginal.objects.select_for_update().get(pk=row.pk)
                if locked.is_signed:
                    continue
                if not source.issued():
                    # Not an issued document (any more): nothing to sign, and to the back of the queue.
                    _touch(locked, SigningUnavailable('The source is not an issued document'))
                    continue
                signed = _sign_locked(locked, source)
            summary['signed' if signed.is_signed else 'still_unsigned'] += 1
        except Exception as exc:
            summary['errors'] += 1
            logger.exception('Signing cron: %s could not be signed', row.number)
            _touch(row, exc)

    due = (
        SignedOriginal.objects
        .filter(
            signed_at__isnull=False, sent_at__isnull=True, paper_original_printed_at__isnull=True,
            delivery__in=(HELD, EMAIL), send_attempts__lt=MAX_SEND_ATTEMPTS,
            created_at__lte=timezone.now() - CRON_GRACE,
        )
        .exclude(channel='')
        .order_by('updated_at')
    )
    for row in due[:limit]:
        try:
            if _send_by_channel(row):
                summary['sent'] += 1
            else:
                summary['not_sent'] += 1
                # Touched, so a row that stays held goes to the back of the queue.
                SignedOriginal.objects.filter(pk=row.pk).update(updated_at=timezone.now())
        except Exception as exc:
            summary['errors'] += 1
            logger.exception('Signing cron: %s could not be mailed', row.number)
            _touch(row, exc)
    return summary


def _touch(row: SignedOriginal, exc: BaseException) -> None:
    # To the back of the queue, with what went wrong: one broken row must not
    # take the cron's whole batch on every run.
    SignedOriginal.objects.filter(pk=row.pk).update(last_error=_short_error(exc), updated_at=timezone.now())


# ── the office ──────────────────────────────────────────────────────────────

class PrintRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


ALREADY_PRINTED = 'המקור כבר הודפס — כל הדפסה נוספת היא העתק'
ALREADY_MAILED = 'המקור נשלח ללקוח במייל — כל הדפסה נוספת היא העתק'
NOT_SIGNED_YET = 'המקור טרם נחתם — הוא ייחתם בדקות הקרובות; נסו שוב'
STORED_FILE_BROKEN = 'קובץ המקור השמור אינו תקין ולכן לא הודפס. יש לפנות לתמיכה'


def print_original(row_id, user) -> SignedOriginal:
    """
    Hand the stored original out once, on paper (נספח ה'(א)(4): "מקור" on one copy only).

    Under the row lock: an original already printed, or already mailed, is
    refused — every further print is a copy. Once printed it is never mailed.
    """
    with transaction.atomic():
        row = SignedOriginal.objects.select_for_update().get(pk=row_id)
        if not row.is_signed:
            raise PrintRefused(NOT_SIGNED_YET)
        if row.paper_original_printed_at:
            raise PrintRefused(ALREADY_PRINTED)
        if row.sent_at:
            raise PrintRefused(ALREADY_MAILED)
        if not row.pdf_intact():
            logger.error('Signing: %s — the stored original does not match its SHA-256; refusing to print it',
                         row.number)
            raise PrintRefused(STORED_FILE_BROKEN, status=500)
        row.paper_original_printed_at = timezone.now()
        row.paper_original_printed_by = user if getattr(user, 'is_authenticated', False) else None
        fields = ['paper_original_printed_at', 'paper_original_printed_by', 'updated_at']
        if row.delivery != PAPER:
            row.delivery, row.delivery_reason = PAPER, REASON_PRINTED
            fields += ['delivery', 'delivery_reason']
        row.save(update_fields=fields)
    return row


def office_copy() -> bool:
    """Whether the office's downloads print "העתק": always, once the originals are signed and stored."""
    return enabled()
