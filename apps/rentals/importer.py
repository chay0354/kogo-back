"""Import — turn the office's confirmed suggestions into tenancies, all of them or none.

The suggestions screen proposes one tenancy per renter and branch
(slots.rental_suggestions). The office confirms each group — who the tenant
is, the amount, the billing day — and sends them together. They are created in
one transaction: a group that fails rolls back every group before it, and the
answer names the group and the reason, so nothing is left half imported and
the office can fix one row and send the batch again.

Each group is checked exactly as the single-tenancy endpoints check one: its
fields by TenancySerializer, its slots by the rules in slots.py. The tenancy
takes the branch of its slots, so every slot of a group must share one branch.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import APIException

from apps.rentals.slots import SlotError, check_slots, link_slots, lock_slots, slot_label

# What a group may carry besides its slot_ids.
GROUP_FIELDS = (
    'tenant_id', 'tenant', 'monthly_amount', 'billing_day',
    'start_date', 'end_date', 'status', 'notes',
)


class GroupError(Exception):
    """One group that could not be imported. Raised inside the transaction, so it rolls back the rest."""

    def __init__(self, index: int, message: str, *, status_code: int = 400, details=None, slot_id=None):
        super().__init__(message)
        self.index = index
        self.message = message
        self.status_code = status_code
        self.details = details
        self.slot_id = slot_id

    def payload(self) -> dict:
        body = {'error': f'קבוצה {self.index + 1}: {self.message}', 'group': self.index}
        if self.slot_id:
            body['slot_id'] = self.slot_id
        if self.details is not None:
            body['details'] = self.details
        return body


def _first_error(errors) -> str:
    """The first message in a DRF error tree, prefixed by the field it belongs to."""
    if isinstance(errors, dict):
        for field, value in errors.items():
            message = _first_error(value)
            return message if field == 'non_field_errors' else f'{field}: {message}'
    if isinstance(errors, list) and errors:
        return _first_error(errors[0])
    return str(errors)


def import_tenancies(groups: list, request) -> list:
    """
    Create one tenancy per group and link its slots, in one transaction.

    Returns the tenancies in the order of the groups. Raises GroupError for the
    first group that fails; by the time it reaches the caller nothing of this
    import is saved.
    """
    created = []
    with transaction.atomic():
        for index, group in enumerate(groups):
            created.append(_import_group(index, group, request))
    return created


def _group_branch_id(slots):
    branch_ids = {slot.branch_id for slot in slots}
    for slot in slots:
        if slot.branch_id is None:
            raise SlotError(f'לשכירות של "{slot_label(slot)}" אין סניף', slot.pk)
    if len(branch_ids) > 1:
        raise SlotError('כל השכירויות בקבוצה חייבות להיות באותו סניף')
    return branch_ids.pop()


def _import_group(index: int, group, request):
    from apps.rentals.serializers import TenancySerializer

    if not isinstance(group, dict):
        raise GroupError(index, 'מבנה הקבוצה אינו תקין')

    user = request.user
    try:
        slots = lock_slots(group.get('slot_ids'), user)
        branch_id = _group_branch_id(slots)
        check_slots(branch_id, slots)
    except SlotError as exc:
        raise GroupError(index, exc.message, slot_id=exc.slot_id)

    data = {field: group[field] for field in GROUP_FIELDS if field in group}
    data['branch'] = str(branch_id)
    serializer = TenancySerializer(data=data, context={'request': request})
    try:
        valid = serializer.is_valid()
    except APIException as exc:
        # A branch the partner may not write to: keep its status (403).
        raise GroupError(index, str(exc.detail), status_code=exc.status_code)
    if not valid:
        raise GroupError(index, _first_error(serializer.errors), details=serializer.errors)
    tenancy = serializer.save()

    try:
        link_slots(tenancy, [slot.pk for slot in slots], user)
    except SlotError as exc:
        raise GroupError(index, exc.message, slot_id=exc.slot_id)
    return tenancy
