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
module writes that file — one row per phone Kogo sends to, the number twice:
once to match the contact, once as the value of ``kogo_whatsapp_phone``.

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


def family_whatsapp_phone(family, parents) -> str:
    """
    The phone Kogo sends a family's messages to, by the same rule as
    ``build_enrollment_whatsapp_context``: the primary parent, else the first
    parent, and the family's own phone when that parent has none.

    ``parents`` is the family's parents in model order (primary first).
    """
    parent = next((p for p in parents if p.is_primary), None) or (parents[0] if parents else None)
    return ((parent.phone if parent else '') or family.phone or '').strip()


def contact_index_phones(scope: str = SCOPE_CURRENT) -> list[str]:
    """Every distinct, valid E.164 phone Kogo sends to within the scope, sorted."""
    from django.db.models import Prefetch

    from apps.customers.models import Family, Parent

    if scope not in SCOPE_STATUSES:
        raise ValueError(f'unknown scope: {scope}')

    families = (
        Family.objects.filter(children__status__in=SCOPE_STATUSES[scope])
        .distinct()
        .order_by()
        .only('id', 'phone')
        .prefetch_related(
            Prefetch('parents', queryset=Parent.objects.only('id', 'family_id', 'phone', 'is_primary', 'first_name'))
        )
    )

    phones: set[str] = set()
    for family in families:
        raw = family_whatsapp_phone(family, list(family.parents.all()))
        e164 = ManyChatService.normalize_phone_e164(raw)
        if _VALID_E164.fullmatch(e164):
            phones.add(e164)
    return sorted(phones)


def contact_index_csv(phones: list[str]) -> str:
    """The import file: a header, then each number twice."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow([WHATSAPP_ID_COLUMN, INDEX_FIELD_NAME])
    for phone in phones:
        writer.writerow([phone, phone])
    return out.getvalue()
