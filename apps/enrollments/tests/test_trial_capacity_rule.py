"""
Two capacities, one room.

A paying place is measured against the paying students. A trial is a body in
the room on its date, so it is measured against the payers PLUS the trials
already booked for that day. Twenty payers and five trials is twenty-five
children in a room built for twenty.
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.enrollment_counts import count_capacity_enrollments, trial_seats_left
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
CAPACITY = 20
WEDNESDAY = 3
PY_WEDNESDAY = 2


def _wednesday(weeks=0):
    today = date.today()
    ahead = (PY_WEDNESDAY - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * weeks)


class TrialCapacityRuleTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='סניף')
        self.room = Room.objects.create(branch=self.branch, name='אולם', capacity=CAPACITY)
        self.course = Course.objects.create(
            course_type=CourseType.objects.create(name='ריקוד'), name='ריקוד',
            price=260, capacity=CAPACITY, branch=self.branch, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=WEDNESDAY,
            start_time=time(17, 0), end_time=time(18, 0), is_recurring=True,
        )
        user = User.objects.create_user(username='mgr@t.com', email='mgr@t.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _child(self, name, status='active'):
        family = Family.objects.create(name=name, phone=f'050{abs(hash(name)) % 10_000_000:07d}', branch=self.branch)
        Parent.objects.create(family=family, first_name='הורה', last_name=name, phone=family.phone, is_primary=True)
        return Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2015, 1, 1), gender='female', status=status,
        )

    def _fill_payers(self, n):
        for i in range(n):
            LessonEnrollment.objects.create(child=self._child(f'משלם{i}'), lesson=self.lesson, status='active')

    def _book_trials(self, n, on=None):
        when = on or _wednesday()
        for i in range(n):
            LessonEnrollment.objects.create(
                child=self._child(f'ניסיון{i}{when}', status='trial_signed'),
                lesson=self.lesson, status='active', trial_lesson_date=when,
            )

    def _try_trial(self, when=None):
        return self.client.post('/api/v1/enrollments/lesson-enrollments/', {
            'lesson': str(self.lesson.id), 'child': str(self._child('חדש').id),
            'status': 'active', 'trial_registration': True,
            'trial_lesson_date': (when or _wednesday()).isoformat(),
        }, format='json')

    def _try_paying(self):
        return self.client.post('/api/v1/enrollments/lesson-enrollments/', {
            'lesson': str(self.lesson.id), 'child': str(self._child('משלם חדש').id), 'status': 'active',
        }, format='json')

    def test_a_trial_is_refused_once_payers_plus_trials_reach_the_capacity(self):
        self._fill_payers(15)
        self._book_trials(5)
        self.assertEqual(trial_seats_left(lesson=self.lesson, occurrence_date=_wednesday()), 0)
        res = self._try_trial()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('התפוסה מלאה לשיעור ניסיון', str(res.json()))

    def test_a_trial_is_allowed_while_there_is_still_room_for_a_body(self):
        self._fill_payers(15)
        self._book_trials(4)
        self.assertEqual(trial_seats_left(lesson=self.lesson, occurrence_date=_wednesday()), 1)
        self.assertEqual(self._try_trial().status_code, 201)

    def test_a_paying_place_ignores_the_trials(self):
        # 15 payers + 5 trials fills the room for a trial, but a paying place is
        # still free: a visitor never costs a subscriber their seat.
        self._fill_payers(15)
        self._book_trials(5)
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 15)
        self.assertEqual(self._try_paying().status_code, 201)

    def test_a_paying_registration_is_refused_only_when_the_payers_fill_it(self):
        self._fill_payers(CAPACITY)
        res = self._try_paying()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('השיעור מלא', str(res.json()))

    def test_trials_on_another_date_do_not_fill_this_one(self):
        self._fill_payers(18)
        self._book_trials(2, on=_wednesday(1))
        self.assertEqual(trial_seats_left(lesson=self.lesson, occurrence_date=_wednesday()), 2)
        self.assertEqual(trial_seats_left(lesson=self.lesson, occurrence_date=_wednesday(1)), 0)

    def test_a_full_date_is_not_offered_by_the_widget_picker(self):
        self._fill_payers(18)
        self._book_trials(2, on=_wednesday())
        res = self.client.get('/api/v1/customers/widget/lesson-occurrences/', {'lesson_id': str(self.lesson.id)})
        self.assertEqual(res.status_code, 200, res.content)
        offered = {row['date'] for row in res.json()}
        self.assertNotIn(_wednesday().isoformat(), offered)
        self.assertIn(_wednesday(1).isoformat(), offered)

    def test_the_catalogue_says_when_a_trial_can_no_longer_be_booked(self):
        self._fill_payers(18)
        self._book_trials(2)
        res = self.client.get('/api/v1/customers/widget/courses/', {'branch_id': str(self.branch.id)})
        self.assertEqual(res.status_code, 200, res.content)
        rows = [c for c in res.json() if c['id'] == str(self.course.id)]
        self.assertTrue(rows, res.json())
        lesson_row = next(l for l in rows[0]['lessons'] if l['id'] == str(self.lesson.id))
        self.assertTrue(lesson_row['trial_is_full'])
        self.assertEqual(lesson_row['trial_spots_left'], 0)
        self.assertFalse(lesson_row['is_full'])
        self.assertEqual(lesson_row['available_spots'], 2)

    def test_the_room_is_the_limit_when_it_is_smaller_than_the_course(self):
        Room.objects.filter(pk=self.room.pk).update(capacity=10)
        self.lesson.refresh_from_db()
        self._fill_payers(8)
        self._book_trials(2)
        self.assertEqual(trial_seats_left(lesson=self.lesson, occurrence_date=_wednesday()), 0)
