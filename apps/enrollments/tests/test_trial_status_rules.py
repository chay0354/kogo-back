"""
A trial moves a child's status only as far as the trial itself goes (27.9.2026).

Checked against production before it was changed:

  * booking a trial wrote נרשם לניסיון over anyone — six bookings by five
    paying children took them off their own course's register, because a
    trial_signed child's regular row is hidden there;
  * cancelling a trial left the child on נרשם לניסיון — the cancelled row kept
    its date, and the date alone was read as a trial still ahead (seven
    children);
  * the 10:00 and after-trial WhatsApp read the same date, so a cancelled
    trial could still be reminded;
  * two trials booked together turned the child into ביצע ניסיון the morning
    after the first, and the second one's reminders never went out.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent, Payment
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.trial_reminders import remove_expired_trial_enrollments, send_due_trial_reminders

User = get_user_model()
JERUSALEM = ZoneInfo('Asia/Jerusalem')
TODAY = date.today()


class _Studio(TestCase):
    """A course the child is on (A) and another one they try (B)."""

    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        course_type = CourseType.objects.create(name='Dance')
        course_a = Course.objects.create(course_type=course_type, name='A', price=260, capacity=10, branch=self.branch)
        course_b = Course.objects.create(course_type=course_type, name='B', price=260, capacity=10, branch=self.branch)
        self.lesson_a = Lesson.objects.create(
            course=course_a, room=room, day_of_week=0, start_time=time(16, 0), end_time=time(17, 0),
        )
        self.lesson_b = Lesson.objects.create(
            course=course_b, room=room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0),
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(
            family=self.family, first_name='Avi', last_name='Cohen', phone='0501234567', is_primary=True,
        )
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

    def make_child(self, status):
        return Child.objects.create(
            family=self.family, first_name='Noa', last_name='Cohen',
            birth_date=date(2018, 1, 1), gender='female', status=status,
        )

    def paying_on_course_a(self, child):
        LessonEnrollment.objects.create(lesson=self.lesson_a, child=child, status='active')
        Payment.objects.create(
            child=child, family=self.family, branch=self.branch, lesson=self.lesson_a,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260'), final_amount=Decimal('260'),
        )


@patch('apps.enrollments.views.stamp_and_notify_trial_enrollment', return_value={'sent': True})
class CrmTrialBookingTest(_Studio):
    def book_trial(self, child):
        return self.client.post('/api/v1/enrollments/lesson-enrollments/', {
            'lesson': str(self.lesson_b.id), 'child': str(child.id),
            'status': 'active', 'trial_registration': True,
        }, format='json')

    def test_a_paying_child_who_books_a_trial_stays_active(self, _notify):
        child = self.make_child('active')
        self.paying_on_course_a(child)
        res = self.book_trial(child)
        self.assertEqual(res.status_code, 201, res.data)
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_a_child_with_a_card_problem_stays_a_card_problem(self, _notify):
        child = self.make_child('payment_problem')
        LessonEnrollment.objects.create(lesson=self.lesson_a, child=child, status='active')
        res = self.book_trial(child)
        self.assertEqual(res.status_code, 201, res.data)
        child.refresh_from_db()
        self.assertEqual(child.status, 'payment_problem')

    def test_a_child_who_left_and_books_a_trial_is_marked_for_it(self, _notify):
        child = self.make_child('inactive')
        res = self.book_trial(child)
        self.assertEqual(res.status_code, 201, res.data)
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')


class CancelTrialTest(_Studio):
    """The CRM's "בוטל שיעור ניסיון" ends with the status the rule gives."""

    def cancel(self, row):
        return self.client.post(
            f'/api/v1/enrollments/lesson-enrollments/{row.id}/drop-course/',
            {'cancellation_reason': 'בוטל שיעור ניסיון'}, format='json',
        )

    def trial_ahead(self, child):
        coming = TODAY + timedelta(days=5)
        return LessonEnrollment.objects.create(
            lesson=self.lesson_b, child=child, status='active', start_date=coming, trial_lesson_date=coming,
        )

    def test_a_child_whose_only_trial_was_cancelled_is_no_longer_signed_for_one(self):
        child = self.make_child('trial_signed')
        res = self.cancel(self.trial_ahead(child))
        self.assertEqual(res.status_code, 200, res.data)
        child.refresh_from_db()
        # Had something, it was cancelled, nothing left: לא פעיל.
        self.assertEqual(child.status, 'inactive')
        self.assertEqual(res.data['child_status'], 'inactive')

    def test_a_cancelled_trial_beside_an_unpaid_sign_up_leaves_the_sign_up(self):
        """Another row is still live, which used to skip the re-check altogether."""
        child = self.make_child('trial_signed')
        LessonEnrollment.objects.create(lesson=self.lesson_a, child=child, status='active')
        self.cancel(self.trial_ahead(child))
        child.refresh_from_db()
        self.assertEqual(child.status, 'pending')

    def test_cancelling_a_students_trial_leaves_them_a_student(self):
        child = self.make_child('active')
        self.paying_on_course_a(child)
        self.cancel(self.trial_ahead(child))
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')


class TrialCleanupTest(_Studio):
    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_the_first_of_two_trials_going_by_leaves_the_child_signed_for_the_second(self):
        child = self.make_child('trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson_a, child=child, status='active', trial_lesson_date=date(2026, 6, 8),
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson_b, child=child, status='active', trial_lesson_date=date(2026, 6, 10),
        )
        now = timezone.make_aware(datetime(2026, 6, 9, 1, 0), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now):
            result = remove_expired_trial_enrollments()
        self.assertEqual(result['removed'], 1)
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_the_last_trial_going_by_still_completes_it(self):
        child = self.make_child('trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson_b, child=child, status='active', trial_lesson_date=date(2026, 6, 10),
        )
        now = timezone.make_aware(datetime(2026, 6, 11, 1, 0), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now):
            remove_expired_trial_enrollments()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_completed')


class CancelledTrialReminderTest(_Studio):
    """A cancelled trial gets no WhatsApp. Nothing new is sent to anyone."""

    TRIAL_DATE = date(2026, 6, 10)

    def run_at_ten_thirty(self):
        now = timezone.make_aware(datetime(2026, 6, 10, 10, 30), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch('apps.enrollments.trial_reminders._build_send_kwargs', return_value={'parent_name': 'x'}), \
             patch('apps.enrollments.trial_reminders._send_trial_whatsapp', return_value=(True, {})) as send:
            summary = send_due_trial_reminders(dry_run=True)
        return summary, send

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_cancelled_trial_is_not_reminded(self):
        child = self.make_child('trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson_b, child=child, status='inactive',
            trial_lesson_date=self.TRIAL_DATE, end_date=date(2026, 6, 5),
        )
        summary, send = self.run_at_ten_thirty()
        self.assertEqual(summary['ten_am_sent'], 0)
        send.assert_not_called()

    @override_settings(TIME_ZONE='Asia/Jerusalem')
    def test_a_trial_still_booked_is(self):
        child = self.make_child('trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson_b, child=child, status='active', trial_lesson_date=self.TRIAL_DATE,
        )
        summary, send = self.run_at_ten_thirty()
        self.assertEqual(summary['ten_am_sent'], 1)
        self.assertEqual(send.call_count, 1)
