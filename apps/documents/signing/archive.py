"""
The signed archive: a signed, stored copy of every document kogo issued before signing existed.

A document issued while DOCUMENT_SIGNING_ENABLED is on gets its original signed
and stored at issue (service.py). Everything issued before that has no stored
file at all — each download draws it again from its record. The owner wants
every one of them in the archive the same way the new ones are: a PDF, signed
with the business's key, its SHA-256 kept, the bytes never rewritten.

It cannot be an original. The customer's "מקור" left when the document was
issued, and the software must never produce "מקור" twice (תקנה 9א(א)(2);
הוראה 18(ב)(2), נספח ה'(א)(4)). So the archive copy is drawn by the same
generators with `archive=True`: "העתק לארכיון" where the original says "מקור",
the signature line, a note saying when and why it was drawn again and that it
is not what the customer got, and the seal with "העתק לארכיון" in its centre.
The signature's own /Reason says the same.

It never goes anywhere. The row is purpose='archive', channel '', delivery
'none': the email exits, the sign-pending cron, the hand-delivery list and
"הדפס מקור" all leave it alone (service.py). It has a switch of its own,
SIGNING_ARCHIVE_ENABLED, because nothing about it reaches a customer — it may
run while the customer-facing switch waits for the letter to פקיד השומה (18ב(ב)).

Which documents: exactly the ones the fiscal register counts as issued
(apps/documents/register.py), in their three tables —

- lesson receipts (Invoice) numbered in the IR run, whose charge went through:
  a receipt that is 'pending' or 'failed' never became a document, and an old
  'INV-…' number is not a fiscal number;
- store sales (StoreInvoice) numbered in the ST/SD run and issued as the
  signing service reads it (StoreSaleSource.issued: paid, refunded afterwards,
  or put on monthly billing) — a sale still pending or failed is not;
- every FormalDocument that is not a draft, except a store sale's Tranzila copy:
  the sale is archived once, as kogo's own ST/SD document, which is also what
  the signing service signs at issue. The copy is Tranzila's document, and
  Tranzila keeps its original.

Only documents created before signing went on. Once DOCUMENT_SIGNING_ENABLED
is on, a new document is signed at issue as its original; if an archive batch
reached it first, the archive copy would take its number and the customer would
never get a signed original. So the archive takes only documents created before
SIGNING_ARCHIVE_ISSUED_BEFORE — the moment signing went on — and refuses to run
at all while signing is on and that moment is not set.

One row per number, as for the originals: a document that already has a row —
its original, or an archive copy from an earlier run — is skipped, and a number
that belongs to another document's row (NumberClash) is reported for a person,
never forced.

Signing is all or nothing. The row is created, the copy drawn and signed, and
the bytes stored in one transaction under the row's lock; when anything fails
the transaction rolls back and no half row is left — above all no unsigned
archive row, which the sign-pending cron would otherwise find.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime

from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q, QuerySet
from django.db.models.functions import Cast
from django.utils import timezone

from apps.documents.models import SignedOriginal
from apps.documents.numbering import LESSON_RUN_REGEX, STORE_RUN_REGEX
from apps.documents.signing import SigningUnavailable, archive_enabled, enabled as signing_enabled
from apps.documents.signing.service import NumberClash, _short_error
from apps.documents.signing.sources import StoreSaleSource, load_source

logger = logging.getLogger(__name__)

ARCHIVE = SignedOriginal.PURPOSE_ARCHIVE
KIND_IR = SignedOriginal.KIND_IR
KIND_STORE = SignedOriginal.KIND_STORE
KIND_FORMAL = SignedOriginal.KIND_FORMAL
# The order a batch walks them in.
KINDS = (KIND_IR, KIND_STORE, KIND_FORMAL)
KIND_LABELS = dict(SignedOriginal.KIND_CHOICES)

# What the office reads beside an archive copy (the originals' list shows it as is).
REASON_ARCHIVE_COPY = 'העתק לארכיון'
# The signature dictionary's /Reason — what Acrobat's signature panel shows.
# The originals' reason starts with "מקור"; this one must not.
SIGNATURE_REASON = 'העתק לארכיון של מסמך ממוחשב, חתום בחתימה אלקטרונית מאובטחת'

# register.lesson_documents: "a receipt is issued with the charge that paid it".
VOID_LESSON_STATUSES = ('pending', 'failed')

# One call signs at most this many: Vercel caps a function's run, and there is
# no maxDuration in vercel.json. The time budget is the other bound.
MAX_BATCH = 50
DEFAULT_BATCH = 25
DEFAULT_TIME_BUDGET = 8.0
_CHUNK = 20

FAILED_CLASH = 'המספר כבר רשום כמקור של מסמך אחר — לא נחתם; יש לבדוק ידנית'
FAILED_OTHER = 'הפקת העתק הארכיון נכשלה'


class ArchiveDisabled(Exception):
    """SIGNING_ARCHIVE_ENABLED is off."""


class ArchiveNeedsCutoff(ArchiveDisabled):
    """Signing is on and SIGNING_ARCHIVE_ISSUED_BEFORE is missing or unreadable."""


# ── which documents ─────────────────────────────────────────────────────────

def issued_before() -> datetime | None:
    """
    The moment signing went on (SIGNING_ARCHIVE_ISSUED_BEFORE), or None.

    None only while signing is off — then every document is from before it.
    With signing on, a missing or unreadable moment is ArchiveNeedsCutoff: the
    archive must not guess where the originals begin.
    """
    from django.conf import settings

    raw = (getattr(settings, 'SIGNING_ARCHIVE_ISSUED_BEFORE', '') or '').strip()
    if not raw:
        if signing_enabled():
            raise ArchiveNeedsCutoff('DOCUMENT_SIGNING_ENABLED is on and SIGNING_ARCHIVE_ISSUED_BEFORE is not set')
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ArchiveNeedsCutoff('SIGNING_ARCHIVE_ISSUED_BEFORE is not an ISO 8601 moment') from None
    if timezone.is_naive(moment):
        raise ArchiveNeedsCutoff('SIGNING_ARCHIVE_ISSUED_BEFORE needs its UTC offset')
    return moment


def eligible(kind: str) -> QuerySet:
    """The documents of `kind` the fiscal register counts as issued before signing went on."""
    documents = _issued(kind)
    cutoff = issued_before()
    return documents.filter(created_at__lt=cutoff) if cutoff is not None else documents


def _issued(kind: str) -> QuerySet:
    """The documents of `kind` the fiscal register counts as issued (see the module's docstring)."""
    if kind == KIND_IR:
        from apps.customers.financial_models import Invoice

        return (
            Invoice.objects
            .filter(invoice_number__regex=LESSON_RUN_REGEX)
            .exclude(status__in=VOID_LESSON_STATUSES)
        )
    if kind == KIND_STORE:
        from apps.store.models import StoreInvoice

        return (
            StoreInvoice.objects
            .filter(invoice_number__regex=STORE_RUN_REGEX)
            .filter(Q(payment_method='monthly_billing') | Q(payment_status__in=StoreSaleSource.ISSUED_STATUSES))
        )
    if kind == KIND_FORMAL:
        from apps.documents.models import FormalDocument
        from apps.store.models import StoreInvoice

        return (
            FormalDocument.objects
            .exclude(document_type='draft')
            .filter(~Exists(StoreInvoice.objects.filter(formal_document=OuterRef('pk'))))
        )
    raise ValueError(f'Unknown kind {kind!r}')


def _has_row(kind: str) -> Exists:
    """The document already has a row of its own — its original, or an archive copy."""
    return Exists(SignedOriginal.objects.filter(
        kind=kind, source_id=Cast(OuterRef('pk'), output_field=CharField()),
    ))


def _dated(kind: str, qs: QuerySet, since: date | None) -> QuerySet:
    """From `since` on, newest first, by the document's own date (the Israeli day for a timestamp)."""
    if kind == KIND_IR:
        if since:
            qs = qs.filter(invoice_date__date__gte=since)
        return qs.order_by('-invoice_date', '-invoice_number')
    if kind == KIND_STORE:
        if since:
            qs = qs.filter(issue_date__date__gte=since)
        return qs.order_by('-issue_date', '-invoice_number')
    if since:
        qs = qs.filter(document_date__gte=since)
    return qs.order_by('-document_date', '-created_at', '-document_number')


def archive_candidates(kind: str, since: date | None = None) -> QuerySet:
    """
    The eligible documents of `kind` with no row yet, newest first by document date.

    A document whose number already belongs to another document's row stays
    here: signing it fails with NumberClash, which is reported, not hidden.
    """
    return _dated(kind, eligible(kind).filter(~_has_row(kind)), since)


# ── signing one ─────────────────────────────────────────────────────────────

def _signer():
    """The configured backend and its certificate. SigningUnavailable when either is missing."""
    from apps.documents.signing.backends import get_backend

    backend = get_backend()
    certificate = backend.certificate()
    if certificate is None:
        raise SigningUnavailable('No signing certificate is configured')
    return backend, certificate


def sign_archive(kind: str, obj) -> SignedOriginal | None:
    """
    Sign `obj` into the archive: "העתק לארכיון", signed and stored. The row, or None when skipped.

    None when the document is not eligible, or already has a row — its
    original, or an archive copy from an earlier run (idempotent: the row is
    created under its lock, and a concurrent run finds it and skips). Raises
    ArchiveDisabled when the switch is off, NumberClash when the number belongs
    to another document's row, and SigningUnavailable when the key or the
    certificate is out of reach; any failure rolls the row back with it.
    Never mails, never marks anything sent, never touches paper delivery.
    """
    from apps.documents.signing.certificate import fingerprint_sha256
    from apps.documents.signing.signer import sign_pdf

    if not archive_enabled():
        raise ArchiveDisabled('SIGNING_ARCHIVE_ENABLED is off')
    if not eligible(kind).filter(pk=obj.pk).exists():
        return None
    source = load_source(kind, obj.pk)
    if SignedOriginal.objects.filter(kind=source.kind, source_id=source.source_id).exists():
        return None

    with transaction.atomic():
        row, created = SignedOriginal.objects.get_or_create(
            number=source.number,
            defaults={
                'kind': source.kind,
                'source_id': source.source_id,
                'purpose': ARCHIVE,
                'channel': '',
                'delivery': SignedOriginal.DELIVERY_NONE,
                'delivery_reason': REASON_ARCHIVE_COPY,
                'document_type_label': source.type_label[:50],
                'customer_name': (source.customer_name or '')[:200],
                'document_date': source.document_date,
                'total': source.total,
                'email_to': '',
            },
        )
        if not created:
            if (row.kind, row.source_id) != (source.kind, source.source_id):
                raise NumberClash(f'{source.number} is already the original of {row.kind}:{row.source_id}')
            return None
        # The lock sign_original takes on an original; the row is ours until the commit.
        row = SignedOriginal.objects.select_for_update().get(pk=row.pk)
        backend, certificate = _signer()
        signed = sign_pdf(source.render_archive(), reason=SIGNATURE_REASON,
                          backend=backend, certificate=certificate)
        row.pdf = signed
        row.sha256 = hashlib.sha256(signed).hexdigest()
        row.size = len(signed)
        row.key_id = backend.key_id[:300]
        row.cert_fingerprint = fingerprint_sha256(certificate)
        row.signed_at = timezone.now()
        row.sign_attempts = 1
        row.save()
    logger.info('Signing archive: %s signed into the archive (%s bytes)', row.number, row.size)
    return row


# ── a batch ─────────────────────────────────────────────────────────────────

def _walk(since: date | None, tried: dict):
    """(kind, document) for every candidate, kind by kind, newest first — each at most once per batch."""
    for kind in KINDS:
        while True:
            chunk = list(archive_candidates(kind, since).exclude(pk__in=tried[kind])[:_CHUNK])
            if not chunk:
                break
            for obj in chunk:
                tried[kind].add(obj.pk)
                yield kind, obj


def remaining(since: date | None = None) -> int:
    return sum(archive_candidates(kind, since).count() for kind in KINDS)


def run_archive_batch(*, limit: int = DEFAULT_BATCH, since: date | None = None,
                      time_budget_seconds: float = DEFAULT_TIME_BUDGET) -> dict:
    """
    Sign up to `limit` documents into the archive, or as many as `time_budget_seconds` allows.

    Returns {signed, skipped, failed: [{number, kind, error}], remaining, done,
    unavailable}:

    - one document's failure never stops the others — it is listed in
      `failed` and the batch moves on (a NumberClash, a document that cannot
      be drawn);
    - SigningUnavailable — the key or the certificate out of reach — stops the
      batch, since every further document would fail the same way, and is
      reported in `unavailable` ('' otherwise);
    - `remaining`: the eligible documents (from `since`, when given) still
      without a row after this batch — including the ones in `failed`;
    - `done`: nothing is left that another run would try and this one did
      not. With failures it is done while `remaining` is not 0: what is left
      needs a person.

    At least one document is attempted on every call, so a slow key still makes progress.
    """
    if not archive_enabled():
        raise ArchiveDisabled('SIGNING_ARCHIVE_ENABLED is off')
    issued_before()          # ArchiveNeedsCutoff before any document is touched
    limit = max(1, min(int(limit or DEFAULT_BATCH), MAX_BATCH))
    budget = max(0.0, float(time_budget_seconds))
    started = time.monotonic()
    result = {'signed': 0, 'skipped': 0, 'failed': [], 'remaining': 0, 'done': False, 'unavailable': ''}

    try:
        # Without a key and a certificate nothing can be signed: say so before touching a document.
        _signer()
    except SigningUnavailable as exc:
        result['unavailable'] = _short_error(exc)
        result['remaining'] = remaining(since)
        logger.warning('Signing archive: not started — %s', result['unavailable'])
        return result

    tried = {kind: set() for kind in KINDS}
    walk = _walk(since, tried)
    attempted = 0
    exhausted = False
    while result['signed'] < limit and not (attempted and time.monotonic() - started >= budget):
        try:
            kind, obj = next(walk)
        except StopIteration:
            exhausted = True
            break
        attempted += 1
        number = getattr(obj, 'invoice_number', '') or getattr(obj, 'document_number', '')
        try:
            row = sign_archive(kind, obj)
        except NumberClash:
            logger.warning('Signing archive: %s not archived — the number belongs to another document', number)
            result['failed'].append({'number': number, 'kind': kind, 'error': FAILED_CLASH})
            continue
        except SigningUnavailable as exc:
            result['unavailable'] = _short_error(exc)
            logger.warning('Signing archive: stopped at %s — %s', number, result['unavailable'])
            break
        except Exception as exc:
            logger.exception('Signing archive: %s could not be archived', number)
            result['failed'].append({
                'number': number, 'kind': kind, 'error': f'{FAILED_OTHER}: {type(exc).__name__}',
            })
            continue
        result['signed' if row is not None else 'skipped'] += 1

    result['remaining'] = remaining(since)
    result['done'] = not result['unavailable'] and (exhausted or result['remaining'] == 0)
    logger.info(
        'Signing archive: batch — %s signed, %s skipped, %s failed, %s remaining%s',
        result['signed'], result['skipped'], len(result['failed']), result['remaining'],
        ' (done)' if result['done'] else '',
    )
    return result


# ── where it stands ─────────────────────────────────────────────────────────

def archive_status() -> dict:
    """
    Per kind: how many documents are eligible, archived, have an original, and remain.

    `archived` and `originals` count the stored rows of the kind; `remaining`
    is what a batch would still try. No network, no key. `blocked` says why a
    batch would refuse to run ('' when it would run); while it is blocked by a
    missing cutoff the counts are left out rather than guessed.
    """
    try:
        cutoff = issued_before()
    except ArchiveNeedsCutoff as exc:
        return {'kinds': [], 'last_signed_at': None, 'issued_before': None, 'blocked': str(exc)}
    kinds = []
    for kind in KINDS:
        rows = SignedOriginal.objects.filter(kind=kind)
        kinds.append({
            'kind': kind,
            'label': KIND_LABELS[kind],
            'eligible': eligible(kind).count(),
            'archived': rows.filter(purpose=ARCHIVE).count(),
            'originals': rows.exclude(purpose=ARCHIVE).count(),
            'remaining': archive_candidates(kind).count(),
        })
    last = (
        SignedOriginal.objects
        .filter(purpose=ARCHIVE, signed_at__isnull=False)
        .order_by('-signed_at')
        .values_list('signed_at', flat=True)
        .first()
    )
    return {'kinds': kinds, 'last_signed_at': last,
            'issued_before': cutoff.isoformat() if cutoff else None, 'blocked': ''}
