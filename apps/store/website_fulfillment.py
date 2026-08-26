"""
B2C website checkout fulfillment: delivery vs pickup at אם המושבות.

The payment endpoint receives the location. Pickup charges no shipping and
decrements the branch size row, not the delivery (branch=null) row.
"""
from __future__ import annotations

from apps.core.models import Branch

PICKUP_BRANCH_NAME_MARKERS = ('המושבות',)


def parse_delivery_method(raw) -> str:
    value = str(raw or 'delivery').strip().lower()
    if value not in ('delivery', 'pickup'):
        raise ValueError('אופן אספקה לא תקין')
    return value


def resolve_pickup_branch(pickup_branch_id=None) -> Branch:
    """
    Branch used for website pickup (אם המושבות, רפאל איתן 5).

    Prefer an explicit id from the website; otherwise match the live branch
    name so we do not hardcode a UUID.
    """
    qs = Branch.objects.filter(is_active=True)
    branch_id = str(pickup_branch_id or '').strip()
    if branch_id:
        branch = qs.filter(pk=branch_id).first()
        if branch is None:
            raise ValueError('סניף האיסוף לא נמצא')
        return branch

    matched = None
    for branch in qs:
        name = branch.name or ''
        if any(marker in name for marker in PICKUP_BRANCH_NAME_MARKERS):
            if matched is not None:
                raise ValueError('נמצאו כמה סניפי איסוף — יש לשלוח pickup_branch_id')
            matched = branch
    if matched is None:
        raise ValueError('לא הוגדר סניף איסוף')
    return matched


def website_line_branch(*, delivery_method: str, pickup_branch: Branch | None):
    if delivery_method == 'pickup':
        if pickup_branch is None:
            raise ValueError('לא הוגדר סניף איסוף')
        return str(pickup_branch.id)
    return 'delivery'
