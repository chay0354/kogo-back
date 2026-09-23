"""
Small things the morning routine puts right by itself.

The owner's rule (23.9.2026): a number on the dashboard that is out of date, a
child still marked as a trial after paying — "no need to ask me, fix it in the
morning routine". What counts as small is narrow on purpose:

  * only what the system can work out from its own records, with the rule it
    already uses everywhere else;
  * never money, never a document, never a message to a customer, never a
    deletion;
  * every change recorded where the office already looks (the child's status
    history), and listed in the morning brief as "fixed this morning";
  * a ceiling on how many children one morning may change, so a mistake in the
    rule cannot sweep through the whole customer list before anyone notices.
"""
from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# One morning may move at most this many children. A backlog clears over a few
# mornings; a bug in the rule cannot touch more than this before it is seen.
MAX_STATUS_FIXES_PER_MORNING = 40

# The transitions the routine may make on its own. Each has money or a lesson
# behind it. Nothing is ever moved *to* payment_problem here: billing sets that,
# and it starts the card-update messages.
AUTO_TRANSITIONS = {
    ('trial_signed', 'active'),
    ('trial_completed', 'active'),
    ('pending', 'active'),
    ('active', 'inactive'),
    ('payment_problem', 'inactive'),
}

FIX_REASON = 'תוקן אוטומטית בשגרת הבוקר'


def status_fix_candidates(*, limit_scan: int = 3000) -> list[tuple]:
    """(child, from, to) for every child the rule says is on the wrong status."""
    from apps.customers.child_status import CHILD_STATUSES, canonical_status, resolve_child_status
    from apps.customers.models import Child, RecurringPayment
    from apps.documents.models import CashPlan, CheckPlan

    # A child someone is still charging is never moved to inactive.
    still_paying = set(
        RecurringPayment.objects.filter(status='active').exclude(tranzila_token='')
        .values_list('child_id', flat=True)
    )
    still_paying |= set(CashPlan.objects.filter(status='active').values_list('child_id', flat=True))
    still_paying |= set(CheckPlan.objects.filter(status='active').values_list('child_id', flat=True))

    children = (
        Child.objects.exclude(status='ghost')
        .select_related('family')
        .prefetch_related('lesson_enrollments', 'payments')
        .order_by('-updated_at')[:limit_scan]
    )
    found = []
    for child in children.iterator(chunk_size=500):
        current = canonical_status(child.status)
        target = resolve_child_status(child)
        if not target or target == current:
            continue
        retired = child.status not in CHILD_STATUSES
        if not retired and (current, target) not in AUTO_TRANSITIONS:
            continue
        if target == 'inactive' and child.id in still_paying:
            continue
        found.append((child, child.status, target))
    return found


def fix_child_statuses() -> dict:
    """Move the children the rule says are wrong, within the morning's ceiling."""
    from apps.customers.child_status import status_label
    from apps.customers.status_history_models import ChildStatusHistory

    candidates = status_fix_candidates()
    applied = []
    for child, was, target in candidates[:MAX_STATUS_FIXES_PER_MORNING]:
        with transaction.atomic():
            # Re-read under a lock: the office may have changed it a moment ago.
            locked = type(child).objects.select_for_update().get(pk=child.pk)
            if locked.status != was:
                continue
            locked.status = target
            locked.save(update_fields=['status', 'updated_at'])
            ChildStatusHistory.objects.create(
                child=locked, previous_status=was, new_status=target,
                reason=f'{FIX_REASON}: {status_label(was)} ← {status_label(target)}',
            )
        applied.append({
            'child_id': str(child.id),
            'name': child.full_name,
            'from': status_label(was),
            'to': status_label(target),
        })
    return {
        'applied': applied,
        'waiting': max(0, len(candidates) - MAX_STATUS_FIXES_PER_MORNING),
    }


def refresh_dashboard_numbers() -> dict:
    """
    Recount this month's dashboard.

    The dashboard reads counts stored per month and never recounts them itself.
    The job meant to do it every night is a Celery task, and nothing runs Celery
    on this hosting — so the numbers stayed where the last manual refresh left
    them. This is the same call the dashboard's own refresh makes.
    """
    from apps.instructors.utils import generate_monthly_snapshots

    month = timezone.localtime(timezone.now()).date().strftime('%Y-%m')
    summary = generate_monthly_snapshots(month, finalize=False)
    return {'month': month, 'summary': summary if isinstance(summary, dict) else {}}
