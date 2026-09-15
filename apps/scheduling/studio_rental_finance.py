"""Studio rental income: what each rented slot brings in, and whose income it is.

The amount has always been the calendar's: price_per_session × the occurrences
that fall in the period. The calendar stays the source of truth for that
(apps/rentals/models.py) — a tenancy points at its events, it never copies
their prices.

What phase 6 adds is *whose* income it is. A rented slot used to be filed under
its branch and nothing else, which is where a private customer's course and a
pickup sale go. But a tenant is a business customer with a business and a
category on their record, exactly like the business customer a manual invoice
is issued to, and the owner reads income by business and category. So a slot
held by a tenancy whose tenant carries those tags is attributed to them, and
lands in the same rows as that tenant's documents. A rental with no tenancy, or
a tenant with no business tag, still belongs to its branch.

Double counting is the thing to be careful about here, because the same month
can be described twice: once by the calendar, and once by the חשבונית מס/קבלה
that apps/rental_billing issues when the month is charged (an RT FormalDocument,
tagged with the same business). So a tenancy-month whose receipt falls inside
the period being asked about is left out of the attribution — the document
counts it. It is *not* left out of `total` / `by_branch_id` / `by_month`: those
are the branch panel's figures, which know nothing about documents, and
changing them would move a number nobody asked to move.

    total, by_branch_id, by_month   unchanged — every rented slot, as before
    untagged_by_branch_id           the part of by_branch_id that is still the
                                    branch's: no tenancy, or a tenant with no
                                    business, and no receipt for the month
    rows                            one RentalIncomeRow per tenancy and month
                                    that carries tags and has no receipt

Read by apps/core/revenue_service.py (the dashboard's income by business) and
apps/documents/undocumented_income.py (the period report's income with no
document behind it).
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from apps.scheduling.models import ScheduleEvent
from apps.scheduling.studio_conflict import iter_occurrence_dates_in_range

UNTAGGED_CUSTOMER = 'שוכר ללא שם'


@dataclass
class RentalIncomeRow:
    """One tenant's (or one unlinked rental's) income for one month of the period."""

    # The tenancy the slots belong to, or None for a rental no agreement holds.
    tenancy_id: object
    period: date            # the month, as its first day
    first_date: date        # the first occurrence counted, for the row's date
    sessions: int
    amount: Decimal
    branch_id: object
    branch_name: str
    customer: str
    # The tenant's own tags. None when the rental has no tenancy, or its tenant
    # was never tagged — then the income is the branch's, as it was before.
    business_id: object = None
    business_name: str = ''
    category_id: object = None
    category_name: str = ''

    @property
    def is_tagged(self) -> bool:
        return self.business_id is not None


def _first_of_month(day: date) -> date:
    return day.replace(day=1)


def _tenant_tags(event) -> dict:
    """The business and category on the tenant of this slot's agreement, or nothing."""
    tenancy = event.tenancy if event.tenancy_id else None
    tenant = tenancy.tenant if tenancy is not None and tenancy.tenant_id else None
    if tenant is None or not tenant.business_id:
        return {}
    tags = {'business_id': tenant.business_id, 'business_name': tenant.business.name}
    if tenant.business_category_id:
        tags['category_id'] = tenant.business_category_id
        tags['category_name'] = tenant.business_category.name
    return tags


def _customer_of(event) -> str:
    """Who the office would call this income: the tenant on the agreement, else the name typed on the event."""
    tenancy = event.tenancy if event.tenancy_id else None
    if tenancy is not None and tenancy.tenant_id:
        name = (tenancy.tenant.full_name or '').strip()
        if name:
            return name
    return (event.renter_name or '').strip() or UNTAGGED_CUSTOMER


def _months_with_a_receipt(tenancy_ids, date_from: date, date_to: date) -> set:
    """
    (tenancy_id, month) for every tenant charge whose receipt is itself dated
    inside this period — the months a document already accounts for.

    Keyed on the receipt's own date, not the month it covers, because that is
    exactly the test the reports apply: a document is counted when its
    document_date falls in the period. A charge whose receipt failed has none,
    and its month stays here as income with no document behind it.
    """
    if not tenancy_ids:
        return set()
    from apps.rental_billing.models import TenantCharge

    rows = (
        TenantCharge.objects
        .filter(
            tenancy_id__in=list(tenancy_ids),
            receipt__isnull=False,
            receipt__document_date__gte=date_from,
            receipt__document_date__lte=date_to,
        )
        .values_list('tenancy_id', 'period')
    )
    return {(tenancy_id, period) for tenancy_id, period in rows}


def aggregate_studio_rental_revenue(date_from, date_to, branch_id=None, city_id=None, branch_ids=None):
    """
    Returns:
      total: Decimal
      by_branch_id: dict[str, Decimal]
      by_month: dict[str, Decimal]        # YYYY-MM -> amount
      untagged_by_branch_id: dict[str, Decimal]
      rows: list[RentalIncomeRow]
    """
    total = Decimal('0.00')
    by_branch: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    by_month: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    untagged: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    empty = {
        'total': total, 'by_branch_id': {}, 'by_month': {}, 'untagged_by_branch_id': {}, 'rows': [],
    }

    qs = ScheduleEvent.objects.filter(
        is_active=True,
        is_studio_rental=True,
        is_daily_event=False,
    ).exclude(start_time__isnull=True).exclude(end_time__isnull=True).select_related(
        'branch', 'tenancy', 'tenancy__tenant', 'tenancy__tenant__business',
        'tenancy__tenant__business_category',
    )

    if branch_id and branch_id != 'all':
        qs = qs.filter(branch_id=branch_id)
    elif branch_ids is not None:
        if not branch_ids:
            return empty
        qs = qs.filter(branch_id__in=branch_ids)

    if city_id and city_id != 'all':
        qs = qs.filter(branch__city_id=city_id)

    events = [
        event for event in qs
        if event.branch_id and (event.price_per_session or Decimal('0.00')) > 0
    ]
    receipted = _months_with_a_receipt(
        {event.tenancy_id for event in events if event.tenancy_id}, date_from, date_to,
    )

    # (tenancy or event, month) -> the row being built for it. A tenant who
    # rents two weekly slots is one line a month, not two: that is how the
    # office thinks of them, and how their receipt is written.
    building: dict[tuple, RentalIncomeRow] = {}
    for event in events:
        price = event.price_per_session
        bid = str(event.branch_id)
        tags = _tenant_tags(event)
        key_part = ('tenancy', event.tenancy_id) if event.tenancy_id else ('event', event.pk)
        for occ in iter_occurrence_dates_in_range(event, date_from, date_to):
            total += price
            by_branch[bid] += price
            by_month[occ.strftime('%Y-%m')] += price

            month = _first_of_month(occ)
            if event.tenancy_id and (event.tenancy_id, month) in receipted:
                # The month's RT receipt is a document of this period already.
                continue
            if not tags:
                untagged[bid] += price
            row = building.get((*key_part, month))
            if row is None:
                row = RentalIncomeRow(
                    tenancy_id=event.tenancy_id,
                    period=month,
                    first_date=occ,
                    sessions=0,
                    amount=Decimal('0.00'),
                    branch_id=event.branch_id,
                    branch_name=event.branch.name if event.branch_id else '',
                    customer=_customer_of(event),
                    **tags,
                )
                building[(*key_part, month)] = row
            row.sessions += 1
            row.amount += price
            if occ < row.first_date:
                row.first_date = occ

    rows = sorted(building.values(), key=lambda row: (row.first_date, row.customer))
    return {
        'total': total,
        'by_branch_id': dict(by_branch),
        'by_month': dict(by_month),
        'untagged_by_branch_id': dict(untagged),
        'rows': rows,
    }
