"""
The weekly audit's day: it has to start by itself, survive being cut into
slices, finish, and say what it found — and the morning brief it feeds has to
exist at 9:00 even when no single request could have built it.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core import system_audit
from apps.core.models import Business, BusinessCategory, DailyBriefSnapshot, SystemAuditRun, UserProfile
from apps.core.system_audit import advance_audit, area_for_day, run_probes
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import RecurringPayment

MONDAY = date(2026, 9, 21)      # registration day
TUESDAY = date(2026, 9, 22)     # documents day


def _manager():
    user = TestDataFactory.create_user(username='audit-m@x.com', role=UserProfile.ROLE_MANAGER)
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    return user


class AdvanceAuditTests(TestCase):
    def setUp(self):
        _manager()

    def test_the_day_picks_its_own_area(self):
        run = advance_audit(budget_seconds=600, today=MONDAY)
        self.assertEqual(run.area, 'registration')
        self.assertEqual(run.area, area_for_day(MONDAY).key)

    def test_a_generous_slice_finishes_the_day_with_its_checks(self):
        run = advance_audit(budget_seconds=600, today=TUESDAY)
        self.assertIsNotNone(run.finished_at)
        self.assertTrue(run.probes_done)
        self.assertEqual(run.next_index, run.total_routes)
        self.assertTrue(run.probes)

    def test_slices_pick_up_where_the_last_one_stopped(self):
        with patch.object(system_audit, 'sweep_slice', wraps=system_audit.sweep_slice) as spy:
            first = advance_audit(budget_seconds=0, today=MONDAY)
            self.assertIsNone(first.finished_at)
            for _ in range(200):
                run = advance_audit(budget_seconds=600, today=MONDAY)
                if run.finished_at:
                    break
        starts = [call.args[1] for call in spy.call_args_list]
        self.assertEqual(starts, sorted(starts), 'a slice started behind the previous one')
        self.assertIsNotNone(run.finished_at)

    def test_a_finished_day_does_no_more_work(self):
        advance_audit(budget_seconds=600, today=TUESDAY)
        with patch.object(system_audit, 'sweep_slice') as sweep:
            advance_audit(budget_seconds=600, today=TUESDAY)
        sweep.assert_not_called()

    def test_a_second_call_steps_aside_while_the_first_holds_the_lease(self):
        run = SystemAuditRun.objects.create(
            day=MONDAY, area='registration', total_routes=10,
            lease_until=timezone.now() + timedelta(minutes=5),
        )
        with patch.object(system_audit, 'sweep_slice') as sweep:
            advance_audit(budget_seconds=600, today=MONDAY)
        sweep.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.next_index, 0)

    def test_an_expired_lease_does_not_block_the_day_forever(self):
        SystemAuditRun.objects.create(
            day=TUESDAY, area='documents', total_routes=system_audit.routes_in_area('documents'),
            lease_until=timezone.now() - timedelta(minutes=5),
        )
        run = advance_audit(budget_seconds=600, today=TUESDAY)
        self.assertIsNotNone(run.finished_at)


class ProbeTests(TestCase):
    def test_an_active_standing_order_with_no_next_date_is_red(self):
        family = TestDataFactory.create_family()
        child = TestDataFactory.create_child(family=family)
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='active', start_date=MONDAY, next_billing_date=None,
        )
        result = system_audit.probe_recurring_integrity()
        self.assertEqual(result.severity, 'red')
        self.assertEqual(len(result.rows), 1)

    def test_a_payment_link_to_an_inactive_business_is_red(self):
        from apps.payment_links.models import PaymentLink

        business = Business.objects.create(name='סגור', is_active=False)
        category = BusinessCategory.objects.create(business=business, name='כללי')
        PaymentLink.objects.create(slug='closed', title='קישור', business=business, business_category=category, is_active=True)
        self.assertEqual(system_audit.probe_payment_links().severity, 'red')

    def test_a_lesson_offered_with_no_price_is_red(self):
        lesson = TestDataFactory.create_lesson()
        course = lesson.course
        course.price = Decimal('0')
        course.show_in_widget = True
        course.is_active = True
        course.save()
        lesson.status = 'scheduled'
        lesson.lesson_price_override = None
        lesson.save()
        self.assertEqual(system_audit.probe_lessons_priced().severity, 'red')

    def test_no_active_manager_is_red(self):
        self.assertEqual(system_audit.probe_active_managers().severity, 'red')
        _manager()
        self.assertEqual(system_audit.probe_active_managers().severity, 'green')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_a_mail_setup_that_sends_nothing_is_red(self):
        self.assertEqual(system_audit.probe_email_backend().severity, 'red')

    def test_a_missing_automation_or_field_is_named(self):
        service = MagicMock()
        service.is_configured = True
        service.get_flows.return_value = [{'ns': 'content_ok'}]
        service._REGISTRATION_KINDS = {'subscription': {}, 'trial': {}}
        service.resolve_flow_for.side_effect = ['content_ok', 'content_gone']
        service.list_custom_fields.return_value = [{'name': 'kogo_parent_name'}]
        with patch('apps.core.manychat_service.ManyChatService', return_value=service):
            result = system_audit.probe_manychat_automations()
        labels = [row['label'] for row in result.rows]
        self.assertIn('אוטומציה · trial', labels)
        self.assertIn('שדה · kogo_child_name', labels)
        self.assertNotIn('אוטומציה · subscription', labels)

    def test_a_probe_that_breaks_is_a_red_finding_not_a_crash(self):
        def probe_boom():
            raise RuntimeError('boom')

        with patch.dict(system_audit.PROBES, {'staff': (probe_boom,)}):
            results = run_probes('staff')
        self.assertEqual(results[0]['severity'], 'red')
        self.assertIn('boom', results[0]['summary'])


class MorningCronTests(TestCase):
    """
    The morning brief did not exist: one request built all of it and saved it at
    the end, and the request was cut before the end. Now each call does a slice.
    """

    def setUp(self):
        _manager()

    @override_settings(CRON_TOKEN='cron-secret')
    def test_one_call_saves_what_it_finished_even_if_it_cannot_finish(self):
        from apps.core import daily_brief_views

        with patch.object(daily_brief_views, 'CRON_BRIEF_SECONDS', 0), \
                patch.object(daily_brief_views, 'CRON_AUDIT_SECONDS', 0):
            res = APIClient().get('/api/v1/core/cron/daily-brief/', HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(res.status_code, 200, res.content)
        snapshot = DailyBriefSnapshot.objects.first()
        self.assertIsNotNone(snapshot, 'a cut-short call must still leave a brief behind')
        keys = [item['key'] for item in snapshot.payload['items']]
        self.assertIn('weekly_audit', keys)

    @override_settings(CRON_TOKEN='cron-secret')
    def test_repeated_calls_complete_the_brief_and_then_stop_working(self):
        from apps.core import daily_brief_views
        from apps.core.daily_brief import check_catalogue

        with patch('apps.core.daily_brief.EXTERNAL_CHECKS', set()), \
                patch.object(daily_brief_views, 'run_check', wraps=daily_brief_views.run_check) as spy:
            for _ in range(6):
                APIClient().get('/api/v1/core/cron/daily-brief/', HTTP_X_CRON_TOKEN='cron-secret')
        items = DailyBriefSnapshot.objects.first().payload['items']
        self.assertEqual(
            {item['key'] for item in items},
            {entry['key'] for entry in check_catalogue()},
        )
        ran = [call.args[0] for call in spy.call_args_list if call.args[0] != 'weekly_audit']
        self.assertEqual(len(ran), len(set(ran)), 'a check ran twice on the same day')

    def test_the_cron_refuses_without_its_token(self):
        self.assertEqual(APIClient().get('/api/v1/core/cron/daily-brief/').status_code, 401)


class AuditEndpointTests(TestCase):
    def _client(self, role=UserProfile.ROLE_MANAGER):
        user = get_user_model().objects.create_user(username=f'{role}-a@x.com', email=f'{role}-a@x.com', password='x12345678!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_the_week_has_seven_days_ending_today(self):
        res = self._client().get('/api/v1/core/system-audit/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data['week']), 7)
        self.assertEqual(res.data['week'][-1]['area'], res.data['today'])

    def test_a_manager_can_move_today_forward(self):
        res = self._client().post('/api/v1/core/system-audit/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn(res.data['verdict'], ('running', 'green', 'yellow', 'red'))

    def test_only_a_manager_sees_it(self):
        self.assertEqual(self._client(UserProfile.ROLE_WORKER).get('/api/v1/core/system-audit/').status_code, 403)


class CutShortCheckTests(TestCase):
    """A check the platform cuts mid-way must not block every check after it."""

    @override_settings(CRON_TOKEN='cron-secret')
    def test_a_check_that_dies_leaves_a_finding_and_the_next_call_moves_on(self):
        from apps.core import daily_brief_views
        from apps.core.daily_brief import check_catalogue

        # The morning fixes run in slices and carry on by themselves (see the
        # test below); this is about an ordinary check.
        catalogue = [
            entry['key'] for entry in check_catalogue()
            if entry['key'] != 'weekly_audit' and not entry['resumable']
        ]
        doomed = catalogue[0]
        real_run_check = daily_brief_views.run_check

        class Killed(BaseException):
            """Stands in for the platform ending the request."""

        def run_check(key, **kwargs):
            if key == doomed:
                raise Killed()
            return real_run_check(key, **kwargs)

        with patch.object(daily_brief_views, 'run_check', side_effect=run_check):
            with self.assertRaises(Killed):
                daily_brief_views.run_pending_checks(budget_seconds=60)
            ran = daily_brief_views.run_pending_checks(budget_seconds=60)

        items = {item['key']: item for item in DailyBriefSnapshot.objects.first().payload['items']}
        self.assertIn('לא הספיקה להסתיים', items[doomed]['summary'])
        self.assertGreater(ran, 0, 'the next call got stuck on the same check')
        self.assertIn(catalogue[1], items)

    @override_settings(CRON_TOKEN='cron-secret')
    def test_a_morning_fix_that_dies_does_not_hold_up_the_ordinary_checks(self):
        """It is tried again on the next call, after the ordinary checks have run."""
        from apps.core import daily_brief_views
        from apps.core.daily_brief import RESUMABLE_CHECKS, check_catalogue

        real_run_check = daily_brief_views.run_check

        class Killed(BaseException):
            """Stands in for the platform ending the request."""

        def run_check(key, **kwargs):
            if key in RESUMABLE_CHECKS:
                raise Killed()
            return real_run_check(key, **kwargs)

        with patch.object(daily_brief_views, 'run_check', side_effect=run_check):
            with self.assertRaises(Killed):
                daily_brief_views.run_pending_checks(budget_seconds=600)

        items = {item['key'] for item in DailyBriefSnapshot.objects.first().payload['items']}
        ordinary = {
            entry['key'] for entry in check_catalogue()
            if entry['key'] != 'weekly_audit' and not entry['resumable']
        }
        self.assertEqual(ordinary - items, set())
        # No "started and never finished" line to stop the next call from retrying it.
        self.assertFalse(items & set(RESUMABLE_CHECKS))
