"""The trial's outcome once its date has passed, the office's blocked-dates calendar, and conversion."""
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.core.payment_service import enroll_child_in_paid_lessons
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonAttendance, LessonEnrollment, TrialBlockedDate
from apps.enrollments.trial_reminders import (
    blocked_trial_lesson_dates,
    configured_blocked_trial_lesson_dates,
    iter_upcoming_lesson_occurrences,
    remove_expired_trial_enrollments,
    send_due_trial_reminders,
    validate_trial_lesson_date,
)

JERUSALEM = ZoneInfo('Asia/Jerusalem')
TRIAL_DATE = date(2026, 6, 10)          # a Wednesday
DAY_AFTER = timezone.make_aware(datetime(2026, 6, 11, 1, 0), JERUSALEM)


def _studio():
    branch = Branch.objects.create(name='Main')
    room = Room.objects.create(branch=branch, name='Studio', capacity=20)
    course_type = CourseType.objects.create(name='Dance')
    course = Course.objects.create(course_type=course_type, name='Kids', price=400, capacity=10, branch=branch)
    return branch, room, course


def _lesson(course, room, *, day_of_week):
    return Lesson.objects.create(
        course=course, room=room, day_of_week=day_of_week,
        start_time=time(16, 0), end_time=time(17, 0), is_recurring=True,
    )


def _trial_child(branch, *, first_name='Trial', phone='0501234567'):
    family = Family.objects.create(name='Cohen', phone=phone, branch=branch)
    return Child.objects.create(
        family=family, first_name=first_name, last_name='Kid',
        birth_date=date(2016, 1, 1), gender='male', status='trial_signed',
    )


class TrialOutcomeOnExpiryTest(TestCase):
    def setUp(self):
        self.branch, room, course = _studio()
        self.lesson = _lesson(course, room, day_of_week=3)
        self.child = _trial_child(self.branch)
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )

    def _expire(self, *, dry_run=False):
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=DAY_AFTER):
            return remove_expired_trial_enrollments(dry_run=dry_run)

    def _mark(self, status):
        LessonAttendance.objects.create(
            lesson=self.lesson, child=self.child, occurrence_date=TRIAL_DATE, status=status,
        )

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_child_marked_present_attended_and_is_counted(self):
        self._mark('present')
        result = self._expire()
        self.enrollment.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(result['attended'], 1)
        self.assertEqual(self.enrollment.trial_outcome, 'attended')
        self.assertEqual(self.enrollment.status, 'inactive')
        self.assertEqual(self.child.status, 'trial_completed')
        self.assertEqual(self.child.trial_classes_attended, 1)

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_child_marked_absent_did_not_show(self):
        self._mark('absent')
        result = self._expire()
        self.enrollment.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(result['no_show'], 1)
        self.assertEqual(self.enrollment.trial_outcome, 'no_show')
        self.assertEqual(self.child.trial_classes_attended, 0)

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_an_unmarked_register_is_not_treated_as_an_absence(self):
        result = self._expire()
        self.enrollment.refresh_from_db()
        self.assertEqual(result['unmarked'], 1)
        self.assertEqual(self.enrollment.trial_outcome, 'unmarked')
        self.assertEqual(self.enrollment.status, 'inactive')

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_dry_run_reports_the_outcome_and_writes_nothing(self):
        self._mark('present')
        result = self._expire(dry_run=True)
        self.enrollment.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(result['attended'], 1)
        self.assertEqual(self.enrollment.status, 'active')
        self.assertEqual(self.enrollment.trial_outcome, '')
        self.assertEqual(self.child.trial_classes_attended, 0)


class BlockedDatesTest(TestCase):
    def setUp(self):
        self.branch, room, course = _studio()
        self.sunday = _lesson(course, room, day_of_week=1)   # Sundays

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='')
    def test_a_date_the_office_blocks_is_not_offered(self):
        now = timezone.make_aware(datetime(2026, 9, 1, 9, 0), JERUSALEM)
        offered_before = iter_upcoming_lesson_occurrences(self.sunday, count=3, now=now)
        TrialBlockedDate.objects.create(date=offered_before[0], reason='חג')
        offered_after = iter_upcoming_lesson_occurrences(self.sunday, count=3, now=now)
        self.assertNotIn(offered_before[0], offered_after)
        self.assertEqual(len(offered_after), 3)

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='')
    def test_a_submit_on_a_blocked_date_is_refused(self):
        now = timezone.make_aware(datetime(2026, 9, 1, 9, 0), JERUSALEM)
        first = iter_upcoming_lesson_occurrences(self.sunday, count=1, now=now)[0]
        TrialBlockedDate.objects.create(date=first)
        with self.assertRaises(ValueError):
            validate_trial_lesson_date(self.sunday, first, now=now)

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='2026-09-13')
    def test_the_office_calendar_joins_the_configured_list(self):
        TrialBlockedDate.objects.create(date=date(2026, 10, 4))
        self.assertEqual(blocked_trial_lesson_dates(), frozenset({date(2026, 9, 13), date(2026, 10, 4)}))
        self.assertEqual(configured_blocked_trial_lesson_dates(), frozenset({date(2026, 9, 13)}))

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='2026-09-13,not-a-date,2026-09-20')
    def test_a_typo_in_the_configured_list_does_not_break_every_request(self):
        self.assertEqual(
            configured_blocked_trial_lesson_dates(),
            frozenset({date(2026, 9, 13), date(2026, 9, 20)}),
        )


class BlockedDatesApiTest(TestCase):
    def setUp(self):
        self.branch, room, course = _studio()
        self.sunday = _lesson(course, room, day_of_week=1)
        User = get_user_model()
        self.manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=self.manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.manager).key}')

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='', TIME_ZONE='Asia/Jerusalem')
    def test_blocking_a_date_moves_the_trial_booked_on_it_and_says_so(self):
        now = timezone.make_aware(datetime(2026, 9, 1, 9, 0), JERUSALEM)
        first, second = iter_upcoming_lesson_occurrences(self.sunday, count=2, now=now)
        child = _trial_child(self.branch)
        enrollment = LessonEnrollment.objects.create(
            lesson=self.sunday, child=child, status='active', trial_lesson_date=first, start_date=first,
        )
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now):
            res = self.client.post('/api/v1/enrollments/trial-blocked-dates/', {'date': first.isoformat(), 'reason': 'חג'})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['moved'], 1)
        self.assertEqual(res.data['unmoved'], 0)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.trial_lesson_date, second)

    def test_the_configured_dates_are_readable_but_not_the_office_list(self):
        with override_settings(BLOCKED_TRIAL_LESSON_DATES='2026-09-13'):
            res = self.client.get('/api/v1/enrollments/trial-blocked-dates/configured/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['dates'], ['2026-09-13'])

    def test_a_date_cannot_be_blocked_twice(self):
        self.client.post('/api/v1/enrollments/trial-blocked-dates/', {'date': '2026-12-25'})
        res = self.client.post('/api/v1/enrollments/trial-blocked-dates/', {'date': '2026-12-25'})
        self.assertEqual(res.status_code, 400)


class ConversionSweepTest(TestCase):
    """A trial child who subscribes must stay on the roster and stop holding trial seats."""

    def setUp(self):
        self.branch, room, course = _studio()
        self.wednesday = _lesson(course, room, day_of_week=3)
        self.sunday = _lesson(course, room, day_of_week=1)
        self.child = _trial_child(self.branch)

    def test_subscribing_to_the_lesson_that_was_trialled_clears_the_trial_date(self):
        row = LessonEnrollment.objects.create(
            lesson=self.wednesday, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )
        enroll_child_in_paid_lessons(child=self.child, lesson=self.wednesday)
        row.refresh_from_db()
        self.assertEqual(row.status, 'active')
        self.assertIsNone(row.trial_lesson_date)

    def test_other_trial_rows_are_retired_on_conversion(self):
        other = LessonEnrollment.objects.create(
            lesson=self.sunday, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )
        enroll_child_in_paid_lessons(child=self.child, lesson=self.wednesday)
        other.refresh_from_db()
        self.assertEqual(other.status, 'inactive')
        self.assertEqual(other.end_date, TRIAL_DATE)
        paid = LessonEnrollment.objects.get(child=self.child, lesson=self.wednesday)
        self.assertEqual(paid.status, 'active')
        self.assertIsNone(paid.trial_lesson_date)


class TrialCronAuthTest(TestCase):
    """The scheduled call must be let in the way Vercel actually makes it."""

    def setUp(self):
        self.client = APIClient()

    # Vercel's secret is read from the process environment, not from settings.
    @override_settings(CRON_TOKEN='')
    def test_the_vercel_bearer_is_accepted(self):
        with patch.dict(os.environ, {'CRON_SECRET': 'night-secret'}), \
             patch('apps.enrollments.views.send_due_trial_reminders', return_value={'ok': 1}) as run:
            res = self.client.post(
                '/api/v1/enrollments/cron/trial-reminders/',
                HTTP_AUTHORIZATION='Bearer night-secret',
            )
        self.assertEqual(res.status_code, 200, res.content)
        run.assert_called_once()

    @override_settings(CRON_TOKEN='')
    def test_an_unauthenticated_call_is_turned_away(self):
        with patch.dict(os.environ, {'CRON_SECRET': 'night-secret'}), \
             patch('apps.enrollments.views.send_due_trial_reminders') as run:
            res = self.client.post('/api/v1/enrollments/cron/trial-reminders/')
        self.assertEqual(res.status_code, 401)
        run.assert_not_called()


class TrialOutcomeIsVisibleTest(TestCase):
    """The outcome lives on the retired row — the card and the register must still read it."""

    def setUp(self):
        self.branch, room, course = _studio()
        self.lesson = _lesson(course, room, day_of_week=3)
        self.child = _trial_child(self.branch)
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )
        LessonAttendance.objects.create(
            lesson=self.lesson, child=self.child, occurrence_date=TRIAL_DATE, status='present',
        )
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=DAY_AFTER):
            remove_expired_trial_enrollments()
        User = get_user_model()
        manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')

    def test_the_child_card_shows_the_finished_trial(self):
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.child.family_id)})
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.data['results']
        self.assertEqual(len(rows), 1)
        trial = rows[0]['trial_enrollment']
        self.assertIsNotNone(trial)
        self.assertEqual(trial['trial_outcome'], 'attended')
        self.assertEqual(trial['trial_lesson_date'], TRIAL_DATE.isoformat())

    def test_the_register_on_the_trial_date_still_lists_the_child(self):
        res = self.client.get(f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': TRIAL_DATE.isoformat()})
        self.assertEqual(res.status_code, 200, res.content)
        rows = [row for row in res.data['enrollments'] if row['child_id'] == str(self.child.id)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['trial_outcome'], 'attended')
        self.assertTrue(rows[0]['is_trial'])

    def test_the_register_on_another_date_does_not(self):
        res = self.client.get(f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': '2026-06-17'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([row for row in res.data['enrollments'] if row['child_id'] == str(self.child.id)], [])

    def test_a_second_run_does_not_count_the_child_twice(self):
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=DAY_AFTER):
            result = remove_expired_trial_enrollments()
        self.child.refresh_from_db()
        self.assertEqual(result['removed'], 0)
        self.assertEqual(self.child.trial_classes_attended, 1)


class ConversionRecordsTheOutcomeTest(TestCase):
    """The parent usually pays on the evening of the trial — before the cron would have written it."""

    def setUp(self):
        self.branch, room, course = _studio()
        self.wednesday = _lesson(course, room, day_of_week=3)
        self.sunday = _lesson(course, room, day_of_week=1)
        self.child = _trial_child(self.branch)

    def test_a_trial_that_took_place_is_recorded_when_the_parent_pays(self):
        row = LessonEnrollment.objects.create(
            lesson=self.wednesday, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )
        LessonAttendance.objects.create(
            lesson=self.wednesday, child=self.child, occurrence_date=TRIAL_DATE, status='present',
        )
        enroll_child_in_paid_lessons(child=self.child, lesson=self.wednesday)
        row.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(row.trial_outcome, 'attended')
        self.assertIsNone(row.trial_lesson_date)
        self.assertEqual(self.child.trial_classes_attended, 1)

    def test_a_trial_still_booked_for_a_coming_date_is_left_alone(self):
        coming = date.today() + timedelta(days=7)
        booked = LessonEnrollment.objects.create(
            lesson=self.sunday, child=self.child, status='active', trial_lesson_date=coming,
        )
        enroll_child_in_paid_lessons(child=self.child, lesson=self.wednesday)
        booked.refresh_from_db()
        self.assertEqual(booked.status, 'active')
        self.assertEqual(booked.trial_lesson_date, coming)
        self.assertEqual(booked.trial_outcome, '')


class ReminderStalenessTest(TestCase):
    """The first scheduled run after a long gap must not greet every old trial parent."""

    def setUp(self):
        self.branch, room, course = _studio()
        self.lesson = _lesson(course, room, day_of_week=3)
        self.child = _trial_child(self.branch)
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson, child=self.child, status='active', trial_lesson_date=TRIAL_DATE,
        )

    def _run(self, now):
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch('apps.enrollments.trial_reminders._build_send_kwargs', return_value={'parent_name': 'x'}), \
             patch('apps.enrollments.trial_reminders._send_trial_whatsapp', return_value=(True, {})) as send:
            summary = send_due_trial_reminders(dry_run=True)
        return summary, send

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_reminder_due_today_goes_out(self):
        summary, send = self._run(timezone.make_aware(datetime(2026, 6, 10, 10, 30), JERUSALEM))
        self.assertEqual(summary['ten_am_sent'], 1)
        self.assertEqual(send.call_count, 1)

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_reminder_from_months_ago_stays_unsent(self):
        summary, send = self._run(timezone.make_aware(datetime(2026, 9, 8, 10, 30), JERUSALEM))
        self.assertEqual(summary['ten_am_sent'], 0)
        self.assertEqual(summary['after_test_sent'], 0)
        send.assert_not_called()


class BlockedDateRulesTest(TestCase):
    def setUp(self):
        self.branch, room, course = _studio()
        self.sunday = _lesson(course, room, day_of_week=1)
        User = get_user_model()
        manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')

    def test_a_date_that_has_passed_cannot_be_blocked(self):
        res = self.client.post('/api/v1/enrollments/trial-blocked-dates/', {'date': '2026-01-04'})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(TrialBlockedDate.objects.exists())

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='', TIME_ZONE='Asia/Jerusalem')
    def test_moving_a_trial_forgets_a_stale_outcome(self):
        now = timezone.make_aware(datetime(2026, 9, 1, 9, 0), JERUSALEM)
        first, _second = iter_upcoming_lesson_occurrences(self.sunday, count=2, now=now)
        child = _trial_child(self.branch)
        enrollment = LessonEnrollment.objects.create(
            lesson=self.sunday, child=child, status='active', trial_lesson_date=first,
            start_date=first, trial_outcome='no_show',
        )
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now):
            res = self.client.post('/api/v1/enrollments/trial-blocked-dates/', {'date': first.isoformat()})
        self.assertEqual(res.status_code, 201, res.content)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.trial_outcome, '')
