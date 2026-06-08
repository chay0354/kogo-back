"""Tests for paying vs trial enrollment counts."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.enrollment_counts import count_paying_enrollments
from apps.enrollments.models import LessonEnrollment


User = get_user_model()


class PayingEnrollmentCountTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        self.ct = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(
            course_type=self.ct, name='Kids', price=400, capacity=10, branch=self.branch
        )
        self.lesson = Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        self.paying_child = Child.objects.create(
            family=self.family,
            first_name='Paid',
            last_name='Kid',
            birth_date=date(2015, 1, 1),
            gender='female',
            status='active',
        )
        self.trial_child = Child.objects.create(
            family=self.family,
            first_name='Trial',
            last_name='Kid',
            birth_date=date(2016, 1, 1),
            gender='male',
            status='trial_signed',
        )
        LessonEnrollment.objects.create(lesson=self.lesson, child=self.paying_child, status='active')
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.trial_child,
            status='active',
            trial_lesson_date=date(2026, 6, 10),
        )

        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_count_paying_enrollments_excludes_trial_signed(self):
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 1)

    def test_course_details_enrolled_count_excludes_trial(self):
        res = self.client.get(f'/api/v1/courses/types/{self.ct.id}/details/')
        self.assertEqual(res.status_code, 200)
        lessons = res.data['courses'][0]['lessons']
        self.assertEqual(lessons[0]['enrolled_count'], 1)
        self.assertEqual(lessons[0]['total_students_count'], 2)

    def test_trial_child_counts_after_converting_to_active(self):
        self.trial_child.status = 'active'
        self.trial_child.save(update_fields=['status'])
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 2)
