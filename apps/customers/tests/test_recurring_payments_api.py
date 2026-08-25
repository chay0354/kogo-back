from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, RecurringPayment


class RecurringPaymentListAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager-recurring@test.com',
            email='manager-recurring@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(
            user=self.user, defaults={'role': UserProfile.ROLE_MANAGER}
        )
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        family = TestDataFactory.create_family()
        child = TestDataFactory.create_child(family=family)
        lesson = TestDataFactory.create_lesson()
        for i in range(25):
            payment = Payment.objects.create(
                child=child,
                family=family,
                branch=lesson.course.branch,
                lesson=lesson,
                payment_type='recurring_subscription',
                status='completed',
                base_amount=Decimal('350.00'),
                discount_amount=Decimal('0.00'),
                final_amount=Decimal('350.00'),
                description=f'מנוי {i}',
            )
            RecurringPayment.objects.create(
                child=child,
                initial_payment=payment,
                status='active',
                amount=Decimal('350.00'),
                billing_day=1,
                start_date=date.today(),
            )

    def test_list_returns_all_standing_orders_unpaginated(self):
        res = self.client.get('/api/v1/customers/recurring-payments/')
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 25)
        first = data[0]
        self.assertIn('child_name', first)
        self.assertTrue(first.get('course_name'))
        details = first['initial_payment_details']
        self.assertIn('lesson_name', details)
        self.assertNotIn('tranzila_transaction', details)
        self.assertNotIn('discount_snapshots', details)
