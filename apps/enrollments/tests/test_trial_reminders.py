"""Tests for trial reminder scheduling."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Branch, Room
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.trial_reminders import (
    TRIAL_LESSON_OCCURRENCE_LIMIT,
    _after_test_send_at,
    _trial_day_10am_send_at,
    compute_trial_lesson_date,
    iter_merged_upcoming_lesson_occurrences,
    iter_upcoming_lesson_occurrences,
    remove_expired_trial_enrollments,
    validate_trial_lesson_date,
)

class TrialReminderTimingTest(TestCase):
    @override_settings(TIME_ZONE='Asia/Jerusalem', TRIAL_10AM_REMINDER_HOUR=10)
    def test_10am_reminder_on_trial_lesson_date(self):
        trial_date = date(2026, 5, 25)
        due = _trial_day_10am_send_at(trial_date)
        self.assertEqual(due.hour, 10)
        self.assertEqual(due.minute, 0)
        self.assertEqual(due.date(), trial_date)

    @override_settings(TIME_ZONE='Asia/Jerusalem', TRIAL_AFTER_TEST_HOURS=2)
    def test_after_test_2h_after_trial_lesson_end(self):
        trial_date = date(2026, 5, 25)
        end = time(18, 30)
        due = _after_test_send_at(trial_date, end)
        self.assertEqual(due.date(), trial_date)
        self.assertEqual(due.hour, 20)
        self.assertEqual(due.minute, 30)

    def test_compute_trial_lesson_date_uses_next_occurrence(self):
        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0))
        now = timezone.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))
        self.assertEqual(compute_trial_lesson_date(lesson, now=now), date(2026, 5, 25))

    def test_validate_trial_lesson_date_accepts_upcoming_occurrence(self):
        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0), is_recurring=True)
        now = timezone.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))
        upcoming = iter_upcoming_lesson_occurrences(lesson, count=1, now=now)[0]
        validate_trial_lesson_date(lesson, upcoming, now=now)

    def test_validate_trial_lesson_date_rejects_past_date(self):
        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0), is_recurring=True)
        with self.assertRaises(ValueError):
            validate_trial_lesson_date(lesson, date(2020, 1, 1))

    def test_iter_upcoming_lesson_occurrences_limits_to_three_for_widget(self):
        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0), is_recurring=True)
        now = timezone.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))
        dates = iter_upcoming_lesson_occurrences(lesson, count=TRIAL_LESSON_OCCURRENCE_LIMIT, now=now)
        self.assertEqual(len(dates), TRIAL_LESSON_OCCURRENCE_LIMIT)
        self.assertEqual((dates[1] - dates[0]).days, 7)
        self.assertEqual((dates[2] - dates[1]).days, 7)

    def test_validate_trial_lesson_date_rejects_beyond_third_occurrence(self):
        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0), is_recurring=True)
        now = timezone.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))
        fourth = iter_upcoming_lesson_occurrences(lesson, count=4, now=now)[3]
        with self.assertRaises(ValueError):
            validate_trial_lesson_date(lesson, fourth, now=now)

    def test_merged_occurrences_return_earliest_dates_across_lessons(self):
        lesson_a = Lesson(day_of_week=0, start_time=time(16, 45), end_time=time(17, 30), is_recurring=True)
        lesson_b = Lesson(day_of_week=3, start_time=time(16, 45), end_time=time(17, 30), is_recurring=True)
        now = timezone.make_aware(datetime(2026, 8, 10, 10, 0), ZoneInfo('Asia/Jerusalem'))
        merged = iter_merged_upcoming_lesson_occurrences([lesson_a, lesson_b], count=3, now=now)
        self.assertEqual(len(merged), 3)
        dates = [occurrence for _, occurrence in merged]
        self.assertEqual(dates, sorted(dates))


class RemoveExpiredTrialEnrollmentTest(TestCase):
    def setUp(self):
        branch = Branch.objects.create(name='Main')
        room = Room.objects.create(branch=branch, name='Studio', capacity=20)
        ct = CourseType.objects.create(name='Dance')
        course = Course.objects.create(
            course_type=ct, name='Kids', price=400, capacity=10, branch=branch
        )
        self.lesson = Lesson.objects.create(
            course=course,
            room=room,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
            is_recurring=True,
        )
        family = Family.objects.create(name='Cohen', phone='0501234567', branch=branch)
        self.child = Child.objects.create(
            family=family,
            first_name='Trial',
            last_name='Kid',
            birth_date=date(2016, 1, 1),
            gender='male',
            status='trial_signed',
        )
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child,
            status='active',
            trial_lesson_date=date(2026, 6, 10),
        )

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_keeps_trial_enrollment_on_trial_day(self):
        fixed_now = timezone.make_aware(
            datetime(2026, 6, 10, 22, 0),
            ZoneInfo('Asia/Jerusalem'),
        )
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=fixed_now):
            result = remove_expired_trial_enrollments()
        self.enrollment.refresh_from_db()
        self.assertEqual(result['removed'], 0)
        self.assertEqual(self.enrollment.status, 'active')

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_removes_trial_enrollment_day_after_trial(self):
        fixed_now = timezone.make_aware(
            datetime(2026, 6, 11, 1, 0),
            ZoneInfo('Asia/Jerusalem'),
        )
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=fixed_now):
            result = remove_expired_trial_enrollments()
        self.enrollment.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(result['removed'], 1)
        self.assertEqual(self.enrollment.status, 'inactive')
        self.assertEqual(self.child.status, 'trial_completed')
