"""
What a widget charge leaves the child as, by the path the parent took.

                      went through        declined
  trial               נרשם לניסיון         בתהליך רישום — never booked; try again
  registration        פעיל                בעיה באשראי

And a declined second lesson in a bundle must say so, not raise.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.child_status import resolve_child_status
from apps.customers.models import Payment
from apps.customers.tests.test_widget_charge import CARD, TRANZILA_OK, _payment_for

TRANZILA_DECLINED = {
    'success': False,
    'error': 'כרטיס נדחה',
    'response_code': '004',
    'raw_response': {},
}

TRIAL_DATE = date.today() + timedelta(days=5)


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
class WidgetChargeStatusByPathTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=self.family)
        self.child = TestDataFactory.create_child(family=self.family, status='pending')
        self.lesson = TestDataFactory.create_lesson()

    def charge(self, *payments):
        return self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_ids': [str(p.id) for p in payments], 'card_details': CARD},
            format='json',
        )

    def trial_payment(self, child=None, lesson=None):
        return _payment_for(
            child or self.child, lesson or self.lesson,
            payment_type='one_time', trial_lesson_date=TRIAL_DATE,
            base_amount=Decimal('40.00'), final_amount=Decimal('40.00'),
            registration_fee=Decimal('0.00'), description='שיעור ניסיון',
        )

    # --- declined -----------------------------------------------------------

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_declined_trial_leaves_the_child_in_registration(self, _charge):
        """The owner's rule: a trial whose card failed was never booked."""
        payment = self.trial_payment()
        res = self.charge(payment)

        self.assertFalse(res.json()['success'])
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'pending')
        self.assertFalse(self.child.lesson_enrollments.exists())

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_declined_registration_is_a_card_problem(self, _charge):
        payment = _payment_for(self.child, self.lesson)
        res = self.charge(payment)

        self.assertFalse(res.json()['success'])
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'payment_problem')

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_declined_trial_does_not_pull_down_a_paying_child(self, _charge):
        """Adding a trial for another course must not cost them their פעיל."""
        self.child.status = 'active'
        self.child.save(update_fields=['status'])
        self.charge(self.trial_payment())

        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    def test_a_bundle_whose_second_lesson_is_declined_answers_instead_of_crashing(
        self, mock_charge, _whatsapp,
    ):
        """
        First lesson paid, second declined. _charge_one used to fall off the end
        with None there, and result.get('success') raised — a server error in
        place of "declined, try again", after money had already been taken.
        """
        mock_charge.side_effect = [TRANZILA_OK, TRANZILA_DECLINED]
        first = _payment_for(self.child, self.lesson)
        second = _payment_for(self.child, TestDataFactory.create_lesson())

        res = self.charge(first, second)

        # A structured answer the widget can show — not a 500.
        self.assertLess(res.status_code, 500, res.content)
        body = res.json()
        self.assertFalse(body['success'])
        self.assertTrue(body.get('partial'))
        self.assertTrue(body.get('error'))
        second.refresh_from_db()
        self.assertEqual(second.status, 'failed')

    # --- went through -------------------------------------------------------

    @patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': False})
    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_paid_trial_is_a_trial_not_an_active_customer(self, _charge, _whatsapp, _stamp):
        """The money bought one lesson to try. That is נרשם לניסיון."""
        payment = self.trial_payment()
        self.charge(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')
        self.child.refresh_from_db()
        self.assertEqual(resolve_child_status(self.child), 'trial_signed')

    def test_a_completed_trial_payment_alone_is_not_money_in(self):
        Payment.objects.filter(pk=self.trial_payment().pk).update(status='completed')
        self.assertNotEqual(resolve_child_status(self.child), 'active')

    def test_a_completed_registration_payment_is(self):
        Payment.objects.filter(pk=_payment_for(self.child, self.lesson).pk).update(status='completed')
        self.assertEqual(resolve_child_status(self.child), 'active')
