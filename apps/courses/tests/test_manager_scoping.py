"""Tests for course visibility scoping."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.courses.models import CourseType, Course, Lesson
from apps.instructors.models import Instructor


User = get_user_model()


def _make_manager(email, superuser=False):
    user = User.objects.create_user(username=email, email=email, password='x')
    if superuser:
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
    UserProfile.objects.update_or_create(
        user=user, defaults={'role': UserProfile.ROLE_MANAGER}
    )
    return user


def _make_worker(email):
    user = User.objects.create_user(username=email, email=email, password='x')
    UserProfile.objects.update_or_create(
        user=user, defaults={'role': UserProfile.ROLE_WORKER}
    )
    return user


def _client_for(user):
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return client


class CourseVisibilityScopingTest(TestCase):
    def setUp(self):
        self.branch_a = Branch.objects.create(name='Branch A')
        self.branch_b = Branch.objects.create(name='Branch B')
        self.ct = CourseType.objects.create(name='Capoeira')

        self.course_a = Course.objects.create(
            course_type=self.ct, name='A', price=100, capacity=10, branch=self.branch_a
        )
        self.course_b = Course.objects.create(
            course_type=self.ct, name='B', price=100, capacity=10, branch=self.branch_b
        )

        self.instructor = Instructor.objects.create(
            first_name='Inst',
            last_name='One',
            email='inst@test.com',
        )
        self.course_a.instructor = self.instructor
        self.course_a.save(update_fields=['instructor'])
        Lesson.objects.create(
            course=self.course_a,
            instructor=self.instructor,
            day_of_week=0,
            start_time='10:00',
            end_time='11:00',
        )

        self.mgr = _make_manager('mgr@test.com')
        self.worker = _make_worker('inst@test.com')
        self.mgr_client = _client_for(self.mgr)
        self.worker_client = _client_for(self.worker)

    def test_manager_sees_all_courses(self):
        res = self.mgr_client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 200)
        ids = {c['id'] for c in res.data}
        self.assertIn(str(self.course_a.id), ids)
        self.assertIn(str(self.course_b.id), ids)

    def test_worker_cannot_access_manager_course_api(self):
        res = self.worker_client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 403)

    def test_worker_sees_only_assigned_lessons_on_schedule(self):
        Lesson.objects.create(
            course=self.course_b,
            day_of_week=1,
            start_time='12:00',
            end_time='13:00',
        )
        res = self.worker_client.get('/api/v1/scheduling/lessons/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['course_name'], self.course_a.name)

    def test_superuser_sees_all_courses(self):
        su = _make_manager('su@test.com', superuser=True)
        client = _client_for(su)
        res = client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 200)
        ids = {c['id'] for c in res.data}
        self.assertIn(str(self.course_a.id), ids)
        self.assertIn(str(self.course_b.id), ids)
