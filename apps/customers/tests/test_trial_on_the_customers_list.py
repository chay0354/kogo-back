"""
The trial a child's row names in the customers list.

Owner, 8.10.2026: "כל אחד שעשה שיעור ניסיון אני רוצה לראות אותו. מי שפעיל שלא
יראה מתי עשה ניסיון. רק מי שנרשם לניסיון או ביצע ניסיון."

The list draws a ביצע ניסיון child's trial from `trial_enrollment`. It used to be
named only once the nightly cron had written what became of it, so the morning
after a trial — and for any trial the cron never retired — the row was empty.
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment


class TrialNamedOnTheListTest(TestCase):
    def setUp(self):
        self.today = date.today()
        self.branch = Branch.objects.create(name='Main')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        course_type = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(
            course_type=course_type, name='Acro', price=400, capacity=10, branch=self.branch,
        )
        self.other_course = Course.objects.create(
            course_type=course_type, name='Capoeira', price=400, capacity=10, branch=self.branch,
        )
        self.lesson = self._lesson(self.course, 1)
        self.other_lesson = self._lesson(self.other_course, 3)
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        self.child = self._child('trial_completed')

        User = get_user_model()
        manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')

    def _lesson(self, course, day_of_week):
        return Lesson.objects.create(
            course=course, room=self.room, day_of_week=day_of_week,
            start_time=time(16, 0), end_time=time(17, 0), is_recurring=True,
        )

    def _child(self, status, first_name='Noa'):
        return Child.objects.create(
            family=self.family, first_name=first_name, last_name='Kid',
            birth_date=date(2016, 1, 1), gender='female', status=status,
        )

    def _trial(self, days_ago, *, lesson=None, child=None, **fields):
        return LessonEnrollment.objects.create(
            lesson=lesson or self.lesson, child=child or self.child,
            trial_lesson_date=self.today - timedelta(days=days_ago),
            **{'status': 'active', **fields},
        )

    def _row(self, child=None):
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        self.assertEqual(res.status_code, 200, res.content)
        wanted = str((child or self.child).id)
        return next(row for row in res.data['results'] if row['id'] == wanted)

    def test_a_trial_held_yesterday_is_named_before_the_cron_wrote_its_outcome(self):
        self._trial(1)
        row = self._row()
        self.assertEqual(row['enrollments'], [])
        self.assertEqual(row['trial_enrollment']['course_name'], 'Acro')
        self.assertEqual(
            row['trial_enrollment']['trial_lesson_date'], (self.today - timedelta(days=1)).isoformat(),
        )
        self.assertIsNone(row['trial_enrollment']['trial_outcome'])

    def test_a_retired_trial_is_named_with_what_became_of_it(self):
        held_on = self.today - timedelta(days=5)
        self._trial(5, status='inactive', end_date=held_on, trial_outcome='no_show')
        trial = self._row()['trial_enrollment']
        self.assertEqual(trial['trial_lesson_date'], held_on.isoformat())
        self.assertEqual(trial['trial_outcome'], 'no_show')

    def test_a_trial_cancelled_ahead_of_its_date_never_took_place(self):
        # The office cancelled it three days before: the row ends on the day it
        # was dropped, and no outcome is written.
        self._trial(1, status='inactive', end_date=self.today - timedelta(days=4))
        self.assertIsNone(self._row()['trial_enrollment'])

    def test_a_trial_taken_off_after_its_date_did_take_place(self):
        self._trial(3, status='inactive', end_date=self.today - timedelta(days=1))
        trial = self._row()['trial_enrollment']
        self.assertEqual(trial['trial_lesson_date'], (self.today - timedelta(days=3)).isoformat())

    def test_a_trial_still_ahead_is_not_a_trial_held(self):
        child = self._child('pending', first_name='Dan')
        self._trial(-3, child=child, status='inactive', end_date=self.today)
        self.assertIsNone(self._row(child)['trial_enrollment'])

    def test_the_latest_of_two_trials_is_the_one_named(self):
        self._trial(20, status='inactive', end_date=self.today - timedelta(days=20), trial_outcome='attended')
        self._trial(1, lesson=self.other_lesson, trial_number=2)
        trial = self._row()['trial_enrollment']
        self.assertEqual(trial['course_name'], 'Capoeira')
        self.assertEqual(trial['trial_number'], 2)

    def test_a_booked_trial_is_still_the_one_named_for_a_child_who_signed_up(self):
        child = self._child('trial_signed', first_name='Dan')
        self._trial(20, child=child, status='inactive', end_date=self.today - timedelta(days=20), trial_outcome='attended')
        booked = self._trial(-2, child=child, lesson=self.other_lesson, trial_number=2)
        row = self._row(child)
        self.assertEqual(row['trial_enrollment']['enrollment_id'], str(booked.id))
        self.assertEqual([e['enrollment_id'] for e in row['enrollments']], [str(booked.id)])

    def test_a_student_who_once_tried_carries_no_trial_among_the_classes(self):
        # Registering clears the trial's date on the row and keeps it aside
        # (trial_held_on). The class is a regular class; the list has no trial
        # to draw for a student.
        student = self._child('active', first_name='Dan')
        row = LessonEnrollment.objects.create(
            lesson=self.lesson, child=student, status='active',
            trial_lesson_date=self.today - timedelta(days=10),
        )
        row.trial_lesson_date = None
        row.save(update_fields=['trial_lesson_date', 'updated_at'])
        listed = self._row(student)
        self.assertEqual([e['trial_lesson_date'] for e in listed['enrollments']], [None])
