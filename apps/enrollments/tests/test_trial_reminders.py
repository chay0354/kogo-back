"""Tests for trial reminder scheduling."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.trial_reminders import (
    _after_test_send_at,
    _trial_day_10am_send_at,
    compute_trial_lesson_date,
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
