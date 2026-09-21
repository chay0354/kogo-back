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
    check_active_without_standing_order,
    check_duplicate_charges,
    check_ended_standing_orders,
    check_overdue_instalments,
    check_overdue_recurring,
    check_refunds,
    check_registration_only_payments,
    check_revenue_drop,
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
    """
    Registration and course are two charges here, not one.

    A fee on its own is the normal shape of a sign-up — the course follows on
    the monthly run — so a lone fee must raise nothing. 307 families were
    reported this way before the check learned the difference.
    """

    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()

    def _fee(self, child, amount='120', days_ago=0):
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=self.lesson,
            base_amount=Decimal(amount), final_amount=Decimal(amount),
            registration_fee=Decimal(amount), status='completed',
        )
        if days_ago:
            Payment.objects.filter(pk=payment.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return payment

    def _course_charge(self, child, amount='225', payment_type='recurring_subscription'):
        return Payment.objects.create(
            child=child, family=child.family, lesson=self.lesson,
            base_amount=Decimal(amount), final_amount=Decimal(amount),
            registration_fee=Decimal('0'), status='completed', payment_type=payment_type,
        )

    def test_a_fee_with_the_course_charged_separately_is_not_a_problem(self):
        """לני עוזר: 120 registration on 31/8, then 225 for the month on 2/9."""
        child = _child('לני')
        self._fee(child, days_ago=21)
        self._course_charge(child)
        item = check_registration_only_payments(TODAY)
        self.assertEqual(item.severity, GREEN)
        self.assertEqual(item.count, 0)

    def test_a_fee_with_a_standing_order_waiting_is_not_a_problem(self):
        child = _child('נרשם עכשיו')
        self._fee(child)
        _recurring(child)
        self.assertEqual(check_registration_only_payments(TODAY).count, 0)

    def test_a_fee_with_nothing_behind_it_is_raised(self):
        child = _child('רק רישום')
        self._fee(child)
        item = check_registration_only_payments(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertEqual(item.count, 1)
        self.assertIn('רק רישום', item.rows[0]['label'])
        self.assertIn('אין חיוב על החוג', item.rows[0]['detail'])

    def test_a_cancelled_standing_order_is_not_cover(self):
        child = _child('ביטל')
        self._fee(child)
        _recurring(child, status='cancelled')
        self.assertEqual(check_registration_only_payments(TODAY).count, 1)

    def test_a_charge_with_no_registration_fee_is_not_examined(self):
        child = _child('ללא רישום')
        self._course_charge(child, payment_type='one_time')
        self.assertEqual(check_registration_only_payments(TODAY).count, 0)

    def test_one_siblings_course_charge_does_not_cover_the_other(self):
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        paid = TestDataFactory.create_child(family=family, first_name='לורן')
        unpaid = TestDataFactory.create_child(family=family, first_name='לני')
        self._fee(paid)
        self._course_charge(paid)
        self._fee(unpaid)
        item = check_registration_only_payments(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('לני', item.rows[0]['label'])


class DuplicateChargeTests(TestCase):
    def _paid(self, child, amount='260'):
        return Payment.objects.create(
            child=child, family=child.family, base_amount=Decimal(amount),
            final_amount=Decimal(amount), status='completed',
        )

    def test_the_same_child_charged_twice_the_same_day(self):
        child = _child('כפול')
        self._paid(child)
        self._paid(child)
        item = check_duplicate_charges(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('2 פעמים', item.rows[0]['detail'])

    def test_two_different_amounts_are_not_a_duplicate(self):
        child = _child('שונה')
        self._paid(child, '260')
        self._paid(child, '120')
        self.assertEqual(check_duplicate_charges(TODAY).severity, GREEN)


class RevenueTests(TestCase):
    def _paid_on(self, day, amount):
        child = _child('הכנסה')
        payment = Payment.objects.create(
            child=child, family=child.family, base_amount=Decimal(amount),
            final_amount=Decimal(amount), status='completed',
        )
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.make_aware(timezone.datetime(day.year, day.month, day.day, 12, 0))
        )

    def test_a_day_with_no_takings_against_a_normal_week_is_red(self):
        yesterday = TODAY - timedelta(days=1)
        for week in range(1, 5):
            self._paid_on(yesterday - timedelta(days=7 * week), '1000')
        item = check_revenue_drop(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('לא נכנס כסף', item.rows[0]['label'])

    def test_a_normal_day_is_quiet(self):
        yesterday = TODAY - timedelta(days=1)
        self._paid_on(yesterday, '1000')
        for week in range(1, 5):
            self._paid_on(yesterday - timedelta(days=7 * week), '1000')
        self.assertEqual(check_revenue_drop(TODAY).severity, GREEN)

    def test_without_history_it_does_not_guess(self):
        item = check_revenue_drop(TODAY)
        self.assertEqual(item.severity, GREEN)
        self.assertIn('אין מספיק היסטוריה', item.summary)


class RefundTests(TestCase):
    def test_refunds_are_listed_with_their_total(self):
        child = _child('זוכה')
        Payment.objects.create(
            child=child, family=child.family, base_amount=Decimal('260'),
            final_amount=Decimal('260'), status='refunded',
        )
        item = check_refunds(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('260', item.summary)


class ActiveWithoutOrderTests(TestCase):
    def test_an_active_child_with_nothing_to_charge_is_raised(self):
        TestDataFactory.create_child(family=TestDataFactory.create_family(), first_name='בלי קבע', status='active')
        item = check_active_without_standing_order(TODAY)
        self.assertEqual(item.count, 1)

    def test_a_child_with_a_standing_order_is_quiet(self):
        child = TestDataFactory.create_child(
            family=TestDataFactory.create_family(), first_name='עם קבע', status='active'
        )
        _recurring(child)
        self.assertEqual(check_active_without_standing_order(TODAY).severity, GREEN)


class EndedOrderTests(TestCase):
    def test_an_order_past_its_end_date_still_charging(self):
        _recurring(_child('נגמר'), end_date=TODAY - timedelta(days=2))
        item = check_ended_standing_orders(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('ימשיך להיות מחויב', item.action)

    def test_an_open_ended_order_is_fine(self):
        _recurring(_child('פתוח'), end_date=None)
        self.assertEqual(check_ended_standing_orders(TODAY).severity, GREEN)


class StatusMismatchTests(TestCase):
    """
    Only the half the office can act on this morning.

    Statuses drift in every direction and a list of every disagreement is too
    long to be read; what matters is a child whose money is coming in, or whose
    place is taken, who still reads as "ניסיון".
    """

    def _child_with(self, name, status):
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        return TestDataFactory.create_child(family=family, first_name=name, status=status)

    def test_a_trial_child_with_a_live_standing_order_is_raised(self):
        from apps.core.daily_brief import check_status_mismatch

        child = self._child_with('עבר לחוג', 'trial_completed')
        _recurring(child)
        Payment.objects.create(
            child=child, family=child.family, base_amount=Decimal('225'),
            final_amount=Decimal('225'), status='completed',
        )
        item = check_status_mismatch(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('עבר לחוג', item.rows[0]['label'])

    def test_a_child_who_really_is_only_a_trial_is_left_alone(self):
        from apps.core.daily_brief import check_status_mismatch

        self._child_with('רק ניסיון', 'trial_signed')
        self.assertEqual(check_status_mismatch(TODAY).count, 0)

    def test_a_child_marked_active_is_not_second_guessed(self):
        from apps.core.daily_brief import check_status_mismatch

        self._child_with('פעיל', 'active')
        self.assertEqual(check_status_mismatch(TODAY).count, 0)


class DocumentNumberingTests(TestCase):
    def test_a_working_series_reports_the_next_number(self):
        from apps.core.daily_brief import check_document_numbering

        with patch('apps.documents.missing_receipts.next_receipt_number', return_value='2026-0042'):
            item = check_document_numbering(TODAY)
        self.assertEqual(item.severity, GREEN)
        self.assertIn('2026-0042', item.summary)

    def test_numbering_that_cannot_answer_is_red_before_a_receipt_fails(self):
        from apps.core.daily_brief import check_document_numbering

        with patch('apps.documents.missing_receipts.next_receipt_number', side_effect=RuntimeError('אין סדרה')):
            item = check_document_numbering(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertIn('הפקת מסמכים תיכשל', item.summary)


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

    def test_each_check_reports_how_long_it_took(self):
        brief = build_daily_brief(today=TODAY, include_external=False)
        self.assertTrue(all('duration_ms' in item for item in brief['items']))

    def test_when_the_time_runs_out_the_brief_says_what_was_not_checked(self):
        """Better an honest gap than a screen that hangs on a request nobody answers."""
        with patch('apps.core.daily_brief.TIME_BUDGET_SECONDS', -1):
            brief = build_daily_brief(today=TODAY, include_external=False)
        skipped = [i for i in brief['items'] if i['key'] == 'skipped_checks']
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['severity'], YELLOW)
        self.assertIn('לא נבדק', skipped[0]['rows'][0]['detail'])

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


class PerCheckEndpointTests(TestCase):
    """
    The screen runs the checks one at a time.

    One request per check is what survives a hosting platform's limit on how
    long a request may take — and an interrupted run keeps whatever it got.
    """

    def _client(self, role=UserProfile.ROLE_MANAGER):
        user = get_user_model().objects.create_user(
            username=f'{role}-check@x.com', email=f'{role}-check@x.com', password='pass12345!'
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_the_screen_is_told_which_checks_to_run_and_which_are_slow(self):
        res = self._client().get('/api/v1/core/daily-brief/check/')
        self.assertEqual(res.status_code, 200)
        keys = [c['key'] for c in res.data['checks']]
        self.assertIn('overdue_recurring', keys)
        external = {c['key'] for c in res.data['checks'] if c['external']}
        self.assertIn('tranzila_reconciliation', external)
        # Cheap first, the outside services last.
        self.assertLess(keys.index('overdue_recurring'), keys.index('tranzila_reconciliation'))

    def test_one_check_runs_and_is_kept(self):
        client = self._client()
        res = client.post('/api/v1/core/daily-brief/check/', {'key': 'overdue_recurring'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['item']['key'], 'overdue_recurring')
        self.assertIn('duration_ms', res.data['item'])
        self.assertEqual(DailyBriefSnapshot.objects.count(), 1)

    def test_a_second_check_joins_the_first_instead_of_replacing_it(self):
        client = self._client()
        client.post('/api/v1/core/daily-brief/check/', {'key': 'overdue_recurring'}, format='json')
        res = client.post('/api/v1/core/daily-brief/check/', {'key': 'failed_payments'}, format='json')
        keys = [i['key'] for i in res.data['brief']['items']]
        self.assertEqual(keys, ['overdue_recurring', 'failed_payments'])
        self.assertEqual(DailyBriefSnapshot.objects.count(), 1)

    def test_running_the_same_check_again_replaces_its_answer(self):
        client = self._client()
        client.post('/api/v1/core/daily-brief/check/', {'key': 'overdue_recurring'}, format='json')
        _recurring(_child('פספוס'), next_billing_date=date.today() - timedelta(days=4))
        res = client.post('/api/v1/core/daily-brief/check/', {'key': 'overdue_recurring'}, format='json')
        items = res.data['brief']['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['severity'], RED)
        self.assertEqual(res.data['brief']['red_count'], 1)

    def test_an_unknown_check_is_refused_in_words(self):
        res = self._client().post('/api/v1/core/daily-brief/check/', {'key': 'nonsense'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('nonsense', res.data['error'])

    def test_only_a_manager_may_run_checks(self):
        res = self._client(UserProfile.ROLE_WORKER).post(
            '/api/v1/core/daily-brief/check/', {'key': 'overdue_recurring'}, format='json'
        )
        self.assertEqual(res.status_code, 403)

    def test_every_check_in_the_catalogue_can_actually_run(self):
        """A name on the list that cannot run would hang the screen on it."""
        from apps.core.daily_brief import check_catalogue, run_check

        for entry in check_catalogue():
            if entry['external']:
                continue
            item = run_check(entry['key'], today=TODAY)
            self.assertEqual(item['key'], entry['key'])
            self.assertIn(item['severity'], (RED, YELLOW, GREEN))


class NoiseControlTests(TestCase):
    """
    What each check must NOT say.

    Every rule here was written after a real false alarm, or after reading the
    code and finding the assumption behind a rule was wrong. A check that cries
    wolf is worse than no check: the office stops reading the brief.
    """

    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()

    def _paid(self, child, amount='225', lesson=None, days_ago=0, status='completed'):
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=lesson,
            base_amount=Decimal(amount), final_amount=Decimal(amount), status=status,
        )
        if days_ago:
            Payment.objects.filter(pk=payment.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago)
            )
        return payment

    def test_one_day_late_is_not_late_because_billing_runs_in_batches(self):
        """The cron charges 40 at a time; a busy first-of-month honestly waits."""
        _recurring(_child('אתמול'), next_billing_date=TODAY - timedelta(days=1))
        self.assertEqual(check_overdue_recurring(TODAY).count, 0)

    def test_an_order_reported_as_stuck_is_not_also_reported_as_late(self):
        recurring = _recurring(_child('תקוע'), next_billing_date=TODAY - timedelta(days=10))
        TranzilaTransaction.objects.create(
            idempotency_key=f'recurring_{recurring.id}_2026-09-01',
            is_successful=False, transaction_type='charge',
        )
        self.assertEqual(check_overdue_recurring(TODAY).count, 0)
        self.assertEqual(check_unresolved_charges(TODAY).count, 1)

    def test_a_failure_the_parent_already_fixed_is_not_this_mornings_news(self):
        child = _child('שילם אחרי')
        self._paid(child, status='failed')
        self._paid(child)
        self.assertEqual(check_failed_payments(TODAY).count, 0)

    def test_two_courses_at_the_same_price_on_one_day_are_not_a_duplicate(self):
        child = _child('שני חוגים')
        other_lesson = TestDataFactory.create_lesson()
        self._paid(child, lesson=self.lesson)
        self._paid(child, lesson=other_lesson)
        self.assertEqual(check_duplicate_charges(TODAY).count, 0)

    def test_the_same_course_charged_twice_in_a_day_still_is(self):
        child = _child('כפול אמיתי')
        self._paid(child, lesson=self.lesson)
        self._paid(child, lesson=self.lesson)
        self.assertEqual(check_duplicate_charges(TODAY).count, 1)

    def test_a_quiet_day_is_not_a_finding_when_quiet_days_are_normal(self):
        """Most days here take nothing: the month's charges land on one day."""
        yesterday = TODAY - timedelta(days=1)
        child = _child('לפני שבוע')
        payment = self._paid(child, amount='1000')
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.make_aware(timezone.datetime(
                (yesterday - timedelta(days=7)).year,
                (yesterday - timedelta(days=7)).month,
                (yesterday - timedelta(days=7)).day, 12, 0,
            ))
        )
        item = check_revenue_drop(TODAY)
        self.assertEqual(item.severity, GREEN)

    def test_a_child_paying_in_cash_is_not_a_child_nobody_charges(self):
        from apps.documents.models import CashPlan

        child = TestDataFactory.create_child(
            family=TestDataFactory.create_family(), first_name='מזומן', status='active'
        )
        CashPlan.objects.create(
            child=child, lesson=self.lesson, status='active',
            total_amount=Decimal('2250'), monthly_amount=Decimal('225'),
        )
        self.assertEqual(check_active_without_standing_order(TODAY).count, 0)

    def test_a_child_charged_last_month_is_not_a_child_nobody_charges(self):
        child = TestDataFactory.create_child(
            family=TestDataFactory.create_family(), first_name='חויב', status='active'
        )
        self._paid(child, days_ago=20)
        self.assertEqual(check_active_without_standing_order(TODAY).count, 0)

    def test_a_child_with_nothing_at_all_still_shows(self):
        TestDataFactory.create_child(
            family=TestDataFactory.create_family(), first_name='לא נגבה', status='active'
        )
        self.assertEqual(check_active_without_standing_order(TODAY).count, 1)


class InstalmentNoiseTests(TestCase):
    """
    The billing cron issues these itself, for active plans, in batches.

    So a document a day late is the batch doing its work, and an instalment on
    a cancelled plan is not waiting for anybody.
    """

    def setUp(self):
        from apps.documents.models import CashPlan

        self.lesson = TestDataFactory.create_lesson()
        self.child = _child('מזומן')
        self.plan = CashPlan.objects.create(
            child=self.child, lesson=self.lesson, status='active',
            total_amount=Decimal('2250'), monthly_amount=Decimal('225'),
        )

    def _month(self, days_ago, status='pending', plan=None):
        from apps.documents.models import CashPlanMonth

        return CashPlanMonth.objects.create(
            plan=plan or self.plan, due_date=TODAY - timedelta(days=days_ago),
            amount=Decimal('225'), status=status,
        )

    def test_yesterdays_instalment_is_the_batch_not_a_backlog(self):
        self._month(1)
        self.assertEqual(check_overdue_instalments(TODAY).count, 0)

    def test_an_instalment_a_week_old_is_a_backlog(self):
        self._month(7)
        item = check_overdue_instalments(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('מזומן', item.rows[0]['label'])

    def test_a_cancelled_plan_is_not_waiting_for_anything(self):
        from apps.documents.models import CashPlan

        cancelled = CashPlan.objects.create(
            child=self.child, lesson=self.lesson, status='cancelled',
            total_amount=Decimal('2250'), monthly_amount=Decimal('225'),
        )
        self._month(30, plan=cancelled)
        self.assertEqual(check_overdue_instalments(TODAY).count, 0)

    def test_an_issued_instalment_is_done(self):
        self._month(30, status='invoiced')
        self.assertEqual(check_overdue_instalments(TODAY).count, 0)
