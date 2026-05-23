"""Tests for trial reminder scheduling."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.enrollments.models import LessonEnrollment
from apps.enrollments.trial_reminders import (
    _evening_send_at,
    _followup_send_at,
    compute_trial_lesson_date,
)


class TrialReminderTimingTest(TestCase):
    @override_settings(TIME_ZONE='Asia/Jerusalem', TRIAL_EVENING_REMINDER_HOUR=19)
    def test_evening_reminder_at_7pm_on_registration_day(self):
        enrollment = LessonEnrollment(
            enrolled_at=timezone.make_aware(
                datetime(2026, 5, 22, 14, 30),
                ZoneInfo('Asia/Jerusalem'),
            ),
        )
        due = _evening_send_at(enrollment)
        self.assertEqual(due.hour, 19)
        self.assertEqual(due.minute, 0)
        self.assertEqual(due.date(), date(2026, 5, 22))

    @override_settings(TIME_ZONE='Asia/Jerusalem', TRIAL_EVENING_REMINDER_HOUR=19)
    def test_evening_reminder_after_7pm_registration_sends_asap(self):
        reg = timezone.make_aware(datetime(2026, 5, 22, 20, 15), ZoneInfo('Asia/Jerusalem'))
        enrollment = LessonEnrollment(enrolled_at=reg)
        due = _evening_send_at(enrollment)
        self.assertEqual(due, reg)

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_followup_72h_after_trial_lesson_end(self):
        trial_date = date(2026, 5, 25)
        end = time(18, 30)
        due = _followup_send_at(trial_date, end)
        self.assertEqual(due.date(), date(2026, 5, 28))
        self.assertEqual(due.hour, 18)
        self.assertEqual(due.minute, 30)

    def test_compute_trial_lesson_date_uses_next_occurrence(self):
        from apps.courses.models import Lesson

        lesson = Lesson(day_of_week=0, start_time=time(16, 0), end_time=time(17, 0))
        now = timezone.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))
        # May 22 2026 is Friday; Sunday trial → May 25
        self.assertEqual(compute_trial_lesson_date(lesson, now=now), date(2026, 5, 25))
