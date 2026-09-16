"""
Closing a date for some lessons and not others.

A blocked date used to mean the whole day. Naming lessons narrows it — the
studio closes one room for an event while the rest of the timetable runs — and
the narrowing has to reach every place that asks whether a trial may be booked.
That is the risk worth testing: a block applied in the picker but not on submit
offers a parent a date the server then refuses, and one applied on submit but
not in the picker hides a date that was never closed.

The other half is the sweep that moves trials off a newly blocked day. Scoped
wrongly it would move children booked into lessons the block never touched.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment, TrialBlockedDate
from apps.enrollments.trial_reminders import (
    blocked_trial_dates_by_lesson,
    blocked_trial_lesson_dates,
    iter_upcoming_lesson_occurrences,
    reschedule_blocked_trial_enrollments,
    validate_trial_lesson_date,
)
from apps.instructors.models import Instructor

User = get_user_model()


class ScopedBlockTestBase(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=self.city)
        self.room = Room.objects.create(name='סטודיו', branch=self.branch, capacity=20)
        self.ctype = CourseType.objects.create(name='קפוארה')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='t@scope.test', primary_branch=self.branch,
        )
        # Two lessons on the same weekday, so one date covers both.
        self.closed = self._lesson('קפוארה ג-ד', min_age=5, max_age=6, hour=17)
        self.open = self._lesson('קפוארה 3-4.5', min_age=1, max_age=1, hour=16)

        self.manager = User.objects.create_user(
            username='mgr@scope.test', email='mgr@scope.test', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.manager)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        token, _ = Token.objects.get_or_create(user=self.manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _lesson(self, name, *, min_age, max_age, hour):
        course = Course.objects.create(
            name=name, branch=self.branch, course_type=self.ctype, price=Decimal('235.00'),
            capacity=20, instructor=self.instructor, min_age=min_age, max_age=max_age,
            is_active=True, show_in_widget=True,
        )
        return Lesson.objects.create(
            course=course, instructor=self.instructor, day_of_week=3, room=self.room,
            start_time=time(hour, 0), end_time=time(hour, 45), is_recurring=True,
        )

    def first_date(self, lesson):
        return iter_upcoming_lesson_occurrences(lesson, count=3)[0]

    def block(self, day, lessons=None, reason='אירוע'):
        row = TrialBlockedDate.objects.create(date=day, reason=reason)
        if lessons:
            row.lessons.set(lessons)
        return row


class AScopedBlockReachesEveryGate(ScopedBlockTestBase):
    def test_it_closes_the_named_lesson_and_leaves_the_others(self):
        day = self.first_date(self.closed)
        self.block(day, [self.closed])
        self.assertIn(day, blocked_trial_lesson_dates(self.closed))
        self.assertNotIn(day, blocked_trial_lesson_dates(self.open))

    def test_the_picker_drops_the_date_only_for_the_named_lesson(self):
        day = self.first_date(self.closed)
        self.block(day, [self.closed])
        self.assertNotIn(day, iter_upcoming_lesson_occurrences(self.closed, count=3))
        self.assertIn(day, iter_upcoming_lesson_occurrences(self.open, count=3))

    def test_the_submit_gate_agrees_with_the_picker(self):
        """
        The two disagreeing is the failure this whole feature can cause.

        What actually refuses here is the occurrence list, which the gate checks
        after its own blocked-date line; mutating either one on its own still
        leaves the other refusing. That is belt and braces, not an accident —
        the point of this test is the agreement, not which line delivers it.
        """
        day = self.first_date(self.closed)
        self.block(day, [self.closed])
        with self.assertRaises(ValueError):
            validate_trial_lesson_date(self.closed, day)
        validate_trial_lesson_date(self.open, day)  # must not raise

    def test_naming_no_lesson_still_closes_the_whole_day(self):
        """Every row written before the scope existed meant this."""
        day = self.first_date(self.closed)
        self.block(day, None)
        self.assertIn(day, blocked_trial_lesson_dates(self.closed))
        self.assertIn(day, blocked_trial_lesson_dates(self.open))

    def test_asking_without_a_lesson_reports_only_whole_day_blocks(self):
        """
        A caller that does not say which lesson must not be handed a date that
        was closed for one room — it would read as closed for everything.
        """
        day = self.first_date(self.closed)
        self.block(day, [self.closed])
        self.assertNotIn(day, blocked_trial_lesson_dates())

    def test_the_batched_map_matches_the_single_answer(self):
        day = self.first_date(self.closed)
        self.block(day, [self.closed])
        batched = blocked_trial_dates_by_lesson([self.closed.id, self.open.id])
        self.assertEqual(batched[self.closed.id], blocked_trial_lesson_dates(self.closed))
        self.assertEqual(batched[self.open.id], blocked_trial_lesson_dates(self.open))


class TheSweepMovesOnlyWhoWasBlocked(ScopedBlockTestBase):
    def trial_on(self, lesson, day, name):
        family = Family.objects.create(name=name, phone='0529999999', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2016, 4, 4), gender='male', status='trial_signed',
        )
        return LessonEnrollment.objects.create(
            lesson=lesson, child=child, status='active', trial_lesson_date=day,
        )

    def test_a_child_booked_into_an_untouched_lesson_is_left_alone(self):
        day = self.first_date(self.closed)
        stays = self.trial_on(self.open, day, 'נשאר')
        moves = self.trial_on(self.closed, day, 'זז')
        self.block(day, [self.closed])

        reschedule_blocked_trial_enrollments()
        stays.refresh_from_db()
        moves.refresh_from_db()
        self.assertEqual(stays.trial_lesson_date, day, 'לא היה אמור לזוז')
        self.assertGreater(moves.trial_lesson_date, day)

    def test_a_whole_day_block_still_moves_everyone(self):
        day = self.first_date(self.closed)
        a = self.trial_on(self.open, day, 'אחד')
        b = self.trial_on(self.closed, day, 'שניים')
        self.block(day, None)

        reschedule_blocked_trial_enrollments()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertGreater(a.trial_lesson_date, day)
        self.assertGreater(b.trial_lesson_date, day)


class TheLessonPickerApi(ScopedBlockTestBase):
    URL = '/api/v1/enrollments/trial-blocked-dates/lessons-on-date/'

    def test_it_lists_the_lessons_that_actually_run_that_day(self):
        day = self.first_date(self.closed)
        res = self.client.get(self.URL, {'date': day.isoformat()})
        self.assertEqual(res.status_code, 200, res.data)
        ids = {row['id'] for row in res.data['lessons']}
        self.assertEqual(ids, {str(self.closed.id), str(self.open.id)})

    def test_a_day_the_lessons_do_not_meet_returns_nothing(self):
        day = self.first_date(self.closed) + timedelta(days=1)
        res = self.client.get(self.URL, {'date': day.isoformat()})
        self.assertEqual(res.data['lessons'], [])

    def test_it_filters_by_age_band(self):
        day = self.first_date(self.closed)
        res = self.client.get(self.URL, {'date': day.isoformat(), 'age_key': '1-1'})
        self.assertEqual([r['id'] for r in res.data['lessons']], [str(self.open.id)])

    def test_the_age_band_is_labelled_as_a_stage_not_as_years(self):
        """min_age/max_age are stage codes. 'גילאי 5–6' on a ג-ד class is wrong."""
        day = self.first_date(self.closed)
        res = self.client.get(self.URL, {'date': day.isoformat(), 'age_key': '5-6'})
        row = res.data['lessons'][0]
        self.assertEqual(row['age_label'], 'כיתה ג–כיתה ד')

    def test_it_filters_by_branch(self):
        day = self.first_date(self.closed)
        other = Branch.objects.create(name='סניף אחר', city=self.city)
        res = self.client.get(self.URL, {'date': day.isoformat(), 'branch_id': str(other.id)})
        self.assertEqual(res.data['lessons'], [])

    def test_it_needs_a_date(self):
        self.assertEqual(self.client.get(self.URL).status_code, 400)
