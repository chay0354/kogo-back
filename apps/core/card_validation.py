"""
Card detail checks that run before anything is sent to Tranzila.

Live acquirers watch decline ratios, so obviously-invalid input must never reach
the gateway. Messages are Hebrew because they are shown to parents as-is.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict


class CardValidationError(ValueError):
    """Raised with a parent-facing Hebrew message."""


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def israeli_id_valid(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(digits):
        value = int(char) * (1 if index % 2 == 0 else 2)
        total += value if value < 10 else value - 9
    return total % 10 == 0


def _digits_only(raw: Any) -> str:
    return ''.join(ch for ch in str(raw or '') if ch.isdigit())


def _normalized_year(raw: Any) -> int:
    year = int(str(raw).strip())
    if year < 100:
        year += 2000
    return year


def validate_card_details(card_details: Dict[str, Any], *, today: date | None = None) -> Dict[str, Any]:
    """
    Return normalized card fields, or raise CardValidationError.

    card_holder_id is validated only when the caller supplied one; Israeli
    terminals normally require it, but the field stays optional here so the
    gateway remains the authority on what its terminal demands.
    """
    if not isinstance(card_details, dict):
        raise CardValidationError('פרטי כרטיס נדרשים')

    card_number = _digits_only(card_details.get('card_number'))
    if not card_number:
        raise CardValidationError('יש להזין מספר כרטיס')
    if not 12 <= len(card_number) <= 19:
        raise CardValidationError('מספר הכרטיס אינו תקין')
    if not luhn_valid(card_number):
        raise CardValidationError('מספר הכרטיס אינו תקין')

    try:
        expiry_month = int(str(card_details.get('expiry_month')).strip())
    except (TypeError, ValueError):
        raise CardValidationError('חודש תפוגה אינו תקין')
    if not 1 <= expiry_month <= 12:
        raise CardValidationError('חודש תפוגה אינו תקין')

    try:
        expiry_year = _normalized_year(card_details.get('expiry_year'))
    except (TypeError, ValueError):
        raise CardValidationError('שנת תפוגה אינה תקינה')

    reference = today or date.today()
    if expiry_year < reference.year or expiry_year > reference.year + 20:
        raise CardValidationError('שנת תפוגה אינה תקינה')
    if (expiry_year, expiry_month) < (reference.year, reference.month):
        raise CardValidationError('תוקף הכרטיס פג')

    cvv = _digits_only(card_details.get('cvv'))
    if len(cvv) not in (3, 4):
        raise CardValidationError('CVV אינו תקין')

    card_holder_id = _digits_only(card_details.get('card_holder_id'))
    if card_holder_id and not israeli_id_valid(card_holder_id):
        raise CardValidationError('תעודת הזהות של בעל הכרטיס אינה תקינה')

    return {
        'card_number': card_number,
        'expiry_month': expiry_month,
        'expiry_year': expiry_year,
        'cvv': cvv,
        'card_holder_id': card_holder_id,
    }
