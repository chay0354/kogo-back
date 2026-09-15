"""
An instructor's trial students, gathered.

The case that drove this: a trial child who subscribes has their
``trial_lesson_date`` cleared, because the register would otherwise keep showing
them on that one date forever. The side effect was that the trials which
*worked* were the only ones nobody could find afterwards — and those are the
ones an instructor most wants to see. ``trial_held_on`` exists so they survive,
and the first test here is the one that proves it.

The second guarantee is smaller and just as easy to lose: ``unmarked`` is not
``no_show``. Telling an instructor a child did not turn up, when in truth nobody
took the register, sends them into a phone call on a false premise.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor

User = get_user_model()

URL = '/api/v1/instructors/my-trials/'


class MyTrialsTestBase(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='סניף בדיקה', city=self.city)
        self.other_branch = Branch.objects.create(name='סניף אחר', city=self.city)
        self.ctype = CourseType.objects.create(name='קפוארה')

        self.user = self._user('teacher@trials.test', UserProfile.ROLE_WORKER)
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה',
            email='teacher@trials.test', primary_branch=self.branch,
        )
        self.other_user = self._user('other@trials.test', UserProfile.ROLE_WORKER)
        self.other_instructor = Instructor.objects.create(
            first_name='מורה', last_name='אחר',
            email='other@trials.test', primary_branch=self.other_branch,
        )

        self.lesson = self._lesson(self.branch, self.instructor)
        self.other_lesson = self._lesson(self.other_branch, self.other_instructor)
        self.today = date.today()
        self.auth(self.user)

    def _user(self, username, role):
        user = User.objects.create_user(username=username, email=username, password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = role
        profile.save(update_fields=['role'])
        return user

    def _lesson(self, branch, instructor):
        course = Course.objects.create(
            name='קפוארה א-ב', branch=branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20, instructor=instructor,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor, day_of_week=1,
            start_time=time(16, 0), end_time=time(16, 45), is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def child(self, first='נועם', last='כהן', phone='0501234567', child_status='trial_signed'):
        family = Family.objects.create(name=last, phone='0529999999', branch=self.branch)
        return Child.objects.create(
            family=family, first_name=first, last_name=last, phone_number=phone,
            birth_date=date(2016, 4, 4), gender='male', status=child_status,
        )

    def trial(self, when, outcome='', lesson=None, child=None, **extra):
        return LessonEnrollment.objects.create(
            lesson=lesson or self.lesson,
            child=child or self.child(),
            status=extra.pop('status', 'inactive'),
            trial_lesson_date=when,
            trial_outcome=outcome,
            **extra,
        )

    def get(self, **params):
        return self.client.get(URL, params)


class TheDateSurvivesConversionTests(MyTrialsTestBase):
    def test_the_date_is_copied_the_moment_a_trial_is_booked(self):
        row = self.trial(self.today - timedelta(days=7), outcome='attended')
        self.assertEqual(row.trial_held_on, self.today - timedelta(days=7))

    def test_a_child_who_subscribed_is_still_found_after_the_date_is_cleared(self):
        """
        The case the whole feature exists for.

        Conversion wipes trial_lesson_date so the register stops showing the
        child on that one date. Before trial_held_on, that also erased them
        from any answer to 'who trialled with me'.
        """
        when = self.today - timedelta(days=10)
        row = self.trial(when, outcome='attended', status='active')
        row.trial_lesson_date = None          # exactly what conversion does
        row.save(update_fields=['trial_lesson_date'])

        res = self.get()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['total'], 1)
        self.assertEqual(res.data['trials'][0]['trial_date'], when.isoformat())

    def test_it_is_never_overwritten_by_a_later_reschedule(self):
        first = self.today - timedelta(days=20)
        row = self.trial(first)
        row.trial_lesson_date = self.today - timedelta(days=2)
        row.save(update_fields=['trial_lesson_date'])
        row.refresh_from_db()
        self.assertEqual(row.trial_held_on, first)


class OutcomeTests(MyTrialsTestBase):
    def test_the_five_states_are_reported_apart(self):
        self.trial(self.today + timedelta(days=3))                      # upcoming
        self.trial(self.today - timedelta(days=3), outcome='attended')
        self.trial(self.today - timedelta(days=4), outcome='no_show')
        self.trial(self.today - timedelta(days=5), outcome='')          # unmarked

        # One who subscribed: a live standing order on the lesson they trialled.
        converted = self.child(first='רות', child_status='active')
        row = self.trial(self.today - timedelta(days=6), outcome='attended', child=converted)
        payment = Payment.objects.create(
            child=converted, family=converted.family, lesson=self.lesson,
            base_amount=Decimal('235.00'), final_amount=Decimal('235.00'), status='completed',
        )
        RecurringPayment.objects.create(
            child=converted, initial_payment=payment, amount=Decimal('235.00'),
            status='active', start_date=self.today, next_billing_date=self.today,
        )

        res = self.get()
        self.assertEqual(res.data['counts'], {
            'upcoming': 1, 'attended': 1, 'no_show': 1, 'unmarked': 1, 'registered': 1,
        })
        self.assertEqual(res.data['total'], 5)

    def test_an_unmarked_register_is_not_reported_as_a_no_show(self):
        """Nobody took the register. Saying the child did not come would be a lie."""
        self.trial(self.today - timedelta(days=2), outcome='unmarked')
        res = self.get()
        self.assertEqual(res.data['counts']['no_show'], 0)
        self.assertEqual(res.data['counts']['unmarked'], 1)
        self.assertEqual(res.data['trials'][0]['outcome_label'], 'לא סומן')

    def test_a_trial_still_to_come_is_upcoming_whatever_the_outcome_field_says(self):
        self.trial(self.today + timedelta(days=5), outcome='unmarked')
        self.assertEqual(self.get().data['counts']['upcoming'], 1)

    def test_a_converted_child_outranks_their_recorded_outcome(self):
        """'They signed up' is the more useful fact than 'they attended'."""
        converted = self.child(first='דנה', child_status='active')
        self.trial(self.today - timedelta(days=8), outcome='attended', child=converted)
        payment = Payment.objects.create(
            child=converted, family=converted.family, lesson=self.lesson,
            base_amount=Decimal('235.00'), final_amount=Decimal('235.00'), status='completed',
        )
        RecurringPayment.objects.create(
            child=converted, initial_payment=payment, amount=Decimal('235.00'),
            status='active', start_date=self.today, next_billing_date=self.today,
        )
        res = self.get()
        self.assertEqual(res.data['trials'][0]['outcome'], 'registered')


class ContactTests(MyTrialsTestBase):
    def test_every_row_carries_a_number_to_call(self):
        self.trial(self.today - timedelta(days=1), outcome='attended')
        row = self.get().data['trials'][0]
        self.assertEqual(row['phone'], '0501234567')
        self.assertEqual(row['course_name'], 'קפוארה א-ב')
        self.assertEqual(row['start_time'], '16:00')

    def test_it_falls_back_to_the_family_number(self):
        child = self.child(phone='')
        self.trial(self.today - timedelta(days=1), child=child)
        self.assertEqual(self.get().data['trials'][0]['phone'], '0529999999')


class ScopeTests(MyTrialsTestBase):
    def test_an_instructor_sees_only_their_own_lessons(self):
        self.trial(self.today - timedelta(days=1), lesson=self.lesson)
        self.trial(self.today - timedelta(days=1), lesson=self.other_lesson)
        res = self.get()
        self.assertEqual(res.data['total'], 1)
        self.assertEqual(res.data['trials'][0]['lesson_id'], str(self.lesson.id))

    def test_a_login_that_matches_no_instructor_gets_nothing(self):
        stranger = self._user('stranger@trials.test', UserProfile.ROLE_WORKER)
        self.trial(self.today - timedelta(days=1))
        self.auth(stranger)
        res = self.get()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['total'], 0)

    def test_the_window_can_be_narrowed(self):
        self.trial(self.today - timedelta(days=80))
        self.trial(self.today - timedelta(days=2))
        res = self.get(date_from=(self.today - timedelta(days=7)).isoformat())
        self.assertEqual(res.data['total'], 1)

    def test_trials_already_booked_ahead_are_in_the_default_window(self):
        """A backward-looking range would hide the row an instructor wants most."""
        self.trial(self.today + timedelta(days=30))
        self.assertEqual(self.get().data['total'], 1)

    def test_it_needs_a_login(self):
        self.client.credentials()
        self.assertEqual(self.get().status_code, status.HTTP_401_UNAUTHORIZED)
