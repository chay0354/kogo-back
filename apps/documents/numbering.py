"""Consecutive document numbers — one gapless series per document kind and tax year.

Each series has a prefix that says which run a number belongs to, so the office,
the accountant and a tax inspector can each check a run on its own:

    IR  — חשבונית מס / קבלה for lesson charges (widget, standing orders, card links)
    ST  — חשבונית מס / קבלה for store sales paid on the spot (card or cash)
    SD  — חשבונית עסקה for store sales put on monthly billing (not yet paid)
    RT  — חשבונית מס / קבלה for studio rentals (the tenants' standing orders)
    MK  — חשבונית מס / קבלה for Michal Kagan's sessions, asked for by her site
          (a business line of the company: apps/documents/michal)
    TI  — חשבונית מס issued by hand
    IRM — חשבונית מס / קבלה issued by hand
    RC  — קבלה issued by hand (the office's check plans too)
    TX  — חשבונית עסקה issued by hand
    CR  — חשבונית מס זיכוי, issued by hand or with a refund

One series per document type, because סעיף 5(ג) wants invoices that double as
receipts numbered in a run of their own. Where two channels issue the same type
(lesson receipts, store sales, the office), each keeps a run of its own under its
own prefix, so every run lives in one table and can be checked on its own.

Documents issued by hand used to share one run across all their types
(DocumentCounter, '2026-0042'). Nothing draws from it any more, and
`continuity()` still checks it. Numbers already issued in the old formats stay
exactly as they are: an issued document is never renumbered (סעיף 23(ב)).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.documents.models import DocumentSeries

SERIES_SUBSCRIPTION = 'IR'
SERIES_STORE = 'ST'
SERIES_STORE_TRANSACTION = 'SD'
SERIES_RENTAL = 'RT'
SERIES_MICHAL = 'MK'
SERIES_TAX_INVOICE = 'TI'
SERIES_MANUAL_INVOICE_RECEIPT = 'IRM'
SERIES_RECEIPT = 'RC'
SERIES_TRANSACTION = 'TX'
SERIES_CREDIT = 'CR'

# A document issued by hand draws from the run of its type.
FORMAL_SERIES = {
    'tax_invoice': SERIES_TAX_INVOICE,
    'combined': SERIES_MANUAL_INVOICE_RECEIPT,
    'receipt': SERIES_RECEIPT,
    'transaction_invoice': SERIES_TRANSACTION,
    'credit_invoice': SERIES_CREDIT,
}

# In the order the runs are read: what customers pay against, what the office
# issues, then credits. The closed shared run comes last in its year.
SERIES_LABELS = {
    SERIES_SUBSCRIPTION: 'חשבונית מס/קבלה · חוגים',
    SERIES_STORE: 'חשבונית מס/קבלה · חנות',
    SERIES_STORE_TRANSACTION: 'חשבונית עסקה · חנות',
    SERIES_RENTAL: 'חשבונית מס/קבלה · שכירויות',
    SERIES_MICHAL: 'חשבונית מס/קבלה · מיכל קגן',
    SERIES_TAX_INVOICE: 'חשבונית מס · ידני',
    SERIES_MANUAL_INVOICE_RECEIPT: 'חשבונית מס/קבלה · ידני',
    SERIES_RECEIPT: 'קבלה · ידני',
    SERIES_TRANSACTION: 'חשבונית עסקה · ידני',
    SERIES_CREDIT: 'חשבונית מס זיכוי',
}
LEGACY_LABEL = 'מסמכים ידניים · סדרה משותפת (סגורה)'

# Postgres regexes for a number from the lesson run and from the store runs. The
# older 'INV-…' numbers were cut from a payment's UUID and match neither: they
# were never fiscal numbers (see apps/documents/register.py).
#
# Six digits at least, not exactly: a run that continues the previous
# software's run (DocumentSeriesOpening) may start past 999999, and its numbers
# are printed in full — 'IRM-2026-1000000' — never cut to fit.
LESSON_RUN_REGEX = r'^IR-[0-9]{4}-[0-9]{6,}$'
STORE_RUN_REGEX = r'^(ST|SD)-[0-9]{4}-[0-9]{6,}$'
# A tenant's receipt: a FormalDocument, issued with a standing order's charge
# (apps/rental_billing), that the register files under a channel of its own.
RENTAL_RUN_REGEX = r'^RT-[0-9]{4}-[0-9]{6,}$'
_RENTAL_RUN = re.compile(RENTAL_RUN_REGEX)


ISRAEL_TZ = ZoneInfo('Asia/Jerusalem')


def israel_today() -> date:
    """Today on the calendar in Israel — what a document issued now is dated, whatever the server's clock."""
    return timezone.localdate(timezone=ISRAEL_TZ)


def is_rental_number(number) -> bool:
    """True for a number from the RT run."""
    return bool(_RENTAL_RUN.match(number or ''))


def _tax_year(when: date | datetime | None) -> int:
    if when is None:
        return timezone.localdate().year
    if isinstance(when, datetime):
        return (timezone.localtime(when) if timezone.is_aware(when) else when).year
    return when.year


class DocumentDateError(ValueError):
    """A document date its run refuses. Its message is the office's, in Hebrew — the view answers 400 with it."""


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return (timezone.localtime(value, ISRAEL_TZ) if timezone.is_aware(value) else value).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise DocumentDateError(f'תאריך המסמך אינו תאריך תקין: {value!r}') from None


def latest_document_date(series: str, year: int) -> date | None:
    """The latest date on a document already issued in the run `series` of `year`, or None."""
    from django.db.models import Max

    from apps.documents.models import FormalDocument

    return (
        FormalDocument.objects
        .filter(document_number__startswith=f'{series}-{year}-')
        .aggregate(latest=Max('document_date'))['latest']
    )


def validate_document_date(series_or_type: str, document_date, issue_date=None) -> date:
    """
    The date a document issued by hand may carry — owner decision D3 (25.9.2026).

    1. Not in the future: a document is made "סמוך לביצוע הפעולה" (הוראה 17),
       never ahead of it.
    2. In the tax year it is issued in: its number comes from that year's run
       (numbering is by tax year, סעיף 18(א)(3)), and a date in another year
       would file it in the wrong year's books.
    3. Not before the latest date already issued in its run: the run's numbers
       stay in the order of their dates. The same day is fine.

    `series_or_type` is a run ('TI') or a document type ('tax_invoice');
    `issue_date` is the day it is issued, today in Israel by default. Returns
    the document date as a date; raises DocumentDateError with the reason.

    Call it after the run's number is taken (DocumentSeries.next_number holds
    the run's row lock until the transaction ends): two documents issued at the
    same moment are then checked one after the other, and a refusal rolls the
    number back with the rest.
    """
    series = FORMAL_SERIES.get(series_or_type, series_or_type)
    day = _as_date(document_date)
    issued_on = _as_date(issue_date) if issue_date is not None else israel_today()

    if day > issued_on:
        raise DocumentDateError(
            f'תאריך המסמך ({day:%d/%m/%Y}) הוא בעתיד. מסמך מתוארך ביום שבו הוא מופק או לפניו.'
        )
    if day.year != issued_on.year:
        raise DocumentDateError(
            f'תאריך המסמך ({day:%d/%m/%Y}) אינו בשנת המס {issued_on.year}. '
            f'מסמך שמופק היום מתוארך בשנת {issued_on.year} בלבד.'
        )
    latest = latest_document_date(series, issued_on.year)
    if latest is not None and day < latest:
        label = SERIES_LABELS.get(series, series)
        raise DocumentDateError(
            f'בסדרה "{label}" כבר הופק מסמך בתאריך {latest:%d/%m/%Y}. '
            f'כדי שהמספרים יישארו לפי סדר התאריכים, תאריך המסמך לא יכול להיות מוקדם ממנו.'
        )
    return day


def format_document_number(series: str, year: int, number: int) -> str:
    """'IR-2026-000123': zero-filled to six digits, and longer when the number is."""
    return f'{series}-{year}-{number:06d}'


def next_document_number(series: str, when: date | datetime | None = None) -> str:
    """'IR-2026-000123'. Call inside the transaction that saves the document."""
    year = _tax_year(when)
    return format_document_number(series, year, DocumentSeries.next_number(series, year))


def formal_document_number(document_type: str) -> str:
    """The next number for a document issued by hand, from the run of its type."""
    series = FORMAL_SERIES.get(document_type)
    if series is None:
        # A draft takes no number and every other type has its run above. A new
        # type has to be given a run on purpose, never fall into another's.
        raise ValueError(f'No number series for document type {document_type!r}')
    return next_document_number(series)


@dataclass(frozen=True)
class SeriesRun:
    """One run in one tax year, and whether every number it handed out is on a document."""
    series: str  # '' for the closed shared run
    year: int
    label: str
    issued: int  # how many numbers the run handed out
    first: str  # '' when it handed out none
    last: str
    missing: tuple = ()  # numbers handed out that no document carries
    start: int = 1  # the run's first number; above 1 when it continues the previous software's run
    # When it continues the previous software's run: that run's type and last number.
    previous_type_label: str = ''
    previous_last_number: int | None = None

    @property
    def name(self) -> str:
        return f'{self.series}-{self.year}' if self.series else str(self.year)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def continues(self) -> str:
        """'ממשיך את הסדרה של התוכנה הקודמת (אחרון 40413)', or '' for a run that starts at 1."""
        if self.previous_last_number is None:
            return ''
        return continuation_note(self.previous_last_number)


def continuation_note(previous_last_number: int) -> str:
    return f'ממשיך את הסדרה של התוכנה הקודמת (אחרון {previous_last_number})'


def _series_sources() -> dict:
    """Series → (queryset, number field). Each run lives in exactly one table."""
    from apps.customers.financial_models import Invoice
    from apps.documents.models import FormalDocument
    from apps.store.models import StoreInvoice

    formal = (FormalDocument.objects.all(), 'document_number')
    store = (StoreInvoice.objects.all(), 'invoice_number')
    return {
        SERIES_SUBSCRIPTION: (Invoice.objects.all(), 'invoice_number'),
        SERIES_STORE: store,
        SERIES_STORE_TRANSACTION: store,
        SERIES_RENTAL: formal,
        SERIES_MICHAL: formal,
        SERIES_TAX_INVOICE: formal,
        SERIES_MANUAL_INVOICE_RECEIPT: formal,
        SERIES_RECEIPT: formal,
        SERIES_TRANSACTION: formal,
        SERIES_CREDIT: formal,
    }


def _check_run(series, year, label, handed_out, queryset, field, prefix, width, start=1, opening=None) -> SeriesRun:
    """
    A run that handed out `start` .. `handed_out`, checked for numbers no document carries.

    `handed_out` is the counter: the last number given. Below `start` the run
    never gave anything, so nothing there is looked for or reported missing.
    """
    numbers = queryset.filter(**{f'{field}__startswith': prefix}).values_list(field, flat=True)
    present = {int(number[len(prefix):]) for number in numbers if number[len(prefix):].isdigit()}
    missing = sorted(set(range(start, handed_out + 1)) - present)
    issued = max(0, handed_out - start + 1)

    def formatted(n: int) -> str:
        return f'{prefix}{n:0{width}d}'

    return SeriesRun(
        series=series,
        year=year,
        label=label,
        issued=issued,
        first=formatted(start) if issued else '',
        last=formatted(handed_out) if issued else '',
        missing=tuple(formatted(n) for n in missing),
        start=start,
        previous_type_label=opening.previous_type_label if opening else '',
        previous_last_number=opening.previous_last_number if opening else None,
    )


def continuity(year: int | None = None) -> list[SeriesRun]:
    """
    Every run the business numbers documents in, each checked for gaps.

    נספח ה׳(א)(5) asks the software itself for "בדיקת רצף המספרים העוקבים": a
    run hands out start .. counter, and each one of those numbers must be on a
    document that exists. A run that continues the previous software's run
    starts above 1, and the numbers below its start were that software's to
    give, so they are not looked for here. Oldest year first; within a year, in
    the order of SERIES_LABELS, the closed shared run last.
    """
    from apps.documents.models import DocumentCounter, DocumentSeriesOpening, FormalDocument

    sources = _series_sources()
    rank = {series: index for index, series in enumerate(SERIES_LABELS)}
    rows = DocumentSeries.objects.all()
    legacy = DocumentCounter.objects.all()
    openings = DocumentSeriesOpening.objects.all()
    if year is not None:
        rows = rows.filter(year=year)
        legacy = legacy.filter(year=year)
        openings = openings.filter(year=year)
    opened = {(opening.series, opening.year): opening for opening in openings}

    runs = []
    for row in rows:
        source = sources.get(row.series)
        if source is None:
            continue
        queryset, field = source
        runs.append(_check_run(
            row.series, row.year, SERIES_LABELS.get(row.series, row.series), row.counter,
            queryset, field, f'{row.series}-{row.year}-', 6,
            start=row.start, opening=opened.get((row.series, row.year)),
        ))
    # The closed shared run numbered '2026-0042': at least four digits.
    for row in legacy:
        runs.append(_check_run(
            '', row.year, LEGACY_LABEL, row.counter,
            FormalDocument.objects.all(), 'document_number', f'{row.year}-', 4,
        ))
    return sorted(runs, key=lambda run: (run.year, rank.get(run.series, len(rank)), run.series))
