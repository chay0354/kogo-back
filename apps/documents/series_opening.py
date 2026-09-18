"""Continuing the previous software's numbering — one kogo run per old run.

The business moved to kogo from another invoicing program. That program
numbered each document type in a plain run of its own that never reset by
year: the last receipt was 33403, the last tax invoice 40413, and so on. So the
books stay one consecutive run per type, a kogo run can be *opened* at the old
run's last number plus one: TI-2026-040414 follows the old חשבונית מס 40413.

What an opening may and may not do:

* A run is opened only while it has handed out nothing in that tax year — for
  this year or the next. A number already issued is never renumbered (סעיף
  23(ב)), so a run that issued even one number keeps its start; it can be
  opened in the next tax year, when kogo starts it again.
* A run is opened once. The opening is written with the change and is never
  edited or deleted (DocumentSeriesOpening).
* One old run is continued by exactly one kogo run a tax year, of the same
  document type. Two kogo runs both starting at 121883 would be two runs of
  one type sharing numbers; a receipt run that continued a tax-invoice run
  would mix two types in one run (סעיף 5(ג)).
* The start is always the old last number plus one: the point is one run with
  no gap and no overlap, so the office types what the old program shows and
  kogo works out the rest.

Race-safety: the check and the change happen under the run's row lock, the
same lock next_number takes (DocumentSeries.open_at), and the database refuses
a second opening of a run or of an old run (the constraints on
DocumentSeriesOpening) even if two requests pass the checks together.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.documents.models import DocumentSeries, DocumentSeriesOpening, SeriesAlreadyIssued
from apps.documents.numbering import (
    SERIES_CREDIT,
    SERIES_LABELS,
    SERIES_MANUAL_INVOICE_RECEIPT,
    SERIES_RECEIPT,
    SERIES_RENTAL,
    SERIES_STORE,
    SERIES_STORE_TRANSACTION,
    SERIES_SUBSCRIPTION,
    SERIES_TAX_INVOICE,
    SERIES_TRANSACTION,
    _series_sources,
    continuation_note,
    format_document_number,
)

# The largest number a run may start from. 'IRM-2026-' and nine digits is 18
# characters: within the uniform structure's twenty for a document number
# (field 1204) and the thirty the document tables hold.
MAX_START = 999_999_999

# What each kogo run numbers, so an old run is continued only by a run of its type.
SERIES_DOCUMENT_TYPE = {
    SERIES_SUBSCRIPTION: 'combined',
    SERIES_STORE: 'combined',
    SERIES_RENTAL: 'combined',
    SERIES_MANUAL_INVOICE_RECEIPT: 'combined',
    SERIES_STORE_TRANSACTION: 'transaction_invoice',
    SERIES_TRANSACTION: 'transaction_invoice',
    SERIES_TAX_INVOICE: 'tax_invoice',
    SERIES_RECEIPT: 'receipt',
    SERIES_CREDIT: 'credit_invoice',
}

# The previous software's runs, as it names them, and the document type each numbers.
PREVIOUS_TYPES = {
    'חשבונית מס': 'tax_invoice',
    'קבלה': 'receipt',
    'חשבונית מס זיכוי': 'credit_invoice',
    'חשבון עיסקה': 'transaction_invoice',
    'חשבונית מס קבלה': 'combined',
}

# Which kogo run continues each old run, unless the office chooses another of
# the same type. חשבונית מס קבלה goes to IR — the run lesson charges issue —
# only while IR has issued nothing that year; otherwise to IRM, the office's own.
_SUGGESTED = {
    'חשבונית מס': SERIES_TAX_INVOICE,
    'קבלה': SERIES_RECEIPT,
    'חשבונית מס זיכוי': SERIES_CREDIT,
    'חשבון עיסקה': SERIES_TRANSACTION,
}


class OpeningRefused(Exception):
    """Why a run cannot be opened, in words for the office. `conflict` when the state, not the input, refuses."""

    def __init__(self, message: str, *, conflict: bool = False):
        super().__init__(message)
        self.message = message
        self.conflict = conflict


def opening_years(today=None) -> tuple[int, int]:
    """This tax year and the next: the only years a run may be opened for."""
    year = (today or timezone.localdate()).year
    return year, year + 1


def normalize_type_label(label) -> str:
    return ' '.join(str(label or '').split())


def series_options(previous_type_label: str) -> list[str]:
    """The kogo runs, in reading order, that may continue an old run of this type."""
    document_type = PREVIOUS_TYPES.get(previous_type_label)
    return [series for series in SERIES_LABELS if SERIES_DOCUMENT_TYPE.get(series) == document_type]


def _issued_in(series: str, year: int, rows: dict) -> int:
    row = rows.get((series, year))
    return row.issued if row else 0


def suggested_series(previous_type_label: str, year: int, rows: dict | None = None) -> str:
    """The kogo run suggested to continue an old run in a year."""
    if previous_type_label in _SUGGESTED:
        return _SUGGESTED[previous_type_label]
    if previous_type_label == 'חשבונית מס קבלה':
        if rows is None:
            rows = {(row.series, row.year): row for row in DocumentSeries.objects.filter(year=year)}
        if _issued_in(SERIES_SUBSCRIPTION, year, rows) == 0:
            return SERIES_SUBSCRIPTION
        return SERIES_MANUAL_INVOICE_RECEIPT
    return ''


def _issued_phrase(issued: int) -> str:
    """'הונפק מסמך אחד' / 'הונפקו 250 מסמכים'."""
    return 'הונפק מסמך אחד' if issued == 1 else f'הונפקו {issued:,} מסמכים'


def _numbers_on_documents(series: str, year: int) -> bool:
    """True when any document already carries a number of this run — whatever its counter says."""
    source = _series_sources().get(series)
    if source is None:
        return False
    queryset, field = source
    return queryset.filter(**{f'{field}__startswith': f'{series}-{year}-'}).exists()


def opening_dict(opening: DocumentSeriesOpening | None) -> dict | None:
    if opening is None:
        return None
    return {
        'series': opening.series,
        'year': opening.year,
        'start': opening.start,
        'previous_last_number': opening.previous_last_number,
        'previous_type_label': opening.previous_type_label,
        'note': opening.note,
        'created_by': opening.created_by_name,
        'created_at': opening.created_at.isoformat() if opening.created_at else None,
        'continues': continuation_note(opening.previous_last_number),
    }


def _why_not(series: str, year: int, row, opening, this_year: int, taken: dict) -> str:
    """
    '' when the run can still be opened; otherwise why not, and when it can be.

    `taken` is the year's old runs already continued: previous type label → kogo series.
    """
    if opening is not None:
        return (
            f'הסדרה כבר ממשיכה את {opening.previous_type_label} של התוכנה הקודמת '
            f'(אחרון {opening.previous_last_number}). פתיחה נעשית פעם אחת.'
        )
    issued = row.issued if row else 0
    later = (
        f' אפשר להמשיך בה את הסדרה של התוכנה הקודמת החל משנת המס {year + 1}.'
        if year == this_year else ''
    )
    if issued:
        return (
            f'בסדרה כבר {_issued_phrase(issued)} ב-{year}, ומספר שהונפק לא משתנה.{later}'
        )
    if row is not None and row.counter:
        # A counter with nothing handed out and no opening: set by hand, not by kogo.
        return f'מספור הסדרה ב-{year} כבר נקבע.{later}'
    compatible = [label for label, kind in PREVIOUS_TYPES.items() if kind == SERIES_DOCUMENT_TYPE[series]]
    if compatible and all(label in taken for label in compatible):
        return ' '.join(
            f'{label} של התוכנה הקודמת כבר ממשיכה ב-{taken[label]}-{year}.' for label in compatible
        ) + ' סדרה ישנה ממשיכה בסדרה אחת בלבד בכל שנת מס.'
    if _numbers_on_documents(series, year):
        return f'כבר קיימים מסמכים במספור של הסדרה ב-{year}.{later}'
    return ''


def series_overview(today=None) -> dict:
    """Every kogo run of this tax year and the next: what it issued, where it starts, and whether it can still be opened."""
    this_year, next_year = opening_years(today)
    years = (this_year, next_year)
    rows = {(row.series, row.year): row for row in DocumentSeries.objects.filter(year__in=years)}
    openings = {
        (opening.series, opening.year): opening
        for opening in DocumentSeriesOpening.objects.filter(year__in=years).select_related('created_by')
    }

    taken = {year: {} for year in years}
    for (series, year), opening in openings.items():
        taken[year][opening.previous_type_label] = series

    runs = []
    for year in years:
        for series, label in SERIES_LABELS.items():
            row = rows.get((series, year))
            opening = openings.get((series, year))
            start = row.start if row else 1
            counter = row.counter if row else 0
            issued = row.issued if row else 0
            reason = _why_not(series, year, row, opening, this_year, taken[year])
            runs.append({
                'series': series,
                'year': year,
                'name': f'{series}-{year}',
                'label': label,
                'document_type': SERIES_DOCUMENT_TYPE[series],
                'issued': issued,
                'first': format_document_number(series, year, start) if issued else '',
                'last': format_document_number(series, year, counter) if issued else '',
                'next_number': format_document_number(series, year, max(counter, start - 1) + 1),
                'start': start,
                'can_open': not reason,
                'reason': reason,
                'opening': opening_dict(opening),
            })

    previous_types = []
    for type_label, document_type in PREVIOUS_TYPES.items():
        previous_types.append({
            'label': type_label,
            'document_type': document_type,
            'series_options': series_options(type_label),
            'suggested': {str(year): suggested_series(type_label, year, rows) for year in years},
            # The kogo run already continuing this old run, per year, if any.
            'continued_by': {
                str(year): taken[year][type_label] for year in years if type_label in taken[year]
            },
        })

    return {
        'current_year': this_year,
        'years': list(years),
        'runs': runs,
        'previous_types': previous_types,
    }


def _positive_int(value, what: str) -> int:
    if isinstance(value, bool):
        raise OpeningRefused(f'{what}: יש להזין מספר שלם')
    if isinstance(value, str):
        value = value.strip().replace(',', '')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise OpeningRefused(f'{what}: יש להזין מספר שלם') from None
    if isinstance(value, float) and value != number:
        raise OpeningRefused(f'{what}: יש להזין מספר שלם')
    return number


def open_series(*, series, year, previous_last_number, previous_type_label, note='', start=None,
                user=None, today=None) -> DocumentSeriesOpening:
    """
    Open a kogo run at the previous software's last number plus one, with its record.

    Raises OpeningRefused with the reason in Hebrew. On success the run's next
    number is `start`, and the returned record says who opened it and from what.
    """
    series = str(series or '').strip().upper()
    if series not in SERIES_LABELS:
        raise OpeningRefused('סדרה לא מוכרת')

    year = _positive_int(year, 'שנת מס')
    this_year, next_year = opening_years(today)
    if year not in (this_year, next_year):
        raise OpeningRefused(f'אפשר לפתוח סדרה לשנת המס {this_year} או {next_year} בלבד')

    type_label = normalize_type_label(previous_type_label)
    if type_label not in PREVIOUS_TYPES:
        raise OpeningRefused('יש לבחור את סוג המסמך בתוכנה הקודמת')
    if PREVIOUS_TYPES[type_label] != SERIES_DOCUMENT_TYPE[series]:
        raise OpeningRefused(
            f'{type_label} ממשיכה רק בסדרה של אותו סוג מסמך, ו-{series} היא {SERIES_LABELS[series]}'
        )

    last = _positive_int(previous_last_number, 'המספר האחרון בתוכנה הקודמת')
    if last < 1:
        raise OpeningRefused('המספר האחרון בתוכנה הקודמת חייב להיות 1 או יותר')
    if last + 1 > MAX_START:
        raise OpeningRefused(f'המספר האחרון בתוכנה הקודמת גדול מדי (עד {MAX_START - 1:,})')
    first = last + 1
    if start not in (None, ''):
        if _positive_int(start, 'המספר הראשון') != first:
            raise OpeningRefused(
                f'הסדרה ממשיכה בדיוק מהמספר שאחרי האחרון בתוכנה הקודמת: {first}, בלי דילוג ובלי חפיפה'
            )

    note = str(note or '').strip()
    if len(note) > 1000:
        raise OpeningRefused('ההערה ארוכה מדי (עד 1000 תווים)')

    name = ''
    if user is not None and getattr(user, 'is_authenticated', False):
        name = (user.get_full_name() or user.get_username() or '')[:150]

    try:
        with transaction.atomic():
            # The row lock first: from here nothing is handed out in this run
            # until the opening commits or rolls back.
            row = DocumentSeries.open_at(series, year, first)
            if DocumentSeriesOpening.objects.filter(series=series, year=year).exists():
                raise OpeningRefused(f'הסדרה {series}-{year} כבר נפתחה', conflict=True)
            if _numbers_on_documents(series, year):
                raise OpeningRefused(
                    f'כבר קיימים מסמכים במספור של {series}-{year}, ומספר שהונפק לא משתנה', conflict=True,
                )
            taken = DocumentSeriesOpening.objects.filter(year=year, previous_type_label=type_label).first()
            if taken is not None:
                raise OpeningRefused(
                    f'{type_label} של התוכנה הקודמת כבר ממשיכה ב-{taken.series}-{year}. '
                    f'סדרה ישנה ממשיכה בסדרה אחת בלבד בכל שנת מס.',
                    conflict=True,
                )
            opening = DocumentSeriesOpening.objects.create(
                series=series,
                year=year,
                start=row.start,
                previous_last_number=last,
                previous_type_label=type_label,
                note=note,
                created_by=user if name else None,
                created_by_name=name,
            )
    except SeriesAlreadyIssued as exc:
        issued = exc.row.issued
        if issued:
            raise OpeningRefused(
                f'בסדרה {series}-{year} כבר {_issued_phrase(issued)}, ומספר שהונפק לא משתנה. '
                f'אפשר להמשיך בה את הסדרה של התוכנה הקודמת בשנת המס הבאה.',
                conflict=True,
            ) from None
        raise OpeningRefused(f'מספור הסדרה {series}-{year} כבר נקבע', conflict=True) from None
    except IntegrityError:
        # Two openings passed the checks together; the database let one through.
        raise OpeningRefused(
            f'הסדרה {series}-{year}, או {type_label} של התוכנה הקודמת, נפתחה זה עתה בבקשה אחרת',
            conflict=True,
        ) from None
    return opening
