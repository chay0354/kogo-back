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
   an unmarked check) or the customer has no address to mail it to; held
   while there is no signature, while a tax invoice waits for its allocation
   number, or — with COMPUTERIZED_CONSENT_ENFORCED — no recorded consent
   (18ב(ג)); otherwise email. Never "none" for an original: the owner's
   decision D5 (25.9.2026) is that every original ends up mailed, on the
   hand-delivery list, or held with a reason. Only an archive copy is "none".
4. **Send** (`claim_email`). The mail exits ask for the stored bytes. They get
   them only when the decision is email, the file still matches its SHA-256,
   and nobody sent it before; the claim is marked on the row first, so the
   issuing request and the cron never both send it. A document its caller
   does not mail (a hand-issued invoice, a till sale, a late receipt) is mailed
   right after the commit by `issue` itself, or by the cron.

Money never waits on any of this: the row is a savepoint insert, and signing
and sending run after the commit, each in its own try — the pattern of
apps/customers/recurring_billing.py (the receipt follows the charge, and its
failure is logged, not raised).

An archive copy (purpose 'archive', apps/documents/signing/archive.py) shares
the table and takes part in none of this: it is never drawn as an original,
never mailed, never on the paper list, and never printed as "מקור".
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
# No address to mail the original to: it goes on the hand-delivery list, and
# the cron mails it after all once the customer's card has an address.
REASON_NO_EMAIL = 'אין כתובת מייל — למסירה ידנית'
# A tax invoice to a business above the threshold waits, unsigned, for the
# allocation number the Tax Authority gives it (sources.FormalDocumentSource.awaiting_allocation).
REASON_AWAITING_ALLOCATION = 'ממתין למספר הקצאה'
REASON_ARCHIVE = 'המערכת אינה שולחת מסמך זה במייל — המקור החתום שמור בארכיון'
REASON_PRINTED = 'המקור הודפס ונמסר על נייר'
REASON_SENT = 'המקור החתום נשלח במייל'
REASON_TAMPERED = 'הקובץ השמור אינו תואם לטביעת האצבע שלו — לא נשלח; יש לפנות לתמיכה'

# A mail that failed this many times is left for a person; the row says why.
MAX_SEND_ATTEMPTS = 5
# The cron leaves a fresh row to the request that issued it.
CRON_GRACE = timedelta(minutes=2)
# An original from before every original had a channel (30.9.2026) that never
# went out is mailed by the cron only while it is this recent. An older one
# waits for the office's "שלח" rather than reaching a customer weeks late with
# no warning; reroute_undelivered --apply gives old rows a channel on purpose.
UNROUTED_AUTO_MAIL_WINDOW = timedelta(days=3)


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
    """
    The document's row, created on first sight.

    A new row always has a mail channel: the caller's, or the kind's own
    (Source.default_channel) when the caller names none — so no original is
    left without a way to reach its customer.
    """
    row, created = SignedOriginal.objects.get_or_create(
        number=source.number,
        defaults={
            'kind': source.kind,
            'source_id': source.source_id,
            'channel': channel or source.default_channel,
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
    if row.is_archive_copy:
        # Issued before signing existed and kept as an archive copy: nothing a
        # caller knows about mailing it applies — it is never mailed.
        return row
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

    `channel` is the exit its caller mails it through after the commit ('' when
    the caller does not mail it); `email_to` and `customer_name` where the mail
    goes when its caller, not the document, knows that (a refund's credit
    note). A document its caller does not mail still gets the kind's channel
    (Source.default_channel) and is mailed here, right after it is signed —
    within the request's inline budget; the cron signs and mails the rest.
    Never raises.
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

    mailed_by_caller = bool(channel)

    def _sign_after_commit():
        try:
            if not mailed_by_caller and not _take_inline_budget():
                # Nobody waits on this mail and the request signed its share
                # (a batch — a check plan, the missing-receipts screen): the
                # cron signs it and mails it.
                return
            row = sign_original(kind, load_source(kind, source_id).obj, channel=channel, email_to=email_to)
        except Exception:
            logger.exception('Signing: %s %s was not signed after issue (the cron retries)', kind, source_id)
            return
        if mailed_by_caller or row is None or not row.is_signed or row.sent_at or row.delivery != EMAIL:
            return
        try:
            # Its caller mails nothing: the kind's own exit does, now.
            _send_by_channel(row)
        except Exception:
            logger.exception('Signing: %s was not mailed after issue (the cron retries)', row.number)

    transaction.on_commit(_sign_after_commit)


# ── signing ─────────────────────────────────────────────────────────────────

def _sign_locked(row: SignedOriginal, source: Source) -> SignedOriginal:
    """Sign `row` (already locked). Leaves it held, with the reason, when it cannot."""
    from apps.documents.signing.backends import get_backend
    from apps.documents.signing.certificate import fingerprint_sha256
    from apps.documents.signing.signer import sign_pdf

    if row.is_archive_copy:
        # Never an original drawn on an archive copy's row — that would be a
        # second "מקור". An archive copy is signed as it is created (archive.py).
        return row
    if not row.channel:
        # A row recorded before every original had a channel: the kind's own.
        row.channel = source.default_channel
    if source.awaiting_allocation():
        # Not drawn, not signed: the number belongs on the original, and a
        # number that arrives after it goes on a copy only. Entering the number
        # signs it (release_allocation_hold); the row says why it waits.
        if (row.delivery, row.delivery_reason) != (HELD, REASON_AWAITING_ALLOCATION):
            row.delivery, row.delivery_reason = HELD, REASON_AWAITING_ALLOCATION
            row.save(update_fields=['channel', 'delivery', 'delivery_reason', 'updated_at'])
            logger.info('Signing: %s held — waiting for its allocation number', row.number)
        return row
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
        row.save(update_fields=['channel', 'sign_attempts', 'delivery', 'delivery_reason', 'last_error', 'updated_at'])
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
    is off, the document is not issued (a draft, a sale not yet paid), or it
    was issued before signing existed and its row is an archive copy: its
    original left unsigned, and a second "מקור" is never drawn.
    A failure to sign is recorded on the row (held), never raised.
    """
    if not enabled():
        return None
    source = source_for(kind, obj)
    if not source.issued():
        return None
    with transaction.atomic():
        row = _ensure_row(source, channel=channel, email_to=email_to, customer_name=customer_name)
    if row.is_archive_copy:
        return None
    if row.is_signed:
        return row
    with transaction.atomic():
        row = SignedOriginal.objects.select_for_update().get(pk=row.pk)
        if row.is_signed:
            return row
        if kind == KIND_FORMAL:
            # Drawn from the document as it stands under the row's lock: an
            # allocation number entered by another request since the caller
            # read the document must be on the original (set_allocation_number
            # takes the same lock before it writes the number).
            source = load_source(kind, row.source_id)
        return _sign_locked(row, source)


# ── where the original goes ─────────────────────────────────────────────────

def _accepts(holder) -> bool:
    return holder is not None and bool(getattr(holder, 'accepts_computerized_documents', False))


def _decide(source: Source, row: SignedOriginal) -> tuple[str, str]:
    """
    Where the original goes, and why — email, paper or held; "none" only for an archive copy.

    In this order: an original already handed over on paper stays paper; an
    original not yet signed because it waits for its allocation number is
    held; 18ב(ד) (how it was paid) sends it on paper; no address sends it on
    paper too (the cron mails it after all once one appears); 18ב(ג) consent,
    reported or enforced; and whether it is signed and sent.
    """
    if row.is_archive_copy:
        return NONE, REASON_ARCHIVE
    if row.paper_original_printed_at:
        return PAPER, REASON_PRINTED
    if not row.is_signed and source.awaiting_allocation():
        return HELD, REASON_AWAITING_ALLOCATION
    verdict = source.payment_verdict()
    if not verdict.allowed:
        return PAPER, verdict.reason
    if not (row.email_to or source.default_email):
        return PAPER, REASON_NO_EMAIL
    reported = ''
    if not _accepts(source.consent_holder):
        if consent_enforced():
            return HELD, REASON_NO_CONSENT
        from apps.core.computerized_docs import check_consent

        check_consent(source.consent_holder, row.number)  # logs the gap (report only)
        reported = REASON_NO_CONSENT_REPORTED
    if not row.is_signed:
        kept = (REASON_UNAVAILABLE, REASON_AWAITING_ALLOCATION)
        return HELD, row.delivery_reason if row.delivery_reason in kept else REASON_PENDING
    if row.sent_at:
        return EMAIL, REASON_SENT
    return EMAIL, reported


def delivery_decision(kind: str, obj, *, channel: str | None = None) -> tuple[str, str]:
    """
    (delivery, reason) for the document: email, paper or held, and why in Hebrew.

    The allocation number first (a document waiting for one is held), then
    18ב(ד) (how it was paid), then whether there is an address, then 18ב(ג)
    consent (reported, or enforced), then whether it is signed.
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


_DEFAULT_CHANNELS = {
    SignedOriginal.KIND_IR: SignedOriginal.CHANNEL_IR,
    SignedOriginal.KIND_STORE: SignedOriginal.CHANNEL_STORE,
    SignedOriginal.KIND_FORMAL: SignedOriginal.CHANNEL_FORMAL,
}


def _send_by_channel(row: SignedOriginal, *, email: str = '') -> bool:
    """
    Mail an unsent original through its channel's exit — the one that mails it at issue.

    `email` is an address the office typed for this send (the send endpoint);
    otherwise each exit finds the customer's own. A row from before every
    original had a channel is mailed through its kind's exit. Each exit goes
    through claim_email, so this never mails an original twice.
    """
    channel = row.channel or _DEFAULT_CHANNELS.get(row.kind, '')
    if channel == SignedOriginal.CHANNEL_IR:
        from apps.customers.financial_models import Invoice
        from apps.customers.subscription_invoice_email import send_subscription_invoice_email

        return bool(send_subscription_invoice_email(Invoice.objects.get(pk=row.source_id), email=email))
    if channel == SignedOriginal.CHANNEL_STORE:
        from apps.store.invoice_email import send_store_invoice_email
        from apps.store.models import StoreInvoice

        # A till sale too: its original goes to the family's address (sources.StoreSaleSource).
        return bool(send_store_invoice_email(
            StoreInvoice.objects.select_related('child__family').get(pk=row.source_id),
            email=email or row.email_to, any_sale=True,
        ))
    if channel == SignedOriginal.CHANNEL_CREDIT_NOTE:
        from apps.documents.models import FormalDocument
        from apps.documents.service import _email_credit_note

        doc = FormalDocument.objects.select_related('business_customer', 'child__family', 'linked_document').get(
            pk=row.source_id,
        )
        return bool(_email_credit_note(doc, customer_name=row.customer_name or None,
                                       email=email or row.email_to or None))
    if channel == SignedOriginal.CHANNEL_RENTAL:
        from apps.rental_billing.receipt_email import send_rental_receipt_email

        return bool(send_rental_receipt_email(row.source_id, email=email))
    if channel == SignedOriginal.CHANNEL_FORMAL:
        from apps.documents.document_email import send_formal_document_email

        return bool(send_formal_document_email(row, email=email))
    return False


def _allocation_arrived(originals, limit: int) -> list[SignedOriginal]:
    """
    Originals held for an allocation number whose document now carries one.

    Normally the number is entered through set_allocation_number, which signs
    the original at once; this finds the ones whose number arrived any other
    way (or whose signing after the entry failed), without loading every
    waiting document on every run.
    """
    from apps.documents.models import FormalDocument

    waiting = originals.filter(
        signed_at__isnull=True, kind=KIND_FORMAL, delivery=HELD, delivery_reason=REASON_AWAITING_ALLOCATION,
    )
    ids = list(waiting.values_list('source_id', flat=True)[:500])
    if not ids:
        return []
    arrived = {
        str(pk) for pk in FormalDocument.objects.filter(pk__in=ids)
        .exclude(allocation_number='').exclude(allocation_number__isnull=True).values_list('pk', flat=True)
    }
    return [row for row in waiting.filter(source_id__in=arrived).order_by('updated_at')[:limit]]


def sign_pending(*, limit: int = 25) -> dict:
    """
    The cron: sign what is still unsigned, then mail what became mailable.

    Three passes, each bounded by `limit`; idempotent (a signed row is skipped,
    a sent one is never sent again):

    1. sign what is unsigned — except an original waiting for its allocation
       number, which is signed once its number is on the document;
    2. mail the signed originals that are due (email, or held and now
       mailable);
    3. look again at the originals on the hand-delivery list only because the
       customer had no address: once the card has one, and the payment allows
       mail, they are mailed — so the paper list shrinks by itself.

    Rows are taken least recently touched first, so a row that stays where it
    is moves to the back and cannot starve the rest.
    """
    if not enabled():
        return {'disabled': True}
    limit = max(1, min(int(limit or 25), 200))
    summary = {'signed': 0, 'still_unsigned': 0, 'sent': 0, 'not_sent': 0, 'paper_to_email': 0, 'errors': 0}

    # An archive copy is never here: it is signed as it is created, and never mailed.
    originals = SignedOriginal.objects.exclude(purpose=SignedOriginal.PURPOSE_ARCHIVE)
    unsigned = list(
        originals.filter(signed_at__isnull=True)
        .exclude(delivery=HELD, delivery_reason=REASON_AWAITING_ALLOCATION)
        .order_by('updated_at')[:limit]
    )
    unsigned += _allocation_arrived(originals, limit)
    for row in unsigned:
        try:
            with transaction.atomic():
                locked = SignedOriginal.objects.select_for_update().get(pk=row.pk)
                if locked.is_signed:
                    continue
                # Read under the row's lock: an allocation number entered or
                # cleared meanwhile (set_allocation_number takes this lock) is
                # what the original shows.
                source = load_source(row.kind, row.source_id)
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

    unsent = originals.filter(
        signed_at__isnull=False, sent_at__isnull=True, paper_original_printed_at__isnull=True,
        send_attempts__lt=MAX_SEND_ATTEMPTS, created_at__lte=timezone.now() - CRON_GRACE,
    ).exclude(channel='', created_at__lt=timezone.now() - UNROUTED_AUTO_MAIL_WINDOW)
    due = unsent.filter(delivery__in=(HELD, EMAIL)).order_by('updated_at')
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

    # Only "no address" can change by itself: cash stays cash. Anything else
    # on the paper list is left for the office to print.
    no_address = unsent.filter(delivery=PAPER, delivery_reason=REASON_NO_EMAIL).order_by('updated_at')
    for row in no_address[:limit]:
        try:
            delivery, _reason = _decide(load_source(row.kind, row.source_id), row)
            if delivery == EMAIL and _send_by_channel(row):
                summary['paper_to_email'] += 1
                summary['sent'] += 1
            else:
                SignedOriginal.objects.filter(pk=row.pk).update(updated_at=timezone.now())
        except Exception as exc:
            summary['errors'] += 1
            logger.exception('Signing cron: %s could not be looked at again', row.number)
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
ARCHIVE_NOT_ORIGINAL = (
    'זהו העתק לארכיון ולא המקור — המקור נמסר ללקוח כשהמסמך הופק. '
    'את ההעתק אפשר להוריד מהארכיון'
)


def print_original(row_id, user) -> SignedOriginal:
    """
    Hand the stored original out once, on paper (נספח ה'(א)(4): "מקור" on one copy only).

    Under the row lock: an original already printed, or already mailed, is
    refused — every further print is a copy. Once printed it is never mailed.
    An archive copy is refused outright: it is not an original at all.
    """
    with transaction.atomic():
        row = SignedOriginal.objects.select_for_update().get(pk=row_id)
        if row.is_archive_copy:
            raise PrintRefused(ARCHIVE_NOT_ORIGINAL)
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


# ── the office: "שלח / שלח שוב" ─────────────────────────────────────────────

class SendRefused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


NO_ADDRESS = 'אין כתובת מייל ללקוח — יש להזין כתובת כדי לשלוח'
NO_MAIL_PROVIDER = 'לא מוגדר שירות דואר — המסמך לא נשלח'
WAITING_FOR_ALLOCATION = 'המסמך ממתין למספר הקצאה — הזינו את המספר, והמקור ייחתם ויישלח'
NOT_SENT = 'המקור לא נשלח'

SENT_ORIGINAL = 'original'
SENT_COPY = 'copy'


@dataclass
class SendResult:
    """What the office's send did: the original (once) or a copy, and to which address."""
    sent: str
    email: str
    number: str
    delivery: str
    delivery_reason: str


def send_to_customer(row_id, *, email: str = '', user=None) -> SendResult:
    """
    "שלח / שלח שוב": mail the customer their document.

    - The original has not left yet: it is mailed now — the stored signed bytes,
      through its channel's exit and claim_email, so exactly once and only when
      the decision is email. An address the office typed is kept on the row
      (where the original went). When it may not go by mail — paid in cash, an
      unmarked check, no signature yet, waiting for its allocation number —
      nothing is sent and the refusal says why, in the row's own words: a copy
      in the customer's mailbox before the paper original is handed over would
      read as the document itself.
    - The original already left (mailed, or printed for hand delivery), or the
      row is an archive copy: a copy is mailed — drawn again as "העתק", never
      the original's bytes (נספח ה'(א)(4)). The row does not change; the send
      is logged.

    Raises SignedOriginal.DoesNotExist, or SendRefused (with an HTTP status).
    Never mails an address that is not a valid e-mail (the view checks it).
    """
    from apps.documents.document_email import email_configured, send_document_copy_email

    row = SignedOriginal.objects.get(pk=row_id)
    source = load_source(row.kind, row.source_id)
    address = (email or row.email_to or source.default_email or '').strip()
    if not address:
        raise SendRefused(NO_ADDRESS, 400)
    if not email_configured():
        raise SendRefused(NO_MAIL_PROVIDER, 503)
    who = getattr(user, 'pk', None)

    if row.is_archive_copy or row.sent_at or row.paper_original_printed_at:
        send_document_copy_email(row, source, email=address)
        logger.info('Signing: a copy of %s was mailed to the customer by user %s', row.number, who)
        return SendResult(SENT_COPY, address, row.number, row.delivery, row.delivery_reason)

    if email and email != row.email_to:
        # Where the original is going, for the record — only while it has not gone.
        SignedOriginal.objects.filter(pk=row.pk, sent_at__isnull=True).update(
            email_to=email[:254], updated_at=timezone.now(),
        )
        row.refresh_from_db()
    sent = _send_by_channel(row, email=address)
    row.refresh_from_db()
    if sent and row.sent_at:
        logger.info('Signing: the original %s was mailed by user %s', row.number, who)
        return SendResult(SENT_ORIGINAL, address, row.number, row.delivery, row.delivery_reason)
    if (row.delivery, row.delivery_reason) == (HELD, REASON_AWAITING_ALLOCATION):
        raise SendRefused(WAITING_FOR_ALLOCATION)
    if row.sent_at:
        # Someone else (the cron) mailed it a moment ago.
        raise SendRefused(REASON_SENT)
    raise SendRefused(row.delivery_reason or NOT_SENT)


# ── the allocation number ───────────────────────────────────────────────────

ALLOCATION_ON_ORIGINAL = (
    'המקור כבר נחתם עם מספר הקצאה {number} — אי אפשר לשנות או למחוק אותו. '
    'מספר שהתקבל אחרי ההנפקה נרשם על העתק בלבד'
)
ALLOCATION_COPY_ONLY = 'המקור נחתם לפני שהוזן מספר הקצאה — המספר יופיע על העתקים בלבד'


def signed_original_of(number: str, *, lock: bool = False) -> SignedOriginal | None:
    """The document's original (never its archive copy), locked when asked — or None."""
    qs = SignedOriginal.objects.exclude(purpose=SignedOriginal.PURPOSE_ARCHIVE).filter(number=number)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def release_allocation_hold(doc_id) -> SignedOriginal | None:
    """
    An allocation number was just written: sign the original now, and mail it after the commit.

    Called once the number's own transaction is closed. Signed from the
    document as it stands (sign_original reloads it under the row's lock), so
    the number is on the original. Mailed after the commit — never from inside
    a transaction that could still roll the number back — through the row's
    channel (the hand-issued exit, or the rental receipt's), exactly once.
    Never raises: a failure is logged, and the cron signs and mails it later.
    """
    if not enabled():
        return None
    try:
        source = load_source(KIND_FORMAL, doc_id)
        row = sign_original(KIND_FORMAL, source.obj)
    except Exception:
        logger.exception('Signing: the original of document %s was not signed after its allocation number', doc_id)
        return None
    if row is None or not row.is_signed or row.sent_at or row.delivery != EMAIL:
        return row
    row_id = row.pk

    def _mail_after_commit():
        try:
            unsent = SignedOriginal.objects.filter(pk=row_id, sent_at__isnull=True).first()
            if unsent is not None:
                _send_by_channel(unsent)
        except Exception:
            logger.exception('Signing: %s was not mailed after its allocation number (the cron retries)', row.number)

    transaction.on_commit(_mail_after_commit)
    return row


# ── the 'none' rows of before 25.9.2026 ─────────────────────────────────────

def reroute_undelivered(*, apply: bool = False, since=None) -> dict:
    """
    Decide again, by today's rules, every original left 'none' before every original had a channel.

    Each gets its kind's channel, and email (the cron mails it), paper (the
    hand-delivery list) or held. Nothing is mailed here, and an archive copy
    is never touched. `since` (a date) limits it to documents dated from then.
    Without `apply` nothing is written: the counts say what would change.
    """
    rows = (
        SignedOriginal.objects.exclude(purpose=SignedOriginal.PURPOSE_ARCHIVE)
        .filter(delivery=NONE).order_by('created_at')
    )
    if since is not None:
        rows = rows.filter(document_date__gte=since)
    counts = {'examined': 0, EMAIL: 0, PAPER: 0, HELD: 0, 'errors': 0, 'by_kind': {}}
    # The ids first: each row is then written in a transaction of its own, never
    # while a cursor over the table is open.
    for row_id in list(rows.values_list('pk', flat=True)):
        row = SignedOriginal.objects.get(pk=row_id)
        counts['examined'] += 1
        try:
            source = load_source(row.kind, row.source_id)
            row.channel = row.channel or source.default_channel
            delivery, reason = _decide(source, row)
        except Exception:
            counts['errors'] += 1
            logger.exception('Signing reroute: %s could not be decided', row.number)
            continue
        counts[delivery] = counts.get(delivery, 0) + 1
        per_kind = counts['by_kind'].setdefault(row.kind, {})
        per_kind[delivery] = per_kind.get(delivery, 0) + 1
        if apply:
            with transaction.atomic():
                locked = SignedOriginal.objects.select_for_update().get(pk=row.pk)
                if locked.delivery != NONE or locked.is_archive_copy:
                    continue
                locked.channel = locked.channel or source.default_channel
                locked.delivery, locked.delivery_reason = delivery, reason
                locked.save(update_fields=['channel', 'delivery', 'delivery_reason', 'updated_at'])
    return counts
