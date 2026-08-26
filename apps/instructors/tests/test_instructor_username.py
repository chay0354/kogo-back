"""Instructor last name is optional; email field is a free-form login username."""
from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.core.scoping import instructor_course_ids, instructor_for_user
from apps.courses.models import Course, CourseType
from apps.instructors.models import Instructor
from apps.instructors.serializers import InstructorCreateUpdateSerializer


User = get_user_model()


class InstructorUsernameTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='גאולים')
        self.manager = User.objects.create_user(
            username='mgr@test.com', email='mgr@test.com', password='secret'
        )
        UserProfile.objects.update_or_create(
            user=self.manager, defaults={'role': UserProfile.ROLE_MANAGER}
        )
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=self.manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_serializer_accepts_username_without_last_name_or_email(self):
        serializer = InstructorCreateUpdateSerializer(data={
            'first_name': 'אלגריה',
            'last_name': '',
            'phone': '0501111111',
            'email': 'alegria',
            'primary_branch': str(self.branch.id),
            'salary_model_type': 'fixed_per_lesson',
            'fixed_salary_per_lesson': 250,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instructor = serializer.save()
        self.assertEqual(instructor.last_name, '')
        self.assertEqual(instructor.email, 'alegria')
        self.assertEqual(instructor.full_name, 'אלגריה')

    def test_username_must_be_unique(self):
        Instructor.objects.create(
            first_name='A', last_name='', phone='0501111111', email='alegria'
        )
        serializer = InstructorCreateUpdateSerializer(data={
            'first_name': 'B',
            'phone': '0502222222',
            'email': 'Alegria',
            'salary_model_type': 'fixed_per_lesson',
            'fixed_salary_per_lesson': 250,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('email', serializer.errors)

    def test_create_via_api(self):
        res = self.client.post('/api/v1/instructors/', {
            'first_name': 'אלגריה',
            'last_name': '',
            'phone': '0503333333',
            'email': 'alegria',
            'primary_branch': str(self.branch.id),
            'salary_model_type': 'fixed_per_lesson',
            'fixed_salary_per_lesson': 250,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['email'], 'alegria')
        self.assertEqual(res.data['last_name'], '')

    def test_login_with_username(self):
        user = User.objects.create_user(username='alegria', email='', password='secret')
        UserProfile.objects.update_or_create(
            user=user, defaults={'role': UserProfile.ROLE_WORKER}
        )
        found = authenticate(email='alegria', password='secret')
        self.assertEqual(found.pk, user.pk)

        res = APIClient().post('/api/v1/core/auth/login/', {
            'email': 'alegria',
            'password': 'secret',
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['user']['id'], user.pk)

    def test_worker_matched_by_username(self):
        instructor = Instructor.objects.create(
            first_name='אלגריה',
            last_name='',
            phone='0504444444',
            email='alegria',
        )
        user = User.objects.create_user(username='alegria', email='', password='secret')
        UserProfile.objects.update_or_create(
            user=user, defaults={'role': UserProfile.ROLE_WORKER}
        )
        ct = CourseType.objects.create(name='Capoeira')
        course = Course.objects.create(
            course_type=ct, name='A', price=100, capacity=10, branch=self.branch,
            instructor=instructor,
        )
        self.assertEqual(instructor_for_user(user).id, instructor.id)
        self.assertEqual(instructor_course_ids(user), [course.id])
