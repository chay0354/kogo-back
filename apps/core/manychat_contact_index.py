"""
The file that makes every Kogo parent findable in ManyChat, imported there once.

ManyChat's API has no search by WhatsApp number. Kogo finds a contact through
the ``kogo_whatsapp_phone`` User Field instead, which a ManyChat rule fills for
each new contact. The 4,196 contacts imported from the previous system on
29.7.2026 predate the rule, and a rule does not run on a bulk action — so they
never got the field, and a broadcast to them fails with "This WhatsApp ID
already exists" (``ManyChatContactUnfindable``).

ManyChat's own CSV import does what the API cannot: a row whose WhatsApp ID
matches an existing contact updates that contact, custom fields included. This
module writes that file — one row per phone Kogo sends to, the number twice
(once to match the contact, once as the value of ``kogo_whatsapp_phone``), and
the parent's first and last name.

The names are not optional. The first file (29.9.2026) carried the number
only, and ManyChat's import blanked First Name and Last Name on each of the 393
contacts it updated — a column the file does not have is imported as empty.

Read-only: it reads families and writes nothing, here or in ManyChat.
"""
from __future__ import annotations

import csv
import io
import re

from apps.core.manychat_service import ManyChatService
from apps.customers.child_status import (
    CHILD_STATUSES,
    STATUS_ACTIVE,
    STATUS_GHOST,
    STATUS_PAYMENT_PROBLEM,
    STATUS_PENDING,
    STATUS_TRIAL_COMPLETED,
    STATUS_TRIAL_SIGNED,
)

# The User Field Kogo searches (see PHONE_LOOKUP_FIELD_NAMES); the column is
# named after it so ManyChat's import offers the right field.
INDEX_FIELD_NAME = 'kogo_whatsapp_phone'
WHATSAPP_ID_COLUMN = 'WhatsApp ID'
FIRST_NAME_COLUMN = 'First Name'
LAST_NAME_COLUMN = 'Last Name'

SCOPE_CURRENT = 'current'
SCOPE_ALL = 'all'

# Current: a family someone would message this season. Everyone: former
# students too — ManyChat creates a contact for a number it does not have,
# and a bigger contact count can mean a bigger ManyChat bill.
SCOPE_STATUSES = {
    SCOPE_CURRENT: (
        STATUS_ACTIVE,
        STATUS_PAYMENT_PROBLEM,
        STATUS_TRIAL_SIGNED,
        STATUS_TRIAL_COMPLETED,
        STATUS_PENDING,
    ),
    SCOPE_ALL: tuple(s for s in CHILD_STATUSES if s != STATUS_GHOST),
}

# 972 and a local number without its 0: 8 digits for a landline, 9 for a
# mobile. Anything else ManyChat would ignore, or match to a stranger.
_VALID_E164 = re.compile(r'972\d{8,9}')


def _family_parent(parents):
    """The parent Kogo writes to: the primary one, else the first (model order)."""
    return next((p for p in parents if p.is_primary), None) or (parents[0] if parents else None)


def family_whatsapp_phone(family, parents) -> str:
    """
    The phone Kogo sends a family's messages to, by the same rule as
    ``build_enrollment_whatsapp_context``: the primary parent, else the first
    parent, and the family's own phone when that parent has none.

    ``parents`` is the family's parents in model order (primary first).
    """
    parent = _family_parent(parents)
    return ((parent.phone if parent else '') or family.phone or '').strip()


def family_whatsapp_name(family, parents) -> tuple[str, str]:
    """
    First and last name of the same parent, as ``build_enrollment_whatsapp_context``
    names them; the family name alone when the family has no parent.
    """
    parent = _family_parent(parents)
    if parent:
        return (parent.first_name or '').strip(), (parent.last_name or '').strip()
    return (family.name or '').strip(), ''


def contact_index_rows(scope: str = SCOPE_CURRENT) -> list[tuple[str, str, str]]:
    """
    (phone, first name, last name) for every distinct, valid E.164 phone Kogo
    sends to within the scope, sorted by phone. Two families on one phone give
    one row, named after the fullest name among them (a first name beats a last
    name alone); on a tie, the first family by id.
    """
    from django.db.models import Prefetch

    from apps.customers.models import Family, Parent

    if scope not in SCOPE_STATUSES:
        raise ValueError(f'unknown scope: {scope}')

    families = (
        Family.objects.filter(children__status__in=SCOPE_STATUSES[scope])
        .distinct()
        .order_by('id')
        .only('id', 'phone', 'name')
        .prefetch_related(
            Prefetch(
                'parents',
                queryset=Parent.objects.only('id', 'family_id', 'phone', 'is_primary', 'first_name', 'last_name'),
            )
        )
    )

    names: dict[str, tuple[str, str]] = {}
    for family in families:
        parents = list(family.parents.all())
        e164 = ManyChatService.normalize_phone_e164(family_whatsapp_phone(family, parents))
        if not _VALID_E164.fullmatch(e164):
            continue
        name = family_whatsapp_name(family, parents)
        if e164 not in names or _name_rank(name) > _name_rank(names[e164]):
            names[e164] = name
    return [(phone, *names[phone]) for phone in sorted(names)]


def _name_rank(name: tuple[str, str]) -> tuple[bool, bool]:
    first, last = name
    return bool(first), bool(last)


def contact_index_phones(scope: str = SCOPE_CURRENT) -> list[str]:
    """Every distinct, valid E.164 phone Kogo sends to within the scope, sorted."""
    return [phone for phone, _first, _last in contact_index_rows(scope)]


def contact_index_csv(rows: list[tuple[str, str, str]]) -> str:
    """The import file: a header, then each number twice and the parent's name."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow([WHATSAPP_ID_COLUMN, INDEX_FIELD_NAME, FIRST_NAME_COLUMN, LAST_NAME_COLUMN])
    for phone, first, last in rows:
        writer.writerow([phone, phone, first, last])
    return out.getvalue()
