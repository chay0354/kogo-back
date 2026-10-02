"""Every discount the office configured reaches the price the form shows — by every door.

A parent reaches the summary three ways: recognised by the form (a token, the
child chosen from the list), typing everything with the card's own phone, or
typing everything with another phone. The discount is decided on the server
from who the child is, so all three must show it, and the registration that
follows must charge what was shown.

A discount left at 0 in the finance settings is "not configured" and shows
nothing — the office's to set (owner, 2.10.2026).
"""
import time
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers import widget_identification as identification
from apps.customers.financial_models import Discount
from apps.customers.models import Payment
from apps.enrollments.models import LessonEnrollment

QUOTE = '/api/v1/customers/widget/quote/'
REGISTER = '/api/v1/customers/widget/register/'
IDENTIFY = '/api/v1/customers/widget/identify/'

PARENT_ID = '123456782'
PHONE = '0501234567'
DEVICE = 'device-aaaaaaaaaaaaaaaa'
FIGURES = ('base_amount', 'discount_amount', 'monthly_amount', 'final_amount', 'trial_credit_amount')


def _discount(name, value, discount_type='fixed', **more):
    return Discount.objects.create(
        name=name, discount_type=discount_type, value=Decimal(value), applies_to='child',
        promotion_type='permanent', is_active=True, is_built_in=True, **more,
    )


@override_settings(WIDGET_IDENTIFICATION_ENABLED=True, REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch.object(identification, 'MIN_ANSWER_SECONDS', 0)
class DiscountsByEveryDoor(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        patch.object(identification, '_alert_office').start()
        patch('apps.customers.widget_views._tell_office_of_other_contact').start()
        self.addCleanup(patch.stopall)

        self.family = TestDataFactory.create_family(name='כהן', phone=PHONE, parent_id_number=PARENT_ID)
        TestDataFactory.create_parent(
            family=self.family, first_name='דנה', last_name='כהן', phone=PHONE, email='dana@example.com',
        )
        self.child = TestDataFactory.create_child(
            family=self.family, first_name='מאיה', last_name='כהן', id_number='218847366',
            birth_date=date(2018, 6, 21), gender='female', status='active',
        )
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.first_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.second_lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        LessonEnrollment.objects.create(child=self.child, lesson=self.first_lesson, status='active')

    # ── the three doors ──────────────────────────────────────────────────────

    def _typed(self, phone=PHONE, **child):
        body = {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': phone, 'parent_email': 'dana@example.com',
            'child_first_name': 'מאיה', 'child_last_name': 'כהן', 'child_id_number': '218847366',
            'child_birth_date': '2018-06-21', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.second_lesson.id),
        }
        body.update(child)
        return body

    def _recognised(self, **overrides):
        ticket = signing.dumps({'t': time.time() - 30}, salt=identification.FORM_SALT)
        answer = self.client.post(IDENTIFY, {
            'parent_id_number': PARENT_ID, 'parent_phone': PHONE, 'device_id': DEVICE, 'ticket': ticket,
        }, format='json').json()
        self.assertEqual(answer['status'], 'known', answer)
        body = {
            'identify_token': answer['token'], 'identified_child_id': str(self.child.id), 'device_id': DEVICE,
            'parent_id_number': PARENT_ID, 'parent_phone': '', 'parent_first_name': '', 'parent_last_name': '',
            'parent_email': '', 'child_first_name': 'מאיה', 'child_last_name': '', 'child_id_number': '',
            'child_birth_date': '', 'child_gender': '',
            'course_id': str(self.course.id), 'lesson_id': str(self.second_lesson.id),
        }
        body.update(overrides)
        return body

    def _a_new_sibling(self, **overrides):
        body = self._typed(
            child_first_name='נועם', child_id_number='345678903', child_birth_date='2016-03-02',
            child_gender='male', lesson_id=str(self.first_lesson.id),
        )
        body.update(overrides)
        return body

    def _shown_then_charged(self, body):
        """The quote, then the registration with the same body: the figures must be the same."""
        quoted = self.client.post(QUOTE, {'items': [body]}, format='json')
        self.assertEqual(quoted.status_code, 200, quoted.content)
        item = quoted.json()['items'][0]
        with patch(
            'apps.core.payment_service.TranzilaService.create_recurring_payment_request',
            return_value='https://pay.test/x',
        ):
            registered = self.client.post(
                REGISTER, {**body, 'signature': 'data:image/png;base64,AAAA'}, format='json',
            )
        self.assertEqual(registered.status_code, 201, registered.content)
        for key in FIGURES:
            self.assertEqual(item.get(key), registered.json().get(key), key)
        return item

    def _types(self, item):
        return [discount['type'] for discount in item['discounts_applied']]

    # ── another lesson for a child who is already on one ─────────────────────

    def test_another_lesson_for_a_recognised_child(self):
        _discount('הנחת שיעור נוסף לילד פעיל', '200.00', 'fixed_final_price')

        item = self._shown_then_charged(self._recognised())

        self.assertEqual((item['base_amount'], item['discount_amount'], item['monthly_amount']), (350.0, 150.0, 200.0))
        self.assertEqual(self._types(item), ['additional_lesson'])

    def test_another_lesson_typed_with_the_cards_phone(self):
        _discount('הנחת שיעור נוסף לילד פעיל', '200.00', 'fixed_final_price')

        item = self._shown_then_charged(self._typed())

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (150.0, 200.0))
        self.assertEqual(self._types(item), ['additional_lesson'])

    def test_another_lesson_typed_with_another_phone(self):
        """The other parent's phone: the child is the same child, so the price is the same price."""
        _discount('הנחת שיעור נוסף לילד פעיל', '200.00', 'fixed_final_price')

        item = self._shown_then_charged(self._typed(phone='0529999999'))

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (150.0, 200.0))

    def test_another_lesson_left_at_zero_in_the_settings_shows_nothing(self):
        """How it stands on the live site on 2.10.2026: the price was never set."""
        _discount('הנחת שיעור נוסף לילד פעיל', '0.00', 'fixed_final_price')

        item = self._shown_then_charged(self._recognised())

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (0.0, 350.0))
        self.assertEqual(item['discounts_applied'], [])

    def test_a_second_lesson_price_set_on_the_lesson_itself_is_the_price(self):
        self.second_lesson.additional_course_prices = [{'course_index': 2, 'price': 250}]
        self.second_lesson.save()

        item = self._shown_then_charged(self._recognised())

        self.assertEqual((item['base_amount'], item['monthly_amount']), (250.0, 250.0))

    # ── a brother or a sister ────────────────────────────────────────────────

    def test_a_sibling_of_a_child_on_a_team_typed(self):
        _discount('הנחת ילד שני', '50.00')

        item = self._shown_then_charged(self._a_new_sibling())

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (50.0, 300.0))
        self.assertEqual(self._types(item), ['second_child'])

    def test_a_sibling_added_by_a_recognised_parent(self):
        """"A new child" chosen on the list: the parent is known, the child is typed."""
        _discount('הנחת ילד שני', '50.00')
        body = self._recognised(
            child_first_name='נועם', child_last_name='כהן', child_id_number='345678903',
            child_birth_date='2016-03-02', child_gender='male', lesson_id=str(self.first_lesson.id),
        )
        del body['identified_child_id']

        item = self._shown_then_charged(body)

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (50.0, 300.0))

    # ── early signup ─────────────────────────────────────────────────────────

    def test_early_signup_and_a_sibling_together(self):
        today = date.today()
        _discount('הנחת ילד שני', '50.00')
        Discount.objects.create(
            name='רישום מוקדם', discount_type='fixed', value=Decimal('10.00'), applies_to='family',
            promotion_type='temporary', start_date=today - timedelta(days=7), end_date=today + timedelta(days=7),
            is_active=True, is_built_in=True,
        )

        item = self._shown_then_charged(self._a_new_sibling())

        self.assertEqual((item['discount_amount'], item['monthly_amount']), (60.0, 290.0))
        self.assertEqual(sorted(self._types(item)), ['early_signup', 'second_child'])

    # ── a trial lesson that was paid for ─────────────────────────────────────

    def test_a_paid_trial_comes_off_the_first_charge(self):
        trial_child = TestDataFactory.create_child(
            family=self.family, first_name='נועם', last_name='כהן', id_number='345678903',
            birth_date=date(2016, 3, 2), gender='male', status='trial_completed',
        )
        Payment.objects.create(
            child=trial_child, family=self.family, branch=self.course.branch, lesson=self.first_lesson,
            payment_type='one_time', status='completed', base_amount=Decimal('30.00'),
            discount_amount=Decimal('0.00'), final_amount=Decimal('30.00'),
            trial_lesson_date=date.today() - timedelta(days=7), payment_date=date.today() - timedelta(days=8),
        )

        item = self._shown_then_charged(self._a_new_sibling())

        self.assertEqual(item['trial_credit_amount'], 30.0)
