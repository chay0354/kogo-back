"""One phone, one spelling: 9725XXXXXXXX. Anything else is not a WhatsApp contact of ours."""
import re

from apps.core.manychat_service import ManyChatService

# Kogo is Israel-only for phones, and WhatsApp reaches mobiles.
_ISRAELI_MOBILE = re.compile(r'9725\d{8}')


def normalize_phone(raw) -> str:
    """'050-123 4567', '+972501234567', 501234567 -> '972501234567'. '' for anything that is not an Israeli mobile."""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    normalized = ManyChatService.normalize_phone_e164(str(raw or '').strip())
    return normalized if _ISRAELI_MOBILE.fullmatch(normalized) else ''


def phone_display(phone: str) -> str:
    """'972501234567' -> '050-1234567'."""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('972'):
        digits = '0' + digits[3:]
    if len(digits) == 10:
        return f'{digits[:3]}-{digits[3:]}'
    if len(digits) == 9:
        return f'{digits[:2]}-{digits[2:]}'
    return digits
