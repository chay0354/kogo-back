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


def add_vat(net: Decimal | float | int | str) -> Decimal:
    """
    The VAT-inclusive total for an amount quoted before VAT, to the agora.

    The other direction from split_vat_inclusive: a studio rental is priced
    before מע"מ (the contract says "לפני מע"מ"), so what the tenant pays is
    the net amount plus VAT at the current rate.
    """
    amount = Decimal(str(net or 0))
    return (amount * (Decimal('1') + VAT_RATE)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def format_vat_breakdown_he(gross: Decimal | float | int | str) -> list[tuple[str, str]]:
    """Hebrew label/value pairs for invoice totals (before VAT, VAT, grand total)."""
    before, vat, total = split_vat_inclusive(gross)
    return [
        ('סה"כ לפני מע"מ', f'₪{before:.2f}'),
        (f'מע"מ {VAT_PERCENT_DISPLAY:g}%', f'₪{vat:.2f}'),
        ('סה"כ כולל מע"מ', f'₪{total:.2f}'),
    ]


def split_vat_inclusive_lines(
    grosses: list[Decimal | float | int | str],
) -> list[tuple[Decimal, Decimal]]:
    """
    (before VAT, VAT) for each VAT-inclusive line, adding up to the whole.

    Splitting each line on its own and printing the column would leave a total
    that is an agora off the document's own — every line rounds separately, and
    over forty identical lines that drifts by a fifth of a shekel. The residue
    is therefore spread an agora at a time over the lines, so the printed column
    of "סה"כ לפני מע"מ" sums to exactly what split_vat_inclusive says for the
    document and no line is more than one agora from its own split.

    A credit line is stored positive and shown with a minus, but a negative one
    still splits by size with the sign put back, so it reads the same way round.
    """
    amounts = [Decimal(str(g or 0)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP) for g in grosses]
    if not amounts:
        return []

    def _before(amount: Decimal) -> Decimal:
        before, _, _ = split_vat_inclusive(abs(amount))
        return -before if amount < 0 else before

    befores = [_before(amount) for amount in amounts]
    residue = _before(sum(amounts)) - sum(befores)
    step = _TWOPLACES if residue > 0 else -_TWOPLACES
    index = 0
    while residue != 0 and index < len(befores) * 2:
        befores[index % len(befores)] += step
        residue -= step
        index += 1
    return [(before, amount - before) for amount, before in zip(amounts, befores)]
