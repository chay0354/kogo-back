"""Widget sign-up shows the discounts a family is entitled to — and only those.

Covers every discount the CRM's finance settings can configure — רישום מוקדם,
ילד שני, שיעור נוסף — against the same rule: a configured built-in discount
reaches the widget's payment summary, and one left at 0 (the value the CRM
creates the row with, and the value it uses to switch a discount off) reaches
nothing at all.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.discount_service import DiscountService
from apps.customers.financial_models import Discount
from apps.enrollments.models import LessonEnrollment


def _payload(**overrides):
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


@override_settings(REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetSiblingDiscountVisibilityTest(TestCase):
    """The sibling discount the widget promises in its question is the one it bills."""

    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    def _sibling_discount(self, value):
        return Discount.objects.create(
            name='הנחת ילד שני',
            discount_type='fixed',
            value=Decimal(value),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )

    def _register(self, first_name, id_number):
        return self.client.post(
            '/api/v1/customers/widget/register/',
            _payload(
                course_id=str(self.course.id),
                lesson_id=str(self.lesson.id),
                child_first_name=first_name,
                child_id_number=id_number,
            ),
            format='json',
        )

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_configured_sibling_discount_reaches_the_payment_summary(self, _tranzila):
        self._sibling_discount('50.00')

        first = self._register('Alpha', '234567892')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()['discount_amount'], 0.0)
        self.assertEqual(first.json()['discounts_applied'], [])

        lookup = self.client.post(
            '/api/v1/customers/widget/lookup/',
            {
                'parent_id_number': '123456782',
                'child_first_name': 'Beta',
                'child_last_name': 'Parent',
            },
            format='json',
        )
        self.assertEqual(lookup.json()['discount_type'], 'sibling')

        second = self._register('Beta', '345678903')
        self.assertEqual(second.status_code, 201, second.content)
        body = second.json()
        self.assertEqual(body['base_amount'], 350.0)
        self.assertEqual(body['discount_amount'], 50.0)
        self.assertEqual(body['monthly_amount'], 300.0)
        self.assertEqual(
            [(d['type'], d['value']) for d in body['discounts_applied']],
            [('second_child', 50.0)],
        )

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_unconfigured_sibling_discount_is_not_reported_as_a_zero_discount(self, _tranzila):
        self._sibling_discount('0.00')

        self._register('Alpha', '234567892')
        body = self._register('Beta', '345678903').json()

        self.assertEqual(body['discount_amount'], 0.0)
        self.assertEqual(body['discounts_applied'], [])


class UnconfiguredAdditionalLessonDiscountTest(TestCase):
    """A "מחיר סופי קבוע" discount left at 0 must not hand out a free lesson."""

    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family, first_name='Active')
        self.child.status = 'active'
        self.child.save()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.first_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.second_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')
        LessonEnrollment.objects.create(child=self.child, lesson=self.second_lesson, status='active')

    def _evaluate(self):
        return DiscountService().evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00'),
            lesson_id=str(self.second_lesson.id),
        )

    def test_zero_fixed_final_price_leaves_the_price_alone(self):
        Discount.objects.create(
            name='הנחת שיעור נוסף לילד פעיל',
            discount_type='fixed_final_price',
            value=Decimal('0.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )

        calc = self._evaluate()

        self.assertEqual(calc.final_price, Decimal('350.00'))
        self.assertEqual(calc.total_discount_amount, Decimal('0.00'))
        self.assertEqual(calc.applicable_discounts, [])

    def test_configured_fixed_final_price_still_applies(self):
        Discount.objects.create(
            name='הנחת שיעור נוסף לילד פעיל',
            discount_type='fixed_final_price',
            value=Decimal('200.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )

        calc = self._evaluate()

        self.assertEqual(calc.final_price, Decimal('200.00'))
        self.assertEqual(calc.total_discount_amount, Decimal('150.00'))


@override_settings(REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetEarlySignupDiscountVisibilityTest(TestCase):
    """The early-signup range in finance settings prices the widget's first charge."""

    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    def _early_signup(self, value):
        today = date.today()
        return Discount.objects.create(
            name='הנחת רישום מוקדם',
            discount_type='fixed',
            value=Decimal(value),
            applies_to='family',
            promotion_type='temporary',
            start_date=today - timedelta(days=7),
            end_date=today + timedelta(days=7),
            is_active=True,
            is_built_in=True,
        )

    def _register(self):
        return self.client.post(
            '/api/v1/customers/widget/register/',
            _payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id)),
            format='json',
        )

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_configured_early_signup_discount_reaches_the_payment_summary(self, _tranzila):
        self._early_signup('100.00')

        body = self._register().json()

        self.assertEqual(body['discount_amount'], 100.0)
        self.assertEqual(body['monthly_amount'], 250.0)
        self.assertEqual(
            [(d['type'], d['value']) for d in body['discounts_applied']],
            [('early_signup', 100.0)],
        )

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_unconfigured_early_signup_discount_is_not_reported(self, _tranzila):
        self._early_signup('0.00')

        body = self._register().json()

        self.assertEqual(body['discount_amount'], 0.0)
        self.assertEqual(body['discounts_applied'], [])

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
    def test_early_signup_and_sibling_discounts_are_listed_together(self, _tranzila):
        self._early_signup('100.00')
        Discount.objects.create(
            name='הנחת ילד שני',
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )

        first = self.client.post(
            '/api/v1/customers/widget/register/',
            _payload(
                course_id=str(self.course.id),
                lesson_id=str(self.lesson.id),
                child_first_name='Alpha',
                child_id_number='234567892',
            ),
            format='json',
        )
        self.assertEqual(first.status_code, 201, first.content)

        second = self.client.post(
            '/api/v1/customers/widget/register/',
            _payload(
                course_id=str(self.course.id),
                lesson_id=str(self.lesson.id),
                child_first_name='Beta',
                child_id_number='345678903',
            ),
            format='json',
        )
        self.assertEqual(second.status_code, 201, second.content)
        body = second.json()

        self.assertEqual(body['discount_amount'], 150.0)
        self.assertEqual(body['monthly_amount'], 200.0)
        self.assertEqual(
            sorted(d['type'] for d in body['discounts_applied']),
            ['early_signup', 'second_child'],
        )
