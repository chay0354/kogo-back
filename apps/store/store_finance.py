"""Aggregate store sales revenue for dashboard financial reports."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth

from apps.store.models import StoreSale

# Completed in-store charges plus confirmed B2C website orders (stock already taken).
_STORE_REVENUE_INVOICE_FILTER = Q(invoice__payment_status='completed') | Q(
    invoice__website_order_number__isnull=False,
    invoice__payment_status='pending',
)


def aggregate_store_revenue(date_from, date_to, branch_id=None, branch_ids=None):
    """
    Sum store line-item revenue in a date range.

    Returns:
      total: Decimal
      by_branch_id: dict[str, Decimal]  # null branch → key "__online__"
      by_month: dict[str, Decimal]  # YYYY-MM → amount
    """
    qs = StoreSale.objects.filter(
        _STORE_REVENUE_INVOICE_FILTER,
        sale_date__date__gte=date_from,
        sale_date__date__lte=date_to,
    )

    if branch_id and branch_id != 'all':
        if branch_id == 'delivery':
            qs = qs.filter(branch__isnull=True)
        else:
            qs = qs.filter(branch_id=branch_id)
    elif branch_ids is not None:
        if not branch_ids:
            return {'total': Decimal('0.00'), 'by_branch_id': {}, 'by_month': {}}
        qs = qs.filter(branch_id__in=branch_ids)

    total = qs.aggregate(amount=Sum('total_price'))['amount'] or Decimal('0.00')

    by_branch: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    for row in qs.values('branch_id').annotate(amount=Sum('total_price')):
        key = str(row['branch_id']) if row['branch_id'] else '__online__'
        by_branch[key] += row['amount'] or Decimal('0.00')

    by_month: dict[str, Decimal] = defaultdict(lambda: Decimal('0.00'))
    for row in qs.annotate(month=TruncMonth('sale_date')).values('month').annotate(
        amount=Sum('total_price'),
    ):
        if row['month']:
            by_month[row['month'].strftime('%Y-%m')] += row['amount'] or Decimal('0.00')

    return {
        'total': total,
        'by_branch_id': dict(by_branch),
        'by_month': dict(by_month),
    }
