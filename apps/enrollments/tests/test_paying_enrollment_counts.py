"""Tests for paying vs trial enrollment counts."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.enrollment_counts import count_capacity_enrollments, count_paying_enrollments
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

    def test_count_capacity_includes_trial_on_trial_date_only(self):
        trial_date = date(2026, 6, 10)
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=trial_date),
            2,
        )
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=date(2026, 6, 17)),
            1,
        )
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)

    def test_schedule_occurrence_returns_roster_and_trial_counts(self):
        trial_date = date(2026, 6, 10)
        # Wednesday: matches the requested occurrence, independent of today's date.
        self.lesson.day_of_week = 3
        self.lesson.save(update_fields=['day_of_week'])

        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': trial_date.isoformat(), 'end_date': trial_date.isoformat()},
        )

        self.assertEqual(res.status_code, 200, res.data)
        row = next(item for item in res.data if item['id'] == str(self.lesson.id))
        self.assertEqual(row['enrollment_count'], 2)
        self.assertEqual(row['student_count'], 2)
        self.assertEqual(row['trial_student_count'], 1)

    def test_course_enrollment_count_same_across_lessons_includes_trial(self):
        lesson_two = Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=2,
            start_time='18:00',
            end_time='19:00',
        )
        res = self.client.get(f'/api/v1/courses/types/{self.ct.id}/details/')
        self.assertEqual(res.status_code, 200)
        course = res.data['courses'][0]
        self.assertEqual(course['course_enrollment_count'], 2)
        counts_by_lesson = {l['id']: l['total_students_count'] for l in course['lessons']}
        self.assertEqual(counts_by_lesson[str(self.lesson.id)], 2)
        self.assertEqual(counts_by_lesson[str(lesson_two.id)], 0)
