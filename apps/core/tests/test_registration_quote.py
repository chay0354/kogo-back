"""
A price quoted from facts is the price the registration charges.

`registration_quote.quote` works a signup out for someone who has no card in
Kogo, from what is told about them. Each case here builds the real thing beside
it — a child with that sibling, that other class, that paid trial — and holds
the two against each other, figure by figure:

  * the registration itself, priced without writing
    (`PaymentService.initiate_subscription_payment(quote_only=True)`), and
  * the registration form's own quote (`widget/quote/`), which rehearses a
    signup and rolls it back.

If the registration's arithmetic changes and the quote does not follow, these fail.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core import registration_quote
from apps.core.payment_service import JERUSALEM_TZ, PaymentService, first_month_charge
from apps.core.registration_quote import Facts
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle
from apps.customers.financial_models import Discount
from apps.customers.models import Child, Family, Payment
from apps.enrollments.models import LessonEnrollment

# Every figure of a registration's answer that a quote from facts also gives.
FIGURES = (
    'course_index', 'base_amount', 'discount_amount', 'monthly_amount', 'prorated_amount', 'prorate_factor',
    'prorate_lessons_remaining', 'total_lessons_this_month', 'registration_fee', 'trial_credit_amount', 'final_amount',
)


def _today():
    from django.utils import timezone

    return timezone.now().astimezone(JERUSALEM_TZ).date()


def _discount(name, value, kind='fixed', **extra):
    return Discount.objects.create(
        name=name, discount_type=kind, value=Decimal(value), applies_to='child',
        promotion_type='permanent', is_active=True, is_built_in=True, **extra,
    )


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class QuoteFromFactsEqualsTheRegistrationTest(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = TestDataFactory.create_branch()
        self.course = TestDataFactory.create_course(
            name='קפוארה', branch=self.branch, price=Decimal('260.00'),
            trial_lesson_is_paid=True, trial_lesson_price=Decimal('30.00'),
        )
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=2)
        self.other_course = TestDataFactory.create_course(name='מחול', branch=self.branch, price=Decimal('300.00'))
        self.other_lesson = TestDataFactory.create_lesson(course=self.other_course, day_of_week=4)
        self.family = TestDataFactory.create_family(branch=self.branch)
        TestDataFactory.create_parent(family=self.family)
        self.child = TestDataFactory.create_child(family=self.family, id_number='234567892')

    # --- the two sides ---

    def _registration(self, child=None, lesson=None, **extra):
        """The registration's own figures for a real child, with nothing written."""
        return PaymentService().initiate_subscription_payment(
            child_id=str((child or self.child).id), lesson_id=str((lesson or self.lesson).id), quote_only=True, **extra,
        )

    def _from_facts(self, lesson=None, **extra):
        facts = Facts(**{key: extra.pop(key) for key in list(extra) if key in ('sibling_registered', 'another_class', 'paid_trial')})
        return registration_quote.quote(lesson or self.lesson, today=_today(), facts=facts, **extra)

    def assertSameFigures(self, facts_quote, registration):
        for key in FIGURES:
            self.assertEqual(
                Decimal(str(facts_quote[key])).quantize(Decimal('0.0001')),
                Decimal(str(registration[key])).quantize(Decimal('0.0001')),
                f'{key}: quote from facts {facts_quote[key]} != registration {registration[key]}',
            )
        self.assertEqual(facts_quote['next_billing_date'].isoformat(), registration['next_billing_date'])
        self.assertEqual(
            [(row['name'], row['type'], float(row['value']), row['reason']) for row in facts_quote['discounts_applied']],
            [(row['name'], row['type'], row['value'], row['reason']) for row in registration['discounts_applied']],
        )

    def _sibling_on_a_team(self):
        sibling = TestDataFactory.create_child(family=self.family, first_name='אח', status='active', id_number='345678903')
        LessonEnrollment.objects.create(child=sibling, lesson=self.other_lesson, status='active')
        return sibling

    def _already_a_student(self):
        """The child pays for another class: on a team, with the yearly fee behind them."""
        Child.objects.filter(pk=self.child.pk).update(status='active')
        self.child.refresh_from_db()
        LessonEnrollment.objects.create(child=self.child, lesson=self.other_lesson, status='active')
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.other_lesson,
            payment_type='recurring_subscription', status='completed', base_amount=Decimal('300.00'),
            discount_amount=Decimal('0.00'), final_amount=Decimal('420.00'), registration_fee=Decimal('120.00'),
        )

    def _paid_trial(self, amount='30.00', days_ago=7):
        return Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='one_time', status='completed', base_amount=Decimal(amount),
            discount_amount=Decimal('0.00'), final_amount=Decimal(amount),
            trial_lesson_date=date.today() - timedelta(days=days_ago), description='שיעור ניסיון',
        )

    # --- the cases ---

    def test_a_new_child_with_nothing_known(self):
        quote = self._from_facts()
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['registration_fee'], Decimal('120.00'))
        self.assertEqual(quote['final_amount'], quote['prorated_amount'] + Decimal('120.00'))

    def test_a_course_with_its_own_registration_fee(self):
        self.course.registration_fee_override = Decimal('60.00')
        self.course.save(update_fields=['registration_fee_override'])
        quote = self._from_facts()
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['registration_fee'], Decimal('60.00'))

    def test_a_sibling_on_a_team_with_an_amount_off(self):
        _discount('הנחת ילד שני', '10.00')
        self._sibling_on_a_team()
        quote = self._from_facts(sibling_registered=True)
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['monthly_amount'], Decimal('250.00'))
        # Without the fact the discount is not taken: nothing about a family is assumed.
        self.assertEqual(self._from_facts()['monthly_amount'], Decimal('260.00'))

    def test_a_sibling_with_a_percentage_and_with_a_fixed_final_price(self):
        row = _discount('הנחת ילד שני', '10.00', kind='percentage')
        self._sibling_on_a_team()
        self.assertSameFigures(self._from_facts(sibling_registered=True), self._registration())
        row.discount_type, row.value = 'fixed_final_price', Decimal('200.00')
        row.save()
        quote = self._from_facts(sibling_registered=True)
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['monthly_amount'], Decimal('200.00'))

    def test_an_early_signup_range_that_holds_today(self):
        _discount('רישום מוקדם', '20.00', start_date=date.today() - timedelta(days=3), end_date=date.today() + timedelta(days=3))
        quote = self._from_facts()
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['monthly_amount'], Decimal('240.00'))

    def test_a_second_class_of_the_same_child_with_the_another_class_discount(self):
        _discount('הנחת שיעור נוסף', '10.00')
        _discount('הנחת ילד שני', '15.00')
        self._already_a_student()
        quote = self._from_facts(another_class=True)
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['registration_fee'], Decimal('0.00'))       # once per child
        self.assertEqual(quote['monthly_amount'], Decimal('250.00'))
        # …and with a brother on a team as well, the two add up.
        self._sibling_on_a_team()
        both = self._from_facts(another_class=True, sibling_registered=True)
        self.assertSameFigures(both, self._registration())
        self.assertEqual(both['monthly_amount'], Decimal('235.00'))

    def test_a_second_class_priced_by_the_lessons_own_tier(self):
        _discount('הנחת שיעור נוסף', '10.00')
        self.lesson.additional_course_prices = [{'course_index': 2, 'price': 200}]
        self.lesson.save(update_fields=['additional_course_prices'])
        self._already_a_student()
        quote = self._from_facts(another_class=True)
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['monthly_amount'], Decimal('200.00'))       # the tier, and no discount on top of it

    def test_a_paid_trial_comes_off_the_first_charge_once(self):
        self._paid_trial()
        quote = self._from_facts(paid_trial=Decimal('30.00'))
        registration = self._registration()
        self.assertSameFigures(quote, registration)
        self.assertEqual(quote['trial_credit_amount'], Decimal('30.00'))
        self.assertEqual(quote['monthly_amount'], Decimal('260.00'))       # the month itself is untouched

    def test_a_trial_that_cost_more_than_the_first_charge_is_capped(self):
        self.course.price = Decimal('8.00')
        self.course.registration_fee_override = Decimal('0.00')
        self.course.save(update_fields=['price', 'registration_fee_override'])
        self._paid_trial(amount='30.00')
        quote = self._from_facts(paid_trial=Decimal('30.00'))
        self.assertSameFigures(quote, self._registration())
        self.assertEqual(quote['final_amount'], Decimal('0.00'))

    def test_a_real_childs_credit_can_be_handed_in(self):
        from apps.customers.trial_credit import credit_for_lesson

        self._paid_trial()
        quote = registration_quote.quote(
            self.lesson, today=_today(),
            credit_for=lambda first_charge: credit_for_lesson(self.child, self.lesson, first_charge=first_charge)['amount'],
        )
        self.assertSameFigures(quote, self._registration())

    def test_a_twice_a_week_track_and_a_track_every_day_of_which_is_required(self):
        second = TestDataFactory.create_lesson(course=self.course, day_of_week=4)
        bundle = LessonBundle.objects.create(course=self.course, name='פעמיים בשבוע', combined_price=Decimal('400.00'))
        bundle.lessons.set([self.lesson, second])
        quote = self._from_facts(bundle=bundle)
        self.assertSameFigures(quote, self._registration(bundle_id=str(bundle.id)))
        self.assertEqual(quote['monthly_amount'], Decimal('400.00'))
        self.course.must_attend_all_lessons = True
        self.course.save(update_fields=['must_attend_all_lessons'])
        bundle.refresh_from_db()
        required = self._from_facts(bundle=bundle)
        self.assertSameFigures(required, self._registration(bundle_id=str(bundle.id)))
        self.assertEqual(required['monthly_amount'], Decimal('260.00'))

    def test_while_monthly_billing_is_put_off_only_the_fee_is_charged(self):
        later = (date.today() + timedelta(days=40)).isoformat()
        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=later):
            quote = self._from_facts()
            self.assertSameFigures(quote, self._registration())
            self.assertEqual((quote['prorated_amount'], quote['final_amount']), (Decimal('0.00'), Decimal('120.00')))
            self.assertEqual(quote['next_billing_date'].isoformat(), later)

    def test_a_lesson_without_a_price_is_refused_as_the_registration_refuses_it(self):
        self.course.price = Decimal('0.00')
        self.course.save(update_fields=['price'])
        with self.assertRaises(ValueError):
            self._from_facts()
        with self.assertRaises(ValueError):
            self._registration()

    def test_nothing_is_written(self):
        _discount('הנחת ילד שני', '10.00')
        before = (Payment.objects.count(), Family.objects.count(), Child.objects.count(), LessonEnrollment.objects.count())
        self._from_facts(sibling_registered=True, another_class=True, paid_trial=Decimal('30.00'))
        self.assertEqual(
            (Payment.objects.count(), Family.objects.count(), Child.objects.count(), LessonEnrollment.objects.count()), before,
        )

    def test_the_forms_own_quote_for_a_new_family_shows_the_same_figures(self):
        """`widget/quote/` — the registration rehearsed and rolled back — against the quote from facts."""
        _discount('רישום מוקדם', '20.00', start_date=date.today() - timedelta(days=3), end_date=date.today() + timedelta(days=3))
        response = APIClient().post('/api/v1/customers/widget/quote/', {'items': [{
            'parent_id_number': '123456782', 'parent_first_name': 'Test', 'parent_last_name': 'Parent',
            'parent_phone': '0501234567', 'child_first_name': 'Kid', 'child_last_name': 'Parent',
            'child_id_number': '456789014', 'child_birth_date': '2019-01-01', 'child_gender': 'male',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
        }]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        form = response.json()['items'][0]
        quote = self._from_facts()
        for key in ('base_amount', 'discount_amount', 'monthly_amount', 'prorated_amount', 'registration_fee',
                    'trial_credit_amount', 'final_amount', 'prorate_lessons_remaining', 'total_lessons_this_month'):
            self.assertEqual(float(quote[key]), float(form[key]), key)
        self.assertEqual(quote['next_billing_date'].isoformat(), form['next_billing_date'])
        self.assertEqual(Family.objects.filter(parent_id_number='123456782').count(), 0)   # the form's quote left nothing


@override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE='')
class FirstMonthChargeTest(TestCase):
    """The lesson part of a first charge on fixed dates. October 2026 has four Sundays: 4, 11, 18, 25."""

    def setUp(self):
        self.lesson = TestDataFactory.create_lesson(day_of_week=0)      # Sunday

    def _charge(self, today, monthly='260.00'):
        return first_month_charge(full_monthly_amount=Decimal(monthly), lesson=self.lesson, today=today)

    def test_the_month_is_charged_by_the_lessons_still_ahead(self):
        self.assertEqual(self._charge(date(2026, 10, 1))['amount'], Decimal('260.00'))
        half = self._charge(date(2026, 10, 15))
        self.assertEqual((half['lessons_remaining'], half['total_lessons'], half['amount']), (2, 4, Decimal('130.00')))
        self.assertEqual(self._charge(date(2026, 10, 25))['amount'], Decimal('65.00'))      # the lesson of that very day counts
        self.assertEqual(half['next_billing_date'], date(2026, 11, 1))

    def test_a_month_with_no_lesson_left_still_charges_one_shekel(self):
        self.assertEqual(self._charge(date(2026, 10, 26))['amount'], Decimal('1.00'))

    def test_a_free_month_charges_nothing(self):
        self.assertEqual(self._charge(date(2026, 10, 15), monthly='0.00')['amount'], Decimal('0.00'))

    def test_a_course_billed_at_once_starts_its_standing_order_today(self):
        self.lesson.course.charge_standing_order_immediately = True
        self.lesson.course.save(update_fields=['charge_standing_order_immediately'])
        self.assertEqual(self._charge(date(2026, 10, 15))['next_billing_date'], date(2026, 10, 15))
