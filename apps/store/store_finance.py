"""Aggregate store sales revenue for dashboard financial reports."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth

from apps.store.models import StoreSale

# Completed charges, at the counter and on the website alike. A pending website
# order counted here once, on the site's word that it was paid; the site paid on
# Tranzila's test terminal until 29.9.2026, and since then an order is completed
# only when Tranzila's report confirms the charge — pending means unpaid
# (apps/documents/register.py, store_documents; 14.10.2026).
_STORE_REVENUE_INVOICE_FILTER = Q(invoice__payment_status='completed')


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
