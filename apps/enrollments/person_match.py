"""
Deciding when two records are the same person.

Full name and phone together, never either alone: a class holds children who
share a first name, and siblings share a phone.
"""
from __future__ import annotations


def normalise_phone(raw: str | None) -> str:
    """
    Digits only, so 050-123-4567 and 0501234567 compare equal.

    A leading country code becomes the zero it stands for, the same way the
    customers search reads a typed 972 number.
    """
    if not raw:
        return ''
    digits = ''.join(ch for ch in str(raw) if ch.isdigit())
    if digits.startswith('972'):
        digits = '0' + digits[3:]
    return digits


def normalise_name(raw: str | None) -> str:
    return ' '.join(str(raw or '').split()).strip().casefold()


def contact_phone(child) -> str:
    """The child's own phone, falling back to the family's."""
    phone = (getattr(child, 'phone_number', None) or '').strip()
    if phone:
        return phone
    family = getattr(child, 'family', None)
    if family:
        return (getattr(family, 'phone', None) or '').strip()
    return ''


def person_key(*, first_name: str | None, last_name: str | None, phone: str | None) -> tuple[str, str] | None:
    """
    What identifies a person for duplicate checks, or None when it cannot.

    A row missing a name or a phone says nothing about who it is, and matching
    on half of it would fold unrelated children together.
    """
    name = normalise_name(f'{first_name or ""} {last_name or ""}')
    digits = normalise_phone(phone)
    if not name or not digits:
        return None
    return name, digits


def child_person_key(child) -> tuple[str, str] | None:
    return person_key(
        first_name=getattr(child, 'first_name', ''),
        last_name=getattr(child, 'last_name', ''),
        phone=contact_phone(child),
    )
