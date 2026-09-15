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
    def test_exactly_six_statuses_exist(self):
        self.assertEqual(
            CHILD_STATUSES,
            ['active', 'trial_signed', 'trial_completed', 'pending', 'payment_problem', 'ghost'],
        )

    def test_the_model_offers_the_same_six(self):
        self.assertEqual([value for value, _ in Child.STATUS_CHOICES], CHILD_STATUSES)

    def test_the_retired_ones_are_gone(self):
        for retired in ('not_paid', 'inactive', 'expired', 'trial', 'non_active', 'paused'):
            self.assertNotIn(retired, CHILD_STATUS_LABELS)

    def test_a_retired_status_still_reads_as_something(self):
        self.assertEqual(canonical_status('not_paid'), 'payment_problem')
        self.assertIsNone(canonical_status('inactive'))   # resolved from the record
        self.assertIsNone(canonical_status('expired'))
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

    def test_a_completed_payment_is_money_in(self):
        child = self.make_child()
        Payment.objects.create(
            child=child, family=self.family, payment_type='one_time',
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

    def test_nothing_recorded_is_pending(self):
        self.assertEqual(resolve_child_status(self.make_child()), 'pending')

    def test_a_payment_that_has_lapsed_is_not_money_in(self):
        """Paid once, two years ago, paid_until long past — not פעיל today."""
        child = self.make_child(paid_until_date=TODAY - timedelta(days=700))
        Payment.objects.create(
            child=child, family=self.family, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('260'),
        )
        self.assertEqual(resolve_child_status(child), 'pending')

    def test_a_fresh_payment_with_no_paid_until_yet_is_money_in(self):
        child = self.make_child()
        Payment.objects.create(
            child=child, family=self.family, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('260'),
        )
        self.assertEqual(resolve_child_status(child), 'active')

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
