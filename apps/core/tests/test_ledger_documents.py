"""What reaches the documents page, and what each row says about where it came from.

A store sale produces a numbered tax document whatever the customer paid with, so
all of them belong in the ledger — the old query demanded a Tranzila transaction
and quietly dropped every cash and monthly-billing sale.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.tranzila_ledger import list_ledger_documents
from apps.store.models import StoreInvoice


class LedgerIncludesEveryStoreSaleTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.card = StoreInvoice.objects.create(
            invoice_number='ST-CARD-1',
            customer_name='קונה אשראי',
            total_amount=Decimal('149.00'),
            payment_method='credit_card',
            payment_status='completed',
            tranzila_transaction_id='TRX-1',
        )
        self.cash = StoreInvoice.objects.create(
            invoice_number='ST-CASH-1',
            customer_name='קונה מזומן',
            total_amount=Decimal('49.00'),
            payment_method='cash',
            payment_status='completed',
        )
        self.monthly = StoreInvoice.objects.create(
            invoice_number='ST-MONTH-1',
            customer_name='חיוב חודשי',
            total_amount=Decimal('60.00'),
            payment_method='monthly_billing',
            payment_status='pending',
        )

    def _numbers(self) -> set[str]:
        result = list_ledger_documents(
            start_date=self.today - timedelta(days=7),
            end_date=self.today,
            local_only=True,
        )
        return {row['document_number'] for row in result['documents']}

    def test_a_cash_sale_is_listed(self):
        self.assertIn('ST-CASH-1', self._numbers())

    def test_a_monthly_billing_sale_is_listed(self):
        self.assertIn('ST-MONTH-1', self._numbers())

    def test_a_card_sale_is_still_listed(self):
        self.assertIn('ST-CARD-1', self._numbers())


class LedgerRowSaysWhereItCameFromTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def _row(self, number: str) -> dict:
        result = list_ledger_documents(
            start_date=self.today - timedelta(days=7),
            end_date=self.today,
            local_only=True,
        )
        return next(row for row in result['documents'] if row['document_number'] == number)

    def test_a_website_order_is_marked_as_a_delivery(self):
        StoreInvoice.objects.create(
            invoice_number='ST-WEB-1',
            customer_name='רותי ניסן',
            website_order_number='CG-260830-ABCD',
            shipping_address='הרצל 12, כפר סבא',
            total_amount=Decimal('149.00'),
            payment_method='credit_card',
            payment_status='completed',
        )

        row = self._row('ST-WEB-1')

        self.assertEqual(row['origin'], 'store_website')
        self.assertEqual(row['origin_label'], 'חנות · אתר')
        self.assertEqual(row['website_order_number'], 'CG-260830-ABCD')

    def test_a_counter_sale_is_marked_as_a_branch_sale(self):
        StoreInvoice.objects.create(
            invoice_number='ST-COUNTER-1',
            customer_name='קונה בסניף',
            total_amount=Decimal('49.00'),
            payment_method='cash',
            payment_status='completed',
        )

        row = self._row('ST-COUNTER-1')

        self.assertEqual(row['origin'], 'store_counter')
        self.assertEqual(row['origin_label'], 'חנות · סניף')
        self.assertEqual(row['payment_method_label'], 'מזומן')


class CancelledReceiptIsNotADebtTest(TestCase):
    """The collection tab chases open balances — a cancelled receipt must not be one."""

    def test_a_cancelled_receipt_has_no_open_balance(self):
        from apps.core.tests.test_fixtures import TestDataFactory
        from apps.customers.financial_models import Invoice

        family = TestDataFactory.create_family()
        Invoice.objects.create(
            invoice_number='IR-TEST-CANCELLED', family=family, amount=Decimal('236.00'),
            status='cancelled', invoice_date=timezone.now(),
        )
        today = timezone.localdate()

        row = next(
            r for r in list_ledger_documents(start_date=today, end_date=today, local_only=True)['documents']
            if r['document_number'] == 'IR-TEST-CANCELLED'
        )

        self.assertEqual(row['open_balance'], 0.0)
