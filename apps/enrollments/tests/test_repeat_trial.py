"""
A child who already had a trial can be booked for another one — from the CRM
only. On the same lesson the old row is reused (the unique lesson+child rule
used to refuse it with a bare 400); on any lesson the trial is numbered, and
the register and the customer card show the number.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
URL = '/api/v1/enrollments/lesson-enrollments/'
WEDNESDAY = 3          # Kogo day_of_week: 0 = Sunday
PY_WEDNESDAY = 2       # python weekday: 0 = Monday


def _wednesday(offset_weeks):
    """A Wednesday `offset_weeks` from the coming one (negative = in the past)."""
    today = date.today()
    ahead = (PY_WEDNESDAY - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * offset_weeks)


@patch('apps.enrollments.views.stamp_and_notify_trial_enrollment', return_value={'sent': True, 'method': 'flow'})
class RepeatTrialTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        ct = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(course_type=ct, name='Kids', price=100, capacity=10, branch=self.branch)
        self.lesson = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=WEDNESDAY, start_time='16:00', end_time='17:00',
        )
        self.other = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=WEDNESDAY, start_time='18:00', end_time='19:00',
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(family=self.family, first_name='Avi', last_name='Cohen', phone='0501234567', is_primary=True)
        self.child = Child.objects.create(
            family=self.family, first_name='Noa', last_name='Cohen',
            birth_date=date(2015, 1, 1), gender='female', status='pending',
        )
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _finished_trial(self, lesson=None, outcome='attended'):
        row = LessonEnrollment.objects.create(
            child=self.child, lesson=lesson or self.lesson, status='inactive',
            trial_lesson_date=_wednesday(-2), trial_outcome=outcome,
        )
        Child.objects.filter(pk=self.child.pk).update(
            status='trial_completed', trial_classes_attended=1 if outcome == 'attended' else 0,
        )
        self.child.refresh_from_db()
        return row

    def _book(self, lesson=None, **extra):
        return self.client.post(URL, {
            'lesson': str((lesson or self.lesson).id), 'child': str(self.child.id),
            'status': 'active', 'trial_registration': True, **extra,
        }, format='json')

    def test_a_finished_trial_can_be_booked_again_on_the_same_lesson(self, notify):
        row = self._finished_trial()
        res = self._book()
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body['trial_applied'])
        self.assertTrue(body['repeat_trial'])
        self.assertEqual(body['trial_number'], 2)
        row.refresh_from_db()
        self.assertEqual(row.status, 'active')
        self.assertEqual(row.trial_number, 2)
        self.assertEqual(row.trial_outcome, '')
        self.assertGreaterEqual(row.trial_lesson_date, date.today())
        self.assertEqual(row.trial_lesson_date.weekday(), PY_WEDNESDAY)
        self.assertEqual(row.start_date, row.trial_lesson_date)
        self.assertEqual(body['trial_lesson_date'], row.trial_lesson_date.isoformat())
        self.assertEqual(LessonEnrollment.objects.filter(child=self.child).count(), 1)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'trial_signed')
        notify.assert_called_once_with(str(row.id))

    def test_a_no_show_can_come_and_try_again(self, notify):
        row = self._finished_trial(outcome='no_show')
        res = self._book()
        self.assertEqual(res.status_code, 200, res.content)
        row.refresh_from_db()
        self.assertEqual((row.trial_number, row.trial_outcome, row.status), (2, '', 'active'))

    def test_an_upcoming_trial_is_not_booked_twice(self, notify):
        soon = _wednesday(0)
        row = LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active', trial_lesson_date=soon)
        res = self._book()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('כבר רשום לשיעור ניסיון', res.json()['error'])
        row.refresh_from_db()
        self.assertEqual((row.trial_number, row.trial_lesson_date), (1, soon))
        notify.assert_not_called()

    def test_a_paying_student_gets_no_trial_on_their_own_lesson(self, notify):
        LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active')
        res = self._book()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('תלמיד קבוע', res.json()['error'])
        notify.assert_not_called()

    def test_a_second_trial_on_another_lesson_is_number_two(self, notify):
        self._finished_trial()
        res = self._book(lesson=self.other)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['trial_number'], 2)
        self.assertTrue(res.json()['repeat_trial'])
        new = LessonEnrollment.objects.get(child=self.child, lesson=self.other)
        self.assertEqual(new.trial_number, 2)
        self.assertEqual(LessonEnrollment.objects.filter(child=self.child).count(), 2)

    def test_a_third_trial_is_number_three(self, notify):
        self._finished_trial()
        self._book(lesson=self.other)
        LessonEnrollment.objects.filter(child=self.child, lesson=self.other).update(
            status='inactive', trial_lesson_date=_wednesday(-1), trial_outcome='no_show',
        )
        res = self._book()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['trial_number'], 3)

    def test_the_first_trial_is_number_one(self, notify):
        res = self._book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['trial_number'], 1)
        self.assertFalse(res.json()['repeat_trial'])

    def test_a_converted_child_counts_the_trial_the_counter_remembers(self, notify):
        # The trial row became a paid registration (its date was cleared); the
        # counter and the outcome still say one trial happened.
        LessonEnrollment.objects.create(child=self.child, lesson=self.other, status='active', trial_outcome='attended')
        Child.objects.filter(pk=self.child.pk).update(status='active', trial_classes_attended=1)
        self.child.refresh_from_db()
        res = self._book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['trial_number'], 2)

    def test_a_full_lesson_refuses_the_repeat_before_anything_changes(self, notify):
        row = self._finished_trial()
        self.course.capacity = 1
        self.course.save()
        other_family = Family.objects.create(name='Levi', phone='0509999999', branch=self.branch)
        taken = Child.objects.create(
            family=other_family, first_name='Dan', last_name='Levi',
            birth_date=date(2015, 1, 1), gender='male', status='active',
        )
        LessonEnrollment.objects.create(child=taken, lesson=self.lesson, status='active')
        res = self._book()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('מלא', str(res.json()))
        row.refresh_from_db()
        self.assertEqual((row.status, row.trial_number, row.trial_outcome), ('inactive', 1, 'attended'))
        notify.assert_not_called()

    def test_a_chosen_date_must_be_a_coming_occurrence(self, notify):
        row = self._finished_trial()
        res = self._book(trial_lesson_date=_wednesday(-3).isoformat())
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('trial_lesson_date', res.json())
        row.refresh_from_db()
        self.assertEqual(row.status, 'inactive')

    def test_the_default_date_skips_a_blocked_day(self, notify):
        row = self._finished_trial()
        # Today may itself be the coming occurrence (before the lesson ends): block both.
        blocked = sorted({date.today().isoformat(), _wednesday(0).isoformat()})
        with override_settings(BLOCKED_TRIAL_LESSON_DATES=blocked):
            res = self._book()
        self.assertEqual(res.status_code, 200, res.content)
        row.refresh_from_db()
        self.assertNotIn(row.trial_lesson_date.isoformat(), blocked)
        self.assertEqual(row.trial_lesson_date.weekday(), PY_WEDNESDAY)
        self.assertGreater(row.trial_lesson_date, date.today())

    def test_the_default_date_skips_cancelled_and_blocked_weeks_from_a_frozen_now(self, notify):
        from zoneinfo import ZoneInfo
        from datetime import datetime
        from django.utils import timezone as dj_tz
        from apps.enrollments.trial_reminders import next_allowed_trial_date
        from apps.scheduling.models import LessonCancellation
        now = dj_tz.make_aware(datetime(2026, 5, 22, 10, 0), ZoneInfo('Asia/Jerusalem'))   # a Friday
        self.lesson.refresh_from_db()   # the fixture's '16:00' strings become times, as the API path sees them
        LessonCancellation.objects.create(lesson=self.lesson, occurrence_date=date(2026, 5, 27))
        with override_settings(BLOCKED_TRIAL_LESSON_DATES=['2026-06-03']):
            self.assertEqual(next_allowed_trial_date(self.lesson, now=now), date(2026, 6, 10))
        self.assertEqual(next_allowed_trial_date(self.lesson, now=now), date(2026, 6, 3))

    def test_a_malformed_id_with_the_trial_flag_is_a_plain_400(self, notify):
        res = self.client.post(URL, {
            'lesson': 'not-a-uuid', 'child': str(self.child.id), 'status': 'active', 'trial_registration': True,
        }, format='json')
        self.assertEqual(res.status_code, 400, res.content)
        notify.assert_not_called()

    def test_an_upcoming_trial_elsewhere_does_not_number_the_new_one(self, notify):
        LessonEnrollment.objects.create(child=self.child, lesson=self.other, status='active', trial_lesson_date=_wednesday(1))
        Child.objects.filter(pk=self.child.pk).update(status='trial_signed')
        self.child.refresh_from_db()
        res = self._book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['trial_number'], 1)

    def test_a_trial_dropped_before_its_date_is_not_a_held_trial(self, notify):
        # Booked, then removed by the office before the day came: inactive, no outcome.
        LessonEnrollment.objects.create(
            child=self.child, lesson=self.other, status='inactive', trial_lesson_date=_wednesday(-2), end_date=_wednesday(-3),
        )
        Child.objects.filter(pk=self.child.pk).update(status='trial_signed')
        self.child.refresh_from_db()
        res = self._book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['trial_number'], 1)

    def test_a_chosen_coming_date_is_kept(self, notify):
        row = self._finished_trial()
        wanted = _wednesday(1)
        res = self._book(trial_lesson_date=wanted.isoformat())
        self.assertEqual(res.status_code, 200, res.content)
        row.refresh_from_db()
        self.assertEqual(row.trial_lesson_date, wanted)

    def test_a_plain_registration_to_an_existing_row_is_refused_as_before(self, notify):
        # Not a trial: the unique rule answers first, exactly as it did before this change.
        row = LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='inactive')
        res = self.client.post(URL, {
            'lesson': str(self.lesson.id), 'child': str(self.child.id), 'status': 'active',
        }, format='json')
        # The unique validator answers first, exactly as before this change.
        self.assertEqual(res.status_code, 400, res.content)
        row.refresh_from_db()
        self.assertEqual(row.status, 'inactive')
        self.assertEqual(row.trial_number, 1)

    def test_the_register_shows_the_trial_number(self, notify):
        row = self._finished_trial()
        self._book()
        row.refresh_from_db()
        res = self.client.get(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': row.trial_lesson_date.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.content)
        ours = next(r for r in res.json()['enrollments'] if r['child_id'] == str(self.child.id))
        self.assertTrue(ours['is_trial'])
        self.assertEqual(ours['trial_number'], 2)

    def test_the_customer_card_carries_the_number(self, notify):
        self._finished_trial()
        self._book()
        # The customers list is what the CRM card reads (the detail route is a plain row).
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json().get('results', res.json())
        card = next(r for r in rows if r['id'] == str(self.child.id))
        self.assertEqual(card['trial_enrollment']['trial_number'], 2)
        self.assertEqual(card['enrollments'][0]['trial_number'], 2)
