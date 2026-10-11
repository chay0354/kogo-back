"""
A lesson change carries the discounts the child was given at signup.

Owner, 11.10.2026: a child with a sibling discount paid 250 on a 260 class. He
was moved to another 260 lesson — and the move charged the other 10 and set the
standing order to 260 for every month ahead. "כל העברת שיעור תהיה מלווה
באינפורמציה שכבר נבנתה לו בהתחלה: אם יש לו הנחת אחים, אם היה לו רישום מוקדם.
אם זה אותו סכום, זה לא אמור לשנות כלום."

The quote used to ask every discount again on the day of the move: the
early-signup window had closed by then, and a sibling who had left (or whose
card was failing) no longer made the child "a second child".
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from apps.courses.models import Course, Lesson
from apps.customers.discount_service import DiscountService
from apps.customers.financial_models import Discount
from apps.customers.models import Child, Payment
from apps.enrollments.change_pricing import _PERCENT_IN_REASON, quote_unit_change
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.tests.test_change_lesson_pricing import OK_CHARGE, TODAY, _Base

SIBLING = {
    'name': 'הנחת ילד שני', 'type': 'second_child', 'value': '10.00', 'amount_deducted': '10.00',
    'reason': 'הנחה אוטומטית לילד שני ומעלה במשפחה',
}
EARLY = {
    'name': 'הנחת רישום מוקדם', 'type': 'early_signup', 'value': '20.00', 'amount_deducted': '20.00',
    'reason': 'תשלום בתאריך 20/06/2026 נמצא בטווח רישום מוקדם',
}


def _built_in(name, value, **fields):
    return Discount.objects.create(**{
        'name': name, 'discount_type': 'fixed', 'value': Decimal(value), 'applies_to': 'child',
        'promotion_type': 'permanent', 'is_built_in': True, 'is_active': True, **fields,
    })


class _SignedUp(_Base):
    def setUp(self):
        super().setUp()
        # Another lesson of the same 260 class, on Sunday.
        self.sun = Lesson.objects.create(
            course=self.course_once, room=self.room, day_of_week=0, start_time=time(16, 45), end_time=time(17, 30),
        )
        _built_in('הנחת ילד שני', '10.00')
        _built_in(
            'הנחת רישום מוקדם', '20.00', promotion_type='temporary',
            start_date=TODAY - timedelta(days=120), end_date=TODAY - timedelta(days=60),      # the window has closed
        )

    def signed_up_with(self, *discounts, amount):
        """The standing order as signup wrote it."""
        self.recurring.discount_details = list(discounts)
        self.recurring.discount_amount = sum((Decimal(d['amount_deducted']) for d in discounts), Decimal('0.00'))
        self.recurring.amount = Decimal(amount)
        self.recurring.save()

    def a_sibling_on_a_team(self):
        sibling = Child.objects.create(
            family=self.family, first_name='Dan', last_name='Cohen', birth_date=date(2016, 1, 1),
            gender='male', status='active',
        )
        LessonEnrollment.objects.create(lesson=self.mon, child=sibling, status='active', start_date=date(2026, 8, 1))
        return sibling

    def quote(self, *targets, bundle=None):
        return quote_unit_change(
            enrollment=self.enrollment, target_lessons=list(targets), target_bundle=bundle, today=TODAY,
        )

    def move(self, body):
        with patch('apps.enrollments.change_pricing.TranzilaService.for_saved_card') as saved_card, self._today():
            saved_card.return_value.charge_with_token.return_value = OK_CHARGE
            res = self.client.post(self.url, body, format='json')
        return res, saved_card.return_value.charge_with_token


class SamePriceChangesNothingTest(_SignedUp):
    def assert_only_moved(self, amount):
        res, charge = self.move({'lesson_id': str(self.sun.id)})
        self.assertEqual(res.status_code, 200, res.content)
        charge.assert_not_called()
        self.assertFalse(Payment.objects.filter(payment_type='one_time').exists())
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal(amount))
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.sun.id)

    def test_a_sibling_discount_stays_after_the_sibling_left(self):
        # The owner's case: nobody else of the family is on a team any more.
        self.signed_up_with(SIBLING, amount='250.00')
        quote = self.quote(self.sun)
        self.assertEqual(quote['direction'], 'same')
        self.assertEqual(quote['new_amount'], '250.00')
        self.assertEqual(quote['prorated_difference'], '0.00')
        self.assert_only_moved('250.00')
        self.assertIsNone(self.recurring.pending_amount)

    def test_an_early_signup_discount_stays_after_its_window_closed(self):
        self.signed_up_with(EARLY, amount='240.00')
        self.assertEqual(self.quote(self.sun)['direction'], 'same')
        self.assert_only_moved('240.00')
        self.assertIsNone(self.recurring.pending_amount)

    def test_both_together(self):
        self.signed_up_with(SIBLING, EARLY, amount='230.00')
        self.assertEqual(self.quote(self.sun)['direction'], 'same')
        self.assert_only_moved('230.00')

    def test_a_child_who_never_had_the_sibling_discount_is_not_given_it_by_moving(self):
        # The first child of the family pays the full price. The move used to
        # find the sibling, lower the price by 10 — and hold the move itself
        # back until next month, as a downgrade.
        self.a_sibling_on_a_team()
        quote = self.quote(self.sun)
        self.assertEqual(quote['direction'], 'same')
        self.assertEqual(quote['new_amount'], '260.00')
        self.assert_only_moved('260.00')
        self.assertIsNone(self.recurring.pending_amount)

    def test_a_price_the_office_set_by_hand_stays(self):
        self.signed_up_with(amount='200.00')
        self.assertEqual(self.quote(self.sun)['direction'], 'same')
        self.assert_only_moved('200.00')
        self.assertIsNone(self.recurring.pending_amount)

    def test_an_amount_already_waiting_for_next_month_is_left_alone(self):
        self.recurring.pending_amount = Decimal('280.00')
        self.recurring.pending_amount_effective_date = date(2026, 10, 1)
        self.recurring.save()
        quote = self.quote(self.sun)
        self.assertEqual(quote['direction'], 'same')
        self.assertFalse(quote['clears_pending'])
        self.assert_only_moved('260.00')
        self.assertEqual(self.recurring.pending_amount, Decimal('280.00'))

    def test_a_partner_can_make_the_move(self):
        # A same-price move is not a pricing act — it must stay one for a child with a discount.
        self.signed_up_with(SIBLING, amount='250.00')
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from apps.core.models import UserProfile
        partner = get_user_model().objects.create_user(username='p@test.com', email='p@test.com', password='x')
        UserProfile.objects.update_or_create(user=partner, defaults={'role': UserProfile.ROLE_PARTNER})
        partner.profile.assigned_branches.add(self.branch)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=partner).key}')
        self.assert_only_moved('250.00')


class AnotherPriceKeepsTheSignupDiscountsTest(_SignedUp):
    def test_the_new_price_less_the_sibling_discount(self):
        self.signed_up_with(SIBLING, amount='250.00')
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['direction'], 'up')
        self.assertEqual(quote['new_base_price'], '335.00')
        self.assertEqual(quote['new_amount'], '325.00')
        self.assertEqual(quote['difference'], '75.00')
        self.assertEqual(quote['prorated_difference'], '56.25')         # 75 × 6/8 — the price gap alone
        self.assertEqual(quote['discount_amount'], '10.00')
        self.assertEqual([line['name'] for line in quote['discounts']], ['הנחת ילד שני'])

    def test_the_move_charges_the_gap_and_schedules_the_discounted_amount(self):
        self.signed_up_with(SIBLING, amount='250.00')
        res, charge = self.move({'bundle_id': str(self.bundle.id), 'expected_new_amount': '325.00'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(charge.call_args.kwargs['amount'], Decimal('56.25'))
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('250.00'))
        self.assertEqual(self.recurring.pending_amount, Decimal('325.00'))

    def test_an_early_signup_discount_goes_along_after_its_window_closed(self):
        self.signed_up_with(SIBLING, EARLY, amount='230.00')
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '305.00')
        self.assertEqual(quote['difference'], '75.00')

    def test_a_percentage_stays_the_same_percentage(self):
        early = {**EARLY, 'value': '26.00', 'amount_deducted': '26.00', 'reason': EARLY['reason'] + ' (10%)'}
        self.signed_up_with(early, amount='234.00')
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '301.50')                  # 335 less 10%
        self.assertEqual(quote['discounts'][0]['value'], '33.50')

    def test_a_fixed_price_stays_that_price(self):
        # "מחיר קבוע": 200 on a 260 class. The signup payment holds the 260.
        fixed = {'name': 'מבצע רישום מוקדם', 'type': 'fixed_final_price', 'value': '60.00', 'amount_deducted': '60.00',
                 'reason': 'מחיר קבוע: ₪200.00 (במקום ₪260.00)'}
        self.signed_up_with(fixed, amount='200.00')
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '200.00')
        self.assertEqual(quote['direction'], 'same')

    def test_a_discount_in_force_today_is_not_picked_up_on_the_way(self):
        self.a_sibling_on_a_team()
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '335.00')
        self.assertEqual(quote['discounts'], [])

    def test_a_move_down_keeps_them_too(self):
        dearer = Course.objects.create(
            course_type=self.course_once.course_type, name='אקרובטיקה', price=300, capacity=10, branch=self.branch,
        )
        tue = Lesson.objects.create(course=dearer, room=self.room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0))
        self.enrollment.lesson = tue
        self.enrollment.save()
        self.payment.lesson = tue
        self.payment.base_amount = Decimal('300.00')
        self.payment.save()
        self.signed_up_with(SIBLING, amount='290.00')
        quote = self.quote(self.sun)
        self.assertEqual(quote['direction'], 'down')
        self.assertEqual(quote['new_amount'], '250.00')

    def test_a_child_who_lost_the_discount_to_an_earlier_move_gets_it_back_with_the_new_price(self):
        # Before this rule: signed up at 250, a move made it 260. The record of
        # the discount is still on the standing order.
        self.signed_up_with(SIBLING, amount='260.00')
        quote = self.quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '325.00')

    def test_the_percent_marker_is_the_one_the_discount_service_writes(self):
        discount = Discount(discount_type='percentage', value=Decimal('12.50'))
        reason = DiscountService._with_percent(discount, 'הנחה אוטומטית לילד שני ומעלה במשפחה')
        self.assertEqual(_PERCENT_IN_REASON.search(reason).group(1), '12.5')
        self.assertIsNone(_PERCENT_IN_REASON.search(SIBLING['reason']))


class CurrentUnitWithoutOnePriceTest(_SignedUp):
    def test_two_loose_classes_of_one_course_are_compared_by_amount(self):
        # Two lessons of a course that is neither a track nor attended in full:
        # there is no single price for "what the child sits on", so the new
        # amount is set against the amount charged — with the signup discounts.
        loose = Lesson.objects.create(
            course=self.course_once, room=self.room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0),
        )
        LessonEnrollment.objects.create(lesson=loose, child=self.child, status='active', start_date=date(2026, 8, 1))
        self.signed_up_with(SIBLING, amount='250.00')
        quote = self.quote(self.sun)
        self.assertEqual(quote['new_amount'], '250.00')
        self.assertEqual(quote['direction'], 'same')


class PriceDriftBySignupDiscountsTest(_SignedUp):
    def drift(self):
        res = self.client.get('/api/v1/customers/recurring-payments/price-drift/')
        self.assertEqual(res.status_code, 200, res.content)
        return res.data['results']

    def test_a_child_paying_the_price_less_the_signup_discounts_is_not_listed(self):
        self.signed_up_with(SIBLING, EARLY, amount='230.00')
        self.assertEqual(self.drift(), [])

    def test_a_first_child_paying_the_full_price_is_not_listed(self):
        self.a_sibling_on_a_team()
        self.assertEqual([row['child_name'] for row in self.drift()], [])

    def test_a_child_whose_discount_a_move_took_away_is_listed(self):
        self.signed_up_with(SIBLING, amount='260.00')
        rows = self.drift()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['current_amount'], '260.00')
        self.assertEqual(rows[0]['expected_amount'], '250.00')
        self.assertEqual(rows[0]['direction'], 'down')

    def test_the_first_of_two_classes_is_not_listed_for_paying_the_full_price(self):
        # A second class joined later, at the second-class price, on its own standing order.
        _built_in('הנחת שיעור נוסף לילד פעיל', '10.00')
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 9, 1))
        second = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.mon,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('10.00'), final_amount=Decimal('250.00'),
        )
        from apps.customers.models import RecurringPayment
        RecurringPayment.objects.create(
            child=self.child, initial_payment=second, status='active',
            tranzila_token='tok_saved', card_expire_month=12, card_expire_year=2030,
            base_amount=Decimal('260.00'), discount_amount=Decimal('10.00'), amount=Decimal('250.00'), billing_day=1,
            start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1),
            discount_details=[{'name': 'הנחת שיעור נוסף לילד פעיל', 'type': 'additional_lesson', 'value': '10.00',
                               'amount_deducted': '10.00', 'reason': 'חוג נוסף לאותו ילד'}],
        )
        self.assertEqual(self.drift(), [])

    def test_a_second_class_moving_at_the_same_price_changes_nothing_for_either(self):
        _built_in('הנחת שיעור נוסף לילד פעיל', '10.00')
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 9, 1))
        # The first class (full price) moves to another lesson of the same price.
        quote = self.quote(self.sun)
        self.assertEqual(quote['direction'], 'same')
        self.assertEqual(quote['new_amount'], '260.00')

    def test_a_discount_a_move_is_about_to_take_away_is_listed_before_it_is_charged(self):
        # Moved before this rule: still 250 this month, 260 waiting for the 1st.
        self.signed_up_with(SIBLING, amount='250.00')
        self.recurring.pending_amount = Decimal('260.00')
        self.recurring.pending_amount_effective_date = date(2026, 10, 1)
        self.recurring.save()
        rows = self.drift()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['current_amount'], rows[0]['pending_amount'], rows[0]['expected_amount']), ('250.00', '260.00', '250.00'))
        self.assertEqual(rows[0]['direction'], 'down')

    def test_a_scheduled_move_to_a_cheaper_unit_is_not_a_mismatch(self):
        from apps.enrollments.models import ScheduledUnitChange
        self.recurring.pending_amount = Decimal('200.00')
        self.recurring.pending_amount_effective_date = date(2026, 10, 1)
        self.recurring.save()
        change = ScheduledUnitChange.objects.create(
            enrollment=self.enrollment, child=self.child, recurring_payment=self.recurring,
            effective_date=date(2026, 10, 1), old_amount=Decimal('260.00'), new_amount=Decimal('200.00'),
            target_label='x',
        )
        change.target_lessons.set([self.sun])
        self.assertEqual(self.drift(), [])
