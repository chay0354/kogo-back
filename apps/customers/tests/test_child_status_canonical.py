"""
The six statuses a child can be in, and the rules that put them there.

active           money actually came in
trial_signed     a trial is booked and still ahead
trial_completed  the trial already happened
pending          details filled in, nothing paid
payment_problem  the card did not go through
ghost            a walk-in the instructor added
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.customers.child_status import (
    CHILD_STATUSES,
    CHILD_STATUS_LABELS,
    canonical_status,
    resolve_child_status,
)
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Child, Family, Payment
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
TODAY = date.today()


class StatusListTest(TestCase):
    def test_exactly_seven_statuses_exist(self):
        self.assertEqual(
            CHILD_STATUSES,
            ['active', 'trial_signed', 'trial_completed', 'pending',
             'payment_problem', 'inactive', 'ghost'],
        )

    def test_the_model_offers_the_same_seven(self):
        self.assertEqual([value for value, _ in Child.STATUS_CHOICES], CHILD_STATUSES)

    def test_the_retired_ones_are_gone(self):
        for retired in ('not_paid', 'expired', 'trial', 'non_active', 'paused'):
            self.assertNotIn(retired, CHILD_STATUS_LABELS)

    def test_a_retired_status_still_reads_as_something(self):
        self.assertEqual(canonical_status('not_paid'), 'payment_problem')
        self.assertEqual(canonical_status('non_active'), 'inactive')
        self.assertIsNone(canonical_status('expired'))    # resolved from the record
        self.assertIsNone(canonical_status('nonsense'))


class ResolveStatusTest(TestCase):
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(branch=self.branch)
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, branch=self.branch)

    def make_child(self, **over):
        fields = dict(
            family=self.family, first_name='ילד', last_name='בדיקה',
            birth_date=date(2015, 1, 1), gender='male', status='pending',
        )
        fields.update(over)
        return Child.objects.create(**fields)

    def test_money_in_makes_a_child_active(self):
        child = self.make_child(paid_until_date=TODAY + timedelta(days=20))
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_completed_payment_for_a_course_is_money_in(self):
        # Until 27.9.2026 this was any completed payment — a one-time charge
        # with no lesson included, which is how a store purchase or a stray
        # charge read as פעיל. Only money for a course counts now.
        child = self.make_child()
        Payment.objects.create(
            child=child, family=self.family, lesson=self.lesson, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('100'), final_amount=Decimal('100'),
        )
        self.assertEqual(resolve_child_status(child), 'active')

    def test_an_enrollment_without_money_is_not_active(self):
        """The old frontend called this פעיל. Nothing was ever paid."""
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active', start_date=TODAY,
        )
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_trial_still_ahead_is_trial_signed(self):
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY, trial_lesson_date=TODAY + timedelta(days=3),
        )
        self.assertEqual(resolve_child_status(child), 'trial_signed')

    def test_a_trial_already_held_is_trial_completed(self):
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY - timedelta(days=10), trial_lesson_date=TODAY - timedelta(days=7),
        )
        self.assertEqual(resolve_child_status(child), 'trial_completed')

    def test_paying_after_a_trial_makes_the_child_active(self):
        """The rule the owner spelled out: a trial child who registers is פעיל."""
        child = self.make_child(status='trial_completed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY - timedelta(days=10), trial_lesson_date=TODAY - timedelta(days=7),
        )
        child.paid_until_date = TODAY + timedelta(days=30)
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_card_problem_stands_until_money_arrives(self):
        child = self.make_child(status='payment_problem')
        self.assertEqual(resolve_child_status(child), 'payment_problem')
        child.paid_until_date = TODAY + timedelta(days=30)
        self.assertEqual(resolve_child_status(child), 'active')

    def test_nothing_recorded_at_all_is_pending(self):
        """Details filled in and nothing else: still בתהליך רישום."""
        self.assertEqual(resolve_child_status(self.make_child()), 'pending')

    def test_an_enrollment_that_was_cancelled_leaves_the_child_inactive(self):
        """Had something, it was cancelled, nothing paid — that is לא פעיל."""
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=30),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_child_still_enrolled_but_unpaid_is_not_inactive(self):
        """Mid-registration is בתהליך רישום; nothing has been cancelled."""
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active', start_date=TODAY,
        )
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_held_trial_still_wins_over_inactive(self):
        """They really were at a trial — that is the truer thing to say."""
        child = self.make_child()
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=20), trial_lesson_date=TODAY - timedelta(days=14),
        )
        self.assertEqual(resolve_child_status(child), 'trial_completed')

    def test_a_payment_that_has_lapsed_is_not_money_in(self):
        """
        Paid once, two years ago, paid_until long past. Not פעיל — and not
        בתהליך רישום either: they were a customer and the money ran out, with
        nothing left open, which is לא פעיל.
        """
        child = self.make_child(paid_until_date=TODAY - timedelta(days=700))
        Payment.objects.create(
            child=child, family=self.family, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('260'),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_fresh_payment_with_no_paid_until_yet_is_money_in(self):
        child = self.make_child()
        Payment.objects.create(
            child=child, family=self.family, lesson=self.lesson, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('260'),
        )
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_child_recorded_as_cancelled_stays_inactive(self):
        """
        The commonest shape of לא פעיל: cancelled, and the cancellation took
        the enrolment rows with it. Nothing is left to prove they ever had
        anything — which is the point, not a reason to call them something else.
        """
        child = self.make_child(status='inactive')
        self.assertEqual(child.lesson_enrollments.count(), 0)
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_cancelled_child_who_books_a_trial_is_back(self):
        child = self.make_child(status='inactive')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY, trial_lesson_date=TODAY + timedelta(days=4),
        )
        self.assertEqual(resolve_child_status(child), 'trial_signed')

    def test_a_cancelled_child_who_pays_is_active(self):
        child = self.make_child(status='inactive', paid_until_date=TODAY + timedelta(days=30))
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_cancelled_child_stays_active_until_the_paid_period_ends(self):
        """The owner's rule: cancel, and פעיל runs to the date you paid up to."""
        child = self.make_child(status='active', paid_until_date=TODAY + timedelta(days=12))
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=60),
        )
        self.assertEqual(resolve_child_status(child), 'active')

    def test_and_turns_inactive_the_moment_it_passes(self):
        child = self.make_child(status='active', paid_until_date=TODAY - timedelta(days=1))
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=60),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_long_past_trial_does_not_resurrect_a_lapsed_customer(self):
        """
        Someone who paid for two years, left, and had a trial before any of it
        was coming out as ביצע ניסיון — a description of them from years ago.
        """
        child = self.make_child(status='active', paid_until_date=TODAY - timedelta(days=5))
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=700),
            trial_lesson_date=TODAY - timedelta(days=700),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_money_stopping_without_a_cancellation_is_a_card_problem(self):
        """Still in the class, nothing paid — somebody has to chase it."""
        child = self.make_child(status='active', paid_until_date=TODAY - timedelta(days=3))
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY - timedelta(days=200),
        )
        self.assertEqual(resolve_child_status(child), 'payment_problem')

    def test_a_ghost_stays_a_ghost(self):
        child = self.make_child(status='ghost')
        self.assertEqual(resolve_child_status(child), 'ghost')


class CalculateStatusTest(ResolveStatusTest):
    """calculate_status() must only ever answer with one of the six."""

    def test_an_ended_subscription_is_not_an_invalid_status(self):
        child = self.make_child(
            subscription_start_date=TODAY - timedelta(days=400),
            subscription_end_date=TODAY - timedelta(days=30),
        )
        self.assertIn(child.calculate_status(), CHILD_STATUSES)

    def test_no_subscription_is_not_an_invalid_status(self):
        self.assertIn(self.make_child().calculate_status(), CHILD_STATUSES)

    def test_update_status_writes_a_real_choice(self):
        child = self.make_child(
            subscription_start_date=TODAY - timedelta(days=400),
            subscription_end_date=TODAY - timedelta(days=30),
        )
        child.update_status()
        child.refresh_from_db()
        self.assertIn(child.status, CHILD_STATUSES)

    def test_a_paid_up_subscription_is_active(self):
        child = self.make_child(
            subscription_start_date=TODAY - timedelta(days=10),
            paid_until_date=TODAY + timedelta(days=20),
        )
        self.assertEqual(child.calculate_status(), 'active')

    def test_a_subscription_starting_later_is_active(self):
        child = self.make_child(subscription_start_date=TODAY + timedelta(days=7))
        self.assertEqual(child.calculate_status(), 'active')

    def test_an_unpaid_running_subscription_is_a_payment_problem(self):
        child = self.make_child(subscription_start_date=TODAY - timedelta(days=10))
        self.assertEqual(child.calculate_status(), 'payment_problem')


class GhostRulesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr-status', password='x', is_staff=True)
        profile = getattr(self.user, 'profile', None)
        if profile is not None:
            profile.role = 'manager'
            profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(name='כהן', branch=self.branch)
        self.family.phone = '0521234567'
        self.family.save(update_fields=['phone'])
        course = TestDataFactory.create_course(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=course, branch=self.branch)

    def test_ghost_cannot_be_set_through_the_child_endpoint(self):
        child = Child.objects.create(
            family=self.family, first_name='נועם', last_name='כהן',
            birth_date=date(2015, 1, 1), gender='male', status='trial_signed',
        )
        response = self.client.patch(
            f'/api/v1/customers/children/{child.id}/', {'status': 'ghost'}, format='json')
        self.assertEqual(response.status_code, 400)
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')

    def test_a_walk_in_the_system_knows_is_that_child(self):
        """Not a ghost beside them — that is how one child ended up twice."""
        existing = Child.objects.create(
            family=self.family, first_name='נועם', last_name='כהן',
            birth_date=date(2015, 1, 1), gender='male', status='trial_signed',
        )
        response = self.client.post('/api/v1/customers/children/create_ghost/', {
            'first_name': 'נועם',
            'family_name': 'כהן',
            'phone_number': '0521234567',
            'lesson_id': str(self.lesson.id),
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data.get('matched_existing'))
        self.assertEqual(response.data['child']['id'], str(existing.id))
        self.assertEqual(Child.objects.filter(status='ghost').count(), 0)
        existing.refresh_from_db()
        self.assertEqual(existing.status, 'trial_signed')

    def test_an_unknown_walk_in_still_becomes_a_ghost(self):
        response = self.client.post('/api/v1/customers/children/create_ghost/', {
            'first_name': 'ילד',
            'family_name': 'חדש',
            'phone_number': '0539999999',
            'lesson_id': str(self.lesson.id),
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Child.objects.filter(status='ghost').count(), 1)

    def test_a_walk_in_with_no_phone_is_not_matched_on_the_name_alone(self):
        Child.objects.create(
            family=self.family, first_name='נועם', last_name='כהן',
            birth_date=date(2015, 1, 1), gender='male', status='active',
        )
        response = self.client.post('/api/v1/customers/children/create_ghost/', {
            'first_name': 'נועם',
            'lesson_id': str(self.lesson.id),
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Child.objects.filter(status='ghost').count(), 1)


class StatusRulesOfSeptember27Test(TestCase):
    """
    The corrections the owner approved on 27.9.2026, each one checked against
    production first:

      * a trial is "ahead" only while its row is live — a cancelled trial left
        seven children on נרשם לניסיון;
      * a cash or cheque plan still running is money in — two cheque payers
        sat on בתהליך רישום;
      * a registration fee alone is not — four children read פעיל on their
        דמי רישום while the September charge was never collected — unless a
        standing order with a card is still there to bill the first month;
      * a declined standing order on a child still in a course is בעיה באשראי.
    """

    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=TestDataFactory.create_course(branch=self.branch))

    def make_child(self, **over):
        fields = dict(
            family=self.family, first_name='ילד', last_name='בדיקה',
            birth_date=date(2015, 1, 1), gender='male', status='pending',
        )
        fields.update(over)
        return Child.objects.create(**fields)

    def registration_fee_paid(self, child, **over):
        fields = dict(
            child=child, family=self.family, lesson=self.lesson, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('120'),
            registration_fee=Decimal('120'),
        )
        fields.update(over)
        return Payment.objects.create(**fields)

    def standing_order(self, child, **over):
        from apps.customers.models import RecurringPayment

        fields = dict(
            child=child, amount=Decimal('260'), base_amount=Decimal('260'), status='active',
            tranzila_token='tok', billing_day=1, start_date=TODAY, next_billing_date=TODAY + timedelta(days=5),
        )
        fields.update(over)
        return RecurringPayment.objects.create(**fields)

    def on_the_course(self, child):
        return LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active', start_date=TODAY - timedelta(days=20),
        )

    # --- trials ---------------------------------------------------------------

    def test_a_cancelled_trial_is_not_a_trial_ahead(self):
        """The office cancelled it; the date is still on the row, the child is not coming."""
        child = self.make_child(status='trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive', end_date=TODAY,
            start_date=TODAY + timedelta(days=4), trial_lesson_date=TODAY + timedelta(days=4),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_trial_dropped_before_its_date_was_never_held(self):
        """
        Cancelled a week ahead, and the date has since gone by. Reading it as
        ביצע ניסיון would also make the widget refuse the child a first trial.
        """
        child = self.make_child(status='trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=TODAY - timedelta(days=3), trial_lesson_date=TODAY - timedelta(days=3),
            end_date=TODAY - timedelta(days=10),
        )
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_trial_the_cron_retired_was_held(self):
        child = self.make_child(status='trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive', trial_outcome='attended',
            start_date=TODAY - timedelta(days=3), trial_lesson_date=TODAY - timedelta(days=3),
            end_date=TODAY - timedelta(days=3),
        )
        self.assertEqual(resolve_child_status(child), 'trial_completed')

    def test_a_live_trial_ahead_brings_a_completed_trial_child_back(self):
        child = self.make_child(status='trial_completed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY + timedelta(days=2), trial_lesson_date=TODAY + timedelta(days=2),
        )
        self.assertEqual(resolve_child_status(child), 'trial_signed')

    # --- cash and cheques -----------------------------------------------------

    def test_a_running_cash_plan_is_money_in(self):
        from apps.documents.models import CashPlan

        child = self.make_child()
        self.on_the_course(child)
        CashPlan.objects.create(
            child=child, lesson=self.lesson, status='active',
            total_amount=Decimal('2600'), monthly_amount=Decimal('260'),
        )
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_running_cheque_plan_is_money_in_even_after_a_card_ran_out(self):
        from apps.documents.models import CheckPlan

        child = self.make_child(status='payment_problem', paid_until_date=TODAY - timedelta(days=40))
        self.on_the_course(child)
        CheckPlan.objects.create(child=child, lesson=self.lesson, status='active')
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_plan_that_finished_this_month_still_pays_for_this_month(self):
        """
        A plan turns 'completed' when its last month's document is issued — on
        the 1st of that month. Owner, 28.9.2026: it counts until the month ends.
        """
        from apps.documents.models import CashPlan, CashPlanMonth

        child = self.make_child()
        self.on_the_course(child)
        plan = CashPlan.objects.create(
            child=child, lesson=self.lesson, status='completed',
            total_amount=Decimal('520'), monthly_amount=Decimal('260'),
        )
        this_month = TODAY.replace(day=1)
        last_month = (this_month - timedelta(days=1)).replace(day=1)
        for due in (last_month, this_month):
            CashPlanMonth.objects.create(plan=plan, due_date=due, amount=Decimal('260'), status='invoiced')
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_plan_that_finished_last_month_no_longer_does(self):
        from apps.documents.models import CheckItem, CheckPlan

        child = self.make_child()
        self.on_the_course(child)
        plan = CheckPlan.objects.create(child=child, lesson=self.lesson, status='completed')
        last_month = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=1)
        CheckItem.objects.create(plan=plan, due_date=last_month, amount=Decimal('260'), status='invoiced')
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_cancelled_plan_is_not_money_in(self):
        from apps.documents.models import CheckPlan

        child = self.make_child()
        self.on_the_course(child)
        CheckPlan.objects.create(child=child, lesson=self.lesson, status='cancelled')
        self.assertEqual(resolve_child_status(child), 'pending')

    # --- what counts as a registration paid for -------------------------------

    def test_a_registration_fee_alone_is_not_money_in(self):
        """The fee was taken, the first month never was, and nothing is left to bill it."""
        child = self.make_child(status='active')
        self.on_the_course(child)
        self.registration_fee_paid(child)
        self.standing_order(child, status='cancelled')
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_fee_only_sign_up_with_a_card_on_a_live_standing_order_is_active(self):
        """Billing starts on the 1st: a student from the day they signed."""
        child = self.make_child(status='active', subscription_start_date=TODAY + timedelta(days=5))
        self.on_the_course(child)
        self.registration_fee_paid(child)
        self.standing_order(child)
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_live_standing_order_without_a_card_does_not_carry_a_fee(self):
        child = self.make_child(status='active')
        self.on_the_course(child)
        self.registration_fee_paid(child)
        self.standing_order(child, tranzila_token='')
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_trial_credit_does_not_turn_a_course_payment_into_a_fee(self):
        """₪120 charged = ₪120 fee + ₪40 of the month − ₪40 credited trial: a month was bought."""
        child = self.make_child()
        self.on_the_course(child)
        self.registration_fee_paid(child, trial_credit_amount=Decimal('40'))
        self.assertEqual(resolve_child_status(child), 'active')

    def test_a_one_time_payment_without_a_lesson_is_not_money_in(self):
        child = self.make_child()
        Payment.objects.create(
            child=child, family=self.family, payment_type='one_time',
            status='completed', base_amount=Decimal('100'), final_amount=Decimal('100'),
        )
        self.assertEqual(resolve_child_status(child), 'pending')

    # --- a declined standing order --------------------------------------------

    def test_a_declined_first_charge_on_a_live_course_is_a_card_problem(self):
        """The owner: a card that failed is still a student, labelled בעיה באשראי."""
        child = self.make_child(status='active')
        self.on_the_course(child)
        self.registration_fee_paid(child)
        self.standing_order(child, status='failed')
        self.assertEqual(resolve_child_status(child), 'payment_problem')

    def test_a_declined_card_with_no_course_left_is_not_a_card_problem(self):
        child = self.make_child(status='active')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive', start_date=TODAY - timedelta(days=60),
        )
        self.registration_fee_paid(child)
        self.standing_order(child, status='failed')
        self.assertEqual(resolve_child_status(child), 'inactive')

    def test_a_declined_card_beside_only_a_trial_is_not_a_card_problem(self):
        child = self.make_child(status='trial_signed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active',
            start_date=TODAY + timedelta(days=3), trial_lesson_date=TODAY + timedelta(days=3),
        )
        self.standing_order(child, status='failed')
        self.assertEqual(resolve_child_status(child), 'trial_signed')


class MarkTrialSignedTest(TestCase):
    """A trial booking marks נרשם לניסיון only over the statuses that are less than a student."""

    def test_only_the_statuses_below_a_student_are_marked(self):
        from apps.customers.child_status import mark_trial_signed

        family = TestDataFactory.create_family()
        expected = {
            'pending': 'trial_signed',
            'inactive': 'trial_signed',
            'trial_completed': 'trial_signed',
            'trial_signed': 'trial_signed',
            'active': 'active',
            'payment_problem': 'payment_problem',
            'ghost': 'ghost',
        }
        for status, after in expected.items():
            child = Child.objects.create(
                family=family, first_name=status, last_name='בדיקה',
                birth_date=date(2015, 1, 1), gender='male', status=status,
            )
            mark_trial_signed(child.pk)
            child.refresh_from_db()
            self.assertEqual(child.status, after, status)
