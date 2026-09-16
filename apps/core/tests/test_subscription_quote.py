"""The office's subscription dialog prices a lesson without writing anything.

Opening the dialog used to create a pending `Payment` just to show a price,
and nothing ever cleaned those rows up. A stale pending row is not harmless:
it carries a registration fee, so the next signup for the same child on
another lesson skipped the fee for good, and it counted as a sibling "signing
up right now" for discount eligibility. The preview is now a quote, and a
pending row that was abandoned hours ago no longer hides the fee.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.payment_service import PaymentService, child_already_has_registration_fee
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.discount_service import DiscountCalculation
from apps.customers.models import Payment, PaymentDiscountSnapshot, RecurringPayment, TranzilaTransaction

User = get_user_model()

CARD_OK = {
    'success': True,
    'transaction_id': 'TRX_QUOTE',
    'confirmation_code': 'CONF_Q',
    'token': 'card_token_q',
    'response_code': '000',
    'raw_response': {},
}


def _passthrough_discount(**kwargs):
    return DiscountCalculation(
        applicable_discounts=[],
        total_discount_amount=Decimal('0.00'),
        final_price=kwargs['base_price'],
        base_price=kwargs['base_price'],
    )


def _money_fields(payload):
    return {
        key: payload[key]
        for key in (
            'course_index', 'base_amount', 'discount_amount', 'prorated_amount', 'registration_fee',
            'final_amount', 'prorate_factor', 'prorate_lessons_remaining', 'total_lessons_this_month',
            'next_billing_date', 'monthly_amount', 'subscription_start_date', 'discounts_applied',
            'trial_credit_amount',
        )
    }


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://tranzila.test/x')
@patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment', side_effect=_passthrough_discount)
class SubscriptionQuoteTest(TestCase):
    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson()

    def test_opening_the_dialog_many_times_writes_nothing(self, _discount, iframe):
        for _ in range(5):
            quote = self.service.initiate_subscription_payment(
                child_id=str(self.child.id), lesson_id=str(self.lesson.id), quote_only=True,
            )
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PaymentDiscountSnapshot.objects.count(), 0)
        iframe.assert_not_called()
        self.assertIsNone(quote['payment_id'])
        self.assertEqual(quote['registration_fee'], 120.00)
        self.assertGreater(quote['final_amount'], 120.00)

    def test_the_quote_shows_the_same_figures_as_the_real_initiation(self, _discount, _iframe):
        quote = self.service.initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id), quote_only=True,
        )
        real = self.service.initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        self.assertEqual(_money_fields(quote), _money_fields(real))
        self.assertEqual(Payment.objects.count(), 1)

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
    @patch('apps.core.payment_service.TranzilaService.charge_with_card', return_value=dict(CARD_OK))
    def test_the_charge_after_the_quotes_bills_exactly_what_was_shown(self, charge, _receipt, _wa, _discount, _iframe):
        quote = self.service.initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id), quote_only=True,
        )
        result = self.service.charge_subscription_with_card(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
            card_number='4580458045804580',
            expiry_month=12,
            expiry_year=2030,
            cvv='123',
            card_holder_id='123456782',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['final_amount'], quote['final_amount'])
        self.assertEqual(result['monthly_amount'], quote['monthly_amount'])
        self.assertEqual(charge.call_args.kwargs['amount'], Decimal(str(quote['final_amount'])))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().registration_fee, Decimal('120.00'))
        self.assertTrue(RecurringPayment.objects.filter(child=self.child, status='active').exists())

    def test_the_dialog_endpoint_quotes_without_a_row(self, _discount, _iframe):
        user = User.objects.create_user(username='manager-quote@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        client = APIClient()
        # Re-fetch: the profile created on signup is cached on `user` with its default role.
        client.force_authenticate(User.objects.get(pk=user.pk))
        body = {
            'child_id': str(self.child.id),
            'lesson_id': str(self.lesson.id),
            'include_registration_fee': True,
            'include_monthly_amount': True,
            'quote_only': True,
        }
        for _ in range(3):
            response = client.post('/api/v1/customers/payments/initiate_subscription/', body, format='json')
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(response.data['registration_fee'], 120.00)
        # The widget's own call carries no flag and still gets its pending row.
        response = client.post('/api/v1/customers/payments/initiate_subscription/', {
            'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id),
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Payment.objects.count(), 1)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class AbandonedPendingRowTest(TestCase):
    def _pending_fee_row(self, child, lesson, age):
        row = Payment.objects.create(
            child=child,
            family=child.family,
            branch=lesson.course.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('380.00'),
            registration_fee=Decimal('120.00'),
            description='מנוי חודשי',
        )
        Payment.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
        return row

    def test_a_pending_row_abandoned_hours_ago_does_not_hide_the_fee(self):
        child = TestDataFactory.create_child()
        other_lesson = TestDataFactory.create_lesson()
        new_lesson = TestDataFactory.create_lesson()
        self._pending_fee_row(child, other_lesson, timedelta(hours=3))
        self.assertFalse(child_already_has_registration_fee(child, current_lesson=new_lesson))

    def test_a_fresh_pending_row_from_the_same_checkout_still_counts(self):
        child = TestDataFactory.create_child()
        other_lesson = TestDataFactory.create_lesson()
        new_lesson = TestDataFactory.create_lesson()
        self._pending_fee_row(child, other_lesson, timedelta(minutes=1))
        self.assertTrue(child_already_has_registration_fee(child, current_lesson=new_lesson))


class RefundBoundsTest(TestCase):
    def _charged_payment(self):
        child = TestDataFactory.create_child()
        payment = Payment.objects.create(
            child=child, family=child.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('260.00'),
            payment_date=timezone.now() - timedelta(days=3),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id='TRX_R', confirmation_code='AUTH_R', transaction_type='recurring_charge',
            is_successful=True, idempotency_key='refund-bounds-1',
        )
        payment.save(update_fields=['tranzila_transaction'])
        return payment

    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value={'success': False, 'error': 'x'})
    def test_a_refund_larger_than_the_charge_never_reaches_tranzila(self, refund):
        payment = self._charged_payment()
        result = PaymentService().refund_payment(str(payment.id), reason='טעות', amount=Decimal('2600.00'))
        self.assertFalse(result['success'])
        refund.assert_not_called()
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value={'success': False, 'error': 'x'})
    def test_a_zero_or_negative_refund_never_reaches_tranzila(self, refund):
        payment = self._charged_payment()
        self.assertFalse(PaymentService().refund_payment(str(payment.id), amount=Decimal('-5.00'))['success'])
        refund.assert_not_called()


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://tranzila.test/x')
@patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment', side_effect=_passthrough_discount)
class PaymentDateIsTheIsraeliDayTest(TestCase):
    """Discount ranges are Israeli calendar days; the server clock is UTC.

    23:30 in Israel on the last day of an early-signup range is 20:30 UTC of
    the same day, and 00:30 on the day after the range is 21:30 UTC of the
    last day. `date.today()` priced the first as still inside the range only by
    luck of the server's clock and the second as still inside it."""

    def _initiate_at(self, service, child, lesson, utc_now):
        with patch('apps.core.payment_service.timezone.now', return_value=utc_now):
            return service.initiate_subscription_payment(child_id=str(child.id), lesson_id=str(lesson.id))

    def test_late_evening_in_israel_is_priced_on_the_israeli_date(self, discount, _iframe):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        last_day_2030_utc = datetime(2026, 9, 30, 20, 30, tzinfo=dt_timezone.utc)   # 23:30 Israel, Sept 30
        self._initiate_at(PaymentService(), child, lesson, last_day_2030_utc)
        self.assertEqual(discount.call_args.kwargs['payment_date'], date(2026, 9, 30))

    def test_half_past_midnight_in_israel_is_already_the_next_day(self, discount, _iframe):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        after_midnight_utc = datetime(2026, 9, 30, 21, 30, tzinfo=dt_timezone.utc)  # 00:30 Israel, Oct 1
        self._initiate_at(PaymentService(), child, lesson, after_midnight_utc)
        self.assertEqual(discount.call_args.kwargs['payment_date'], date(2026, 10, 1))

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
    @patch('apps.core.payment_service.TranzilaService.charge_with_card', return_value=dict(CARD_OK))
    def test_the_card_charge_uses_the_same_israeli_date(self, _charge, _receipt, _wa, discount, _iframe):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        after_midnight_utc = datetime(2026, 9, 30, 21, 30, tzinfo=dt_timezone.utc)  # 00:30 Israel, Oct 1
        with patch('apps.core.payment_service.timezone.now', return_value=after_midnight_utc):
            PaymentService().charge_subscription_with_card(
                child_id=str(child.id), lesson_id=str(lesson.id), card_number='4580458045804580',
                expiry_month=12, expiry_year=2030, cvv='123', card_holder_id='123456782',
            )
        self.assertEqual(discount.call_args.kwargs['payment_date'], date(2026, 10, 1))
