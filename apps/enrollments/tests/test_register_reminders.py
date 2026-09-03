from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Branch, City, Room
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonAttendance, LessonEnrollment, RegisterReminder
from apps.enrollments.register_reminders import (
    LESSON_REMINDER_MAX_PER_DAY,
    instructor_register_gaps,
    missing_registers,
    send_due_register_reminders,
)
from apps.instructors.models import Instructor
from apps.scheduling.models import LessonCancellation

MONDAY = date(2026, 9, 7)


class RegisterReminderTestBase(TestCase):
    def setUp(self):
        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='מרכז העיר', city=city)
        self.room = Room.objects.create(name='אולם', branch=self.branch, capacity=20)
        course_type = CourseType.objects.create(name='קפוארה')
        self.instructor = Instructor.objects.create(
            first_name='עידו', last_name='לוי', phone='0501234567', primary_branch=self.branch,
        )
        self.course = Course.objects.create(
            name='קפוארה ואקרובטיקה', course_type=course_type, branch=self.branch,
            price=300, capacity=20,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, room=self.room, instructor=self.instructor,
            day_of_week=1, start_time='16:00', end_time='16:45', is_recurring=True,
        )

    def enrol(self, name='דניאל'):
        family = Family.objects.create(name='כהן', phone='0522659322', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2016, 5, 1), gender='male', status='active',
        )
        return LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active')

    def mark(self, child, day=MONDAY, status='present'):
        return LessonAttendance.objects.create(
            lesson=self.lesson, child=child, occurrence_date=day, status=status,
        )


class MissingRegisterTests(RegisterReminderTestBase):
    def test_an_unmarked_lesson_is_reported(self):
        self.enrol()
        rows = missing_registers(MONDAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].lesson, self.lesson)
        self.assertEqual(rows[0].roster, 1)
        self.assertEqual(rows[0].marked, 0)

    def test_a_finished_register_is_not_reported(self):
        enrollment = self.enrol()
        self.mark(enrollment.child)
        self.assertEqual(missing_registers(MONDAY), [])

    def test_a_half_finished_register_is_still_open(self):
        first = self.enrol()
        self.enrol('נועה')
        self.mark(first.child)
        rows = missing_registers(MONDAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].roster, rows[0].marked), (2, 1))

    def test_a_lesson_with_nobody_on_it_is_not_a_gap(self):
        self.assertEqual(missing_registers(MONDAY), [])

    def test_a_cancelled_occurrence_is_not_a_gap(self):
        self.enrol()
        LessonCancellation.objects.create(lesson=self.lesson, occurrence_date=MONDAY, reason='חג')
        self.assertEqual(missing_registers(MONDAY), [])

    def test_a_lesson_that_does_not_meet_that_day_is_not_a_gap(self):
        self.enrol()
        self.assertEqual(missing_registers(MONDAY + timedelta(days=1)), [])

    def test_a_lesson_that_had_not_started_yet_is_not_a_gap(self):
        self.enrol()
        self.lesson.lesson_date = MONDAY + timedelta(days=7)
        self.lesson.save(update_fields=['lesson_date'])
        self.assertEqual(missing_registers(MONDAY), [])


class RegisterGapReportTests(RegisterReminderTestBase):
    def test_the_office_view_groups_open_registers_by_instructor(self):
        self.enrol()
        self.lesson.lesson_date = MONDAY
        self.lesson.save(update_fields=['lesson_date'])
        with patch('apps.enrollments.register_reminders.timezone.localdate', return_value=MONDAY + timedelta(days=3)):
            rows = instructor_register_gaps(days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['instructor_name'], 'עידו לוי')
        self.assertEqual(rows[0]['open_count'], 1)
        self.assertEqual(rows[0]['oldest_days_open'], 3)
        self.assertTrue(rows[0]['needs_attention'])

    def test_a_gap_from_today_is_not_in_the_office_view_yet(self):
        self.enrol()
        # The lesson starts today, so today's is the only occurrence there is.
        self.lesson.lesson_date = MONDAY
        self.lesson.save(update_fields=['lesson_date'])
        with patch('apps.enrollments.register_reminders.timezone.localdate', return_value=MONDAY):
            self.assertEqual(instructor_register_gaps(days=7), [])


@override_settings(MANYCHAT_REGISTER_LESSON_FLOW_NS='', MANYCHAT_REGISTER_MORNING_FLOW_NS='')
class ReminderSendingTests(RegisterReminderTestBase):
    def _now(self, hour, minute=0, day=MONDAY):
        return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute))

    def test_nothing_is_sent_before_the_lesson_ends(self):
        self.enrol()
        with patch('apps.enrollments.register_reminders._send') as send:
            summary = send_due_register_reminders(now=self._now(16, 30))
        send.assert_not_called()
        self.assertEqual(summary['lesson_sent'], 0)

    def test_the_reminder_goes_five_minutes_after_the_lesson(self):
        self.enrol()
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            too_soon = send_due_register_reminders(now=self._now(16, 48))
            due = send_due_register_reminders(now=self._now(16, 51))
        self.assertEqual(too_soon['lesson_sent'], 0)
        self.assertEqual(due['lesson_sent'], 1)
        fields = send.call_args.kwargs['fields']
        self.assertIn('קפוארה ואקרובטיקה', fields['kogo_lesson_name'])

    def test_it_is_said_again_every_hour_while_the_register_is_open(self):
        self.enrol()
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            send_due_register_reminders(now=self._now(16, 51))
            same_hour = send_due_register_reminders(now=self._now(17, 20))
            next_hour = send_due_register_reminders(now=self._now(17, 55))
        self.assertEqual(same_hour['lesson_sent'], 0)
        self.assertEqual(next_hour['lesson_sent'], 1)
        self.assertEqual(send.call_count, 2)

    def test_the_asking_stops_when_the_day_quota_is_spent(self):
        self.enrol()
        # Every ten minutes from the moment it is due until the day is over —
        # far more runs than the quota, which is the point of the quota. The
        # quota is lowered here so the test is about the rule and not about how
        # many hours happen to be left after a 16:45 lesson.
        with patch('apps.enrollments.register_reminders.LESSON_REMINDER_MAX_PER_DAY', 3), \
             patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            for minutes in range(0, 7 * 60, 10):
                send_due_register_reminders(now=self._now(16, 51) + timedelta(minutes=minutes))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(RegisterReminder.objects.filter(kind=RegisterReminder.KIND_LESSON).count(), 3)

    def test_an_afternoon_lesson_is_asked_about_once_an_hour_until_the_day_ends(self):
        self.enrol()
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            for minutes in range(0, 7 * 60, 10):
                send_due_register_reminders(now=self._now(16, 51) + timedelta(minutes=minutes))
        # 16:51 through 23:51 — one an hour, and never more than the day's quota.
        self.assertEqual(send.call_count, 7)
        self.assertLessEqual(send.call_count, LESSON_REMINDER_MAX_PER_DAY)

    def test_marking_the_register_stops_the_asking(self):
        enrollment = self.enrol()
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            send_due_register_reminders(now=self._now(16, 51))
            self.mark(enrollment.child)
            send_due_register_reminders(now=self._now(18, 0))
        self.assertEqual(send.call_count, 1)

    def test_a_register_finished_in_time_gets_no_reminder(self):
        enrollment = self.enrol()
        self.mark(enrollment.child)
        with patch('apps.enrollments.register_reminders._send') as send:
            send_due_register_reminders(now=self._now(18, 0))
        send.assert_not_called()

    def test_the_morning_summary_lists_yesterday_and_goes_once(self):
        self.enrol()
        morning = self._now(8, 5, day=MONDAY + timedelta(days=1))
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            first = send_due_register_reminders(now=morning)
            second = send_due_register_reminders(now=self._now(9, 5, day=MONDAY + timedelta(days=1)))
        self.assertEqual(first['morning_sent'], 1)
        self.assertEqual(second['morning_sent'], 0)
        fields = send.call_args.kwargs['fields']
        self.assertEqual(fields['kogo_missing_count'], '1')
        self.assertIn('קפוארה ואקרובטיקה', fields['kogo_missing_lessons'])

    def test_the_morning_summary_waits_for_eight(self):
        self.enrol()
        with patch('apps.enrollments.register_reminders._send') as send:
            summary = send_due_register_reminders(now=self._now(6, 30, day=MONDAY + timedelta(days=1)))
        send.assert_not_called()
        self.assertEqual(summary['morning_sent'], 0)

    def test_without_a_manychat_flow_nothing_leaves_and_nothing_is_recorded(self):
        self.enrol()
        summary = send_due_register_reminders(now=self._now(18, 0))
        self.assertEqual(summary['lesson_sent'], 0)
        self.assertEqual(summary['skipped'], 1)
        self.assertFalse(RegisterReminder.objects.exists())
