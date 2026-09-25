"""
The old course notify (POST /api/v1/customers/payments/webhook/) is closed.

It trusted the POST body: a payment id and Response=000 completed the payment,
opened a standing order with any token and enrolled the child, and a forged
decline flagged the child and sent WhatsApp. It now answers 410 and changes
nothing, and no hosted page points at it.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction

URL = '/api/v1/customers/payments/webhook/'


class ClosedCourseWebhookTest(TestCase):
    def setUp(self):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        self.payment = Payment.objects.create(
            child=child, family=child.family, lesson=lesson, branch=lesson.course.branch,
            payment_type='recurring_subscription', status='pending',
            base_amount=Decimal('300'), discount_amount=Decimal('0'), final_amount=Decimal('300'),
            registration_fee=Decimal('0'), description='מנוי',
        )

    def post(self, **fields):
        body = {'pdesc': self.payment.id.hex, 'index': '555', 'ConfirmationCode': '0001',
                'TranzilaTK': 'forged-token', 'sum': '300'}
        body.update(fields)
        with patch(
            'apps.core.payment_service.PaymentService.process_webhook_callback',
            return_value={'success': True},
        ) as process:
            res = APIClient().post(URL, body)
        return res, process

    def test_a_forged_success_changes_nothing(self):
        res, process = self.post(Response='000')

        self.assertEqual(res.status_code, 410)
        process.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'pending')
        self.assertFalse(RecurringPayment.objects.exists())
        self.assertFalse(TranzilaTransaction.objects.exists())

    def test_a_forged_decline_changes_nothing(self):
        res, process = self.post(Response='033')

        self.assertEqual(res.status_code, 410)
        process.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'pending')


@override_settings(
    TRANZILA_TERMINAL='cogolive', TRANZILA_PUBLIC_KEY='pk', TRANZILA_SECRET_KEY='sk',
    TRANZILA_HOSTED_PAGE_ENABLED=True, TRANZILA_HANDSHAKE_ENABLED=False,
    CRM_API_BASE_URL='https://kogo-back.vercel.app',
)
class NoDefaultNotifyTest(TestCase):
    def test_a_page_without_its_own_callback_notifies_nowhere(self):
        url = TranzilaService.iframe().create_payment_request(amount=Decimal('10.00'), transaction_id='x1')
        self.assertNotIn('notify_url_address', url)
        self.assertNotIn('payments%2Fwebhook', url)

    def test_a_page_with_its_callback_notifies_there(self):
        url = TranzilaService.iframe().create_payment_request(
            amount=Decimal('10.00'), transaction_id='x1',
            callback_url='https://kogo-back.vercel.app/api/v1/store/payment/callback/',
        )
        self.assertIn('notify_url_address=https%3A%2F%2Fkogo-back.vercel.app%2Fapi%2Fv1%2Fstore%2Fpayment%2Fcallback%2F', url)
