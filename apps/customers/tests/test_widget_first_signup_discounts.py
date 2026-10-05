"""A family nobody knows yet gets its discounts in the very form that brings it in.

A parent who registers for the first time with two children has no "brother
already on a team" on record, and a child who starts with two classes has no
"first class" on record: both are being opened by this same form. The second
child and the second class are still a second child and a second class, and the
discounts the office configured for them are shown in the summary and charged
(owner, 5.10.2026: "the system must recognise it at once, although there was no
brother before" — and the same for another class).

Everything goes through the public doors the form uses: the quote, then one
registration per child and class, in the form's own order. What the quote
showed is what each registration charges.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers import widget_identification as identification
from apps.customers.discount_service import DiscountService
from apps.customers.financial_models import Discount
from apps.customers.models import Child, Family, Payment

QUOTE = '/api/v1/customers/widget/quote/'
REGISTER = '/api/v1/customers/widget/register/'

PARENT_ID = '123456782'
PHONE = '0501234567'
FIGURES = (
    'base_amount', 'discount_amount', 'monthly_amount', 'final_amount', 'registration_fee', 'trial_credit_amount',
)

MAYA = {
    'child_first_name': 'מאיה', 'child_last_name': 'כהן', 'child_id_number': '218847366',
    'child_birth_date': '2018-06-21', 'child_gender': 'female',
}
NOAM = {
    'child_first_name': 'נועם', 'child_last_name': 'כהן', 'child_id_number': '345678903',
    'child_birth_date': '2016-03-02', 'child_gender': 'male',
}


def _discount(name, value, discount_type='fixed', **more):
    return Discount.objects.create(
        name=name, discount_type=discount_type, value=Decimal(value), applies_to='child',
        promotion_type='permanent', is_active=True, is_built_in=True, **more,
    )


def _second_child(value='50.00'):
    return _discount('הנחת ילד שני', value)


def _another_class(value='200.00'):
    return _discount('הנחת שיעור נוסף לילד פעיל', value, 'fixed_final_price')


@override_settings(WIDGET_IDENTIFICATION_ENABLED=True, REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch.object(identification, 'MIN_ANSWER_SECONDS', 0)
class AFamilyNobodyKnowsYet(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        patch.object(identification, '_alert_office').start()
        patch('apps.customers.widget_views._tell_office_of_other_contact').start()
        self.addCleanup(patch.stopall)

        self.capoeira = TestDataFactory.create_course(price=Decimal('350.00'))
        self.capoeira_lesson = TestDataFactory.create_lesson(course=self.capoeira, day_of_week=0)
        self.hiphop = TestDataFactory.create_course(price=Decimal('350.00'))
        self.hiphop_lesson = TestDataFactory.create_lesson(course=self.hiphop, day_of_week=3)

    # ── the form ─────────────────────────────────────────────────────────────

    def _item(self, child, lesson, **more):
        body = {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': PHONE, 'parent_email': 'dana@example.com',
            'course_id': str(lesson.course_id), 'lesson_id': str(lesson.id),
        }
        body.update(child)
        body.update(more)
        return body

    def _shown_then_charged(self, items):
        """
        The quote for the whole form, then its registrations one after the other,
        as the form sends them. Each registration must charge what the quote showed.
        Returns the quote's items.
        """
        self.assertFalse(Family.objects.filter(parent_id_number=PARENT_ID).exists())
        quoted = self.client.post(QUOTE, {'items': items}, format='json')
        self.assertEqual(quoted.status_code, 200, quoted.content)
        shown = quoted.json()['items']
        # A quote leaves nothing behind: the family is still one nobody knows.
        self.assertFalse(Family.objects.filter(parent_id_number=PARENT_ID).exists())
        self.assertFalse(Payment.objects.exists())

        child_ids = []
        for index, item in enumerate(items):
            body = dict(item)
            earlier = body.pop('same_child_as', None)
            if earlier is not None:
                body['existing_child_id'] = child_ids[earlier]
                body['discount_confirmed'] = True
            with patch(
                'apps.core.payment_service.TranzilaService.create_recurring_payment_request',
                return_value='https://pay.test/x',
            ):
                registered = self.client.post(
                    REGISTER, {**body, 'signature': 'data:image/png;base64,AAAA'}, format='json',
                )
            self.assertEqual(registered.status_code, 201, registered.content)
            answer = registered.json()
            child_ids.append(answer['child_id'])
            for key in FIGURES:
                self.assertEqual(shown[index].get(key), answer.get(key), f'item {index}: {key}')
            self.assertEqual(
                self._types(shown[index]), self._types(answer), f'item {index}: discounts',
            )
        return shown

    @staticmethod
    def _types(item):
        return sorted(discount['type'] for discount in item['discounts_applied'])

    def _prices(self, shown):
        return [(item['base_amount'], item['discount_amount'], item['monthly_amount']) for item in shown]

    # ── two children, for the first time ─────────────────────────────────────

    def test_the_second_of_two_new_children_gets_the_second_child_discount(self):
        _second_child()

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(NOAM, self.capoeira_lesson),
        ])

        self.assertEqual(self._prices(shown), [(350.0, 0.0, 350.0), (350.0, 50.0, 300.0)])
        self.assertEqual([self._types(item) for item in shown], [[], ['second_child']])
        self.assertEqual(Child.objects.filter(family__parent_id_number=PARENT_ID).count(), 2)

    def test_a_third_new_child_gets_it_too(self):
        _second_child()
        yael = {
            'child_first_name': 'יעל', 'child_last_name': 'כהן', 'child_id_number': '039337423',
            'child_birth_date': '2014-01-15', 'child_gender': 'female',
        }

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(NOAM, self.capoeira_lesson),
            self._item(yael, self.hiphop_lesson),
        ])

        self.assertEqual([item['monthly_amount'] for item in shown], [350.0, 300.0, 300.0])

    def test_a_percentage_second_child_discount(self):
        _discount('הנחת ילד שני', '10.00', 'percentage')

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(NOAM, self.hiphop_lesson),
        ])

        self.assertEqual(self._prices(shown), [(350.0, 0.0, 350.0), (350.0, 35.0, 315.0)])

    def test_one_new_child_alone_gets_no_second_child_discount(self):
        _second_child()

        shown = self._shown_then_charged([self._item(MAYA, self.capoeira_lesson)])

        self.assertEqual(self._prices(shown), [(350.0, 0.0, 350.0)])
        self.assertEqual(shown[0]['discounts_applied'], [])

    # ── one child, two classes, for the first time ───────────────────────────

    def test_the_second_class_of_a_new_child_gets_the_another_class_price(self):
        _another_class()

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson, same_child_as=0),
        ])

        self.assertEqual(self._prices(shown), [(350.0, 0.0, 350.0), (350.0, 150.0, 200.0)])
        self.assertEqual([self._types(item) for item in shown], [[], ['additional_lesson']])
        self.assertEqual(Child.objects.filter(family__parent_id_number=PARENT_ID).count(), 1)

    def test_the_second_class_typed_again_without_the_forms_hint(self):
        """The same child typed twice, with nothing saying it is the same child: found by the details."""
        _another_class()

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson),
        ])

        self.assertEqual([item['monthly_amount'] for item in shown], [350.0, 200.0])
        self.assertEqual(Child.objects.filter(family__parent_id_number=PARENT_ID).count(), 1)

    def test_a_third_class_of_a_new_child_gets_it_too(self):
        _another_class()
        third = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(price=Decimal('350.00')), day_of_week=2,
        )

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson, same_child_as=0),
            self._item(MAYA, third, same_child_as=0),
        ])

        self.assertEqual([item['monthly_amount'] for item in shown], [350.0, 200.0, 200.0])

    def test_one_class_alone_stays_at_its_price(self):
        _another_class()

        shown = self._shown_then_charged([self._item(MAYA, self.capoeira_lesson)])

        self.assertEqual(self._prices(shown), [(350.0, 0.0, 350.0)])

    def test_another_class_price_left_at_zero_shows_nothing(self):
        _another_class('0.00')

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson, same_child_as=0),
        ])

        self.assertEqual([item['monthly_amount'] for item in shown], [350.0, 350.0])
        self.assertEqual([item['discounts_applied'] for item in shown], [[], []])

    def test_another_class_price_above_the_class_price_is_not_charged(self):
        """One figure serves every class; on a cheaper class it would raise the price, so it does not apply."""
        _another_class('400.00')

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson, same_child_as=0),
        ])

        self.assertEqual([item['monthly_amount'] for item in shown], [350.0, 350.0])

    # ── everything at once ───────────────────────────────────────────────────

    @override_settings(REGISTRATION_FEE_ILS=120)
    def test_two_new_children_two_classes_each_with_every_discount_configured(self):
        """
        The whole of it in one form. Each child's first class takes the amounts
        off (early signup, and second child for the second of them); each child's
        other class is at the another-class price, which is a final price — the
        office's own name for it — and nothing more comes off it. The yearly fee
        is charged once for each child.
        """
        today = date.today()
        _second_child()
        _another_class()
        Discount.objects.create(
            name='רישום מוקדם', discount_type='fixed', value=Decimal('10.00'), applies_to='family',
            promotion_type='temporary', start_date=today - timedelta(days=7), end_date=today + timedelta(days=7),
            is_active=True, is_built_in=True,
        )

        shown = self._shown_then_charged([
            self._item(MAYA, self.capoeira_lesson),
            self._item(MAYA, self.hiphop_lesson, same_child_as=0),
            self._item(NOAM, self.capoeira_lesson),
            self._item(NOAM, self.hiphop_lesson, same_child_as=2),
        ])

        self.assertEqual(self._prices(shown), [
            (350.0, 10.0, 340.0),
            (350.0, 150.0, 200.0),
            (350.0, 60.0, 290.0),
            (350.0, 150.0, 200.0),
        ])
        self.assertEqual([self._types(item) for item in shown], [
            ['early_signup'],
            ['additional_lesson'],
            ['early_signup', 'second_child'],
            ['additional_lesson'],
        ])
        self.assertEqual([item['registration_fee'] for item in shown], [120.0, 0.0, 120.0, 0.0])

    # ── what must not earn it ────────────────────────────────────────────────

    def test_a_signup_left_unpaid_long_ago_is_not_a_first_class(self):
        """A form abandoned hours ago is not "another class being opened now"."""
        _another_class()
        with patch(
            'apps.core.payment_service.TranzilaService.create_recurring_payment_request',
            return_value='https://pay.test/x',
        ):
            first = self.client.post(
                REGISTER,
                {**self._item(MAYA, self.capoeira_lesson), 'signature': 'data:image/png;base64,AAAA'},
                format='json',
            )
        self.assertEqual(first.status_code, 201, first.content)
        Payment.objects.update(created_at=timezone.now() - timedelta(hours=3))

        quoted = self.client.post(QUOTE, {'items': [self._item(MAYA, self.hiphop_lesson)]}, format='json')

        self.assertEqual(quoted.status_code, 200, quoted.content)
        item = quoted.json()['items'][0]
        self.assertEqual((item['discount_amount'], item['monthly_amount']), (0.0, 350.0))

    def test_a_child_who_left_does_not_get_it_for_a_single_class(self):
        """Only a class being opened in this same form counts for a child who is not on a team."""
        _another_class()
        family = TestDataFactory.create_family(name='כהן', phone=PHONE, parent_id_number=PARENT_ID)
        child = TestDataFactory.create_child(
            family=family, first_name='מאיה', last_name='כהן', id_number='218847366',
            birth_date=date(2018, 6, 21), gender='female', status='inactive',
        )

        self.assertIsNone(
            DiscountService().check_additional_lesson_discount(str(child.id), str(self.hiphop_lesson.id)),
        )
