"""
A trial that turns into a subscription must not keep the trial's end date.

The chain, exactly as production ran it: the office books a trial, the trial
cron closes the row the next morning with end_date = the trial day, the parent
pays, and the payment reuses that row. The row came back active with the end
date still on it. The register never reads end_date, so the child looked fine
there — but the instructor's dashboard, the salary tiers and the monthly
snapshots all do, and each of them dropped the child from the month after the
trial. On 23.9.2026: 82 paying children.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, Room, UserProfile
from apps.core.payment_service import enroll_child_in_paid_lessons
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor
from apps.instructors.utils import _count_enrollments_for_period

User = get_user_model()


class ConvertedTrialTestBase(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        room = Room.objects.create(name='סטודיו', branch=self.branch, capacity=20)
        ctype = CourseType.objects.create(name='קפוארה')
        self.user = User.objects.create_user(
            username='teacher@enddate.test', email='teacher@enddate.test', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.role = UserProfile.ROLE_WORKER
        profile.save(update_fields=['role'])
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='teacher@enddate.test', primary_branch=self.branch,
        )
        course = Course.objects.create(
            name='קפוארה א-ב', branch=self.branch, course_type=ctype, price=Decimal('235.00'),
            capacity=20, instructor=self.instructor, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=course, instructor=self.instructor, room=room,
            day_of_week=(date.today().weekday() + 1) % 7,
            start_time=time(16, 0), end_time=time(16, 45), is_recurring=True,
        )
        self.trial_day = date.today() - timedelta(days=40)

    def child(self, name='נועה'):
        family = Family.objects.create(name='כהן', phone='0501234567', branch=self.branch)
        return Child.objects.create(
            family=family, first_name=name, last_name='כהן', birth_date=date(2017, 1, 1),
            gender='female', status='trial_completed',
        )

    def trial_closed_by_the_cron(self, child):
        """The row as the trial cron leaves it the morning after the trial."""
        return LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='inactive',
            start_date=self.trial_day, end_date=self.trial_day, trial_outcome='attended',
        )

    def pays(self, child):
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=self.lesson,
            base_amount=Decimal('235.00'), final_amount=Decimal('235.00'), status='completed',
        )
        RecurringPayment.objects.create(
            child=child, initial_payment=payment, amount=Decimal('235.00'), status='active',
            start_date=date.today(), next_billing_date=date.today(),
        )
        Child.objects.filter(pk=child.pk).update(status='active', paid_until_date=date.today() + timedelta(days=20))
        enroll_child_in_paid_lessons(child=Child.objects.get(pk=child.pk), lesson=self.lesson)


class ConversionEndsNothing(ConvertedTrialTestBase):
    def test_the_paid_row_has_no_end_date(self):
        child = self.child()
        row = self.trial_closed_by_the_cron(child)
        self.pays(child)
        row.refresh_from_db()
        self.assertEqual(row.status, 'active')
        self.assertIsNone(row.end_date)

    def test_the_child_counts_toward_salary_in_the_months_after_the_trial(self):
        child = self.child()
        self.trial_closed_by_the_cron(child)
        self.pays(child)
        this_month = date.today().replace(day=1)
        self.assertEqual(
            _count_enrollments_for_period(self.lesson, this_month, this_month + timedelta(days=27), ('active',)),
            1,
        )

    def test_the_instructor_dashboard_counts_the_child(self):
        child = self.child()
        self.trial_closed_by_the_cron(child)
        self.pays(child)
        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        res = self.client.get('/api/v1/instructors/my-dashboard/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['total_active_students'], 1)


class TheRepairOfRowsAlreadyWrong(ConvertedTrialTestBase):
    """Migration 0019, run against rows written by the old code."""

    def run_repair(self):
        import_module('apps.enrollments.migrations.0019_clear_converted_trial_end_dates').clear(django_apps, None)

    def converted_the_old_way(self, child):
        row = self.trial_closed_by_the_cron(child)
        self.pays(child)
        # What the old conversion left behind.
        LessonEnrollment.objects.filter(pk=row.pk).update(end_date=self.trial_day)
        return row

    def test_it_clears_the_trial_signature_on_a_paying_child(self):
        row = self.converted_the_old_way(self.child())
        self.run_repair()
        row.refresh_from_db()
        self.assertIsNone(row.end_date)

    def test_it_leaves_an_end_date_that_is_not_the_trial_day(self):
        """A cancellation or a course change writes a different date. Not ours."""
        child = self.child()
        row = self.converted_the_old_way(child)
        LessonEnrollment.objects.filter(pk=row.pk).update(end_date=self.trial_day + timedelta(days=6))
        self.run_repair()
        row.refresh_from_db()
        self.assertEqual(row.end_date, self.trial_day + timedelta(days=6))

    def test_it_leaves_a_child_without_a_live_standing_order(self):
        row = self.converted_the_old_way(self.child())
        RecurringPayment.objects.update(status='cancelled')
        self.run_repair()
        row.refresh_from_db()
        self.assertEqual(row.end_date, self.trial_day)

    def test_it_leaves_a_closed_trial_that_never_became_a_subscription(self):
        row = self.trial_closed_by_the_cron(self.child())
        self.run_repair()
        row.refresh_from_db()
        self.assertEqual(row.end_date, self.trial_day)
