"""Tests for per-manager course scoping."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.courses.models import CourseType, Course


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


def _client_for(user):
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return client


class ManagerCourseScopingTest(TestCase):
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

        self.mgr = _make_manager('mgr@test.com')
        self.course_a.managers.add(self.mgr)  # assigned to A only

        self.client = _client_for(self.mgr)

    def test_manager_sees_only_assigned_courses(self):
        res = self.client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 200)
        ids = {c['id'] for c in res.data}
        self.assertIn(str(self.course_a.id), ids)
        self.assertNotIn(str(self.course_b.id), ids)

    def test_manager_with_no_courses_sees_nothing(self):
        empty_mgr = _make_manager('empty@test.com')
        client = _client_for(empty_mgr)
        res = client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 0)

    def test_superuser_sees_all_courses(self):
        su = _make_manager('su@test.com', superuser=True)
        client = _client_for(su)
        res = client.get('/api/v1/courses/courses/')
        self.assertEqual(res.status_code, 200)
        ids = {c['id'] for c in res.data}
        self.assertIn(str(self.course_a.id), ids)
        self.assertIn(str(self.course_b.id), ids)

    def test_creator_is_auto_assigned(self):
        payload = {
            'course_type': str(self.ct.id),
            'name': 'New',
            'price': 120,
            'capacity': 15,
            'branch': str(self.branch_a.id),
            'min_age': 6,
            'max_age': 12,
        }
        res = self.client.post('/api/v1/courses/courses/', payload, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        new_course = Course.objects.get(id=res.data['id'])
        self.assertIn(self.mgr, new_course.managers.all())
