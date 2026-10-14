"""
The eight September sales paid on Tranzila's test terminal leave the books;
nothing else does (migration 0017, owner 14.10.2026).
"""
import importlib
from decimal import Decimal

from django.apps import apps
from django.test import TestCase

from apps.store.models import StoreInvoice

step = importlib.import_module('apps.store.migrations.0017_void_realtest_sales')


def sale(number, index, status='completed'):
    return StoreInvoice.objects.create(
        invoice_number=number, customer_name='לקוח', total_amount=Decimal('170.00'),
        payment_method='credit_card', payment_status=status, tranzila_transaction_id=index,
    )


class VoidRealtestSalesTests(TestCase):
    def test_only_the_listed_test_terminal_sales_are_voided_and_it_reverses(self):
        fake = sale('INV-202609-00001', '25300')
        real = sale('INV-202609-00003', '469194')
        moved = sale('INV-202609-00002', '99999')          # its number changed since the check
        refunded = sale('INV-202609-00006', '25539', status='refunded')

        step.void(apps, None)

        fake.refresh_from_db()
        self.assertEqual(fake.payment_status, 'failed')
        self.assertIn('realtest', fake.notes)
        for untouched, status in ((real, 'completed'), (moved, 'completed'), (refunded, 'refunded')):
            untouched.refresh_from_db()
            self.assertEqual(untouched.payment_status, status)

        step.void(apps, None)  # a second run changes nothing
        fake.refresh_from_db()
        self.assertEqual(fake.notes.count('realtest'), 1)

        step.restore(apps, None)
        fake.refresh_from_db()
        self.assertEqual((fake.payment_status, fake.notes), ('completed', ''))
