"""
A parent who paid for a trial lesson does not pay for it twice: the amount comes
off the first charge of the registration, once, in every path that charges it.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.core.payment_service import (
    PaymentService,
    payment_is_fee_only,
    payment_prorated_lesson_amount,
    subscription_tranzila_items,
)
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent, Payment
from apps.customers.trial_credit import (
    TRIAL_CREDIT_WINDOW_DAYS,
    credit_still_held_by,
    creditable_trial_payment,
    credit_for_lesson,
)

User = get_user_model()


class TrialCreditBase(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='סניף א')
        self.other_branch = Branch.objects.create(name='סניף ב')
        self.room = Room.objects.create(branch=self.branch, name='אולם', capacity=30)
        ct = CourseType.objects.create(name="ג'ודו")
        self.course = Course.objects.create(
            course_type=ct, name="ג'ודו א", price=Decimal('260.00'), capacity=20, branch=self.branch,
            trial_lesson_is_paid=True, trial_lesson_price=Decimal('30.00'),
        )
        self.lesson = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=0, start_time='16:00', end_time='17:00',
        )
        self.sister_course = Course.objects.create(
            course_type=ct, name='כדורסל א', price=Decimal('300.00'), capacity=20, branch=self.branch,
        )
        self.sister_lesson = Lesson.objects.create(
            course=self.sister_course, room=self.room, day_of_week=2, start_time='17:00', end_time='18:00',
        )
        far_room = Room.objects.create(branch=self.other_branch, name='אולם ב', capacity=30)
        self.far_course = Course.objects.create(
            course_type=ct, name="ג'ודו ב", price=Decimal('260.00'), capacity=20, branch=self.other_branch,
        )
        self.far_lesson = Lesson.objects.create(
            course=self.far_course, room=far_room, day_of_week=1, start_time='16:00', end_time='17:00',
        )
        self.family = Family.objects.create(name='כהן', phone='0501234567', branch=self.branch)
        Parent.objects.create(
            family=self.family, first_name='אבי', last_name='כהן', phone='0501234567', is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן', id_number='111111118',
            birth_date=date(2015, 1, 1), gender='female', status='pending',
        )

    def _paid_trial(self, *, lesson=None, days_ago=7, amount='30.00', status='completed', child=None):
        lesson = lesson or self.lesson
        return Payment.objects.create(
            child=child or self.child, family=(child or self.child).family, branch=lesson.course.branch,
            lesson=lesson, payment_type='one_time', status=status,
            base_amount=Decimal(amount), discount_amount=Decimal('0.00'), final_amount=Decimal(amount),
            trial_lesson_date=date.today() - timedelta(days=days_ago),
            description='שיעור ניסיון',
        )


class CreditRulesTest(TrialCreditBase):
    def test_a_paid_trial_is_credited_against_a_registration_in_the_same_branch(self):
        trial = self._paid_trial()
        quote = credit_for_lesson(self.child, self.lesson, first_charge=Decimal('380.00'))
        self.assertEqual(quote['amount'], Decimal('30.00'))
        self.assertEqual(quote['source'], trial)
        self.assertIn('שיעור ניסיון', quote['reason'])
        self.assertIn('30', quote['reason'])

    def test_another_course_in_the_same_branch_is_credited_too(self):
        self._paid_trial()
        quote = credit_for_lesson(self.child, self.sister_lesson, first_charge=Decimal('380.00'))
        self.assertEqual(quote['amount'], Decimal('30.00'))

    def test_another_branch_is_not_credited(self):
        self._paid_trial()
        quote = credit_for_lesson(self.child, self.far_lesson, first_charge=Decimal('380.00'))
        self.assertEqual(quote['amount'], Decimal('0.00'))
        self.assertIsNone(quote['source'])

    def test_a_trial_older_than_the_window_is_not_credited(self):
        self._paid_trial(days_ago=TRIAL_CREDIT_WINDOW_DAYS + 1)
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_trial_on_the_last_day_of_the_window_still_counts(self):
        self._paid_trial(days_ago=TRIAL_CREDIT_WINDOW_DAYS)
        self.assertIsNotNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_trial_already_paid_for_but_still_ahead_is_credited(self):
        self._paid_trial(days_ago=-7)
        quote = credit_for_lesson(self.child, self.lesson, first_charge=Decimal('380.00'))
        self.assertEqual(quote['amount'], Decimal('30.00'))
        self.assertIn('שיעור ניסיון שנקבע ל', quote['reason'])

    def test_a_trial_that_was_never_charged_is_not_credited(self):
        self._paid_trial(status='pending')
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_free_trial_credits_nothing(self):
        self._paid_trial(amount='0.00')
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_the_credit_never_exceeds_the_charge(self):
        self._paid_trial(amount='50.00')
        quote = credit_for_lesson(self.child, self.lesson, first_charge=Decimal('20.00'))
        self.assertEqual(quote['amount'], Decimal('20.00'))
        self.assertIn('עד גובה התשלום הראשון', quote['reason'])

    def test_nothing_to_charge_means_nothing_to_credit(self):
        self._paid_trial()
        self.assertEqual(credit_for_lesson(self.child, self.lesson, first_charge=Decimal('0.00'))['amount'], Decimal('0.00'))

    def test_a_credited_trial_is_not_credited_again(self):
        trial = self._paid_trial()
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('350.00'),
            registration_fee=Decimal('120.00'), trial_credit_amount=Decimal('30.00'), trial_credit_source=trial,
        )
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_registration_that_failed_releases_the_credit(self):
        trial = self._paid_trial()
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='recurring_subscription', status='failed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('350.00'),
            registration_fee=Decimal('120.00'), trial_credit_amount=Decimal('30.00'), trial_credit_source=trial,
        )
        self.assertEqual(creditable_trial_payment(self.child, branch_id=self.branch.id), trial)

    def test_an_abandoned_checkout_releases_the_credit_after_two_hours(self):
        trial = self._paid_trial()
        held = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='recurring_subscription', status='pending',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('350.00'),
            registration_fee=Decimal('120.00'), trial_credit_amount=Decimal('30.00'), trial_credit_source=trial,
        )
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))
        Payment.objects.filter(pk=held.pk).update(created_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(creditable_trial_payment(self.child, branch_id=self.branch.id), trial)

    def test_two_paid_trials_are_credited_oldest_first_and_each_once(self):
        older = self._paid_trial(days_ago=20, amount='30.00')
        newer = self._paid_trial(days_ago=5, amount='50.00', lesson=self.sister_lesson)
        self.assertEqual(creditable_trial_payment(self.child, branch_id=self.branch.id), older)
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('350.00'),
            registration_fee=Decimal('120.00'), trial_credit_amount=Decimal('30.00'), trial_credit_source=older,
        )
        self.assertEqual(creditable_trial_payment(self.child, branch_id=self.branch.id), newer)

    def test_an_id_number_written_with_leading_zeros_still_matches(self):
        # Same parent phone, id typed with and without the leading zero.
        Child.objects.filter(pk=self.child.pk).update(id_number='011111118')
        self.child.refresh_from_db()
        twin_family = Family.objects.create(name='כהן', phone='0501234567', branch=self.branch)
        twin = Child.objects.create(
            family=twin_family, first_name='נועה', last_name='כהן', id_number='11111118',
            birth_date=date(2015, 1, 1), gender='female', status='pending',
        )
        self._paid_trial(child=twin)
        self.assertIsNotNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_child_without_an_id_number_matches_only_itself(self):
        Child.objects.filter(pk=self.child.pk).update(id_number='')
        self.child.refresh_from_db()
        other_family = Family.objects.create(name='לוי', phone='0508888888', branch=self.branch)
        stranger = Child.objects.create(
            family=other_family, first_name='דן', last_name='לוי', id_number='',
            birth_date=date(2015, 1, 1), gender='male', status='pending',
        )
        self._paid_trial(child=stranger)
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_split_family_of_the_same_parent_is_credited(self):
        # The widget opens a second family when a parent types their id differently.
        Family.objects.filter(pk=self.family.pk).update(parent_id_number='039876545')
        twin_family = Family.objects.create(
            name='כהן', phone='0501234567', parent_id_number='39876545', branch=self.branch,
        )
        twin = Child.objects.create(
            family=twin_family, first_name='נועה', last_name='כהן', id_number='111111118',
            birth_date=date(2015, 1, 1), gender='female', status='pending',
        )
        self._paid_trial(child=twin)
        self.child.refresh_from_db()
        self.assertIsNotNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_sibling_in_the_same_family_shares_the_credit(self):
        sibling = Child.objects.create(
            family=self.family, first_name='דן', last_name='כהן', id_number='222222226',
            birth_date=date(2013, 1, 1), gender='male', status='pending',
        )
        self._paid_trial(child=sibling)
        self.assertIsNotNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_an_unrelated_family_with_the_same_child_id_is_not_credited(self):
        stranger_family = Family.objects.create(
            name='לוי', phone='0507777777', parent_id_number='888888888', branch=self.branch,
        )
        stranger = Child.objects.create(
            family=stranger_family, first_name='נועה', last_name='לוי', id_number='111111118',
            birth_date=date(2015, 1, 1), gender='female', status='pending',
        )
        self._paid_trial(child=stranger)
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))

    def test_a_placeholder_id_number_does_not_make_children_twins(self):
        Child.objects.filter(pk=self.child.pk).update(id_number='000000000')
        self.child.refresh_from_db()
        stranger_family = Family.objects.create(
            name='לוי', phone='0507777777', parent_id_number='888888888', branch=self.branch,
        )
        stranger = Child.objects.create(
            family=stranger_family, first_name='דן', last_name='לוי', id_number='000000000',
            birth_date=date(2015, 1, 1), gender='male', status='pending',
        )
        self._paid_trial(child=stranger)
        self.assertIsNone(creditable_trial_payment(self.child, branch_id=self.branch.id))


class GatewayLinesTest(TrialCreditBase):
    def test_the_lines_add_up_to_what_the_card_is_charged(self):
        items = subscription_tranzila_items(
            label='ג׳ודו', prorated_lesson=Decimal('195.00'), registration_fee=Decimal('120.00'),
            trial_credit=Decimal('30.00'),
        )
        self.assertEqual(sum(Decimal(str(i['unit_price'])) for i in items), Decimal('285.00'))
        self.assertIn('בניכוי שיעור ניסיון', items[0]['name'])

    def test_a_credit_bigger_than_the_month_eats_into_the_fee(self):
        items = subscription_tranzila_items(
            label='ג׳ודו', prorated_lesson=Decimal('20.00'), registration_fee=Decimal('120.00'),
            trial_credit=Decimal('50.00'),
        )
        self.assertEqual(sum(Decimal(str(i['unit_price'])) for i in items), Decimal('90.00'))
        self.assertEqual(len(items), 1)
        self.assertIn('דמי רישום', items[0]['name'])

    def test_no_credit_leaves_the_lines_exactly_as_before(self):
        items = subscription_tranzila_items(
            label='ג׳ודו', prorated_lesson=Decimal('195.00'), registration_fee=Decimal('120.00'),
        )
        self.assertEqual([i['name'] for i in items], ['מנוי חודשי (יחסי) - ג׳ודו', 'דמי רישום'])
        self.assertEqual(sum(Decimal(str(i['unit_price'])) for i in items), Decimal('315.00'))

    def test_the_month_a_credited_payment_bought_is_still_a_month(self):
        payment = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.lesson,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'),
            final_amount=Decimal('90.00'), registration_fee=Decimal('120.00'),
            trial_credit_amount=Decimal('30.00'),
        )
        # 90 = 0 prorated? No: 90 − 120 + 30 = 0 … a fee-only signup stays fee-only.
        self.assertEqual(payment_prorated_lesson_amount(payment), Decimal('0.00'))
        self.assertTrue(payment_is_fee_only(payment))
        payment.final_amount = Decimal('285.00')   # 195 month + 120 fee − 30 credit
        payment.save(update_fields=['final_amount'])
        self.assertEqual(payment_prorated_lesson_amount(payment), Decimal('195.00'))
        self.assertFalse(payment_is_fee_only(payment))


@patch('apps.core.payment_service.TranzilaService')
class WidgetRegistrationCreditTest(TrialCreditBase):
    def test_the_first_charge_is_reduced_and_the_reason_is_returned(self, _tranzila):
        trial = self._paid_trial()
        out = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        payment = Payment.objects.get(id=out['payment_id'])
        expected = Decimal(str(out['prorated_amount'])) + Decimal(str(out['registration_fee'])) - Decimal('30.00')
        self.assertEqual(payment.final_amount, expected)
        self.assertEqual(payment.trial_credit_amount, Decimal('30.00'))
        self.assertEqual(payment.trial_credit_source, trial)
        self.assertEqual(Decimal(str(out['trial_credit_amount'])), Decimal('30.00'))
        self.assertIn('שיעור ניסיון', out['trial_credit_reason'])
        # the monthly standing amount is untouched
        self.assertEqual(Decimal(str(out['monthly_amount'])), Decimal('260.00'))

    def test_without_a_paid_trial_nothing_changes(self, _tranzila):
        out = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        payment = Payment.objects.get(id=out['payment_id'])
        self.assertEqual(payment.trial_credit_amount, Decimal('0.00'))
        self.assertIsNone(payment.trial_credit_source)
        self.assertEqual(Decimal(str(out['trial_credit_amount'])), Decimal('0.00'))
        self.assertEqual(out['trial_credit_reason'], '')
        self.assertEqual(
            payment.final_amount,
            Decimal(str(out['prorated_amount'])) + Decimal(str(out['registration_fee'])),
        )

    def test_a_price_quote_does_not_starve_the_charge_that_follows_it(self, _tranzila):
        # The CRM dialog prices a registration by creating a pending Payment. The
        # real charge for the same lesson must still get the credit.
        self._paid_trial()
        preview = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        self.assertEqual(Decimal(str(preview['trial_credit_amount'])), Decimal('30.00'))
        again = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        self.assertEqual(Decimal(str(again['trial_credit_amount'])), Decimal('30.00'))

    def test_a_stale_charge_is_refused_once_the_credit_went_elsewhere(self, _tranzila):
        self._paid_trial()
        stale = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        stale_payment = Payment.objects.get(id=stale['payment_id'])
        self.assertTrue(credit_still_held_by(stale_payment))
        taken = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.sister_lesson.id),
        )
        Payment.objects.filter(id=taken['payment_id']).update(
            status='completed', trial_credit_amount=Decimal('30.00'),
            trial_credit_source=stale_payment.trial_credit_source,
        )
        stale_payment.refresh_from_db()
        self.assertFalse(credit_still_held_by(stale_payment))

    def test_a_second_registration_does_not_take_the_credit_twice(self, _tranzila):
        self._paid_trial()
        first = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        Payment.objects.filter(id=first['payment_id']).update(status='completed')
        second = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.sister_lesson.id),
        )
        self.assertEqual(Decimal(str(second['trial_credit_amount'])), Decimal('0.00'))
        self.assertEqual(Payment.objects.get(id=second['payment_id']).trial_credit_amount, Decimal('0.00'))


class CardLinkCreditTest(TrialCreditBase):
    """The card link the office sends an existing customer credits the trial too."""

    def setUp(self):
        super().setUp()
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.user = user
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _link(self):
        from apps.payment_links.models import CardLink

        return CardLink.objects.create(
            kind=CardLink.KIND_STANDING_ORDER, child=self.child, lesson=self.lesson,
            include_registration_fee=True, created_by=self.user,
        )

    def test_the_quote_and_the_preview_carry_the_credit(self):
        from apps.customers.card_link import preview_payload, quote_standing_order

        self._paid_trial()
        link = self._link()
        quote = quote_standing_order(link)
        self.assertEqual(quote['trial_credit'], Decimal('30.00'))
        self.assertEqual(
            quote['first_charge'], quote['prorated_lesson'] + quote['registration_fee'] - Decimal('30.00'),
        )
        link.refresh_from_db()   # the fixture's '16:00' strings become times, as the API path sees them
        preview = preview_payload(link)
        self.assertEqual(preview['trial_credit'], '30.00')
        self.assertIn('שיעור ניסיון', preview['trial_credit_reason'])

    def test_without_a_paid_trial_the_quote_is_unchanged(self):
        from apps.customers.card_link import quote_standing_order

        quote = quote_standing_order(self._link())
        self.assertEqual(quote['trial_credit'], Decimal('0.00'))
        self.assertEqual(quote['first_charge'], quote['prorated_lesson'] + quote['registration_fee'])


@patch('apps.core.payment_service.TranzilaService')
class GatewaySumTest(TrialCreditBase):
    """
    Tranzila has no amount field: the sum of the line items IS the money. Every
    charge path must therefore send lines that add up to Payment.final_amount.
    """

    def _lines_for(self, payment):
        from apps.core.payment_service import subscription_tranzila_items

        return subscription_tranzila_items(
            label='ג׳ודו',
            prorated_lesson=payment_prorated_lesson_amount(payment),
            registration_fee=payment.registration_fee or Decimal('0.00'),
            prorated=payment_prorated_lesson_amount(payment) > 0,
            trial_credit=payment.trial_credit_amount or Decimal('0.00'),
        )

    def test_the_widget_lines_add_up_to_the_credited_charge(self, _tranzila):
        self._paid_trial()
        out = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        payment = Payment.objects.get(id=out['payment_id'])
        self.assertGreater(payment.trial_credit_amount, 0)
        total = sum(Decimal(str(line['unit_price'])) for line in self._lines_for(payment))
        self.assertEqual(total, payment.final_amount)

    def test_the_widget_lines_add_up_without_a_credit_too(self, _tranzila):
        out = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        payment = Payment.objects.get(id=out['payment_id'])
        total = sum(Decimal(str(line['unit_price'])) for line in self._lines_for(payment))
        self.assertEqual(total, payment.final_amount)

    def test_a_credit_bigger_than_the_whole_charge_leaves_no_lines(self, _tranzila):
        self._paid_trial(amount='500.00')
        out = PaymentService().initiate_subscription_payment(
            child_id=str(self.child.id), lesson_id=str(self.lesson.id),
        )
        payment = Payment.objects.get(id=out['payment_id'])
        self.assertEqual(payment.final_amount, Decimal('0.00'))
        self.assertEqual(self._lines_for(payment), [])
