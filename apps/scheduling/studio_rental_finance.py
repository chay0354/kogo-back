"""Aggregate studio rental revenue (price_per_session × occurrences) for date ranges."""
from collections import defaultdict
from decimal import Decimal

from apps.scheduling.models import ScheduleEvent
from apps.scheduling.studio_conflict import iter_occurrence_dates_in_range


def aggregate_studio_rental_revenue(date_from, date_to, branch_id=None, city_id=None):
    """
    Returns:
      total: Decimal
      by_branch_id: dict[str, Decimal]
      by_month: dict[str, Decimal]  # YYYY-MM -> amount
    """
    total = Decimal('0.00')
    by_branch: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    by_month: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))

    qs = ScheduleEvent.objects.filter(
        is_active=True,
        is_studio_rental=True,
        is_daily_event=False,
    ).exclude(start_time__isnull=True).exclude(end_time__isnull=True)

    if branch_id and branch_id != 'all':
        qs = qs.filter(branch_id=branch_id)

    if city_id and city_id != 'all':
        qs = qs.filter(branch__city_id=city_id)

    for ev in qs:
        price = ev.price_per_session or Decimal('0.00')
        if price <= 0:
            continue
        if not ev.branch_id:
            continue

        for occ in iter_occurrence_dates_in_range(ev, date_from, date_to):
            total += price
            bid = str(ev.branch_id)
            by_branch[bid] += price
            mkey = occ.strftime('%Y-%m')
            by_month[mkey] += price

    return {
        'total': total,
        'by_branch_id': dict(by_branch),
        'by_month': dict(by_month),
    }
