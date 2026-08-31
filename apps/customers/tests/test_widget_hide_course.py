from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory


class WidgetHideCourseTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.branch = TestDataFactory.create_branch()
        self.visible = TestDataFactory.create_course(
            name='Visible',
            branch=self.branch,
            price=Decimal('200.00'),
        )
        self.hidden = TestDataFactory.create_course(
            name='Hidden',
            branch=self.branch,
            course_type=self.visible.course_type,
            price=Decimal('200.00'),
            show_in_widget=False,
        )
        TestDataFactory.create_lesson(course=self.visible)
        TestDataFactory.create_lesson(course=self.hidden)

    def test_catalog_omits_hidden_course(self):
        res = self.client.get('/api/v1/customers/widget/courses/', {'branch_id': str(self.branch.id)})
        self.assertEqual(res.status_code, 200)
        names = [row['name'] for row in res.json()]
        self.assertIn('Visible', names)
        self.assertNotIn('Hidden', names)

    def test_register_rejects_hidden_course(self):
        res = self.client.post(
            '/api/v1/customers/widget/register/',
            {
                'parent_id_number': '123456782',
                'parent_first_name': 'Test',
                'parent_last_name': 'Parent',
                'parent_phone': '0501234567',
                'child_first_name': 'Kid',
                'child_last_name': 'Parent',
                'child_id_number': '234567892',
                'child_birth_date': '2015-01-01',
                'child_gender': 'male',
                'course_id': str(self.hidden.id),
            },
            format='json',
        )
        self.assertEqual(res.status_code, 404)
        self.assertIn('אינו פתוח', res.json()['error'])
