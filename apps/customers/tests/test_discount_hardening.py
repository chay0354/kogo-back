"""Every discount the CRM's finance settings can configure is billed the same way
everywhere — signup, card link, CRM — and never promised where it cannot apply.

Covers:
- הנחת שיעור נוסף: the lesson being paid for at signup is not enrolled yet, so it
  must still count as the child's additional lesson (the same way it does when the
  standing order is re-billed from a card link after the enrollment exists).
- A "מחיר סופי קבוע" never raises the price above the lesson's own price.
- A percentage discount takes a percentage, not shekels.
- The widget's sibling question is asked only when the sibling discount will be billed.
- An early-signup range keeps its identity whatever name the office types.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.discount_service import DiscountService
from apps.customers.financial_models import Discount
from apps.customers.models import Payment
from apps.enrollments.models import LessonEnrollment

TRANZILA = 'apps.core.payment_service.TranzilaService.create_recurring_payment_request'


def _additional_lesson_discount(value):
    return Discount.objects.create(
        name='הנחת שיעור נוסף לילד פעיל',
        discount_type='fixed_final_price',
        value=Decimal(value),
        applies_to='child',
        promotion_type='permanent',
        is_active=True,
        is_built_in=True,
    )


def _sibling_discount(value, discount_type='fixed'):
    return Discount.objects.create(
        name='הנחת ילד שני',
        discount_type=discount_type,
        value=Decimal(value),
        applies_to='child',
        promotion_type='permanent',
        is_active=True,
        is_built_in=True,
    )


def _early_signup_discount(value, discount_type='fixed', name='הנחת רישום מוקדם'):
    today = date.today()
    return Discount.objects.create(
        name=name,
        discount_type=discount_type,
        value=Decimal(value),
        applies_to='family',
        promotion_type='temporary',
        start_date=today - timedelta(days=7),
        end_date=today + timedelta(days=7),
        is_active=True,
        is_built_in=True,
    )


def _manager_client():
    user = get_user_model().objects.create_user(
        username='manager@test.com', email='manager@test.com', password='pass12345!', is_active=True,
    )
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


class AdditionalLessonDiscountAtSignupTest(TestCase):
    """The additional-lesson price applies when the lesson is bought, not only when re-billed."""

    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family, first_name='Active', status='active')
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.first_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.second_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.service = DiscountService()

    def _evaluate(self, lesson, base_price='350.00'):
        return self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal(base_price),
            lesson_id=str(lesson.id),
        )

    def test_second_lesson_is_additional_before_its_enrollment_exists(self):
        _additional_lesson_discount('200.00')
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')

        calc = self._evaluate(self.second_lesson)

        self.assertEqual(calc.final_price, Decimal('200.00'))
        self.assertEqual(calc.total_discount_amount, Decimal('150.00'))
        self.assertEqual([d.discount_type for d in calc.applicable_discounts], ['additional_lesson'])

    def test_signup_and_card_link_agree_on_the_second_lesson(self):
        """Once the enrollment exists (card link, replacement) the same price comes out."""
        _additional_lesson_discount('200.00')
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')
        before = self._evaluate(self.second_lesson).final_price
        LessonEnrollment.objects.create(child=self.child, lesson=self.second_lesson, status='active')

        after = self._evaluate(self.second_lesson).final_price

        self.assertEqual(before, Decimal('200.00'))
        self.assertEqual(after, before)

    def test_first_lesson_keeps_full_price_when_rebilled(self):
        _additional_lesson_discount('200.00')
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')
        LessonEnrollment.objects.create(child=self.child, lesson=self.second_lesson, status='active')

        calc = self._evaluate(self.first_lesson)

        self.assertEqual(calc.final_price, Decimal('350.00'))
        self.assertEqual(calc.applicable_discounts, [])

    def test_only_lesson_is_not_additional(self):
        _additional_lesson_discount('200.00')

        calc = self._evaluate(self.first_lesson)

        self.assertEqual(calc.final_price, Decimal('350.00'))

    def test_trial_on_another_lesson_does_not_make_this_one_additional(self):
        _additional_lesson_discount('200.00')
        LessonEnrollment.objects.create(
            child=self.child, lesson=self.first_lesson, status='active', trial_lesson_date=date.today(),
        )

        calc = self._evaluate(self.second_lesson)

        self.assertEqual(calc.final_price, Decimal('350.00'))

    def test_in_flight_payment_for_another_lesson_counts(self):
        """Two lessons in one widget checkout: the second is priced as additional."""
        _additional_lesson_discount('200.00')
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.course.branch, lesson=self.first_lesson,
            payment_type='recurring_subscription', status='pending',
            base_amount=Decimal('350.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('350.00'),
        )

        calc = self._evaluate(self.second_lesson)

        self.assertEqual(calc.final_price, Decimal('200.00'))

    def test_fixed_final_price_never_raises_the_price(self):
        """A global ₪200 second-lesson price on a ₪150 course is not a ₪50 surcharge."""
        _additional_lesson_discount('200.00')
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')

        calc = self._evaluate(self.second_lesson, base_price='150.00')

        self.assertEqual(calc.final_price, Decimal('150.00'))
        self.assertEqual(calc.total_discount_amount, Decimal('0.00'))
        self.assertEqual(calc.applicable_discounts, [])


@override_settings(REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetAdditionalLessonDiscountTest(TestCase):
    """An active child adding a lesson in the widget pays the configured additional-lesson price."""

    def setUp(self):
        self.client = APIClient()
        self.family = TestDataFactory.create_family(parent_id_number='123456782')
        self.child = TestDataFactory.create_child(
            family=self.family, first_name='Kid', last_name='Parent', status='active', id_number='234567892',
        )
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.first_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.second_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')

    @patch(TRANZILA, return_value='https://pay.test/x')
    def test_additional_lesson_price_reaches_the_payment_summary(self, _tranzila):
        _additional_lesson_discount('200.00')

        lookup = self.client.post(
            '/api/v1/customers/widget/lookup/',
            {'parent_id_number': '123456782', 'child_first_name': 'Kid', 'child_last_name': 'Parent'},
            format='json',
        )
        self.assertEqual(lookup.json()['discount_type'], 'additional_lesson')

        response = self.client.post(
            '/api/v1/customers/widget/register/',
            {
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
                'course_id': str(self.course.id),
                'lesson_id': str(self.second_lesson.id),
                'existing_child_id': lookup.json()['child_id'],
                'discount_confirmed': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()

        self.assertEqual(body['base_amount'], 350.0)
        self.assertEqual(body['discount_amount'], 150.0)
        self.assertEqual(body['monthly_amount'], 200.0)
        self.assertEqual([d['type'] for d in body['discounts_applied']], ['additional_lesson'])


class PercentageDiscountTest(TestCase):
    """A percentage discount takes a percentage of the price, not that many shekels."""

    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.first = TestDataFactory.create_child(family=self.family, first_name='First')
        self.second = TestDataFactory.create_child(family=self.family, first_name='Second')
        lesson = TestDataFactory.create_lesson()
        LessonEnrollment.objects.create(child=self.first, lesson=lesson, status='active')
        self.service = DiscountService()

    def _evaluate(self, base_price='350.00'):
        return self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.second.id),
            payment_date=date.today(),
            base_price=Decimal(base_price),
        )

    def test_second_child_percentage(self):
        _sibling_discount('10.00', discount_type='percentage')

        calc = self._evaluate()

        self.assertEqual(calc.total_discount_amount, Decimal('35.00'))
        self.assertEqual(calc.final_price, Decimal('315.00'))
        self.assertEqual(calc.applicable_discounts[0].value, Decimal('35.00'))

    def test_early_signup_percentage_is_rounded_to_agorot(self):
        _early_signup_discount('12.50', discount_type='percentage')

        calc = self._evaluate(base_price='333.33')

        # 12.5% of 333.33 = 41.66625 → ₪41.67
        self.assertEqual(calc.total_discount_amount, Decimal('41.67'))
        self.assertEqual(calc.final_price, Decimal('291.66'))

    def test_percentage_and_fixed_add_up(self):
        _early_signup_discount('10.00', discount_type='percentage')
        _sibling_discount('50.00')

        calc = self._evaluate()

        self.assertEqual(calc.total_discount_amount, Decimal('85.00'))
        self.assertEqual(calc.final_price, Decimal('265.00'))


class WidgetSiblingQuestionTest(TestCase):
    """The widget asks about a sibling only when the sibling discount will actually be billed."""

    def setUp(self):
        self.client = APIClient()
        self.family = TestDataFactory.create_family(parent_id_number='123456782')
        self.lesson = TestDataFactory.create_lesson()

    def _lookup(self):
        response = self.client.post(
            '/api/v1/customers/widget/lookup/',
            {'parent_id_number': '123456782', 'child_first_name': 'Beta', 'child_last_name': 'Parent'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_sibling_on_a_team_is_asked(self):
        _sibling_discount('50.00')
        alpha = TestDataFactory.create_child(family=self.family, first_name='Alpha', status='active')
        LessonEnrollment.objects.create(child=alpha, lesson=self.lesson, status='active')

        body = self._lookup()

        self.assertEqual(body['family_status'], 'existing')
        self.assertEqual(body['child_status'], 'new')
        self.assertEqual(body['discount_type'], 'sibling')
        self.assertTrue(body['discount_question'])

    def test_trial_only_sibling_is_not_promised_a_discount(self):
        _sibling_discount('50.00')
        alpha = TestDataFactory.create_child(family=self.family, first_name='Alpha', status='trial_signed')
        LessonEnrollment.objects.create(
            child=alpha, lesson=self.lesson, status='active', trial_lesson_date=date.today(),
        )

        body = self._lookup()

        self.assertEqual(body['family_status'], 'existing')
        self.assertEqual(body['child_status'], 'new')
        self.assertIsNone(body['discount_type'])
        self.assertIsNone(body['discount_question'])

    def test_inactive_sibling_is_not_promised_a_discount(self):
        _sibling_discount('50.00')
        TestDataFactory.create_child(family=self.family, first_name='Alpha', status='inactive')

        body = self._lookup()

        self.assertIsNone(body['discount_type'])
        self.assertIsNone(body['discount_question'])

    def test_unconfigured_sibling_discount_is_not_promised(self):
        _sibling_discount('0.00')
        alpha = TestDataFactory.create_child(family=self.family, first_name='Alpha', status='active')
        LessonEnrollment.objects.create(child=alpha, lesson=self.lesson, status='active')

        body = self._lookup()

        self.assertIsNone(body['discount_type'])
        self.assertIsNone(body['discount_question'])


class EarlySignupDiscountNameTest(TestCase):
    """An early-signup range is recognised by its name; the office's own name must not hide it."""

    def setUp(self):
        self.client = _manager_client()
        self.today = date.today()
        self.start = self.today - timedelta(days=7)
        self.end = self.today + timedelta(days=7)

    def _create(self, **overrides):
        payload = {
            'start_date': self.start.isoformat(),
            'end_date': self.end.isoformat(),
            'value': '100.00',
            'is_active': True,
        }
        payload.update(overrides)
        response = self.client.post('/api/v1/customers/discounts/early-signup/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def _listed_ids(self):
        response = self.client.get('/api/v1/customers/discounts/early-signup/')
        self.assertEqual(response.status_code, 200, response.content)
        return [row['id'] for row in response.json()]

    def test_custom_name_still_lists_and_bills_the_range(self):
        created = self._create(name='מבצע פתיחת שנה')

        self.assertIn(created['id'], self._listed_ids())
        self.assertIn('מבצע פתיחת שנה', created['name'])
        applied = DiscountService().check_early_signup_discount(self.today)
        self.assertIsNotNone(applied)
        self.assertEqual(str(applied.id), created['id'])

    def test_blank_name_is_generated_from_the_dates(self):
        created = self._create(name='')

        self.assertEqual(
            created['name'],
            f"הנחת רישום מוקדם {self.start:%d/%m/%Y} - {self.end:%d/%m/%Y}",
        )

    def test_clearing_the_name_on_edit_regenerates_it(self):
        created = self._create()

        response = self.client.patch(
            f"/api/v1/customers/discounts/{created['id']}/", {'name': ''}, format='json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['name'], created['name'])
        self.assertIn(created['id'], self._listed_ids())

    def test_generated_name_follows_the_dates_when_the_dialog_sends_it_back(self):
        """The dialog sends the name it was shown; a generated one must track the new dates."""
        created = self._create()
        new_start = self.start + timedelta(days=30)
        new_end = self.end + timedelta(days=30)

        response = self.client.patch(
            f"/api/v1/customers/discounts/{created['id']}/",
            {
                'name': created['name'],
                'start_date': new_start.isoformat(),
                'end_date': new_end.isoformat(),
                'value': '100.00',
                'is_active': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()['name'],
            f"הנחת רישום מוקדם {new_start:%d/%m/%Y} - {new_end:%d/%m/%Y}",
        )

    def test_custom_name_on_edit_keeps_the_range_recognisable(self):
        created = self._create()

        response = self.client.patch(
            f"/api/v1/customers/discounts/{created['id']}/", {'name': 'מבצע חנוכה'}, format='json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('מבצע חנוכה', response.json()['name'])
        self.assertIn(created['id'], self._listed_ids())
        self.assertIsNotNone(DiscountService().check_early_signup_discount(self.today))
