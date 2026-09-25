"""
The store's Tranzila notify is a public POST that carries no signature. An
order is completed on the strength of one only when Tranzila's own ledger shows
an approved transaction with that index and that sum — and a notify that
arrives twice must not sell the cart twice.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tranzila_service import TranzilaService
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

CALLBACK_URL = '/api/v1/store/payment/callback/'


def report_clock(moment):
    """transaction_date / transaction_time as the report writes them: Israel local time."""
    local = moment.astimezone(ZoneInfo('Asia/Jerusalem'))
    return {'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S')}


@override_settings(
    WEBSITE_INTEGRATION_URL='',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_WEBHOOK_SECRET='',
)
class StoreCallbackGuardTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.product = StoreProduct.objects.create(
            name='חולצה', category='ביגוד',
            sale_price=Decimal('4.00'), cost_price=Decimal('1.00'), stock_quantity=10,
        )
        self.invoice = StoreInvoice.objects.create(
            customer_name='דור סער',
            total_amount=Decimal('8.00'),
            payment_method='credit_card',
            payment_status='pending',
            notes=json.dumps([{'product_id': str(self.product.id), 'quantity': 2, 'size': ''}]),
        )

    def _notify(self, **overrides):
        payload = {
            'pdesc': self.invoice.id.hex,
            'Response': '000',
            'index': '123456',
            'ConfirmationCode': '0001234',
            'sum': '8.00',
            'currency': '1',
        }
        payload.update(overrides)
        return self.client.post(CALLBACK_URL, payload)

    def _ledger(self, rows):
        """The terminal's report, as looked up by the notify's index."""
        def find(service, index):
            match = next((row for row in rows if row.get('index') == str(index)), None)
            return {'success': True, 'transaction': match}
        return patch.object(TranzilaService, 'find_transaction', autospec=True, side_effect=find)

    # The shape /v1/transactions really returns (cogolive, 23.9.2026): the
    # amount in agorot and no pdesc — 8 ₪ is '800' — with the approval number
    # the notify quotes and the time it was made.
    @property
    def PAID(self):
        return {
            'index': '123456', 'amount': '800', 'processor_response_code': '000', 'tranmode': 'A',
            'authorization_number': '0001234', **report_clock(timezone.now()),
        }

    def _state(self):
        self.invoice.refresh_from_db()
        self.product.refresh_from_db()
        return (
            self.invoice.payment_status,
            StoreSale.objects.filter(invoice=self.invoice).count(),
            self.product.stock_quantity,
        )

    def test_a_notify_tranzila_does_not_know_leaves_the_order_pending(self):
        # The forged POST from TASKS-OPEN: pdesc + Response=000, nothing else.
        with self._ledger([]):
            response = self._notify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_a_notify_with_another_sum_leaves_the_order_pending(self):
        with self._ledger([{**self.PAID, 'amount': '100'}]):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_a_notify_the_ledger_cannot_answer_for_leaves_the_order_pending(self):
        with patch.object(TranzilaService, 'credential_error', return_value='REST API credentials not configured'):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))
        # What Tranzila reported is kept, so a person can check it on the terminal.
        self.assertEqual(self.invoice.tranzila_transaction_id, '123456')

    def test_a_verified_notify_completes_the_order_once(self):
        ledger = [self.PAID]
        with self._ledger(ledger):
            first = self._notify()
            second = self._notify()  # Tranzila retried
        self.assertTrue(first.data['success'])
        self.assertTrue(second.data['success'])
        self.assertEqual(self._state(), ('completed', 1, 8))
        self.assertEqual(self.invoice.tranzila_transaction_id, '123456')

    def test_a_website_order_issues_its_document_once(self):
        self.invoice.website_order_number = 'CG-260915-AAA1'
        self.invoice.save(update_fields=['website_order_number'])
        ledger = [self.PAID]
        with self._ledger(ledger), \
                patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as issue, \
                patch('apps.store.invoice_email.send_store_invoice_email') as email:
            self._notify()
            self._notify()
        issue.assert_called_once()
        email.assert_called_once()
        self.assertEqual(self._state(), ('completed', 1, 8))

    def test_the_terminal_that_took_the_charge_is_kept(self):
        with self._ledger([self.PAID]):
            self._notify()
        self.assertEqual(self._state(), ('completed', 1, 8))
        self.assertEqual(self.invoice.tranzila_terminal, 'iframe_terminal')

    def test_a_charge_that_already_paid_another_order_does_not_complete_this_one(self):
        # A public notify can quote a real transaction of someone else's order.
        StoreInvoice.objects.create(
            total_amount=Decimal('8.00'), payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='123456', tranzila_terminal='iframe_terminal',
        )
        with self._ledger([self.PAID]):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_the_same_number_on_another_terminal_is_another_charge(self):
        # Numbers repeat across terminals: an old order paid elsewhere blocks nothing.
        StoreInvoice.objects.create(
            total_amount=Decimal('8.00'), payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='123456', tranzila_terminal='fxpmichalweb',
        )
        with self._ledger([self.PAID]):
            self._notify()
        self.assertEqual(self._state(), ('completed', 1, 8))

    def test_a_notify_for_an_order_that_is_not_ours_changes_nothing(self):
        # The other website on the terminal: its pdesc is no invoice of ours.
        with self._ledger([self.PAID]):
            response = self._notify(pdesc='a1b2c3d4e5f60718293a4b5c6d7e8f90')
        self.assertFalse(response.data['success'])
        self.assertEqual(response.data['error'], 'Invoice not found')
        self.assertEqual(self._state(), ('pending', 0, 10))

    # --- 25.9.2026: only a real charge, with its own approval, after the order ---

    def test_a_card_check_with_the_same_sum_is_not_a_payment(self):
        # The handshake locks the sum, not tranmode: a payer can turn the page
        # into a J2 check (tranmode N), which comes back approved and moves no money.
        with self._ledger([{**self.PAID, 'tranmode': 'N'}]):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_another_approval_number_is_not_this_payment(self):
        with self._ledger([{**self.PAID, 'authorization_number': '0009999'}]):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_a_notify_without_an_approval_number_is_not_trusted(self):
        with self._ledger([self.PAID]):
            self._notify(ConfirmationCode='')
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_a_charge_made_before_the_order_did_not_pay_for_it(self):
        earlier = {**self.PAID, **report_clock(timezone.now() - timedelta(hours=2))}
        with self._ledger([earlier]):
            self._notify()
        self.assertEqual(self._state(), ('pending', 0, 10))

    def test_leading_zeros_do_not_make_another_approval(self):
        with self._ledger([{**self.PAID, 'authorization_number': '1234'}]):
            self._notify()
        self.assertEqual(self._state(), ('completed', 1, 8))
