"""
Card data that must never be kept.

Tranzila's REST answer echoes the request it received (`original_request`),
and for a typed card that echo carries the CVV. The whole answer used to be
stored in `tranzila_transactions.response_data` — 759 rows held a CVV on
25.9.2026. Card rules forbid keeping a CVV after authorisation at all, and a
full card number unmasked.

`scrub_card_data` is applied to every gateway answer before anything keeps or
returns it, and again by TranzilaTransaction.save() as the last line. It keeps
what refunds read back — the expiry, the saved-card token, the terminal name —
and drops or masks the rest.
"""
from __future__ import annotations

import re
from typing import Any

# Security codes: dropped wherever they appear, whatever the nesting.
SECURITY_CODE_KEYS = frozenset({'cvv', 'cvv2', 'cvc', 'ccv', 'mycvv', 'card_cvv', 'cvv_code'})

# Keys that may hold a card number. A token or a masked number is kept (refunds
# read it back); a full card number is masked to its last four digits.
CARD_NUMBER_KEYS = frozenset({'card_number', 'ccno', 'credit_card_number', 'cardnum', 'pan'})
_FULL_CARD_NUMBER = re.compile(r'^\d{13,19}$')


def mask_card_number(value: str) -> str:
    digits = str(value)
    return '*' * (len(digits) - 4) + digits[-4:]


def scrub_card_data(value: Any) -> Any:
    """A copy of `value` with every security code removed and full card numbers masked."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SECURITY_CODE_KEYS:
                continue
            if lowered in CARD_NUMBER_KEYS and isinstance(item, (str, int)) and _FULL_CARD_NUMBER.match(str(item).strip()):
                clean[key] = mask_card_number(str(item).strip())
                continue
            clean[key] = scrub_card_data(item)
        return clean
    if isinstance(value, list):
        return [scrub_card_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_card_data(item) for item in value)
    return value


def holds_card_data(value: Any) -> bool:
    """True when `value` still carries a security code or a full card number."""
    return scrub_card_data(value) != value
