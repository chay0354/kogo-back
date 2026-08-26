"""Widget multi-child / multi-lesson registration pricing."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.payment_service import PaymentService, get_child_lesson_index_for_billing
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Discount
from apps.customers.models import Payment
from apps.courses.models import LessonBundle


def _register_payload(**overrides):
    base = {
        'parent_id_number': '123456782',
        'parent_first_name': 'Test',
        'parent_last_name': 'Parent',
        'parent_phone': '0501234567',
        'parent_email': 'parent@example.com',
        'child_first_name': 'Kid',
        'child_last_name': 'Parent',
        'child_id_number': '234567892',
        'child_birth_date': '2015-01-01',
        'child_gender': 'male',
    }
    base.update(overrides)
    return base


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetMultiLessonRegistrationFeeTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.lesson_b.additional_course_prices = [{'course_index': 2, 'price': 250}]
        self.lesson_b.save()

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    @patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment')
    def test_second_lesson_includes_registration_fee(self, mock_discount, _mock_tranzila):
        from apps.customers.discount_service import DiscountCalculation

        mock_discount.side_effect = lambda **kwargs: DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=kwargs['base_price'],
            base_price=kwargs['base_price'],
        )

        first = self.client.post(
            '/api/v1/customers/widget/register/',
            _register_payload(course_id=str(self.course.id), lesson_id=str(self.lesson_a.id)),
            format='json',
        )
        self.assertEqual(first.status_code, 201, first.content)
        child_id = first.json()['child_id']
        self.assertEqual(first.json()['registration_fee'], 120.0)

        second = self.client.post(
            '/api/v1/customers/widget/register/',
            _register_payload(
                course_id=str(self.course.id),
                lesson_id=str(self.lesson_b.id),
                existing_child_id=child_id,
            ),
            format='json',
        )
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()['registration_fee'], 120.0)
        self.assertEqual(second.json()['course_index'], 2)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetSiblingDiscountIntegrationTest(TestCase):
    """Second child pricing uses sibling discount once the first child has a pending payment."""

    def setUp(self):
        Discount.objects.create(
            name='הנחת ילד שני',
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )
        self.family = TestDataFactory.create_family()
        self.branch = self.family.branch
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course)
        self.child1 = TestDataFactory.create_child(family=self.family, first_name='First')
        self.child2 = TestDataFactory.create_child(family=self.family, first_name='Second')

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_second_child_pending_after_first_gets_discount(self, _mock_tranzila):
        service = PaymentService()
        first = service.initiate_subscription_payment(
            child_id=str(self.child1.id),
            lesson_id=str(self.lesson.id),
        )
        second = service.initiate_subscription_payment(
            child_id=str(self.child2.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(first['discount_amount'], 0.0)
        self.assertEqual(second['discount_amount'], 50.0)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetBundleRegistrationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('200.00'))
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        self.bundle.lessons.set([self.lesson_a, self.lesson_b])

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    @patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment')
    def test_bundle_charges_registration_fee_once(self, mock_discount, _mock_tranzila):
        from apps.customers.discount_service import DiscountCalculation

        mock_discount.side_effect = lambda **kwargs: DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=kwargs['base_price'],
            base_price=kwargs['base_price'],
        )

        res = self.client.post(
            '/api/v1/customers/widget/register/',
            _register_payload(
                course_id=str(self.course.id),
                bundle_id=str(self.bundle.id),
            ),
            format='json',
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertTrue(body['is_bundle'])
        self.assertEqual(body['registration_fee'], 120.0)
        self.assertEqual(body['monthly_amount'], 300.0)
        self.assertEqual(len(body['payments']), 1)
        self.assertEqual(body['payments'][0]['registration_fee'], 120.0)
        self.assertEqual(body['payments'][0]['monthly_amount'], 300.0)


class BillingIndexInflightTest(TestCase):
    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    @patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment')
    def test_pending_first_lesson_advances_course_index(self, mock_discount, _mock_tranzila):
        from apps.customers.discount_service import DiscountCalculation
        from apps.core.payment_service import PaymentService

        mock_discount.side_effect = lambda **kwargs: DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=kwargs['base_price'],
            base_price=kwargs['base_price'],
        )

        child = TestDataFactory.create_child()
        course = TestDataFactory.create_course(price=Decimal('350.00'))
        lesson_a = TestDataFactory.create_lesson(course=course, day_of_week=0)
        lesson_b = TestDataFactory.create_lesson(course=course, day_of_week=3)
        lesson_b.additional_course_prices = [{'course_index': 2, 'price': 250}]
        lesson_b.save()

        service = PaymentService()
        first = service.initiate_subscription_payment(
            child_id=str(child.id),
            lesson_id=str(lesson_a.id),
        )
        self.assertEqual(first['course_index'], 1)

        second = service.initiate_subscription_payment(
            child_id=str(child.id),
            lesson_id=str(lesson_b.id),
            include_registration_fee=False,
        )
        self.assertEqual(second['course_index'], 2)
        self.assertEqual(
            get_child_lesson_index_for_billing(child, lesson_b),
            2,
        )


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetRegisterReusesExistingChildTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('200.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    @patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment')
    def test_retry_register_reuses_pending_child(self, mock_discount, _mock_tranzila):
        from apps.customers.discount_service import DiscountCalculation
        from apps.customers.models import Child

        mock_discount.side_effect = lambda **kwargs: DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=kwargs['base_price'],
            base_price=kwargs['base_price'],
        )
        payload = _register_payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id))

        first = self.client.post('/api/v1/customers/widget/register/', payload, format='json')
        self.assertEqual(first.status_code, 201, first.content)
        child_id = first.json()['child_id']

        second = self.client.post('/api/v1/customers/widget/register/', payload, format='json')
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()['child_id'], child_id)
        self.assertEqual(Child.objects.filter(id_number=payload['child_id_number']).count(), 1)

    def test_lookup_does_not_treat_pending_child_as_sibling(self):
        family = TestDataFactory.create_family(parent_id_number='123456782')
        TestDataFactory.create_parent(family=family)
        pending = TestDataFactory.create_child(
            family=family,
            first_name='Kid',
            last_name='Parent',
            status='pending',
        )

        response = self.client.post(
            '/api/v1/customers/widget/lookup/',
            {
                'parent_id_number': '123456782',
                'child_first_name': 'Kid',
                'child_last_name': 'Parent',
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['family_status'], 'existing')
        self.assertIsNone(body['discount_type'])
        self.assertEqual(body['child_id'], str(pending.id))

