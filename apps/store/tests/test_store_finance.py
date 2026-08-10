"""Tests for store revenue aggregation used by the financial dashboard."""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Branch
from apps.store.models import StoreInvoice, StoreProduct, StoreSale
from apps.store.store_finance import aggregate_store_revenue


class StoreFinanceAggregationTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Test Branch', is_active=True)
        self.product = StoreProduct.objects.create(
            name='Test Shirt',
            category='Test',
            sale_price=Decimal('100.00'),
            cost_price=Decimal('40.00'),
            stock_quantity=10,
            branch=self.branch,
        )

    def _sale(self, *, amount, sale_day, branch=None, website_order=None, status='completed'):
        invoice = StoreInvoice.objects.create(
            customer_name='Buyer',
            total_amount=amount,
            payment_method='credit_card',
            payment_status=status,
            branch=branch,
            website_order_number=website_order,
        )
        sale = StoreSale.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=1,
            unit_price=amount,
            total_price=amount,
            payment_method='credit_card',
            branch=branch,
        )
        StoreSale.objects.filter(pk=sale.pk).update(sale_date=f'{sale_day} 12:00:00')
        return sale

    def test_counts_completed_branch_sales(self):
        self._sale(amount=Decimal('100.00'), sale_day='2026-06-15', branch=self.branch)
        agg = aggregate_store_revenue(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(agg['total'], Decimal('100.00'))
        self.assertEqual(agg['by_branch_id'][str(self.branch.id)], Decimal('100.00'))
        self.assertEqual(agg['by_month']['2026-06'], Decimal('100.00'))

    def test_counts_pending_website_orders(self):
        self._sale(
            amount=Decimal('50.00'),
            sale_day='2026-06-20',
            branch=None,
            website_order='CG-260620-ABCD',
            status='pending',
        )
        agg = aggregate_store_revenue(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(agg['total'], Decimal('50.00'))
        self.assertEqual(agg['by_branch_id']['__online__'], Decimal('50.00'))

    def test_excludes_failed_in_store_charges(self):
        self._sale(amount=Decimal('75.00'), sale_day='2026-06-10', branch=self.branch, status='failed')
        agg = aggregate_store_revenue(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(agg['total'], Decimal('0.00'))
