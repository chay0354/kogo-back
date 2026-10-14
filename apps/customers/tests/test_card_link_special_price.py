"""
A monthly price the office sets for one child on a card link (owner, 14.10.2026).

The case behind it: a course that must be attended twice a week (Sunday +
Wednesday, one price), and one pupil the teacher allowed to come on Sunday
only, at a once-a-week price. The link opens her standing order on that one
lesson at that price; the catalogue is not touched.
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson, LessonBundle
from apps.customers.card_link import apply_card_link, quote_standing_order
from apps.customers.models import Child, Family, Parent, RecurringPayment
from apps.customers.tests.test_card_link import CARD, OK_CHARGE, _client_for, _user
from apps.enrollments.change_pricing import price_drift_report, price_with_signup_discounts
from apps.enrollments.models import LessonEnrollment
from apps.payment_links.models import CardLink

URL = '/api/v1/customers/card-links/'


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class SpecialMonthlyPriceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = Branch.objects.create(name='ראש העין')
        room = Room.objects.create(branch=self.branch, name='A', capacity=20)
        ctype = CourseType.objects.create(name='מחול')
        # Twice a week, both days required, one price.
        self.course = Course.objects.create(
            course_type=ctype, name='פאנקי ג׳אז + היפ הופ', price=Decimal('390.00'), capacity=12,
            branch=self.branch, must_attend_all_lessons=True,
        )
        self.sunday = Lesson.objects.create(
            course=self.course, room=room, day_of_week=0, start_time=time(17, 15), end_time=time(18, 15), is_recurring=True,
        )
        self.wednesday = Lesson.objects.create(
            course=self.course, room=room, day_of_week=3, start_time=time(17, 15), end_time=time(18, 15), is_recurring=True,
        )
        self.bundle = LessonBundle.objects.create(course=self.course, name='פעמיים בשבוע', combined_price=Decimal('390.00'))
        self.bundle.lessons.set([self.sunday, self.wednesday])

        self.family = Family.objects.create(name='כהן', phone='0501111111', branch=self.branch)
        Parent.objects.create(family=self.family, first_name='דנה', last_name='כהן', phone='0501111111', is_primary=True)
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן', birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)

    def _link(self, special=None):
        return CardLink.objects.create(
            kind=CardLink.KIND_STANDING_ORDER, child=self.child, lesson=self.sunday, branch=self.branch,
            created_by=self.manager, special_monthly_price=special,
        )

    # --- the office creates the link ---

    def test_the_office_sets_a_special_price_on_one_lesson(self):
        res = self.client.post(URL, {
            'kind': 'standing_order', 'child_id': str(self.child.id), 'lesson_id': str(self.sunday.id),
            'special_monthly_price': '260',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['special_monthly_price'], '260.00')
        self.assertEqual(Decimal(res.data['quote']['monthly_amount']), Decimal('260.00'))
        self.assertIsNone(res.data['bundle_id'])

    def test_without_it_one_day_of_a_required_course_costs_the_twice_a_week_price(self):
        """Why the office needs it: the catalogue price of a required course is for both days."""
        self.assertEqual(quote_standing_order(self._link(), date(2026, 10, 14))['monthly_amount'], Decimal('390.00'))

    def test_a_track_cannot_take_a_special_price(self):
        res = self.client.post(URL, {
            'kind': 'standing_order', 'child_id': str(self.child.id), 'bundle_id': str(self.bundle.id),
            'special_monthly_price': '260',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(CardLink.objects.exists())

    def test_a_nonsense_price_is_refused(self):
        for value in ('0', '-5', 'abc', '5001'):
            res = self.client.post(URL, {
                'kind': 'standing_order', 'child_id': str(self.child.id), 'lesson_id': str(self.sunday.id),
                'special_monthly_price': value,
            }, format='json')
            self.assertEqual(res.status_code, 400, value)
        self.assertFalse(CardLink.objects.exists())

    def test_a_blank_price_is_the_ordinary_link(self):
        res = self.client.post(URL, {
            'kind': 'standing_order', 'child_id': str(self.child.id), 'lesson_id': str(self.sunday.id),
            'special_monthly_price': '',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(res.data['special_monthly_price'])

    # --- the price ---

    def test_the_special_price_is_final(self):
        """No sibling, early-signup or another-class discount comes off it."""
        with patch('apps.customers.card_link.PaymentService') as service:
            quote = quote_standing_order(self._link(Decimal('260.00')), date(2026, 10, 14))
        service.assert_not_called()
        self.assertEqual(quote['monthly_amount'], Decimal('260.00'))
        self.assertEqual(quote['base_amount'], Decimal('390.00'))
        self.assertEqual(quote['discount_amount'], Decimal('130.00'))
        self.assertEqual([d.discount_type for d in quote['discounts']], ['fixed_final_price'])

    def test_a_special_price_above_the_catalogue_is_billed_as_it_is(self):
        quote = quote_standing_order(self._link(Decimal('420.00')), date(2026, 10, 14))
        self.assertEqual(quote['monthly_amount'], Decimal('420.00'))
        self.assertEqual(quote['discount_amount'], Decimal('0.00'))
        self.assertEqual(quote['discounts'], [])

    def test_the_first_charge_is_the_month_at_the_special_price(self):
        quote = quote_standing_order(self._link(Decimal('260.00')), date(2026, 10, 14))
        self.assertLessEqual(quote['prorated_lesson'], Decimal('260.00'))
        self.assertEqual(quote['first_charge'], quote['prorated_lesson'] + quote['registration_fee'] - quote['trial_credit'])

    # --- the parent pays ---

    def _pay(self, special=Decimal('260.00')):
        link = self._link(special)
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(link, CARD)
        self.assertTrue(out['success'], out)
        return RecurringPayment.objects.get(child=self.child)

    def test_paying_opens_the_standing_order_at_the_special_price_on_sunday_only(self):
        recurring = self._pay()
        self.assertEqual(recurring.amount, Decimal('260.00'))
        self.assertTrue(LessonEnrollment.objects.filter(child=self.child, lesson=self.sunday, status='active').exists())
        self.assertFalse(LessonEnrollment.objects.filter(child=self.child, lesson=self.wednesday).exists())
        self.assertEqual([row['type'] for row in recurring.discount_details], ['fixed_final_price'])

    def test_the_price_drift_report_does_not_call_it_a_mistake(self):
        recurring = self._pay()
        self.assertNotIn(str(recurring.id), [row['recurring_payment_id'] for row in price_drift_report()])

    def test_a_later_move_keeps_the_special_price(self):
        recurring = self._pay()
        kept, lines = price_with_signup_discounts(recurring, Decimal('390.00'))
        self.assertEqual(kept, Decimal('260.00'))
        self.assertEqual(len(lines), 1)

    def test_the_catalogue_is_untouched(self):
        """The lesson stays closed on its own: nothing about the course or its track changes."""
        self._pay()
        self.course.refresh_from_db()
        self.bundle.refresh_from_db()
        self.assertTrue(self.course.must_attend_all_lessons)
        self.assertEqual(self.course.price, Decimal('390.00'))
        self.assertEqual(set(self.bundle.lessons.all()), {self.sunday, self.wednesday})
        self.assertFalse(self.sunday.price_options.exists())
