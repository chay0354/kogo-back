"""The old software's PDFs: matched to the imported documents, fingerprinted, and locked away.

The office uploads the PDFs the old software issued — a ZIP, or the PDFs
themselves — after the documents were imported. Each file is matched to one
LegacyDocument of the chosen software by the number in its name (and, when two
types share that number, by the type its name or folder says). For a match:

* the database keeps its SHA-256 and size — never the bytes (Supabase's space
  is small; the fingerprint is enough to prove a copy is the original);
* the bytes go to the locked bucket (signing/backup.create_object: create-only,
  so nothing there is ever replaced) as legacy/<software>/<year>/<type>/<number>.pdf
  — only when SIGNING_BACKUP_BUCKET is set. Without it only the fingerprint is
  kept, the answer says so, and the office keeps its files.

Nothing is guessed. A file whose number matches no document, or matches two it
cannot tell apart, is reported and left alone; so is a second file for a
document that already has a different one (the first is kept — it may already
be in the bucket, which cannot replace it).

Each file is written to the bucket and recorded before the next one is read,
within a time budget: a request that runs out of time says how many are left,
and sending the same files again picks up where it stopped — every file already
recorded is recognised by its fingerprint and skipped.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.legacy_import.models import LegacyDocument
from apps.legacy_import.reader import ImportFileError

logger = logging.getLogger(__name__)

# The same limit as the file import: Vercel refuses a body over 4.5 MB.
MAX_UPLOAD_BYTES = 4_300_000
MAX_FILES = 500
MAX_PDF_BYTES = 20_000_000
# What a ZIP may unpack to in all: PDFs barely compress, so a 4.3MB ZIP of real
# PDFs is far below this; a ZIP that claims more is not the old software's.
MAX_ZIP_UNPACKED_BYTES = 60_000_000
# Like the signed-file backup's cron (signing/backup.DEFAULT_TIME_BUDGET): well
# inside a serverless function's limit. What is left is reported, and the
# screen sends the same files again.
TIME_BUDGET_SECONDS = 8.0
REPORT_LIMIT = 300

_DIGITS = re.compile(r'\d+')
_NOT_SLUG = re.compile(r'[^\w.-]+', re.UNICODE)

# Words in a file's name or folder that say which type it is, most specific first.
_TYPE_WORDS = (
    ('credit_invoice', ('זיכוי', 'credit', 'refund')),
    ('combined', ('חשבונית מס קבלה', 'חשבונית מס-קבלה', 'חשבונית מס/קבלה', 'חשבונית מס - קבלה',
                  'invoice receipt', 'invoice-receipt', 'invoicereceipt')),
    ('transaction_invoice', ('עסקה', 'עיסקה', 'proforma', 'transaction')),
    ('receipt', ('קבלה', 'receipt')),
    ('tax_invoice', ('חשבונית מס', 'חשבונית', 'tax invoice', 'invoice')),
)
# A folder named by its מבנה אחיד type code: 305/40001.pdf.
_TYPE_CODES = {'300': 'transaction_invoice', '305': 'tax_invoice', '320': 'combined',
               '330': 'credit_invoice', '400': 'receipt'}
# A date in a file's name is not its number: 2025-01-05, 05.01.2025, 05_01_25.
_DATES = re.compile(r'(?<!\d)(\d{4}[-_.]\d{1,2}[-_.]\d{1,2}|\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4})(?!\d)')


@dataclass
class Upload:
    name: str
    content: bytes


def _is_pdf(content: bytes) -> bool:
    return content[:1024].lstrip().startswith(b'%PDF-')


def files_from_request(zip_upload=None, pdf_uploads=()) -> list:
    """The uploaded ZIP's entries and/or the uploaded PDFs, as Uploads. Raises ImportFileError."""
    total = sum(getattr(u, 'size', 0) or 0 for u in ([zip_upload] if zip_upload else []) + list(pdf_uploads))
    if total > MAX_UPLOAD_BYTES:
        raise ImportFileError('הבקשה גדולה מדי (הגבול 4.3MB). שלחו את הקבצים בכמה חלקים.')
    files = []
    if zip_upload is not None:
        files.extend(_zip_entries(zip_upload.read()))
    for upload in pdf_uploads:
        files.append(Upload(name=getattr(upload, 'name', '') or 'file.pdf', content=upload.read()))
    if not files:
        raise ImportFileError('לא נבחרו קבצים')
    if len(files) > MAX_FILES:
        raise ImportFileError(f'יותר מדי קבצים בבקשה אחת (הגבול {MAX_FILES}). שלחו בכמה חלקים.')
    return files


def _zip_entries(content: bytes) -> list:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ImportFileError('קובץ ה-ZIP פגום') from exc
    files = []
    if sum(info.file_size for info in archive.infolist()) > MAX_ZIP_UNPACKED_BYTES:
        raise ImportFileError('קובץ ה-ZIP גדול מדי לפתיחה. שלחו את קובצי ה-PDF עצמם, בכמה חלקים.')
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.startswith('__MACOSX/') or '/.' in f'/{info.filename}':
                continue
            if info.file_size > MAX_PDF_BYTES:
                files.append(Upload(name=info.filename, content=b''))
                continue
            files.append(Upload(name=info.filename, content=archive.read(info)))
    return files


def _stem(name: str) -> str:
    return name.replace('\\', '/').rsplit('/', 1)[-1].rsplit('.', 1)[0].strip()


def numbers_in(name: str) -> list:
    """The candidate document numbers in a file's base name: its runs of digits, dates left out."""
    stem = _DATES.sub(' ', _stem(name))
    return [int(d) for d in _DIGITS.findall(stem) if len(d) <= 18 and int(d) > 0]


def type_hint(name: str) -> str:
    """The document type the file's name or folder says, or ''."""
    path = name.replace('\\', '/')
    text = path.replace('_', ' ').casefold()
    for doc_type, words in _TYPE_WORDS:
        if any(word in text for word in words):
            return doc_type
    for folder in path.split('/')[:-1]:
        if folder.strip() in _TYPE_CODES:
            return _TYPE_CODES[folder.strip()]
    return ''


def object_name(doc: LegacyDocument) -> str:
    """legacy/greeninvoice/2025/tax_invoice/40001.pdf"""
    source = _NOT_SLUG.sub('-', doc.source_system).strip('-') or 'unknown'
    return f'legacy/{source}/{doc.document_date.year}/{doc.doc_type}/{doc.number}.pdf'


def _match(upload: Upload, source_system: str):
    """(document, '', []) — or (None, reason, the candidates the name could mean)."""
    numbers = numbers_in(upload.name)
    stem = _stem(upload.name)
    condition = Q(number__in=numbers) if numbers else Q(pk__in=[])
    if stem:
        condition |= Q(original_number__iexact=stem)
    candidates = list(LegacyDocument.objects.filter(condition, source_system=source_system))
    if not candidates:
        reason = 'לא נמצא מסמך מיובא עם המספר הזה' if numbers or stem else 'בשם הקובץ אין מספר מסמך'
        return None, reason, []
    hint = type_hint(upload.name)
    if len(candidates) > 1 and hint:
        candidates = [doc for doc in candidates if doc.doc_type == hint] or candidates
    if len(candidates) > 1:
        # "חשבונית 40001 עותק 2": the longest number in the name is the document's, not the 2.
        longest = max((len(str(n)) for n in numbers), default=0)
        candidates = [doc for doc in candidates if len(str(doc.number)) == longest
                      or doc.original_number.casefold() == stem.casefold()] or candidates
    if len(candidates) > 1:
        return None, 'כמה מסמכים מתאימים לשם הקובץ — הוסיפו לשם את סוג המסמך', candidates
    return candidates[0], '', []


def _label(doc) -> dict:
    return {
        'id': str(doc.pk), 'doc_type': doc.doc_type, 'type_label': doc.get_doc_type_display(),
        'number': doc.number, 'original_number': doc.original_number, 'date': doc.document_date.isoformat(),
    }


def attach(files: list, source_system: str, *, time_budget: float = TIME_BUDGET_SECONDS) -> dict:
    """
    Match, fingerprint and (when a bucket is set) store each file. Returns the report:
    {bucket_configured, counts: {...}, files: [{file, status, reason?, document?}], remaining, stopped}.
    """
    from apps.documents.signing import SigningUnavailable
    from apps.documents.signing.backup import bucket, create_object

    bucket_name = bucket()
    started = time.monotonic()
    report, counts = [], Counter()
    seen_hashes, seen_docs = {}, {}
    stopped = ''
    remaining = 0

    def note(upload, status, **extra):
        counts[status] += 1
        if len(report) < REPORT_LIMIT:
            report.append({'file': upload.name[-200:], 'status': status, **extra})

    for position, upload in enumerate(files):
        if stopped or (counts['stored'] + counts['fingerprinted'] and time.monotonic() - started >= time_budget):
            remaining = len(files) - position
            break
        if not upload.content:
            note(upload, 'rejected', reason='הקובץ ריק או גדול מ-20MB')
            continue
        if not _is_pdf(upload.content):
            note(upload, 'rejected', reason='אינו קובץ PDF')
            continue
        sha256 = hashlib.sha256(upload.content).hexdigest()
        if sha256 in seen_hashes:
            note(upload, 'duplicate', reason=f'אותו קובץ כמו {seen_hashes[sha256]}')
            continue
        seen_hashes[sha256] = upload.name[-120:]
        doc, reason, candidates = _match(upload, source_system)
        if doc is None:
            note(upload, 'ambiguous' if candidates else 'unmatched', reason=reason,
                 candidates=[_label(c) for c in candidates[:5]])
            continue
        if doc.pk in seen_docs:
            note(upload, 'duplicate', reason=f'גם {seen_docs[doc.pk]} הוא של המסמך הזה', document=_label(doc))
            continue
        seen_docs[doc.pk] = upload.name[-120:]
        if doc.pdf_sha256 and doc.pdf_sha256 != sha256:
            note(upload, 'conflict', reason='למסמך כבר צורף PDF אחר — הקודם נשמר', document=_label(doc))
            continue
        if doc.pdf_sha256 == sha256 and (doc.pdf_object or not bucket_name):
            note(upload, 'already', document=_label(doc))
            continue

        name = ''
        if bucket_name:
            name = object_name(doc)
            try:
                written = create_object(bucket_name, name, upload.content, content_type='application/pdf', metadata={
                    'source_system': doc.source_system,
                    'doc_type': doc.doc_type,
                    'number': str(doc.number),
                    'original_number': doc.original_number,
                    'document_date': doc.document_date.isoformat(),
                    'sha256': sha256,
                    'file_name': upload.name[-200:],
                    'legacy_document_id': str(doc.pk),
                })
            except SigningUnavailable as exc:
                stopped = str(exc)[:300]
                remaining = len(files) - position
                logger.warning('Legacy PDFs: the bucket is out of reach — %s', stopped)
                break
            except Exception as exc:  # noqa: BLE001 - one file's refusal must not stop the others
                logger.exception('Legacy PDFs: %s could not be stored', doc.pk)
                note(upload, 'failed', reason=f'האחסון סירב: {type(exc).__name__}', document=_label(doc))
                continue
            if not written:
                # The name was taken: an earlier upload wrote it and did not get to record it.
                logger.info('Legacy PDFs: %s was already in the bucket', name)
        LegacyDocument.objects.filter(pk=doc.pk).update(
            pdf_sha256=sha256, pdf_size=len(upload.content), pdf_file_name=upload.name[-255:],
            pdf_object=name, pdf_attached_at=timezone.now(), updated_at=timezone.now(),
        )
        note(upload, 'stored' if name else 'fingerprinted', document=_label(doc), sha256=sha256)

    summary = {
        'bucket_configured': bool(bucket_name),
        'source_system': source_system,
        'counts': dict(counts),
        'files': report,
        'total': len(files),
        'remaining': remaining,
        'stopped': stopped,
    }
    logger.info('Legacy PDFs for %s: %s (remaining %s)', source_system, dict(counts), remaining)
    return summary
