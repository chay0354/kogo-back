"""Widget card charge: idempotent success after timeout / DCdisable duplicate."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment


CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2028,
    'cvv': '123',
    'card_holder_id': '123456782',
}

TRANZILA_OK = {
    'success': True,
    'transaction_id': '888',
    'confirmation_code': 'AUTH1',
    'token': 'tok_widget',
    'amount': 120.0,
    'response_code': '000',
    'raw_response': {},
}


def _payment_for(child, lesson, **kwargs):
    defaults = {
        'child': child,
        'family': child.family,
        'parent': child.family.parents.first(),
        'lesson': lesson,
        'branch': lesson.course.branch,
        'payment_type': 'recurring_subscription',
        'status': 'pending',
        'base_amount': Decimal('350.00'),
        'discount_amount': Decimal('0.00'),
        'final_amount': Decimal('120.00'),
        'registration_fee': Decimal('120.00'),
        'description': 'מנוי',
    }
    defaults.update(kwargs)
    return Payment.objects.create(**defaults)


@override_settings(
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_PUBLIC_KEY='test_public_key',
    TRANZILA_SECRET_KEY='test_secret_key',
    TRANZILA_PROD_TERMINAL='test_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='test_terminal',
    TRANZILA_PROD_PUBLIC_KEY='test_public_key',
    TRANZILA_PROD_SECRET_KEY='test_secret_key',
    SUBSCRIPTION_FIRST_CHARGE_DATE='',
)
class WidgetChargeIdempotencyTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=self.family)
        self.child = TestDataFactory.create_child(family=self.family)
        self.lesson = TestDataFactory.create_lesson()

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_successful_charge_enrolls_and_completes(self, _charge, _whatsapp):
        payment = _payment_for(self.child, self.lesson)
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_id': str(payment.id), 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['success'])
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')
        self.assertTrue(
            LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson, status='active').exists()
        )

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    def test_already_completed_charge_returns_success_without_charging_again(self, mock_charge):
        payment = _payment_for(self.child, self.lesson, status='completed')
        LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active')
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_id': str(payment.id), 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['success'])
        mock_charge.assert_not_called()

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    def test_in_flight_processing_does_not_look_like_a_decline(self, mock_charge):
        payment = _payment_for(self.child, self.lesson, status='processing')
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_id': str(payment.id), 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertFalse(body['success'])
        self.assertTrue(body['processing'])
        mock_charge.assert_not_called()

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    def test_gateway_timeout_leaves_processing_not_failed(self, mock_charge, _whatsapp):
        mock_charge.return_value = {
            'success': False,
            'error': 'Request timed out',
            'response_code': '999',
            'uncertain': True,
        }
        payment = _payment_for(self.child, self.lesson)
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_id': str(payment.id), 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['processing'])
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'processing')
        self.child.refresh_from_db()
        self.assertNotEqual(self.child.status, 'payment_problem')

    def test_payment_status_poll_reports_completed(self):
        payment = _payment_for(self.child, self.lesson, status='completed')
        res = self.client.get(
            '/api/v1/customers/widget/payment-status/',
            {'payment_id': str(payment.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['success'])

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_stale_processing_retries_and_completes(self, mock_charge, _whatsapp):
        from datetime import timedelta

        payment = _payment_for(self.child, self.lesson, status='processing')
        Payment.objects.filter(pk=payment.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_id': str(payment.id), 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['success'])
        mock_charge.assert_called_once()
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.verify_card')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_zero_amount_bundle_day_reuses_token_without_verify(self, mock_charge, mock_verify, _whatsapp):
        """After דמי רישום on day 1, day 2 is ₪0 and must not hit Tranzila verify (20004)."""
        lesson_b = TestDataFactory.create_lesson(course=self.lesson.course, day_of_week=3)
        first = _payment_for(self.child, self.lesson, final_amount=Decimal('120.00'), registration_fee=Decimal('120.00'))
        second = _payment_for(
            self.child, lesson_b,
            final_amount=Decimal('0.00'),
            registration_fee=Decimal('0.00'),
        )
        res = self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_ids': [str(first.id), str(second.id)], 'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()['success'])
        mock_charge.assert_called_once()
        mock_verify.assert_not_called()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, 'completed')
        self.assertEqual(second.status, 'completed')
        self.assertEqual(RecurringPayment.objects.filter(child=self.child, status='active').count(), 2)
        self.assertTrue(LessonEnrollment.objects.filter(child=self.child, lesson=lesson_b, status='active').exists())
