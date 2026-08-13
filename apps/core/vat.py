"""Israeli VAT helpers for customer-facing invoices.

Retail prices in Cogomelo are VAT-inclusive (ברוטו). On a חשבונית מס / קבלה
we extract מע"מ at the current statutory rate (18% since Jan 2025):

    net = gross / 1.18
    vat = gross - net

so net + vat always equals the amount the customer paid.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

VAT_RATE = Decimal('0.18')
VAT_PERCENT_DISPLAY = Decimal('18')
DOCUMENT_TITLE = 'חשבונית מס / קבלה'
_TWOPLACES = Decimal('0.01')


def split_vat_inclusive(gross: Decimal | float | int | str) -> tuple[Decimal, Decimal, Decimal]:
    """Return (amount_before_vat, vat_amount, gross) for a VAT-inclusive total."""
    total = Decimal(str(gross)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    if total <= 0:
        zero = Decimal('0.00')
        return zero, zero, total
    before = (total / (Decimal('1') + VAT_RATE)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    vat = (total - before).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)
    return before, vat, total


def format_vat_breakdown_he(gross: Decimal | float | int | str) -> list[tuple[str, str]]:
    """Hebrew label/value pairs for invoice totals (before VAT, VAT, grand total)."""
    before, vat, total = split_vat_inclusive(gross)
    return [
        ('סה"כ לפני מע"מ', f'₪{before:.2f}'),
        (f'מע"מ {VAT_PERCENT_DISPLAY:g}%', f'₪{vat:.2f}'),
        ('סה"כ כולל מע"מ', f'₪{total:.2f}'),
    ]
