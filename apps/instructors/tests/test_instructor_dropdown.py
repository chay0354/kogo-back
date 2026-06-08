"""Fast instructor dropdown list endpoint."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.instructors.models import Instructor


User = get_user_model()


class InstructorDropdownListTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.instructor = Instructor.objects.create(
            first_name='Test',
            last_name='Teacher',
            email='teacher@test.com',
            phone='0501111111',
            primary_branch=self.branch,
            fixed_salary_per_lesson=250,
        )
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_dropdown_returns_lightweight_list_without_metrics(self):
        res = self.client.get('/api/v1/instructors/?dropdown=true')
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.data, list)
        self.assertEqual(len(res.data), 1)
        row = res.data[0]
        self.assertEqual(row['full_name'], self.instructor.full_name)
        self.assertIn('fixed_salary_per_lesson', row)
        self.assertNotIn('revenue', row)
        self.assertNotIn('salary', row)
        self.assertNotIn('profit', row)
