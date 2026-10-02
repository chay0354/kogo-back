"""The registration form's quote: the price before the signature.

A quote runs the registration itself and rolls it back. Two promises are held
here: the figures are the ones the registration then gets, and nothing — no
family, child, payment, consent or signature — is left behind.
"""
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Discount
from apps.customers.models import Child, Family, Parent, Payment, PaymentDiscountSnapshot
from apps.signatures.models import Signature

QUOTE = '/api/v1/customers/widget/quote/'
REGISTER = '/api/v1/customers/widget/register/'

# What the form shows: every figure of the payment summary.
FIGURES = (
    'base_amount', 'discount_amount', 'prorated_amount', 'registration_fee', 'final_amount',
    'monthly_amount', 'next_billing_date', 'subscription_start_date', 'prorate_lessons_remaining',
    'total_lessons_this_month', 'trial_credit_amount', 'discounts_applied',
)


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


def _rows():
    return (
        Family.objects.count(), Parent.objects.count(), Child.objects.count(),
        Payment.objects.count(), PaymentDiscountSnapshot.objects.count(), Signature.objects.count(),
    )


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetQuoteTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.other = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.other.additional_course_prices = [{'course_index': 2, 'price': 250}]
        self.other.save()
        Discount.objects.create(
            name='הנחת ילד שני', discount_type='fixed', value=Decimal('50.00'), applies_to='child',
            promotion_type='permanent', is_active=True, is_built_in=True,
        )

    def _item(self, **overrides):
        return _payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id), **overrides)

    def _quote(self, *items):
        return self.client.post(QUOTE, {'items': list(items)}, format='json')

    def _register(self, **overrides):
        return self.client.post(
            REGISTER, self._item(signature='data:image/png;base64,AAAA', **overrides), format='json',
        )

    def test_a_new_family_is_quoted_and_nothing_is_saved(self):
        before = _rows()

        response = self._quote(self._item())

        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['items'][0]
        self.assertEqual(item['base_amount'], 350.0)
        self.assertEqual(item['registration_fee'], 120.0)
        self.assertEqual(item['monthly_amount'], 350.0)
        self.assertEqual(item['final_amount'], item['prorated_amount'] + 120.0)
        self.assertEqual(_rows(), before)

    def test_the_answer_names_no_row_that_was_rolled_back(self):
        item = self._quote(self._item()).json()['items'][0]

        for key in ('payment_id', 'child_id', 'payments', 'tranzila_url'):
            self.assertNotIn(key, item)

    def test_the_registration_that_follows_gets_the_quoted_figures(self):
        self.assertEqual(self._register().status_code, 201)
        sibling = {'child_first_name': 'Beta', 'child_id_number': '345678903'}

        quoted = self._quote(self._item(**sibling)).json()['items'][0]
        registered = self._register(**sibling).json()

        self.assertEqual(quoted['discount_amount'], 50.0)
        for key in FIGURES:
            self.assertEqual(quoted.get(key), registered.get(key), key)

    def test_a_quote_twice_gives_the_same_answer(self):
        self.assertEqual(self._register().status_code, 201)
        sibling = self._item(child_first_name='Beta', child_id_number='345678903')

        first = self._quote(sibling).json()
        second = self._quote(sibling).json()

        self.assertEqual(first, second)

    def test_a_second_lesson_for_the_same_child_sees_the_first(self):
        """The fee is once per child, and the second lesson takes its own price tier."""
        first = self._item()
        second = _payload(course_id=str(self.course.id), lesson_id=str(self.other.id), same_child_as=0)
        before = _rows()

        quoted = self._quote(first, second).json()['items']

        self.assertEqual(_rows(), before)
        self.assertEqual(quoted[0]['registration_fee'], 120.0)
        self.assertEqual(quoted[1]['registration_fee'], 0.0)
        self.assertEqual(quoted[1]['base_amount'], 250.0)

        one = self._register().json()
        two = self.client.post(REGISTER, _payload(
            course_id=str(self.course.id), lesson_id=str(self.other.id),
            existing_child_id=one['child_id'], discount_confirmed=True,
            signature='data:image/png;base64,AAAA',
        ), format='json').json()
        for key in FIGURES:
            self.assertEqual(quoted[0].get(key), one.get(key), key)
            self.assertEqual(quoted[1].get(key), two.get(key), key)

    def test_a_known_family_is_left_exactly_as_it_was(self):
        """A quote carries a phone and an email; neither reaches the family's card."""
        self.assertEqual(self._register().status_code, 201)
        family = Family.objects.get(parent_id_number='123456782')
        family.computerized_docs_consent_at = None
        family.save(update_fields=['computerized_docs_consent_at'])
        before = _rows()

        response = self._quote(self._item(
            child_first_name='Beta', child_id_number='345678903',
            parent_phone='0529999999', parent_email='someone.else@example.com',
            computerized_docs_consent=True,
        ))

        self.assertEqual(response.status_code, 200, response.content)
        family.refresh_from_db()
        self.assertEqual(family.phone, '0501234567')
        self.assertEqual(family.email, 'parent@example.com')
        self.assertIsNone(family.computerized_docs_consent_at)
        self.assertEqual(_rows(), before)

    def test_a_child_already_on_the_lesson_is_refused_as_registration_refuses(self):
        registered = self._register()
        self.assertEqual(registered.status_code, 201)
        payment = Payment.objects.get(id=registered.json()['payment_id'])
        payment.status = 'completed'
        payment.save(update_fields=['status'])
        from apps.enrollments.models import LessonEnrollment
        LessonEnrollment.objects.create(child=payment.child, lesson=self.lesson, status='active')

        response = self._quote(self._item())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['index'], 0)
        self.assertEqual(response.json()['error'], self._register().json()['error'])

    def test_an_unknown_course_is_refused_with_its_place_in_the_basket(self):
        missing = _payload(course_id='00000000-0000-0000-0000-000000000000')
        before = _rows()

        response = self._quote(self._item(), missing)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['index'], 1)
        self.assertEqual(_rows(), before)

    def test_a_request_that_is_not_a_basket_is_refused(self):
        for body in ({}, {'items': []}, {'items': 'x'}, {'items': [1]}, {'items': [self._item()] * 13}):
            response = self.client.post(QUOTE, body, format='json')
            self.assertEqual(response.status_code, 400, body)
