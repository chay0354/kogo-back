"""Tests for trial lesson enrollment side effects."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.models import LessonEnrollment


User = get_user_model()


class TrialLessonEnrollmentTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        self.ct = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(
            course_type=self.ct, name='Kids', price=100, capacity=10, branch=self.branch
        )
        self.lesson = Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(
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
            status='pending',
        )

        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    @patch('apps.enrollments.views.stamp_and_notify_trial_enrollment')
    def test_trial_registration_updates_status_and_returns_whatsapp(self, mock_notify):
        mock_notify.return_value = {'sent': True, 'method': 'flow'}

        res = self.client.post(
            '/api/v1/enrollments/lesson-enrollments/',
            {
                'lesson': str(self.lesson.id),
                'child': str(self.child.id),
                'status': 'active',
                'trial_registration': True,
            },
            format='json',
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'trial_signed')
        self.assertTrue(res.data.get('trial_applied'))
        self.assertTrue(LessonEnrollment.objects.filter(lesson=self.lesson, child=self.child).exists())
        self.assertEqual(res.data.get('whatsapp'), {'sent': True, 'method': 'flow'})
        mock_notify.assert_called_once()

    def test_staff_can_reschedule_trial_lesson_date(self):
        from datetime import time as dt_time

        from apps.enrollments.trial_reminders import iter_upcoming_lesson_occurrences

        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child,
            status='active',
            trial_lesson_date=date(2026, 1, 1),
        )
        self.child.status = 'trial_signed'
        self.child.save(update_fields=['status'])
        self.lesson.start_time = dt_time(16, 0)
        self.lesson.end_time = dt_time(17, 0)
        self.lesson.save(update_fields=['start_time', 'end_time'])

        dates_res = self.client.get(f'/api/v1/enrollments/lesson-enrollments/{enrollment.id}/trial-dates/')
        self.assertEqual(dates_res.status_code, 200, dates_res.data)
        upcoming = [row['date'] for row in dates_res.data['dates']]
        self.assertTrue(upcoming)
        next_date = next((d for d in upcoming if d != '2026-01-01'), upcoming[0])

        patch_res = self.client.patch(
            f'/api/v1/enrollments/lesson-enrollments/{enrollment.id}/',
            {'trial_lesson_date': next_date},
            format='json',
        )
        self.assertEqual(patch_res.status_code, 200, patch_res.data)
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.trial_lesson_date.isoformat(), next_date)

        bad = self.client.patch(
            f'/api/v1/enrollments/lesson-enrollments/{enrollment.id}/',
            {'trial_lesson_date': '2010-01-01'},
            format='json',
        )
        self.assertEqual(bad.status_code, 400)
        allowed = iter_upcoming_lesson_occurrences(self.lesson, count=8)
        self.assertNotIn(date(2010, 1, 1), allowed)
