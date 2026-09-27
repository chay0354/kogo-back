"""
Refunds, the till's saved card and the store notify: no money moves twice.

* A refund holds a claim row while it runs: a double click is refused before
  Tranzila is touched. An answered "no" drops the claim; no answer keeps it,
  and nothing is tried behind it — a cancel that timed out may have gone
  through, and a credit after it would pay the customer back twice.
* The till's saved-card purchase goes to the card's terminal, and one that got
  no answer stays pending: the next saved-card purchase for that child is
  refused until someone has looked at Tranzila.
* A paid store invoice reported paid again under another number is recorded
  and shown as a double payment; a decline arriving after the payment does
  not undo it.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.payment_service import (
    REFUND_ALREADY_CLAIMED,
    TILL_CHARGE_UNCERTAIN_MARK,
    PaymentService,
)
from apps.customers.models import Payment, TranzilaTransaction
from apps.customers.tests.test_charge_survives_receipt_failure import _due_standing_order
from apps.customers.tests.test_saved_card_terminal import KEYS
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

CHARGE_URL = '/v1/transaction/credit_card/create'
REFUNDED = {'error_code': 0, 'message': 'Success',
            'transaction_result': {'processor_response_code': '000', 'transaction_id': '5001', 'auth_number': '0001'}}
REFUSED = {'error_code': 20004, 'message': 'Invalid card'}
APPROVED = {'error_code': 0, 'message': 'Success',
            'transaction_result': {'processor_response_code': '000', 'transaction_id': '4411', 'auth_number': '0098798'}}
TIMEOUT = 'timeout'
NO_BODY = 'no body'


class _Answer:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError('no JSON')
        return self._body


class _Gateway:
    """requests.post for tranzila_service: answers each call from a script, keeps what was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        assert url.endswith(CHARGE_URL), url
        self.sent.append({'payload': json, 'app_key': (headers or {}).get('X-tranzila-api-app-key')})
        answer = self.answers.pop(0) if self.answers else REFUNDED
        if answer == TIMEOUT:
            raise requests.exceptions.Timeout('Read timed out')
        if answer == NO_BODY:
            return _Answer(502, None)
        return _Answer(200, answer)


def _gateway(*answers):
    gateway = _Gateway(*answers)
    return gateway, patch('apps.core.tranzila_service.requests.post', side_effect=gateway)


def _paid_course_payment(*, terminal='', paid_at=None):
    recurring = _due_standing_order()
    recurring.tranzila_terminal = terminal
    recurring.save(update_fields=['tranzila_terminal'])
    payment = recurring.initial_payment
    txn = TranzilaTransaction.objects.create(
        transaction_id='4411', confirmation_code='0098798', transaction_type='charge',
        response_code='000', response_message='', request_data={}, response_data={},
        idempotency_key=f'test_charge_{payment.id}', is_successful=True, tranzila_terminal=terminal,
    )
    payment.tranzila_transaction = txn
    payment.payment_date = paid_at or timezone.now() - timedelta(days=3)
    payment.save(update_fields=['tranzila_transaction', 'payment_date'])
    return payment


@override_settings(**KEYS)
@patch('apps.core.payment_service.PaymentService._issue_payment_credit_note')
class CourseRefundTests(TestCase):
    def _refund(self, payment):
        return PaymentService().refund_payment(str(payment.id), reason='ביטול')

    def test_a_refund_is_made_once_and_recorded_on_its_claim(self, _note):
        payment = _paid_course_payment()
        gateway, post = _gateway(REFUNDED)
        with post:
            result = self._refund(payment)
            again = self._refund(payment)

        self.assertTrue(result['success'])
        self.assertFalse(again['success'])
        self.assertEqual(len(gateway.sent), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')
        record = TranzilaTransaction.objects.get(idempotency_key=f'refund_claim_payment_{payment.id}')
        self.assertTrue(record.is_successful)
        self.assertEqual(record.transaction_id, '5001')

    def test_a_refund_already_running_is_refused_before_tranzila(self, _note):
        payment = _paid_course_payment()
        TranzilaTransaction.objects.create(
            transaction_id='', confirmation_code='', transaction_type='refund', response_code='',
            response_message='', request_data={}, response_data={},
            idempotency_key=f'refund_claim_payment_{payment.id}', is_successful=False,
        )
        gateway, post = _gateway(REFUNDED)
        with post:
            result = self._refund(payment)
        self.assertEqual(result['error'], REFUND_ALREADY_CLAIMED)
        self.assertEqual(gateway.sent, [])

    def test_no_answer_keeps_the_claim_and_blocks_the_retry(self, _note):
        payment = _paid_course_payment()
        gateway, post = _gateway(NO_BODY)
        with post:
            result = self._refund(payment)
            retry = self._refund(payment)

        self.assertTrue(result.get('uncertain'))
        self.assertEqual(retry['error'], REFUND_ALREADY_CLAIMED)
        self.assertEqual(len(gateway.sent), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

    def test_a_same_day_cancel_that_timed_out_is_not_followed_by_a_credit(self, _note):
        payment = _paid_course_payment(paid_at=timezone.now())
        gateway, post = _gateway(TIMEOUT, REFUNDED)
        with post:
            result = self._refund(payment)

        self.assertTrue(result.get('uncertain'))
        self.assertEqual([s['payload']['txn_type'] for s in gateway.sent], ['cancel'])

    def test_a_refusal_frees_the_payment_for_another_try(self, _note):
        payment = _paid_course_payment()
        gateway, post = _gateway(REFUSED, REFUNDED)
        with post:
            first = self._refund(payment)
            second = self._refund(payment)

        self.assertFalse(first['success'])
        self.assertFalse(first.get('uncertain'))
        self.assertTrue(second['success'])
        self.assertEqual(len(gateway.sent), 2)

    def test_a_cogolivetok_charge_is_refunded_there_with_cogolive_keys(self, _note):
        payment = _paid_course_payment(terminal='cogolivetok')
        gateway, post = _gateway(REFUNDED)
        with post:
            result = self._refund(payment)

        self.assertTrue(result['success'])
        self.assertEqual(gateway.sent[0]['payload']['terminal_name'], 'cogolivetok')
        self.assertEqual(gateway.sent[0]['app_key'], 'cogolive-app-key')

    def test_an_old_charge_is_refunded_as_before(self, _note):
        payment = _paid_course_payment()
        gateway, post = _gateway(REFUNDED)
        with post:
            self._refund(payment)
        self.assertEqual(gateway.sent[0]['payload']['terminal_name'], 'fxpmichalwebtok')
        self.assertEqual(gateway.sent[0]['app_key'], 'michal-app-key')


@override_settings(**KEYS)
@patch('apps.core.payment_service.PaymentService._issue_store_credit_note')
class StoreRefundTests(TestCase):
    def test_no_answer_is_not_refund_failed_and_the_retry_is_held(self, _note):
        recurring = _due_standing_order()
        invoice = StoreInvoice.objects.create(
            child=recurring.child, total_amount=Decimal('49.00'), payment_method='credit_card',
            payment_status='completed', tranzila_transaction_id='4411', tranzila_confirmation_code='0098798',
            charged_with_token=True,
        )
        gateway, post = _gateway(NO_BODY)
        with post:
            result = PaymentService().refund_store_invoice(str(invoice.id), reason='החזרה')
            retry = PaymentService().refund_store_invoice(str(invoice.id), reason='החזרה')

        self.assertTrue(result.get('uncertain'))
        self.assertEqual(retry['error'], REFUND_ALREADY_CLAIMED)
        self.assertEqual(len(gateway.sent), 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed')


@override_settings(**KEYS)
@patch('apps.core.payment_service._sign_store_sale')
class TillSavedCardTests(TestCase):
    def setUp(self):
        self.recurring = _due_standing_order()
        self.child = self.recurring.child
        self.product = StoreProduct.objects.create(
            name='חולצה', category='clothing', size='', cost_price=Decimal('20'), sale_price=Decimal('50'),
            stock_quantity=10,
        )

    def _buy(self):
        return PaymentService().initiate_store_purchase(
            product_items=[{'product_id': str(self.product.id), 'quantity': 1}],
            child_id=str(self.child.id),
        )

    def test_no_answer_leaves_the_invoice_pending_and_holds_the_next_purchase(self, _sign):
        gateway, post = _gateway(TIMEOUT, APPROVED)
        with post:
            first = self._buy()
            second = self._buy()

        self.assertFalse(first['success'])
        self.assertTrue(first['uncertain'])
        self.assertTrue(second['uncertain'])
        self.assertEqual(len(gateway.sent), 1, 'the second press must not charge the card')
        invoice = StoreInvoice.objects.get(child=self.child)
        self.assertEqual(invoice.payment_status, 'pending')
        self.assertEqual(invoice.tranzila_confirmation_code, TILL_CHARGE_UNCERTAIN_MARK)

    def test_a_cogolivetok_card_is_charged_there_and_refunds_go_back_there(self, _sign):
        self.recurring.tranzila_terminal = 'cogolivetok'
        self.recurring.save(update_fields=['tranzila_terminal'])
        gateway, post = _gateway(APPROVED)
        with post:
            result = self._buy()

        self.assertTrue(result['success'])
        self.assertEqual(gateway.sent[0]['payload']['terminal_name'], 'cogolivetok')
        self.assertEqual(gateway.sent[0]['app_key'], 'cogolive-app-key')
        self.assertEqual(StoreInvoice.objects.get(child=self.child).tranzila_terminal, 'cogolivetok')

    def test_an_old_card_is_charged_on_the_michal_token_terminal(self, _sign):
        gateway, post = _gateway(APPROVED)
        with post:
            self._buy()
        self.assertEqual(gateway.sent[0]['payload']['terminal_name'], 'fxpmichalwebtok')
        self.assertEqual(StoreInvoice.objects.get(child=self.child).tranzila_terminal, 'fxpmichalwebtok')

    def test_stock_gone_after_the_charge_keeps_the_sale(self, _sign):
        gateway, post = _gateway(APPROVED)
        with post, patch('apps.store.stock_utils.available_stock_for_item', side_effect=[10, 0]):
            result = self._buy()

        self.assertTrue(result['success'])
        invoice = StoreInvoice.objects.get(child=self.child)
        self.assertEqual(invoice.payment_status, 'completed')
        self.assertEqual(StoreSale.objects.filter(invoice=invoice).count(), 1)


@override_settings(**KEYS)
class StoreNotifyTests(TestCase):
    def setUp(self):
        self.invoice = StoreInvoice.objects.create(
            total_amount=Decimal('49.00'), payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='1', tranzila_confirmation_code='0000123', tranzila_terminal='cogolive',
        )

    def _notify(self, **response):
        payload = {'is_successful': True, 'transaction_id': '2', 'confirmation_code': '0000777', 'response_code': '000'}
        payload.update(response)
        return PaymentService().complete_store_purchase_from_webhook(str(self.invoice.id), payload)

    def test_a_second_real_charge_is_recorded_and_shown_as_a_double_payment(self):
        from apps.core.daily_brief import check_duplicate_charges

        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('verified', {})):
            result = self._notify()
            self._notify()  # Tranzila retries: still one record

        self.assertTrue(result['already_processed'])
        rows = TranzilaTransaction.objects.filter(idempotency_key__startswith=f'store_second_{self.invoice.id}_')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().tranzila_terminal, 'cogolive')
        item = check_duplicate_charges(timezone.localdate())
        self.assertEqual(item.severity, 'red')
        self.assertIn(self.invoice.invoice_number, item.rows[0]['label'])

    def test_a_forged_second_charge_records_nothing(self):
        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('unverified', None)):
            self._notify()
        self.assertFalse(TranzilaTransaction.objects.filter(idempotency_key__startswith='store_second_').exists())

    def test_the_same_transaction_again_is_not_a_second_charge(self):
        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila') as verify:
            self._notify(transaction_id='1')
        verify.assert_not_called()

    def test_a_decline_after_the_payment_does_not_undo_it(self):
        result = self._notify(is_successful=False, response_code='004')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, 'completed')
        self.assertTrue(result['already_processed'])


class UnresolvedRefundBriefTests(TestCase):
    def _claim(self, payment, *, minutes_ago):
        return TranzilaTransaction.objects.create(
            transaction_id='', confirmation_code='', transaction_type='refund', response_code='',
            response_message='', request_data={'amount': '260.00', 'original_transaction_id': '4411'},
            response_data={}, idempotency_key=f'refund_claim_payment_{payment.id}', is_successful=False,
            request_timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_an_unanswered_refund_is_red_with_the_child(self):
        from apps.core.daily_brief import check_unresolved_refunds

        payment = _paid_course_payment()
        self._claim(payment, minutes_ago=30)
        item = check_unresolved_refunds(timezone.localdate())
        self.assertEqual(item.severity, 'red')
        self.assertEqual(item.rows[0]['label'], payment.child.full_name)
        self.assertIn('4411', item.rows[0]['detail'])

    def test_a_refund_still_running_is_not_news(self):
        from apps.core.daily_brief import check_unresolved_refunds

        self._claim(_paid_course_payment(), minutes_ago=1)
        self.assertEqual(check_unresolved_refunds(timezone.localdate()).severity, 'green')


@override_settings(**KEYS)
class OfficeHearsTest(TestCase):
    """The till and a refund left without an answer reach the office at once."""

    @patch('apps.core.payment_service._sign_store_sale')
    def test_a_till_charge_without_an_answer(self, _sign):
        from apps.core.models import OfficeAlert

        recurring = _due_standing_order()
        product = StoreProduct.objects.create(
            name='חולצה', category='clothing', size='', cost_price=Decimal('20'), sale_price=Decimal('50'),
            stock_quantity=10,
        )
        gateway, post = _gateway(TIMEOUT)
        with post, self.captureOnCommitCallbacks(execute=True):
            PaymentService().initiate_store_purchase(
                product_items=[{'product_id': str(product.id), 'quantity': 1}], child_id=str(recurring.child_id),
            )
        alert = OfficeAlert.objects.get(kind='till_uncertain')
        self.assertIn('קופה', alert.where)
        self.assertIn(recurring.child.full_name, alert.customer)

    @patch('apps.core.payment_service.PaymentService._issue_payment_credit_note')
    def test_a_refund_without_an_answer(self, _note):
        from apps.core.models import OfficeAlert

        payment = _paid_course_payment()
        gateway, post = _gateway(NO_BODY)
        with post, self.captureOnCommitCallbacks(execute=True):
            PaymentService().refund_payment(str(payment.id), reason='ביטול')
        alert = OfficeAlert.objects.get(kind='refund_uncertain')
        self.assertIn('4411', alert.what)
        self.assertIn(payment.child.full_name, alert.customer)
