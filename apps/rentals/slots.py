"""Studio slots and the tenancies that hold them.

A slot is a studio-rental calendar event (ScheduleEvent with is_studio_rental).
The rules that tie slots to tenancies live here, in one place, so the screen
that edits one tenancy and the bulk import cannot drift apart:

    suggested_monthly_amount — what the rental contract would bill a month
    link_slots / unlink_slot — attach slots to a tenancy, or let one go
    rental_suggestions       — rentals no tenancy holds yet, grouped by renter

A slot joins a tenancy only when it is a studio rental, sits in the tenancy's
branch, and no other tenancy holds it. Linking locks the slot rows before it
checks them, so two people linking the same slot at once cannot both succeed:
the second waits for the first to commit, then finds the slot taken.
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import CharField, Func, Q
from django.utils import timezone

from apps.core.scoping import scope_branches
from apps.scheduling.models import ScheduleEvent

# The contract's own rule: a month is four weeks of every weekday rented.
# apps/scheduling/rental_agreement/generator.py prints "rate × 4" per weekday row.
WEEKS_PER_MONTH = 4

_TWOPLACES = Decimal('0.01')
_NON_DIGITS = re.compile(r'\D+')


class SlotError(Exception):
    """A slot that cannot be linked or unlinked. The message is shown to the office as is."""

    def __init__(self, message: str, slot_id=None):
        super().__init__(message)
        self.message = message
        self.slot_id = str(slot_id) if slot_id is not None else None


def id_digits(value) -> str:
    """'51-234567-8' -> '512345678': an ID or company number, however it was typed."""
    return _NON_DIGITS.sub('', str(value or ''))


class DigitsOnly(Func):
    """id_digits in SQL: the column with every non-digit removed (Postgres)."""

    function = 'REGEXP_REPLACE'
    template = "%(function)s(%(expressions)s, '[^0-9]', '', 'g')"
    output_field = CharField()


def _weekday_count(days) -> int:
    distinct = set()
    for day in days or []:
        try:
            distinct.add(int(day))
        except (TypeError, ValueError):
            continue
    return len(distinct) or 1


def suggested_monthly_amount(slots) -> Decimal:
    """
    What the rental contract bills a month for these slots, before VAT.

    Σ price_per_session × 4 × the number of weekdays each weekly slot repeats on
    (weekly_repeat_days) — the PDF's "rate × 4" per weekday row, so the office
    sees one number in both places. A weekly slot with no weekdays listed counts
    one: that is how the calendar reads a weekly rental saved before the list
    existed.

    A one-time rental adds nothing: it is not monthly, and the PDF bills it once.
    Neither does an inactive slot — nothing is rented there any more. The tenants
    screen (tenancyUtils.estimateMonthlyAmount) follows the same rule, and the
    office confirms the amount either way.
    """
    total = Decimal('0')
    for slot in slots:
        if not slot.is_active or slot.event_type != 'weekly':
            continue
        rate = Decimal(str(slot.price_per_session or 0))
        total += rate * WEEKS_PER_MONTH * _weekday_count(slot.weekly_repeat_days)
    return total.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def slot_label(slot) -> str:
    """How a slot is named in an error: the renter, else the event's own name."""
    return (slot.renter_name or '').strip() or (slot.name or '').strip() or str(slot.pk)


def _parse_ids(raw_ids) -> list:
    """Distinct UUIDs in the order given. Anything that is not one names no slot."""
    if not isinstance(raw_ids, (list, tuple)):
        raise SlotError('slot_ids חייב להיות רשימה')
    ids = []
    for raw in raw_ids:
        try:
            value = uuid.UUID(str(raw))
        except (TypeError, ValueError, AttributeError):
            raise SlotError('השכירות לא נמצאה', slot_id=raw)
        if value not in ids:
            ids.append(value)
    if not ids:
        raise SlotError('יש לבחור לפחות שכירות אחת')
    return ids


def lock_slots(raw_ids, user) -> list:
    """
    The slots raw_ids names, locked until the transaction ends, in the order given.

    A slot outside the user's branches is reported as not found, exactly like
    one that does not exist, so an id from another branch reveals nothing.
    Call inside a transaction.
    """
    ids = _parse_ids(raw_ids)
    # No select_related here: Postgres refuses FOR UPDATE on the nullable side
    # of an outer join, and branch and studio are both nullable.
    rows = scope_branches(ScheduleEvent.objects.filter(pk__in=ids), user, 'branch').select_for_update()
    found = {slot.pk: slot for slot in rows}
    for pk in ids:
        if pk not in found:
            raise SlotError('השכירות לא נמצאה', slot_id=pk)
    return [found[pk] for pk in ids]


def check_slots(branch_id, slots, tenancy=None) -> None:
    """
    Raise SlotError for the first slot that may not join a tenancy in this branch.

    A slot must be a studio rental, sit in the tenancy's branch, and be held by
    no other tenancy. A slot the tenancy itself already holds passes.
    """
    from apps.rentals.models import Tenancy

    if branch_id is None:
        raise SlotError('יש לבחור סניף להסכם לפני שיוך שכירויות')
    for slot in slots:
        label = slot_label(slot)
        if not slot.is_studio_rental:
            raise SlotError(f'"{label}" אינו שכירות סטודיו', slot.pk)
        if slot.branch_id != branch_id:
            raise SlotError(f'השכירות של "{label}" בסניף אחר מסניף ההסכם', slot.pk)
        if slot.tenancy_id and (tenancy is None or slot.tenancy_id != tenancy.pk):
            # Slots only ever join a tenancy of their own branch, so the holder
            # is in a branch the user can already see.
            holder = Tenancy.objects.select_related('tenant').filter(pk=slot.tenancy_id).first()
            who = f' ({holder.tenant.full_name})' if holder else ''
            raise SlotError(f'השכירות של "{label}" כבר משויכת להסכם שכירות אחר{who}', slot.pk)


def link_slots(tenancy, raw_ids, user) -> int:
    """Attach slots to a tenancy, all of them or none. Returns how many were newly linked."""
    with transaction.atomic():
        slots = lock_slots(raw_ids, user)
        check_slots(tenancy.branch_id, slots, tenancy)
        ids = [slot.pk for slot in slots if slot.tenancy_id != tenancy.pk]
        # The lock makes this the only writer; the filter keeps the rule even so.
        linked = (
            ScheduleEvent.objects
            .filter(pk__in=ids, tenancy__isnull=True)
            .update(tenancy=tenancy, updated_at=timezone.now())
        )
        if linked != len(ids):
            raise SlotError('אחת השכירויות שויכה בינתיים להסכם אחר. יש לרענן ולנסות שוב')
        return linked


def unlink_slot(tenancy, raw_id) -> None:
    """
    Let one slot go from a tenancy. The event itself stays on the calendar.

    Looked up among the tenancy's own slots rather than the user's branches:
    the caller already reached the tenancy, and every slot it holds is part of it.
    """
    try:
        pk = uuid.UUID(str(raw_id))
    except (TypeError, ValueError, AttributeError):
        raise SlotError('השכירות לא נמצאה', slot_id=raw_id)
    with transaction.atomic():
        # Through ScheduleEvent rather than tenancy.slots: a prefetched related
        # manager brings its select_related joins along, and FOR UPDATE refuses them.
        slot = ScheduleEvent.objects.select_for_update().filter(pk=pk, tenancy=tenancy).first()
        if slot is None:
            raise SlotError('השכירות אינה משויכת להסכם הזה', slot_id=pk)
        ScheduleEvent.objects.filter(pk=slot.pk).update(tenancy=None, updated_at=timezone.now())


def _renter_name(slots) -> str:
    """The name the renter goes by most often across their slots; the first one on a tie."""
    names = [(slot.renter_name or '').strip() for slot in slots]
    names = [name for name in names if name]
    if not names:
        return ''
    counts = Counter(names)
    return max(names, key=lambda name: (counts[name], -names.index(name)))


def _existing_tenants(wanted: set, user) -> dict:
    """ID digits -> the merchant on file with that company or ID number, oldest first."""
    from apps.customers.models import BusinessCustomer

    customers = (
        scope_branches(BusinessCustomer.objects.all(), user, 'branch')
        .annotate(_company_digits=DigitsOnly('company_number'), _id_digits=DigitsOnly('id_number'))
        .filter(Q(_company_digits__in=wanted) | Q(_id_digits__in=wanted))
        .order_by('created_at', 'id')
    )
    found = {}
    for customer in customers:
        for digits in (customer._company_digits, customer._id_digits):
            if digits in wanted and digits not in found:
                found[digits] = {
                    'id': str(customer.pk),
                    'full_name': customer.full_name,
                    'company_number': customer.company_number,
                    'id_number': customer.id_number,
                }
    return found


def rental_suggestions(user) -> list:
    """
    The studio rentals in the user's branches that no tenancy holds, grouped by renter.

    Grouped by the renter's ID number, digits only, so '51-234567-8' and
    '512345678' are one renter. A rental with no ID is a group of its own, keyed
    by its event: two renters who share a name must never become one tenant. A
    renter who rents in two branches is split into one group per branch,
    because a tenancy belongs to one branch.

    Each group names the merchant already on file with that company or ID number,
    when the user may see one, so the office can attach the tenancy to it rather
    than create the same merchant twice. Only active rentals with a branch are
    offered: the others cannot join a tenancy.
    """
    events = (
        scope_branches(
            ScheduleEvent.objects.filter(
                is_studio_rental=True, is_active=True, tenancy__isnull=True, branch__isnull=False,
            ),
            user,
            'branch',
        )
        .select_related('branch', 'studio')
        .order_by('event_date', 'start_time', 'created_at')
    )
    groups = {}
    for event in events:
        digits = id_digits(event.renter_id_number)
        key = f'id:{digits}:{event.branch_id}' if digits else f'event:{event.pk}'
        group = groups.setdefault(key, {'key': key, 'digits': digits, 'branch': event.branch, 'slots': []})
        group['slots'].append(event)

    wanted = {group['digits'] for group in groups.values() if group['digits']}
    tenants = _existing_tenants(wanted, user) if wanted else {}

    rows = []
    for group in groups.values():
        slots = group['slots']
        digits = group['digits']
        starts = [slot.contract_start_date for slot in slots if slot.contract_start_date]
        ends = [slot.contract_end_date for slot in slots if slot.contract_end_date]
        rows.append({
            'key': group['key'],
            'renter_name': _renter_name(slots),
            'renter_id_number': digits or (slots[0].renter_id_number or '').strip(),
            'branch': group['branch'].pk,
            'branch_name': group['branch'].name,
            'slots': slots,
            'suggested_monthly_amount': suggested_monthly_amount(slots),
            'contract_start_date': min(starts) if starts else None,
            'contract_end_date': max(ends) if ends else None,
            'existing_tenant': tenants.get(digits) if digits else None,
        })
    rows.sort(key=lambda row: (row['branch_name'] or '', row['renter_name'], row['key']))
    return rows
