"""HTTP end-to-end: widget lookup → register → card charge.

The live gateway is mocked. These tests cover the public widget API the
frontend actually calls, including enroll-only-after-approval.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.card_validation import israeli_id_valid
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Child, Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment


PROD_TRANZILA = dict(
    TRANZILA_TERMINAL='mock-terminal',
    TRANZILA_PUBLIC_KEY='',
    TRANZILA_SECRET_KEY='',
    TRANZILA_PROD_TERMINAL='kogo-prod-terminal',
    TRANZILA_PROD_PUBLIC_KEY='pk-not-a-placeholder',
    TRANZILA_PROD_SECRET_KEY='sk-not-a-placeholder',
    TRANZILA_HANDSHAKE_ENABLED=False,
    REGISTRATION_FEE_ILS=0,
    SUBSCRIPTION_FIRST_CHARGE_DATE='',
)

CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2028,
    'cvv': '123',
    'card_holder_id': '123456782',
}

APPROVED = {
    'success': True,
    'transaction_id': 'txn-widget-e2e',
    'confirmation_code': '0516401',
    'response_code': '000',
    'token': 'tok_widget_e2e',
    'raw_response': {'processor_response_code': '000'},
}

DECLINED = {
    'success': False,
    'error': 'הכרטיס נדחה',
    'response_code': '033',
}

TIMEOUT = {
    'success': False,
    'error': 'Request timed out',
    'indeterminate': True,
    'response_code': '999',
}


def _israeli_id(seed: int) -> str:
    body = f'{seed:08d}'
    for digit in range(10):
        candidate = body + str(digit)
        if israeli_id_valid(candidate):
            return candidate
    raise AssertionError(f'no valid Israeli ID for seed {seed}')


@override_settings(**PROD_TRANZILA)
class WidgetPaymentHttpE2ETest(TestCase):
    def setUp(self):
        # Register builds an iframe URL and would handshake Tranzila with the
        # fake prod keys unless this is stubbed — that call hung the suite.
        iframe = patch(
            'apps.core.tranzila_service.TranzilaService.create_recurring_payment_request',
            return_value='https://example.test/iframe',
        )
        iframe.start()
        self.addCleanup(iframe.stop)
        self.client = APIClient()
        self.course = TestDataFactory.create_course(
            name='בדיקה 5 ש״ח E2E',
            price=Decimal('5.00'),
        )
        self.lesson = TestDataFactory.create_lesson(course=self.course)
        self._id_seed = 88001100

    def _unique_ids(self):
        parent_id = _israeli_id(self._id_seed)
        self._id_seed += 1
        child_id = _israeli_id(self._id_seed)
        self._id_seed += 1
        return parent_id, child_id

    def _register_payload(self):
        parent_id, child_id = self._unique_ids()
        return {
            'parent_id_number': parent_id,
            'parent_first_name': 'הורה',
            'parent_last_name': 'בדיקות',
            'parent_phone': '0501234567',
            'parent_email': 'widget-e2e@example.com',
            'child_first_name': 'ילד',
            'child_last_name': 'בדיקות',
            'child_id_number': child_id,
            'child_birth_date': '2015-06-01',
            'child_gender': 'male',
            'course_id': str(self.course.id),
            'lesson_id': str(self.lesson.id),
        }

    def _register(self, payload=None):
        payload = payload or self._register_payload()
        lookup = self.client.post('/api/v1/customers/widget/lookup/', {
            'parent_id_number': payload['parent_id_number'],
            'child_first_name': payload['child_first_name'],
            'child_last_name': payload['child_last_name'],
        }, format='json')
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.data['family_status'], 'new')

        register = self.client.post(
            '/api/v1/customers/widget/register/',
            payload,
            format='json',
        )
        self.assertEqual(register.status_code, 201, register.data)
        return payload, register.data

    def test_register_creates_pending_payment_without_enrollment(self):
        payload, data = self._register()
        payment = Payment.objects.get(id=data['payment_id'])
        child = Child.objects.get(id=data['child_id'])

        self.assertEqual(payment.status, 'pending')
        self.assertEqual(child.status, 'pending')
        self.assertFalse(LessonEnrollment.objects.filter(child=child).exists())
        self.assertGreater(payment.final_amount, 0)
        self.assertEqual(payload['child_id_number'], child.id_number)

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email')
    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_approved_charge_enrolls_and_activates_child(
        self, mock_production, _mock_email, _mock_whatsapp,
    ):
        mock_production.return_value.charge_with_card.return_value = APPROVED
        mock_production.return_value.credential_error.return_value = None

        _payload, data = self._register()
        child_id = data['child_id']
        payment_id = data['payment_id']

        charge = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': payment_id,
            'card_details': CARD,
        }, format='json')

        self.assertEqual(charge.status_code, 200, charge.data)
        self.assertTrue(charge.data['success'])

        payment = Payment.objects.get(id=payment_id)
        child = Child.objects.get(id=child_id)
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(child.status, 'active')
        self.assertTrue(
            LessonEnrollment.objects.filter(
                child=child, lesson=self.lesson, status='active',
            ).exists()
        )
        self.assertTrue(
            RecurringPayment.objects.filter(child=child, status='active').exists()
        )

        retry = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': payment_id,
            'card_details': CARD,
        }, format='json')
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.data['success'])
        self.assertTrue(retry.data.get('already_completed'))
        self.assertEqual(mock_production.return_value.charge_with_card.call_count, 1)

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_declined_charge_does_not_enroll(self, mock_production):
        mock_production.return_value.charge_with_card.return_value = DECLINED
        mock_production.return_value.credential_error.return_value = None

        _payload, data = self._register()
        charge = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': data['payment_id'],
            'card_details': CARD,
        }, format='json')

        self.assertEqual(charge.status_code, 400, charge.data)
        self.assertFalse(charge.data['success'])

        payment = Payment.objects.get(id=data['payment_id'])
        child = Child.objects.get(id=data['child_id'])
        self.assertEqual(payment.status, 'failed')
        self.assertEqual(child.status, 'payment_problem')
        self.assertFalse(LessonEnrollment.objects.filter(child=child).exists())

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_timeout_returns_pending_without_failing_the_child(self, mock_production):
        mock_production.return_value.charge_with_card.return_value = TIMEOUT
        mock_production.return_value.find_successful_transaction.return_value = None
        mock_production.return_value.credential_error.return_value = None

        _payload, data = self._register()
        charge = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': data['payment_id'],
            'card_details': CARD,
        }, format='json')

        self.assertEqual(charge.status_code, 202, charge.data)
        self.assertTrue(charge.data.get('pending'))
        self.assertFalse(charge.data['success'])

        payment = Payment.objects.get(id=data['payment_id'])
        child = Child.objects.get(id=data['child_id'])
        self.assertEqual(payment.status, 'processing')
        self.assertEqual(child.status, 'pending')
        self.assertFalse(LessonEnrollment.objects.filter(child=child).exists())

    def test_invalid_card_is_rejected_before_gateway(self):
        _payload, data = self._register()
        with patch('apps.core.tranzila_service.TranzilaService.production') as mock_production:
            mock_production.return_value.credential_error.return_value = None
            charge = self.client.post('/api/v1/customers/widget/charge/', {
                'payment_id': data['payment_id'],
                'card_details': {**CARD, 'card_number': '4111111111111112'},
            }, format='json')

        self.assertEqual(charge.status_code, 400)
        self.assertFalse(charge.data['success'])
        mock_production.return_value.charge_with_card.assert_not_called()
        payment = Payment.objects.get(id=data['payment_id'])
        self.assertEqual(payment.status, 'pending')

    @override_settings(
        TRANZILA_PROD_TERMINAL='',
        TRANZILA_PROD_PUBLIC_KEY='',
        TRANZILA_PROD_SECRET_KEY='',
    )
    def test_missing_production_keys_returns_503(self):
        _payload, data = self._register()
        charge = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': data['payment_id'],
            'card_details': CARD,
        }, format='json')
        self.assertEqual(charge.status_code, 503)
        self.assertIn('סליקה', charge.data['error'])
        payment = Payment.objects.get(id=data['payment_id'])
        self.assertEqual(payment.status, 'pending')

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email')
    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_charge_uses_production_terminal_not_default_mock(
        self, mock_production, _mock_email, _mock_whatsapp,
    ):
        """Default TRANZILA_TERMINAL stays mock; live keys are TRANZILA_PROD_*."""
        mock_production.return_value.charge_with_card.return_value = APPROVED
        mock_production.return_value.credential_error.return_value = None

        _payload, data = self._register()
        charge = self.client.post('/api/v1/customers/widget/charge/', {
            'payment_id': data['payment_id'],
            'card_details': CARD,
        }, format='json')
        self.assertEqual(charge.status_code, 200, charge.data)
        mock_production.assert_called()
        self.assertTrue(charge.data['success'])
