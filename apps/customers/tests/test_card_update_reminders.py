"""Chasing a standing order that failed and was never fixed."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_replacement import children_without_standing_order
from apps.customers.card_update import send_card_update_reminders
from apps.customers.models import Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
SENT = {'sent': True, 'method': 'flow'}
NOT_SENT = {'sent': False, 'reason': 'no_parent_phone'}


def _failed_sto(status='failed', token='OLD'):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    child = TestDataFactory.create_child(family=family)
    lesson = TestDataFactory.create_lesson()
    initial = Payment.objects.create(
        child=child, family=family, lesson=lesson, branch=lesson.course.branch,
        payment_type='recurring_subscription', status='completed',
        base_amount=Decimal('240'), final_amount=Decimal('240'),
        registration_fee=Decimal('0.00'), description='מנוי',
    )
    return RecurringPayment.objects.create(
        child=child, initial_payment=initial, tranzila_token=token, status=status,
        base_amount=Decimal('240'), amount=Decimal('240'), billing_day=1,
        start_date=date(2026, 8, 1), next_billing_date=date(2026, 9, 1),
        card_expire_month=8, card_expire_year=2026,
    )


@override_settings(CARD_UPDATE_REMINDER_DAYS=14, CARD_UPDATE_REMINDER_MAX=3)
class ReminderTests(TestCase):
    def test_a_fresh_failure_is_chased_once(self):
        rec = _failed_sto()
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT) as send:
            out = send_card_update_reminders()
        self.assertEqual(out['sent'], 1)
        self.assertEqual(send.call_count, 1)
        rec.refresh_from_db()
        self.assertEqual(rec.card_update_reminders_sent, 1)
        self.assertIsNotNone(rec.card_update_last_reminder_at)

    def test_not_chased_again_before_the_interval(self):
        _failed_sto()
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT) as send:
            send_card_update_reminders()
            send_card_update_reminders()
        self.assertEqual(send.call_count, 1)

    def test_chased_again_once_the_interval_has_passed(self):
        rec = _failed_sto()
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT):
            send_card_update_reminders()
        RecurringPayment.objects.filter(pk=rec.pk).update(
            card_update_last_reminder_at=timezone.now() - timedelta(days=15),
        )
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT) as send:
            send_card_update_reminders()
        self.assertEqual(send.call_count, 1)
        rec.refresh_from_db()
        self.assertEqual(rec.card_update_reminders_sent, 2)

    def test_it_stops_after_three_and_says_who_needs_a_call(self):
        rec = _failed_sto()
        for _ in range(3):
            RecurringPayment.objects.filter(pk=rec.pk).update(
                card_update_last_reminder_at=timezone.now() - timedelta(days=15),
            )
            with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT):
                send_card_update_reminders()
        rec.refresh_from_db()
        self.assertEqual(rec.card_update_reminders_sent, 3)

        RecurringPayment.objects.filter(pk=rec.pk).update(
            card_update_last_reminder_at=timezone.now() - timedelta(days=90),
        )
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT) as send:
            out = send_card_update_reminders()
        send.assert_not_called()
        self.assertEqual(out['needs_a_phone_call'], 1)

    def test_a_send_that_failed_does_not_burn_an_attempt(self):
        """A ManyChat outage must not spend a parent's three tries on nothing."""
        rec = _failed_sto()
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=NOT_SENT):
            out = send_card_update_reminders()
        self.assertEqual(out['failed'], 1)
        rec.refresh_from_db()
        self.assertEqual(rec.card_update_reminders_sent, 0)
        self.assertIsNone(rec.card_update_last_reminder_at)

    def test_an_order_that_is_not_failed_is_left_alone(self):
        _failed_sto(status='active')
        with patch('apps.customers.card_update.send_card_update_whatsapp', return_value=SENT) as send:
            send_card_update_reminders()
        send.assert_not_called()

    def test_the_cron_endpoint_needs_the_token(self):
        client = APIClient()
        self.assertEqual(client.get('/api/v1/customers/cron/card-update-reminders/').status_code, 401)


class ChildrenWithoutStandingOrderTests(TestCase):
    """The Diners hole: enrolled, paying, and no standing order behind them."""

    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(username='mgr-orph@x.com', email='mgr-orph@x.com',
                                        password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

    def _paying_child(self, first='יתום'):
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        child = TestDataFactory.create_child(family=family, first_name=first, status='active')
        LessonEnrollment.objects.create(child=child, lesson=TestDataFactory.create_lesson(), status='active')
        return child

    def test_a_paying_child_with_no_standing_order_is_listed(self):
        child = self._paying_child()
        rows = children_without_standing_order()
        self.assertIn(str(child.id), [r['child_id'] for r in rows])

    def test_a_child_with_a_standing_order_is_not(self):
        rec = _failed_sto(status='active', token='HASTOKEN')
        LessonEnrollment.objects.create(
            child=rec.child, lesson=TestDataFactory.create_lesson(), status='active',
        )
        rows = children_without_standing_order()
        self.assertNotIn(str(rec.child_id), [r['child_id'] for r in rows])

    def test_a_standing_order_with_no_token_still_counts_as_missing(self):
        """The Diners shape exactly: a row exists but nothing can be billed on it."""
        rec = _failed_sto(status='active', token='')
        LessonEnrollment.objects.create(
            child=rec.child, lesson=TestDataFactory.create_lesson(), status='active',
        )
        rows = children_without_standing_order()
        self.assertIn(str(rec.child_id), [r['child_id'] for r in rows])

    def test_the_row_carries_what_it_takes_to_call_them(self):
        self._paying_child()
        row = children_without_standing_order()[0]
        self.assertTrue(row['child_name'])
        self.assertTrue(row['parent_phone'])
        self.assertIn('days_unbilled', row)

    def test_the_endpoint_answers(self):
        self._paying_child()
        res = self.client.get('/api/v1/customers/children-without-standing-order/')
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(res.data['count'], 1)

    def test_a_worker_is_refused(self):
        worker = User.objects.create_user(username='w-orph@x.com', email='w-orph@x.com',
                                          password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=worker, defaults={'role': UserProfile.ROLE_WORKER})
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=worker).key}')
        self.assertEqual(c.get('/api/v1/customers/children-without-standing-order/').status_code, 403)
