"""
The quarterly backup of the books — הוראות ניהול פנקסי חשבונות, סעיף 25(ו)(2).

A computerised accounting system keeps "a backup in the first week of every
quarter", in a place the assessing officer was told of in writing, other than
where the system itself is kept, and — for a business with income in Israel —
in Israel. This builds that backup and writes it to a Google Cloud Storage
bucket in me-west1 (Tel Aviv) under a retention policy: the one named by
SIGNING_QUARTERLY_BACKUP_BUCKET, else the signed files' own locked bucket
(SIGNING_BACKUP_BUCKET), always under ``books/``. With no bucket it writes the
same files to a local directory instead (``--out``), for a copy kept by hand.
run_daily_backup, at the end, copies the fiscal data there once a day as well.

One run is one folder, ``books/quarterly/<YYYY>-Q<n>/<YYYYMMDDTHHMMSS>/``, holding:

* ``fiscal-data.jsonl.gz`` — every fiscal table, whole, as of the run: the
  documents module (documents, lines, payments, settlements, number runs and
  their openings, check and cash plans), the lesson receipts and their charges,
  the store's sales and their lines, the tenants' charges, the signed originals'
  record (never the PDF bytes — those are in SIGNING_BACKUP_BUCKET), the
  previous software's documents, and only the identity fields of the customers,
  families, children and parents a document names. Django's JSON-lines
  serialisation: one record per line, ``{"model", "pk", "fields"}``, written
  table by table straight into a gzip stream so memory never holds the tables;
* ``register-YYYY-MM.csv`` for each month of the quarter — the accountant's
  register (register.py), which is the printable view 25(ו)(3) asks a backup to
  allow;
* ``uniform-YYYY-MM.zip`` for each month — the uniform-structure files
  (uniform_export.py);
* ``README.txt`` — what each file is and how to read it;
* ``manifest.json`` — counts per table, the SHA-256 and size of every part,
  when it was made and from which commit. Written last: a folder without it is
  a run that did not finish.

Every object is created only if its name is free (ifGenerationMatch=0) through
the same upload helper the signed-file copy uses (signing/backup.create_object),
so a run never overwrites anything; a second run makes a second folder.

Read-only on the database. Sizes: at the September 2026 volume (about 1,200
documents and 2,000 people) the gzip is a few hundred KB and a month's uniform
file a few KB. The whole run is expected to stay under a minute and a few MB
for years; each part is sent in one multipart request.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.utils import timezone

logger = logging.getLogger(__name__)

# Connect, read. A part of a few MB goes up in seconds; the read waits for
# Google's answer after the whole body is sent.
UPLOAD_TIMEOUT = (5, 120)

FISCAL_DATA = 'fiscal-data.jsonl.gz'
MANIFEST = 'manifest.json'
README = 'README.txt'

ALL = None

# (model, the fields kept — ALL for every column — and the columns left out).
# The order is the order the file is written in.
FISCAL_TABLES: tuple[tuple[str, tuple | None, tuple], ...] = (
    # The documents module and the number runs.
    ('documents.DocumentSeries', ALL, ()),
    ('documents.DocumentSeriesOpening', ALL, ()),
    ('documents.DocumentCounter', ALL, ()),
    ('documents.FormalDocument', ALL, ()),
    ('documents.DocumentLineItem', ALL, ()),
    ('documents.DocumentPayment', ALL, ()),
    ('documents.DocumentSettlement', ALL, ()),
    ('documents.CheckPlan', ALL, ()),
    ('documents.CheckItem', ALL, ()),
    ('documents.CashPlan', ALL, ()),
    ('documents.CashPlanMonth', ALL, ()),
    # The record of every signed file. The files are in the locked signed-documents bucket.
    ('documents.SignedOriginal', ALL, ('pdf',)),
    # Lesson charges and their receipts (the IR run, and the old INV- numbers).
    ('customers.Invoice', ALL, ()),
    ('customers.InvoiceChild', ALL, ()),
    ('customers.InvoiceActivityLog', ALL, ()),
    ('customers.Payment', ALL, ()),
    # The store's sales (ST/SD) and what was sold.
    ('store.StoreInvoice', ALL, ()),
    ('store.StoreSale', ALL, ()),
    # Studio rentals: the monthly charges; their receipts are FormalDocuments (RT).
    ('rental_billing.TenantCharge', ALL, ()),
    # The previous software's documents; an import's record without the raw file it was read from.
    ('legacy_import.LegacyImport', ALL, ('rows', 'summary')),
    ('legacy_import.LegacyDocument', ALL, ()),
    # Who a document names, and the names its references point at — identity fields only.
    ('customers.BusinessCustomer', (
        'first_name', 'last_name', 'email', 'phone', 'id_number', 'company_number', 'address',
        'business', 'business_category', 'branch', 'computerized_docs_consent_at',
        'computerized_docs_consent_source', 'computerized_docs_consent_revoked_at',
    ), ()),
    ('customers.Family', (
        'name', 'phone', 'email', 'address', 'parent_id_number', 'branch', 'computerized_docs_consent_at',
        'computerized_docs_consent_source', 'computerized_docs_consent_revoked_at',
    ), ()),
    ('customers.Parent', ('family', 'first_name', 'last_name', 'phone', 'email', 'is_primary'), ()),
    ('customers.Child', ('family', 'first_name', 'last_name'), ()),
    ('core.Branch', ('name',), ()),
    ('core.Business', ('name',), ()),
    ('core.BusinessCategory', ('business', 'name'), ()),
    ('courses.Course', ('name', 'branch', 'business', 'business_category'), ()),
    ('store.StoreProduct', ('name',), ()),
)

_QUARTER = re.compile(r'^(\d{4})-?Q([1-4])$', re.IGNORECASE)


class BackupInputError(ValueError):
    """A quarter that cannot be read, or one that has not started."""


class BackupNotConfigured(Exception):
    """Neither a bucket nor a local directory to write to."""


# Every backup of the books sits under this prefix, so it can share a bucket
# with the signed files (whose names start with their purpose).
BOOKS_PREFIX = 'books'


def quarterly_bucket() -> str:
    """
    Where the books go: SIGNING_QUARTERLY_BACKUP_BUCKET, else the signed files' bucket.

    The signed files' bucket (SIGNING_BACKUP_BUCKET) is already in Tel Aviv,
    locked for ten years, and writable by the signing account, so the backups
    need no bucket of their own.
    """
    own = (getattr(settings, 'SIGNING_QUARTERLY_BACKUP_BUCKET', '') or '').strip()
    return own or (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip()


# ── the quarter ─────────────────────────────────────────────────────────────

def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    first_month = 3 * (quarter - 1) + 1
    start = date(year, first_month, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, first_month + 3, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def previous_quarter(today: date) -> tuple[int, int]:
    """The quarter that ended last — the one the first week of this quarter backs up."""
    quarter = (today.month - 1) // 3 + 1
    return (today.year - 1, 4) if quarter == 1 else (today.year, quarter - 1)


def parse_quarter(raw: str | None, today: date | None = None) -> tuple[int, int]:
    today = today or timezone.localdate()
    if not raw:
        return previous_quarter(today)
    match = _QUARTER.match(str(raw).strip())
    if not match:
        raise BackupInputError(f'Not a quarter: {raw!r} (expected YYYY-Qn, e.g. 2026-Q3)')
    year, quarter = int(match.group(1)), int(match.group(2))
    if quarter_bounds(year, quarter)[0] > today:
        raise BackupInputError(f'{year}-Q{quarter} has not started yet')
    return year, quarter


def quarter_months(year: int, quarter: int, today: date) -> list[tuple[date, date]]:
    """The quarter's months, up to this one: a quarter still running is backed up as far as it went."""
    months = []
    for month in range(3 * (quarter - 1) + 1, 3 * quarter + 1):
        start = date(year, month, 1)
        if start > today:
            break
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        months.append((start, date.fromordinal(end.toordinal() - 1)))
    return months


# ── the parts ───────────────────────────────────────────────────────────────

@dataclass
class Part:
    name: str
    path: str
    content_type: str
    sha256: str = ''
    size: int = 0
    tables: dict = field(default_factory=dict)

    def describe(self) -> dict:
        out = {'name': self.name, 'sha256': self.sha256, 'size': self.size, 'content_type': self.content_type}
        if self.tables:
            out['tables'] = self.tables
        return out


def _digest(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _finish(part: Part) -> Part:
    part.sha256, part.size = _digest(part.path)
    return part


def _write_bytes(directory: str, name: str, payload: bytes, content_type: str) -> Part:
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return _finish(Part(name=name, path=path, content_type=content_type))


def _selected_fields(model, keep, leave_out) -> list:
    if keep is not ALL:
        return list(keep)
    return [f.name for f in model._meta.concrete_fields if not f.primary_key and f.name not in leave_out]


def write_fiscal_data(directory: str) -> Part:
    """Every table in FISCAL_TABLES into one gzip of JSON lines, streamed table by table."""
    serializer_class = serializers.get_serializer('jsonl')
    path = os.path.join(directory, FISCAL_DATA)
    counts: dict = {}
    with open(path, 'wb') as raw, gzip.GzipFile(filename='fiscal-data.jsonl', mode='wb', fileobj=raw,
                                                  compresslevel=9, mtime=0) as packed, \
            io.TextIOWrapper(packed, encoding='utf-8', newline='\n') as text:
        for label, keep, leave_out in FISCAL_TABLES:
            model = apps.get_model(label)
            counted = {'n': 0}

            def rows(queryset=model._default_manager.order_by('pk'), counted=counted):
                for row in queryset.iterator(chunk_size=500):
                    counted['n'] += 1
                    yield row

            serializer_class().serialize(rows(), stream=text, fields=_selected_fields(model, keep, leave_out))
            counts[model._meta.label_lower] = counted['n']
    return _finish(Part(name=FISCAL_DATA, path=path, content_type='application/gzip', tables=counts))


def _month_parts(directory: str, start: date, end: date) -> tuple[list, list]:
    """The register CSV and the uniform-structure ZIP of one month. (parts, errors)."""
    from apps.documents.period_report import HEBREW_MONTHS, build_report
    from apps.documents.register import register_csv
    from apps.documents.undocumented_income import collect_undocumented
    from apps.documents.uniform_export import build_uniform_export

    label = f'{HEBREW_MONTHS[start.month - 1]} {start.year}'
    tag = f'{start:%Y-%m}'
    parts, errors = [], []
    try:
        # No user: the whole business, as the manager's export reads it.
        report = build_report(None, start, end, label)
        report.undocumented = collect_undocumented(None, start, end)
        parts.append(_write_bytes(directory, f'register-{tag}.csv', register_csv(report), 'text/csv'))
    except Exception as exc:
        logger.exception('Quarterly backup: the register of %s failed', tag)
        errors.append(f'register-{tag}.csv: {type(exc).__name__}: {exc}'[:300])
    try:
        archive, _name = build_uniform_export(None, start, end, label)
        parts.append(_write_bytes(directory, f'uniform-{tag}.zip', archive, 'application/zip'))
    except Exception as exc:
        logger.exception('Quarterly backup: the uniform export of %s failed', tag)
        errors.append(f'uniform-{tag}.zip: {type(exc).__name__}: {exc}'[:300])
    return parts, errors


def app_commit() -> str:
    """The commit the code runs from: Vercel's variable, else the checkout's HEAD, else ''."""
    sha = (os.environ.get('VERCEL_GIT_COMMIT_SHA') or '').strip()
    if sha:
        return sha
    try:
        done = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=str(settings.BASE_DIR), capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


README_TEXT = """גיבוי רבעוני של מערכת החשבונות — Kogo
הוראות ניהול פנקסי חשבונות, סעיף 25(ו)(2)

רבעון: {quarter} ({start} עד {end})
הופק: {generated_at}
קוד המערכת (commit): {commit}

הקבצים בתיקייה:
- fiscal-data.jsonl.gz — כל טבלאות המסמכים והכספים במלואן נכון לרגע ההפקה (לא רק הרבעון):
  מסמכים, שורות, תשלומים, קיזוזים, סדרות מספור ופתיחתן, תוכניות צ'קים ומזומן,
  קבלות חוגים והחיובים שלהן, מכירות חנות ושורותיהן, חיובי שוכרים,
  רישום המקורות החתומים (בלי קובצי ה-PDF), מסמכי התוכנה הקודמת,
  ופרטי הזיהוי בלבד של הלקוחות שהמסמכים נקובים על שמם.
  פורמט: gzip של JSON Lines — שורה לכל רשומה: {{"model", "pk", "fields"}}. נקרא בכל עורך
  טקסט אחרי פתיחת ה-gzip, או ב-Django: manage.py loaddata.
- register-YYYY-MM.csv — מרשם המסמכים של כל חודש, כפי שנמסר לרו"ח (Excel, UTF-8).
  בסופו: הכנסה ללא מסמך באותו חודש.
- uniform-YYYY-MM.zip — קובצי המבנה האחיד (INI.TXT, BKMVDATA) של כל חודש.
- manifest.json — ספירת רשומות לכל טבלה, SHA-256 וגודל של כל קובץ.

קובצי ה-PDF החתומים עצמם (מקור והעתק לארכיון) שמורים בדלי הנעול {signed_bucket}.
תיקייה בלי manifest.json היא ריצה שלא הושלמה.
"""


def _readme(directory: str, *, quarter: str, start: date, end: date, generated_at: str, commit: str) -> Part:
    signed = (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip() or '(לא מוגדר)'
    text = README_TEXT.format(
        quarter=quarter, start=start.isoformat(), end=end.isoformat(), generated_at=generated_at,
        commit=commit or 'לא ידוע', signed_bucket=signed,
    )
    return _write_bytes(directory, README, text.encode('utf-8'), 'text/plain; charset=utf-8')


# ── the run ─────────────────────────────────────────────────────────────────

def _upload(bucket_name: str, prefix: str, part: Part, meta: dict) -> None:
    from apps.documents.signing.backup import create_object

    with open(part.path, 'rb') as handle:
        payload = handle.read()
    created = create_object(
        bucket_name, f'{prefix}/{part.name}', payload, content_type=part.content_type,
        metadata={**meta, 'sha256': part.sha256}, timeout=UPLOAD_TIMEOUT,
    )
    if not created:
        # Names carry the second the run started: a taken one is another run's file, never ours to count.
        raise RuntimeError(f'{prefix}/{part.name} already exists in the bucket')


def _upload_folder(bucket_name: str, prefix: str, parts: list, manifest_part: Part, meta: dict,
                   errors: list) -> list:
    """Every part, then the manifest over a complete folder. The names uploaded; failures go to `errors`."""
    from apps.documents.signing import SigningUnavailable

    uploaded = []
    for part in parts:
        try:
            _upload(bucket_name, prefix, part, meta)
        except SigningUnavailable as exc:
            # The bucket is out of reach: every further part would fail the same way.
            errors.append(f'upload stopped at {part.name}: {exc}'[:300])
            break
        except Exception as exc:
            errors.append(f'upload of {part.name} failed: {type(exc).__name__}: {exc}'[:300])
        else:
            uploaded.append(part.name)
    if len(uploaded) == len(parts):
        # Last, and only over a complete folder: its presence says the run finished.
        try:
            _upload(bucket_name, prefix, manifest_part, meta)
            uploaded.append(manifest_part.name)
        except Exception as exc:
            errors.append(f'upload of {MANIFEST} failed: {type(exc).__name__}: {exc}'[:300])
    return uploaded


def run_quarterly_backup(quarter: str | None = None, *, out_dir: str | None = None,
                         bucket_name: str | None = None, now=None) -> dict:
    """
    Build the quarter's backup and put it in the bucket (or `out_dir`). Returns a summary.

    {quarter, prefix, bucket, local_dir, parts: [...], uploaded: [...], errors: [...], ok}.
    BackupInputError for a quarter that cannot be backed up; BackupNotConfigured
    when there is neither a bucket nor a directory.
    """
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    year, number = parse_quarter(quarter, today)
    start, end = quarter_bounds(year, number)
    bucket_name = quarterly_bucket() if bucket_name is None else bucket_name.strip()
    if not bucket_name and not out_dir:
        raise BackupNotConfigured('No backup bucket is set (SIGNING_QUARTERLY_BACKUP_BUCKET / SIGNING_BACKUP_BUCKET) '
                                  'and no --out directory was given')

    label = f'{year}-Q{number}'
    local = timezone.localtime(now)
    prefix = f'{BOOKS_PREFIX}/quarterly/{label}/{local:%Y%m%dT%H%M%S}'
    generated_at = local.isoformat()
    commit = app_commit()

    temporary = None
    if out_dir:
        directory = os.path.join(out_dir, *prefix.split('/'))
        os.makedirs(directory, exist_ok=False)
    else:
        temporary = tempfile.mkdtemp(prefix='kogo-quarterly-')
        directory = temporary

    errors: list = []
    uploaded: list = []
    try:
        parts = [write_fiscal_data(directory)]
        for month_start, month_end in quarter_months(year, number, today):
            month_parts, month_errors = _month_parts(directory, month_start, month_end)
            parts.extend(month_parts)
            errors.extend(month_errors)
        parts.append(_readme(directory, quarter=label, start=start, end=end,
                             generated_at=generated_at, commit=commit))

        manifest = {
            'kind': 'kogo-quarterly-backup',
            'version': 1,
            'rule': 'הוראות ניהול פנקסי חשבונות 25(ו)(2)',
            'quarter': label,
            'period': {'start': start.isoformat(), 'end': end.isoformat()},
            'generated_at': generated_at,
            'app_commit': commit,
            'prefix': prefix,
            'signed_files_bucket': (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip(),
            'parts': [part.describe() for part in parts],
            'errors': errors,
        }
        manifest_part = _write_bytes(
            directory, MANIFEST,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json',
        )

        if bucket_name:
            uploaded = _upload_folder(bucket_name, prefix, parts, manifest_part,
                                      {'quarter': label, 'generated_at': generated_at}, errors)
        all_parts = parts + [manifest_part]
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)

    summary = {
        'quarter': label,
        'prefix': prefix,
        'bucket': bucket_name,
        'local_dir': directory if out_dir else '',
        'parts': [part.describe() for part in all_parts],
        'uploaded': uploaded,
        'errors': errors,
        'ok': not errors and (not bucket_name or len(uploaded) == len(all_parts)),
    }
    log = logger.info if summary['ok'] else logger.warning
    log('Quarterly backup %s: %s parts, %s uploaded, %s errors', prefix, len(all_parts), len(uploaded), len(errors))
    return summary


# ── the daily copy ──────────────────────────────────────────────────────────
#
# The live books are on a server abroad (the owner's decision, 27.9.2026: the
# database stays where it is). Once a day every fiscal table goes to Israel as
# well, so what is kept here is never more than a day behind. One folder a day,
# ``books/daily/YYYY-MM-DD/<HHMMSS>/``: the whole fiscal data (the same file the
# quarter holds), a README and the manifest, written last. The quarter's
# registers and uniform files stay in the quarterly run.

DAILY_README_TEXT = """עותק יומי של ספרי החשבונות — Kogo

המערכת עצמה רצה על שרת בחו"ל. פעם ביום נשמר כאן, בישראל, עותק מלא של כל נתוני
המסמכים והכספים, כך שהעותק בישראל לעולם אינו מפגר ביותר מיום.

תאריך: {day}
הופק: {generated_at}
קוד המערכת (commit): {commit}

- fiscal-data.jsonl.gz — כל טבלאות המסמכים והכספים במלואן נכון לרגע ההפקה, באותו מבנה
  כמו בגיבוי הרבעוני (gzip של JSON Lines, שורה לכל רשומה: {{"model", "pk", "fields"}}).
- manifest.json — ספירת רשומות לכל טבלה, SHA-256 וגודל של כל קובץ, ומשך הריצה.

מרשמי החודשים וקובצי המבנה האחיד נמצאים בגיבוי הרבעוני (books/quarterly/).
קובצי ה-PDF החתומים עצמם שמורים בדלי הנעול {signed_bucket}.
תיקייה בלי manifest.json היא ריצה שלא הושלמה.
"""


def run_daily_backup(*, out_dir: str | None = None, bucket_name: str | None = None, now=None) -> dict:
    """
    Copy every fiscal table to the bucket in Israel (or `out_dir`). Returns a summary.

    {day, prefix, bucket, local_dir, parts, uploaded, errors, seconds, ok}.
    BackupNotConfigured when there is neither a bucket nor a directory. Every run
    makes its own folder (the second it started is in the name), so a second
    run on one day adds a second copy and never touches the first.
    """
    import time

    started = time.monotonic()
    now = now or timezone.now()
    local = timezone.localtime(now)
    bucket_name = quarterly_bucket() if bucket_name is None else bucket_name.strip()
    if not bucket_name and not out_dir:
        raise BackupNotConfigured('No backup bucket is set (SIGNING_QUARTERLY_BACKUP_BUCKET / SIGNING_BACKUP_BUCKET) '
                                  'and no directory was given')

    day = local.date().isoformat()
    prefix = f'{BOOKS_PREFIX}/daily/{day}/{local:%H%M%S}'
    generated_at = local.isoformat()
    commit = app_commit()

    temporary = None
    if out_dir:
        directory = os.path.join(out_dir, *prefix.split('/'))
        os.makedirs(directory, exist_ok=False)
    else:
        temporary = tempfile.mkdtemp(prefix='kogo-daily-')
        directory = temporary

    errors: list = []
    uploaded: list = []
    try:
        parts = [write_fiscal_data(directory)]
        signed = (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip() or '(לא מוגדר)'
        readme = DAILY_README_TEXT.format(day=day, generated_at=generated_at, commit=commit or 'לא ידוע',
                                          signed_bucket=signed)
        parts.append(_write_bytes(directory, README, readme.encode('utf-8'), 'text/plain; charset=utf-8'))
        manifest = {
            'kind': 'kogo-daily-backup',
            'version': 1,
            'day': day,
            'generated_at': generated_at,
            'app_commit': commit,
            'prefix': prefix,
            'signed_files_bucket': (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip(),
            'build_seconds': round(time.monotonic() - started, 2),
            'parts': [part.describe() for part in parts],
        }
        manifest_part = _write_bytes(
            directory, MANIFEST,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'), 'application/json',
        )
        if bucket_name:
            uploaded = _upload_folder(bucket_name, prefix, parts, manifest_part,
                                      {'day': day, 'generated_at': generated_at}, errors)
        all_parts = parts + [manifest_part]
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)

    summary = {
        'day': day,
        'prefix': prefix,
        'bucket': bucket_name,
        'local_dir': directory if out_dir else '',
        'parts': [part.describe() for part in all_parts],
        'uploaded': uploaded,
        'errors': errors,
        'seconds': round(time.monotonic() - started, 2),
        'ok': not errors and (not bucket_name or len(uploaded) == len(all_parts)),
    }
    log = logger.info if summary['ok'] else logger.warning
    log('Daily backup %s: %s parts, %s uploaded, %s errors, %ss',
        prefix, len(all_parts), len(uploaded), len(errors), summary['seconds'])
    return summary
