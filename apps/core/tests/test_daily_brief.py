"""
The morning brief has one job: say the true thing about this morning.

Each check here is pinned twice — once on a morning where the problem exists,
once on a quiet morning — because a brief that cries wolf stops being read, and
a brief that stays silent through a missed charge is worse than none.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.daily_brief import (
    GREEN,
    RED,
    YELLOW,
    build_daily_brief,
    check_business_categories,
    check_expiring_cards,
    check_failed_payments,
    check_overdue_recurring,
    check_registration_only_payments,
    check_recurring_without_lesson,
    check_unresolved_charges,
)
from apps.core.models import Business, BusinessCategory, DailyBriefSnapshot, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction

TODAY = date(2026, 9, 21)


def _child(first='דני'):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    return TestDataFactory.create_child(family=family, first_name=first)


def _recurring(child, **kwargs):
    defaults = dict(
        child=child,
        amount=Decimal('260.00'),
        status='active',
        tranzila_token='tok-123',
        tranzila_recurring_index='',
        start_date=TODAY - timedelta(days=90),
        next_billing_date=TODAY,
        billing_day=1,
    )
    defaults.update(kwargs)
    return RecurringPayment.objects.create(**defaults)


class OverdueRecurringTests(TestCase):
    def test_a_charge_whose_day_passed_is_red(self):
        _recurring(_child('שלא ירד'), next_billing_date=TODAY - timedelta(days=5))
        item = check_overdue_recurring(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertEqual(item.count, 1)
        self.assertIn('שלא ירד', item.rows[0]['label'])
        self.assertIn('5', item.rows[0]['detail'])

    def test_today_is_not_late_yet(self):
        """The billing cron runs through the morning; today is not a miss."""
        _recurring(_child('היום'), next_billing_date=TODAY)
        self.assertEqual(check_overdue_recurring(TODAY).severity, GREEN)

    def test_a_cancelled_or_gateway_owned_order_is_not_chased(self):
        _recurring(_child('מבוטל'), status='cancelled', next_billing_date=TODAY - timedelta(days=9))
        _recurring(_child('טרנזילה'), tranzila_recurring_index='55', next_billing_date=TODAY - timedelta(days=9))
        item = check_overdue_recurring(TODAY)
        self.assertEqual(item.count, 0)
        self.assertIn('בזמן', item.summary)


class UnresolvedChargeTests(TestCase):
    """The billing cron stops for these. The office has to know it stopped."""

    def _attempt(self, recurring, *, successful, message='', days_ago=1):
        row = TranzilaTransaction.objects.create(
            idempotency_key=f'recurring_{recurring.id}_2026-09-{20 - days_ago:02d}',
            is_successful=successful,
            response_message=message,
            transaction_type='charge',
        )
        TranzilaTransaction.objects.filter(pk=row.pk).update(
            request_timestamp=timezone.now() - timedelta(days=days_ago)
        )
        return row

    def test_a_stopped_standing_order_is_red_and_names_the_child(self):
        recurring = _recurring(_child('תקוע'))
        self._attempt(recurring, successful=False, message='timeout')
        item = check_unresolved_charges(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertEqual(item.count, 1)
        self.assertIn('תקוע', item.rows[0]['label'])
        self.assertIn('לא תחייב את הלקוח הזה שוב', item.action)

    def test_a_successful_charge_is_not_a_stop(self):
        recurring = _recurring(_child('שולם'))
        self._attempt(recurring, successful=True)
        self.assertEqual(check_unresolved_charges(TODAY).severity, GREEN)

    def test_a_cancelled_order_is_not_chased(self):
        recurring = _recurring(_child('עזב'), status='cancelled')
        self._attempt(recurring, successful=False)
        self.assertEqual(check_unresolved_charges(TODAY).count, 0)

    def test_two_failed_attempts_on_one_order_are_one_line(self):
        recurring = _recurring(_child('פעמיים'))
        self._attempt(recurring, successful=False, days_ago=2)
        self._attempt(recurring, successful=False, days_ago=1)
        self.assertEqual(check_unresolved_charges(TODAY).count, 1)


class FailedPaymentTests(TestCase):
    def test_last_weeks_failures_are_listed(self):
        child = _child('נכשל')
        Payment.objects.create(child=child, family=child.family, base_amount=Decimal('260'), final_amount=Decimal('260'), status='failed')
        item = check_failed_payments(TODAY)
        self.assertEqual(item.severity, YELLOW)
        self.assertIn('נכשל', item.rows[0]['label'])

    def test_a_successful_charge_says_nothing(self):
        child = _child('שולם')
        Payment.objects.create(child=child, family=child.family, base_amount=Decimal('260'), final_amount=Decimal('260'), status='completed')
        self.assertEqual(check_failed_payments(TODAY).severity, GREEN)


class ExpiringCardTests(TestCase):
    def test_a_card_that_expired_is_raised(self):
        _recurring(_child('פג'), card_expire_month=8, card_expire_year=2026)
        item = check_expiring_cards(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('08/2026', item.rows[0]['detail'])

    def test_a_card_good_for_next_year_is_quiet(self):
        _recurring(_child('בתוקף'), card_expire_month=8, card_expire_year=2027)
        self.assertEqual(check_expiring_cards(TODAY).severity, GREEN)


class UnchargeableOrderTests(TestCase):
    def test_an_order_with_no_lesson_is_red(self):
        _recurring(_child('בלי שיעור'), initial_payment=None)
        item = check_recurring_without_lesson(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('מדלג', item.summary)


class RegistrationOnlyTests(TestCase):
    """260 for the course plus 120 registration, and only the 120 was taken."""

    def _payment(self, child, *, final, registration, lesson):
        return Payment.objects.create(
            child=child, family=child.family, lesson=lesson,
            base_amount=Decimal('260'), final_amount=Decimal(final),
            registration_fee=Decimal(registration), status='completed',
        )

    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()

    def test_only_the_registration_fee_was_charged(self):
        self._payment(_child('חלקי'), final='120', registration='120', lesson=self.lesson)
        item = check_registration_only_payments(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('חלקי', item.rows[0]['label'])

    def test_the_full_charge_is_quiet(self):
        self._payment(_child('מלא'), final='380', registration='120', lesson=self.lesson)
        self.assertEqual(check_registration_only_payments(TODAY).severity, GREEN)

    def test_a_charge_with_no_registration_fee_is_not_examined(self):
        self._payment(_child('ללא רישום'), final='260', registration='0', lesson=self.lesson)
        self.assertEqual(check_registration_only_payments(TODAY).count, 0)


class BusinessCategoryTests(TestCase):
    def test_a_business_with_no_category_blocks_invoices(self):
        Business.objects.create(name='עסק ללא קטגוריה')
        item = check_business_categories(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('חשבונית', item.action)

    def test_a_business_with_one_is_fine(self):
        business = Business.objects.create(name='עסק תקין')
        BusinessCategory.objects.create(business=business, name='כללי')
        self.assertEqual(check_business_categories(TODAY).severity, GREEN)


class BuildBriefTests(TestCase):
    def test_a_quiet_morning_says_so(self):
        brief = build_daily_brief(today=TODAY, include_external=False)
        self.assertEqual(brief['headline'], 'אין בעיות דחופות')
        self.assertEqual(brief['red_count'], 0)
        self.assertTrue(brief['items'])

    def test_a_missed_charge_reaches_the_headline(self):
        _recurring(_child('פספוס'), next_billing_date=TODAY - timedelta(days=3))
        brief = build_daily_brief(today=TODAY, include_external=False)
        overdue = [i for i in brief['items'] if i['key'] == 'overdue_recurring'][0]
        self.assertEqual(overdue['severity'], RED)
        self.assertGreaterEqual(brief['red_count'], 1)
        self.assertIn('דורשים טיפול היום', brief['headline'])

    def test_a_check_that_breaks_becomes_a_finding_instead_of_a_crash(self):
        def check_failed_payments(today):
            raise RuntimeError('boom')

        with patch('apps.core.daily_brief.CHECKS', (check_failed_payments,)):
            brief = build_daily_brief(today=TODAY, include_external=False)
        broken = [i for i in brief['items'] if 'נכשלה' in i['title']]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]['severity'], RED)

    def test_the_outside_services_are_left_out_of_a_quick_brief(self):
        brief = build_daily_brief(today=TODAY, include_external=False)
        keys = {item['key'] for item in brief['items']}
        self.assertNotIn('tranzila_reconciliation', keys)
        self.assertIn('overdue_recurring', keys)


class EndpointTests(TestCase):
    def _client(self, role=UserProfile.ROLE_MANAGER):
        user = get_user_model().objects.create_user(
            username=f'{role}@x.com', email=f'{role}@x.com', password='pass12345!'
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_the_screen_reads_the_stored_brief(self):
        DailyBriefSnapshot.objects.create(payload={'headline': 'אין בעיות דחופות'}, red_count=0)
        res = self._client().get('/api/v1/core/daily-brief/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['brief']['headline'], 'אין בעיות דחופות')

    def test_before_the_first_run_the_screen_gets_nothing_rather_than_an_error(self):
        res = self._client().get('/api/v1/core/daily-brief/')
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data['brief'])

    def test_a_manager_can_ask_for_a_fresh_one(self):
        res = self._client().post('/api/v1/core/daily-brief/', {'include_external': '0'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertIn('headline', res.data['brief'])
        self.assertEqual(DailyBriefSnapshot.objects.count(), 1)

    def test_only_a_manager_sees_it(self):
        res = self._client(UserProfile.ROLE_WORKER).get('/api/v1/core/daily-brief/')
        self.assertEqual(res.status_code, 403)

    def test_the_cron_refuses_without_its_token(self):
        res = APIClient().get('/api/v1/core/cron/daily-brief/')
        self.assertEqual(res.status_code, 401)
