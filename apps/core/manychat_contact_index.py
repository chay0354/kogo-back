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
module writes that file — one row per phone Kogo sends to (each family's own
number, and the mobiles of its other parents, which broadcasts also reach): the
number twice (once to match the contact, once as the value of
``kogo_whatsapp_phone``), and that parent's first and last name.

The names are not optional. The first file (29.9.2026) carried the number
only, and ManyChat's import blanked First Name and Last Name on each of the 393
contacts it updated — a column the file does not have is imported as empty.

Read-only: it reads families and writes nothing, here or in ManyChat.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

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


# The other parents' phones a broadcast also writes to (apps/customers/broadcast.py
# `_extra_recipients`): mobiles only, the same test as there.
_MOBILE_E164 = re.compile(r'9725\d{8}')


@dataclass
class Recipient:
    """One phone Kogo writes to, and whose it is."""
    phone: str
    first_name: str
    last_name: str
    # A current child of the family, for a link to the card; '' when none.
    child_id: str = ''
    child_name: str = ''
    # The family's own number (primary parent, else first, else family phone),
    # as opposed to another parent's that only broadcasts reach.
    primary: bool = True

    @property
    def name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()


def current_recipients(scope: str = SCOPE_CURRENT) -> list[Recipient]:
    """
    Every distinct phone Kogo sends to within the scope, sorted by phone: each
    family's own number, and the mobiles of its other parents.

    One row per phone. A family's own number beats another parent's; between
    equals the fuller name wins (a first name beats a last name alone), and on
    a tie the first family by id — so the answer never depends on UUID order.
    """
    from django.db.models import Prefetch

    from apps.customers.models import Child, Family, Parent

    if scope not in SCOPE_STATUSES:
        raise ValueError(f'unknown scope: {scope}')
    statuses = SCOPE_STATUSES[scope]

    families = (
        Family.objects.filter(children__status__in=statuses)
        .distinct()
        .order_by('id')
        .only('id', 'phone', 'name')
        .prefetch_related(
            Prefetch(
                'parents',
                queryset=Parent.objects.only('id', 'family_id', 'phone', 'is_primary', 'first_name', 'last_name'),
            ),
            Prefetch(
                'children',
                queryset=Child.objects.filter(status__in=statuses).only('id', 'family_id', 'first_name', 'last_name'),
                to_attr='current_children',
            ),
        )
    )

    best: dict[str, Recipient] = {}

    def keep(candidate: Recipient) -> None:
        held = best.get(candidate.phone)
        if held is None or _recipient_rank(candidate) > _recipient_rank(held):
            best[candidate.phone] = candidate

    for family in families:
        parents = list(family.parents.all())
        children = sorted(family.current_children, key=lambda c: (c.first_name or '', str(c.id)))
        child = children[0] if children else None
        child_id = str(child.id) if child else ''
        child_name = f'{child.first_name} {child.last_name}'.strip() if child else ''

        own = ManyChatService.normalize_phone_e164(family_whatsapp_phone(family, parents))
        if _VALID_E164.fullmatch(own):
            first, last = family_whatsapp_name(family, parents)
            keep(Recipient(own, first, last, child_id, child_name, primary=True))
        for parent in parents:
            other = ManyChatService.normalize_phone_e164(parent.phone or '')
            if not other or other == own or not _MOBILE_E164.fullmatch(other):
                continue
            keep(Recipient(
                other, (parent.first_name or '').strip(), (parent.last_name or '').strip(),
                child_id, child_name, primary=False,
            ))
    return [best[phone] for phone in sorted(best)]


def _recipient_rank(recipient: Recipient) -> tuple[bool, bool, bool]:
    return (recipient.primary, *_name_rank((recipient.first_name, recipient.last_name)))


def contact_index_rows(scope: str = SCOPE_CURRENT) -> list[tuple[str, str, str]]:
    """(phone, first name, last name) for every recipient in the scope, sorted by phone."""
    return [(r.phone, r.first_name, r.last_name) for r in current_recipients(scope)]


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
