"""Tests for didnt_arrive WhatsApp after 3 consecutive non-present marks."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.attendance_whatsapp import consecutive_non_present_count
from apps.enrollments.models import LessonAttendance, LessonEnrollment


User = get_user_model()


class DidntArriveWhatsAppTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        self.ct = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(
            course_type=self.ct, name='Kids', price=100, capacity=10, branch=self.branch
        )
        self.lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
            is_recurring=True,
            lesson_date=date(2026, 5, 1),
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        self.parent = Parent.objects.create(
            family=self.family,
            first_name='Avi',
            last_name='Cohen',
            phone='0501234567',
            is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family,
            first_name='Noa',
            last_name='Cohen',
            birth_date=date(2015, 1, 1),
            gender='female',
        )
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson, child=self.child, status='active'
        )

        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.course.managers.add(user)
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _mark(self, occ_date: date, status: str):
        return self.client.post(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/mark_attendance/',
            {'date': occ_date.isoformat(), 'attendance': [{'child_id': str(self.child.id), 'status': status}]},
            format='json',
        )

    def test_streak_counts_consecutive_non_present(self):
        for d in (date(2026, 5, 1), date(2026, 5, 8), date(2026, 5, 15)):
            LessonAttendance.objects.create(
                lesson=self.lesson, child=self.child, occurrence_date=d, status='absent'
            )
        self.assertEqual(
            consecutive_non_present_count(child_id=self.child.id, lesson_id=self.lesson.id),
            3,
        )

    def test_present_breaks_streak(self):
        LessonAttendance.objects.create(
            lesson=self.lesson, child=self.child, occurrence_date=date(2026, 5, 15), status='absent'
        )
        LessonAttendance.objects.create(
            lesson=self.lesson, child=self.child, occurrence_date=date(2026, 5, 8), status='present'
        )
        LessonAttendance.objects.create(
            lesson=self.lesson, child=self.child, occurrence_date=date(2026, 5, 1), status='absent'
        )
        self.assertEqual(
            consecutive_non_present_count(child_id=self.child.id, lesson_id=self.lesson.id),
            1,
        )

    @patch('apps.core.manychat_service.ManyChatService.notify_registration')
    def test_sends_on_third_consecutive_absence(self, mock_notify):
        mock_notify.return_value = {'sent': True, 'method': 'flow'}

        self._mark(date(2026, 5, 1), 'absent')
        self._mark(date(2026, 5, 8), 'absent')
        self._mark(date(2026, 5, 15), 'absent')

        self.assertEqual(mock_notify.call_count, 1)
        call_kwargs = mock_notify.call_args.kwargs
        from apps.core.manychat_service import ManyChatService
        self.assertEqual(call_kwargs['kind'], ManyChatService.REGISTRATION_KIND_DIDNT_ARRIVE)
        self.enrollment.refresh_from_db()
        self.assertIsNotNone(self.enrollment.didnt_arrive_whatsapp_sent_at)

    @patch('apps.core.manychat_service.ManyChatService.notify_registration')
    def test_does_not_send_twice_same_streak(self, mock_notify):
        mock_notify.return_value = {'sent': True, 'method': 'flow'}

        for d in (date(2026, 5, 1), date(2026, 5, 8), date(2026, 5, 15)):
            self._mark(d, 'absent')
        self._mark(date(2026, 5, 22), 'absent')

        self.assertEqual(mock_notify.call_count, 1)

    @patch('apps.core.manychat_service.ManyChatService.notify_registration')
    def test_present_clears_flag_and_allows_resend_after_new_streak(self, mock_notify):
        mock_notify.return_value = {'sent': True, 'method': 'flow'}

        self._mark(date(2026, 5, 1), 'absent')
        self._mark(date(2026, 5, 8), 'absent')
        self._mark(date(2026, 5, 15), 'absent')
        self.assertEqual(mock_notify.call_count, 1)

        self._mark(date(2026, 5, 22), 'present')
        self.enrollment.refresh_from_db()
        self.assertIsNone(self.enrollment.didnt_arrive_whatsapp_sent_at)

        self._mark(date(2026, 5, 29), 'absent')
        self._mark(date(2026, 6, 5), 'absent')
        self._mark(date(2026, 6, 12), 'absent')
        self.assertEqual(mock_notify.call_count, 2)
