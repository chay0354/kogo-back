"""
Two things a refund learned on 6.10.2026.

A charge Tranzila declined is not refunded: it was written down as paid and no
money came, and in production one such ₪120 went back to a card. And a monthly
charge refunded with "cancel the standing order too" does not come back next
month — while a refund without it leaves the standing order alone, as before.

Tranzila is never reached: its refund and its cancel are both stand-ins here.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.payment_service import REFUND_OF_DECLINED_CHARGE, PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import recorded_decline_code
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction

User = get_user_model()
REFUND_OK = {
    'success': True, 'transaction_id': 'REFUND_1', 'confirmation_code': 'R1',
    'response_code': '000', 'message': 'ok', 'raw_response': {},
}
REFUND_REFUSED = {'success': False, 'error': 'הזיכוי נדחה'}
REFUND_NO_ANSWER = {'success': False, 'uncertain': True, 'error': 'timeout'}
CANCEL_OK = {'success': True}


class RefundTestCase(APITestCase):
    def setUp(self):
        user = User.objects.create_user(username='manager-refund@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(branch=self.branch, email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family, status='active')
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, branch=self.branch)
        self._n = 0

        # The gateway's two doors, both closed and both watched — in one mock,
        # so the order of the calls can be read off it.
        self.gateway = MagicMock()
        self.gateway.refund.return_value = REFUND_OK
        self.gateway.cancel.return_value = CANCEL_OK
        for target, stand_in in (
            ('apps.core.tranzila_service.TranzilaService.refund_transaction', self.gateway.refund),
            ('apps.core.tranzila_service.TranzilaService.cancel_recurring_payment', self.gateway.cancel),
            ('apps.core.credit_note_email.send_credit_note_email', MagicMock()),
        ):
            patcher = patch(target, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)

    def charge(self, *, child=None, lesson='same', code='000', confirmation=None, amount='260.00', **over):
        self._n += 1
        txn = TranzilaTransaction.objects.create(
            transaction_id=f'TRX_{self._n}', confirmation_code=confirmation or f'AUTH_{self._n}',
            transaction_type='recurring_charge', response_code=code, is_successful=True,
            idempotency_key=f'refund-guard-{self._n}',
        )
        child = child or self.child
        fields = dict(
            child=child, family=child.family, lesson=self.lesson if lesson == 'same' else lesson,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal(amount), final_amount=Decimal(amount),
            payment_date=timezone.now() - timedelta(days=12), tranzila_transaction=txn,
        )
        fields.update(over)
        return Payment.objects.create(**fields)

    def standing_order(self, *, child=None, lesson='same', **over):
        child = child or self.child
        initial = self.charge(child=child, lesson=lesson)
        fields = dict(
            child=child, initial_payment=initial, status='active', tranzila_token='tok-1234',
            amount=Decimal('260.00'), start_date=timezone.localdate() - timedelta(days=90),
            next_billing_date=timezone.localdate() + timedelta(days=20),
        )
        fields.update(over)
        return RecurringPayment.objects.create(**fields)

    def refund(self, payment, **body):
        return self.client.post(f'/api/v1/customers/payments/{payment.id}/refund/', body, format='json')

    def claims(self, payment):
        return TranzilaTransaction.objects.filter(idempotency_key=f'refund_claim_payment_{payment.id}')


class DeclinedChargeIsNotRefundedTest(RefundTestCase):
    def test_a_declined_charge_is_refused_before_tranzila_hears_of_it(self):
        payment = self.charge(code='141', confirmation='0000000', amount='120.00')

        result = PaymentService().refund_payment(str(payment.id), reason='ביטול')

        self.assertFalse(result['success'])
        self.assertTrue(result['declined'])
        self.assertIn(REFUND_OF_DECLINED_CHARGE, result['error'])
        self.gateway.refund.assert_not_called()
        self.assertFalse(self.claims(payment).exists())
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

    def test_the_screen_gets_the_reason_in_hebrew(self):
        payment = self.charge(code='004', confirmation='0000000')

        response = self.refund(payment, reason='ביטול')

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.data['declined'])
        self.assertIn('נדחה בטרנזילה ולא נגבה', response.data['error'])
        self.gateway.refund.assert_not_called()

    def test_an_old_row_with_no_code_is_refunded_as_before(self):
        payment = self.charge(code='')

        result = PaymentService().refund_payment(str(payment.id), reason='ביטול')

        self.assertTrue(result['success'])
        self.gateway.refund.assert_called_once()
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')

    def test_an_approved_charge_is_refunded_as_before(self):
        for code in ('000', '0'):
            payment = self.charge(code=code)
            self.assertTrue(PaymentService().refund_payment(str(payment.id))['success'], code)
        self.assertEqual(self.gateway.refund.call_count, 2)

    def test_which_stored_codes_are_a_decline(self):
        for code in ('141', '004', '006', '003', '026', '36'):
            self.assertEqual(recorded_decline_code(code), code)
        for code in ('', None, '000', '0', '0000', ' 000 ', '999', 'N/A', 'none'):
            self.assertEqual(recorded_decline_code(code), '', repr(code))


class RefundAsksAboutTheStandingOrderTest(RefundTestCase):
    def test_without_the_tick_the_standing_order_is_left_alone(self):
        order = self.standing_order()
        payment = self.charge()

        response = self.refund(payment, reason='ביטול')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('standing_order_cancelled', response.data)
        self.gateway.cancel.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')

    def test_with_the_tick_it_is_cancelled_after_the_refund(self):
        order = self.standing_order()
        payment = self.charge()  # a month the cron charged: nobody's initial payment

        response = self.refund(payment, reason='עזב את החוג', cancel_standing_order=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['standing_order_cancelled'])
        self.assertEqual(response.data['standing_orders_cancelled'], [str(order.id)])
        self.assertIn('הוראת הקבע בוטלה', response.data['message'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertIn('עזב את החוג', order.cancellation_reason)
        self.assertEqual([call[0] for call in self.gateway.mock_calls], ['refund', 'cancel'])
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')

    def test_only_an_explicit_true_cancels(self):
        order = self.standing_order()
        for value in ('true', 1, 'yes', None):
            payment = self.charge()
            self.refund(payment, reason='ביטול', cancel_standing_order=value)
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')
        self.gateway.cancel.assert_not_called()

    def test_a_refund_tranzila_refused_cancels_nothing(self):
        order = self.standing_order()
        payment = self.charge()
        self.gateway.refund.return_value = REFUND_REFUSED

        response = self.refund(payment, reason='ביטול', cancel_standing_order=True)

        self.assertEqual(response.status_code, 400)
        self.gateway.cancel.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')

    def test_a_refund_with_no_answer_cancels_nothing(self):
        order = self.standing_order()
        payment = self.charge()
        self.gateway.refund.return_value = REFUND_NO_ANSWER

        response = self.refund(payment, reason='ביטול', cancel_standing_order=True)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.data['uncertain'])
        self.gateway.cancel.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')

    def test_only_the_order_of_that_child_and_that_lesson(self):
        mine = self.standing_order()
        other_lesson = TestDataFactory.create_lesson(course=self.course, branch=self.branch, day_of_week=3)
        another_class = self.standing_order(lesson=other_lesson)
        brother = TestDataFactory.create_child(family=self.family, first_name='איתי', status='active')
        his = self.standing_order(child=brother)
        payment = self.charge()

        self.refund(payment, reason='ביטול', cancel_standing_order=True)

        statuses = {
            order.id: RecurringPayment.objects.get(pk=order.pk).status for order in (mine, another_class, his)
        }
        self.assertEqual(statuses, {mine.id: 'cancelled', another_class.id: 'active', his.id: 'active'})

    def test_a_cancel_that_fails_leaves_the_refund_a_success(self):
        order = self.standing_order()
        payment = self.charge()

        with patch.object(PaymentService, 'cancel_subscription', side_effect=RuntimeError('db down')):
            response = self.refund(payment, reason='ביטול', cancel_standing_order=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['standing_order_cancelled'])
        self.assertIn('לא בוטלה', response.data['message'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')

    def test_tranzila_not_cancelling_is_said_and_the_order_is_still_closed_here(self):
        order = self.standing_order()
        payment = self.charge()
        self.gateway.cancel.return_value = {'success': False, 'error': 'no sto', 'manual_cancellation_required': True}

        response = self.refund(payment, reason='ביטול', cancel_standing_order=True)

        self.assertTrue(response.data['standing_order_cancelled'])
        self.assertIn('בטרנזילה יש לוודא ידנית', response.data['message'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')

    def test_no_live_order_behind_the_charge(self):
        payment = self.charge()

        response = self.refund(payment, reason='ביטול', cancel_standing_order=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['standing_order_cancelled'])
        self.assertIn('לא נמצאה הוראת קבע פעילה', response.data['message'])


class RefundInfoTest(RefundTestCase):
    def info(self, payment):
        response = self.client.get(f'/api/v1/customers/payments/{payment.id}/refund-info/')
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_a_monthly_charge_names_the_order_behind_it(self):
        order = self.standing_order()
        payment = self.charge()

        info = self.info(payment)

        self.assertTrue(info['refundable'])
        self.assertEqual(info['blocked_reason'], '')
        self.assertEqual([row['id'] for row in info['standing_orders']], [str(order.id)])
        self.assertEqual(info['standing_orders'][0]['amount'], '260.00')
        self.assertEqual(info['standing_orders'][0]['course_name'], self.course.name)

    def test_a_declined_charge_says_why_before_anyone_confirms(self):
        self.standing_order()
        payment = self.charge(code='141', confirmation='0000000')

        info = self.info(payment)

        self.assertFalse(info['refundable'])
        self.assertTrue(info['declined'])
        self.assertIn('נדחה בטרנזילה ולא נגבה', info['blocked_reason'])
        self.assertEqual(info['standing_orders'], [])

    def test_a_charge_already_refunded(self):
        payment = self.charge(status='refunded')

        info = self.info(payment)

        self.assertFalse(info['refundable'])
        self.assertFalse(info['declined'])

    def test_a_trial_payment_has_no_order_behind_it(self):
        self.standing_order()
        payment = self.charge(payment_type='one_time')

        self.assertEqual(self.info(payment)['standing_orders'], [])

    def test_reading_it_touches_nothing(self):
        order = self.standing_order()
        payment = self.charge()

        self.info(payment)

        self.gateway.refund.assert_not_called()
        self.gateway.cancel.assert_not_called()
        self.assertFalse(self.claims(payment).exists())
        order.refresh_from_db()
        self.assertEqual(order.status, 'active')


class DocumentsTabRowTest(RefundTestCase):
    """The documents tab's lesson receipt names its charge, and whether "זיכוי" can be offered on it."""

    def rows(self):
        from apps.core.tranzila_ledger import _local_crm_invoice_rows

        today = timezone.localdate()
        return {row['document_number']: row for row in _local_crm_invoice_rows(today - timedelta(days=5), today)}

    def receipt(self, payment):
        return PaymentService()._create_invoice_from_payment(payment, payment.tranzila_transaction, send_email=False)

    def test_a_receipt_of_a_completed_charge_can_be_refunded(self):
        payment = self.charge()
        receipt = self.receipt(payment)

        row = self.rows()[receipt.invoice_number]

        self.assertEqual(row['payment_id'], str(payment.id))
        self.assertTrue(row['payment_refundable'])
        self.assertEqual(row['payment_amount'], 260.0)
        self.assertTrue(row['payment_is_monthly'])

    def test_one_already_refunded_is_not_offered_again(self):
        payment = self.charge()
        receipt = self.receipt(payment)
        PaymentService().refund_payment(str(payment.id), reason='ביטול')

        self.assertFalse(self.rows()[receipt.invoice_number]['payment_refundable'])

    def test_a_declined_charge_is_not_offered(self):
        payment = self.charge(code='141', confirmation='0000000')
        receipt = self.receipt(payment)

        self.assertFalse(self.rows()[receipt.invoice_number]['payment_refundable'])

    def test_a_family_receipt_for_several_charges_is_refunded_charge_by_charge(self):
        from apps.customers.checkout_invoice import issue_widget_checkout_invoice

        brother = TestDataFactory.create_child(family=self.family, first_name='איתי', status='active')
        first, second = self.charge(), self.charge(child=brother)
        receipt = issue_widget_checkout_invoice([first, second], send_email=False)

        row = self.rows()[receipt.invoice_number]

        self.assertFalse(row['payment_refundable'])
