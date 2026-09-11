"""Consecutive document numbers — one gapless series per document kind and tax year.

Each series has a prefix that says which run a number belongs to, so the office,
the accountant and a tax inspector can each check a run on its own:

    IR — חשבונית מס / קבלה for lesson charges (widget, standing orders, card links)
    ST — חשבונית מס / קבלה for store sales paid on the spot (card or cash)
    SD — חשבונית עסקה for store sales put on monthly billing (not yet paid)

One series per document type, because סעיף 5(ג) wants invoices that double as
receipts numbered in a run of their own.

Formal documents (the documents module) keep their existing DocumentCounter run.
Numbers already issued in the old formats stay exactly as they are: an issued
document is never renumbered (סעיף 23(ב)).
"""
from __future__ import annotations

from datetime import date, datetime

from django.utils import timezone

from apps.documents.models import DocumentSeries

SERIES_SUBSCRIPTION = 'IR'
SERIES_STORE = 'ST'
SERIES_STORE_TRANSACTION = 'SD'


def _tax_year(when: date | datetime | None) -> int:
    if when is None:
        return timezone.localdate().year
    if isinstance(when, datetime):
        return (timezone.localtime(when) if timezone.is_aware(when) else when).year
    return when.year


def next_document_number(series: str, when: date | datetime | None = None) -> str:
    """'IR-2026-000123'. Call inside the transaction that saves the document."""
    year = _tax_year(when)
    return f'{series}-{year}-{DocumentSeries.next_number(series, year):06d}'
