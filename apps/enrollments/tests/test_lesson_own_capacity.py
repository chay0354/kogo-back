"""
One lesson can be limited without limiting its group.

A group's capacity is one figure for all its days. To close a Sunday that had
filled, the office lowered the group's figure — and its Wednesday, with six
children in a studio for twenty, was shown as full too (owner, 6.10.2026). A
lesson now has a limit of its own: the tightest of the lesson's, the group's
and the room's is what the lesson takes, and a lesson with no limit of its own
behaves as it always did.

Every door that asks "is there room" is held to the same answer: the public
class list, the office enrolling a child, a trial booked by the office, a child
moved between classes, a twice-a-week track, and the lesson's own row in the
office screens.
"""
from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, City, Room, UserProfile
from apps.core.payment_service import validate_bundle_capacity
from apps.courses.models import Course, CourseType, Lesson, LessonBundle
from apps.customers.models import Child, Family, Parent
from apps.enrollments.change_course import _lesson_has_room
from apps.enrollments.enrollment_counts import resolve_lesson_capacity, tightest_capacity
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
SUNDAY, WEDNESDAY = 0, 3
ENROLL = '/api/v1/enrollments/lesson-enrollments/'


class ALessonWithItsOwnLimit(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='אם המושבות', city=City.objects.create(name='פתח תקווה'))
        self.room = Room.objects.create(branch=self.branch, name='סטודיו', capacity=20)
        self.course = Course.objects.create(
            course_type=CourseType.objects.create(name='קפוארה'), name='קפוארה ג-ד-ה',
            price=260, capacity=20, branch=self.branch, is_active=True,
        )
        self.sunday = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=SUNDAY,
            start_time=time(16, 0), end_time=time(16, 45), is_recurring=True, capacity=4,
        )
        self.wednesday = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=WEDNESDAY,
            start_time=time(16, 0), end_time=time(16, 45), is_recurring=True,
        )
        user = User.objects.create_user(username='office@t.com', email='office@t.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.office = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.office.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self._families = 0

    def _child(self, name, status='active'):
        self._families += 1
        family = Family.objects.create(name=name, phone=f'05{self._families:08d}', branch=self.branch)
        Parent.objects.create(family=family, first_name='הורה', last_name=name, phone=family.phone, is_primary=True)
        return Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2015, 1, 1), gender='female', status=status,
        )

    def _fill(self, lesson, n):
        for i in range(n):
            LessonEnrollment.objects.create(
                child=self._child(f'{lesson.day_of_week}-{i}'), lesson=lesson, status='active',
            )

    def _lesson_put(self, lesson, **changes):
        """What the lesson edit window sends."""
        body = {
            'course': str(self.course.id), 'room': str(self.room.id), 'instructor': None,
            'day_of_week': lesson.day_of_week, 'start_time': '16:00', 'end_time': '16:45',
            'notes': '', 'trial_registration_open': None, 'is_recurring': True, 'status': 'scheduled',
        }
        body.update(changes)
        return self.office.put(f'/api/v1/courses/lessons/{lesson.id}/', body, format='json')

    # ── the rule ─────────────────────────────────────────────────────────────

    def test_the_tightest_of_the_lesson_the_group_and_the_room(self):
        self.assertEqual(resolve_lesson_capacity(self.sunday), 4)
        self.assertEqual(resolve_lesson_capacity(self.wednesday), 20)

    def test_a_limit_above_the_group_does_not_raise_it(self):
        self.sunday.capacity = 35
        self.assertEqual(resolve_lesson_capacity(self.sunday), 20)

    def test_a_room_smaller_than_the_lessons_limit_still_wins(self):
        self.room.capacity = 3
        self.room.save()
        self.sunday.refresh_from_db()
        self.assertEqual(resolve_lesson_capacity(self.sunday), 3)

    def test_zero_and_empty_are_no_limit(self):
        self.assertEqual(tightest_capacity(0, None, 20), 20)
        self.assertIsNone(tightest_capacity(None, 0, None))
        self.sunday.capacity = 0
        self.assertEqual(resolve_lesson_capacity(self.sunday), 20)

    # ── the public class list ────────────────────────────────────────────────

    def _listed(self):
        answer = APIClient().get('/api/v1/customers/widget/courses/', {'branch_id': str(self.branch.id)})
        self.assertEqual(answer.status_code, 200, answer.content)
        courses = answer.json()
        self.assertEqual(len(courses), 1, courses)
        return {lesson['day_of_week']: lesson for lesson in courses[0]['lessons']}

    def test_the_site_shows_the_limited_day_full_and_the_other_day_open(self):
        self._fill(self.sunday, 4)
        self._fill(self.wednesday, 6)

        listed = self._listed()

        sunday, wednesday = listed[SUNDAY], listed[WEDNESDAY]
        self.assertEqual((sunday['capacity'], sunday['enrolled_count'], sunday['available_spots']), (4, 4, 0))
        self.assertTrue(sunday['is_full'])
        self.assertTrue(sunday['trial_is_full'])
        self.assertEqual((wednesday['capacity'], wednesday['enrolled_count'], wednesday['available_spots']), (20, 6, 14))
        self.assertFalse(wednesday['is_full'])
        self.assertFalse(wednesday['trial_is_full'])

    def test_the_limited_day_is_open_until_it_fills(self):
        self._fill(self.sunday, 3)

        sunday = self._listed()[SUNDAY]

        self.assertEqual((sunday['capacity'], sunday['available_spots']), (4, 1))
        self.assertFalse(sunday['is_full'])

    # ── the office ───────────────────────────────────────────────────────────

    def _office_enrolls(self, lesson, **more):
        return self.office.post(ENROLL, {
            'lesson': str(lesson.id), 'child': str(self._child('חדש').id), 'status': 'active', **more,
        }, format='json')

    def test_the_office_cannot_enroll_past_the_lessons_limit(self):
        self._fill(self.sunday, 4)

        refused = self._office_enrolls(self.sunday)

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn('קיבולת מקסימלית: 4', str(refused.json()))

    def test_the_office_enrolls_freely_on_the_groups_other_day(self):
        self._fill(self.sunday, 4)
        self._fill(self.wednesday, 6)

        self.assertEqual(self._office_enrolls(self.wednesday).status_code, 201)

    def test_a_trial_booked_by_the_office_respects_the_lessons_limit(self):
        self._fill(self.sunday, 4)

        refused = self._office_enrolls(self.sunday, trial_registration=True)

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn('קיבולת מקסימלית: 4', str(refused.json()))

    # ── moving a child, and a hosted-page checkout ───────────────────────────

    def test_a_child_cannot_be_moved_into_the_full_day(self):
        self._fill(self.sunday, 4)
        self._fill(self.wednesday, 6)
        newcomer = self._child('עובר')

        self.assertEqual(_lesson_has_room(self.sunday, newcomer.id), 'השיעור מלא — קיבולת מקסימלית: 4 תלמידים')
        self.assertIsNone(_lesson_has_room(self.wednesday, newcomer.id))

    # ── a twice-a-week track ─────────────────────────────────────────────────

    def test_a_track_with_the_full_day_in_it_is_full(self):
        self._fill(self.sunday, 4)
        bundle = LessonBundle.objects.create(course=self.course, name='פעמיים בשבוע', combined_price=400)
        bundle.lessons.set([self.sunday, self.wednesday])

        with self.assertRaisesMessage(ValueError, 'קיבולת מקסימלית: 4'):
            validate_bundle_capacity(bundle)

        self.sunday.capacity = None
        self.sunday.save()
        validate_bundle_capacity(LessonBundle.objects.get(pk=bundle.pk))

    # ── setting it, in the lesson edit window ────────────────────────────────

    def test_the_office_sets_changes_and_clears_the_limit(self):
        self.assertEqual(self._lesson_put(self.wednesday, capacity=8).status_code, 200)
        self.wednesday.refresh_from_db()
        self.assertEqual(self.wednesday.capacity, 8)

        self.assertEqual(self._lesson_put(self.wednesday, capacity=None).status_code, 200)
        self.wednesday.refresh_from_db()
        self.assertIsNone(self.wednesday.capacity)

    def test_zero_is_saved_as_no_limit_and_a_negative_number_is_refused(self):
        self.assertEqual(self._lesson_put(self.sunday, capacity=0).status_code, 200)
        self.sunday.refresh_from_db()
        self.assertIsNone(self.sunday.capacity)

        self.assertEqual(self._lesson_put(self.sunday, capacity=-3).status_code, 400)

    def test_a_screen_that_does_not_know_the_field_leaves_the_limit_alone(self):
        """An office tab still open on the older screen saves a lesson without `capacity`."""
        self.assertEqual(self._lesson_put(self.sunday, notes='הערה').status_code, 200)

        self.sunday.refresh_from_db()
        self.assertEqual(self.sunday.capacity, 4)

    def test_the_edit_window_is_handed_what_it_sends_back(self):
        """
        The catalog's lesson rows open the edit window, and the window sends the
        whole lesson back. What the rows did not carry was wiped on any save: a
        lesson closed to trials went back to the general rule.
        """
        self.sunday.trial_registration_open = False
        self.sunday.save()

        answer = self.office.get(f'/api/v1/courses/courses/{self.course.id}/lessons_detail/')

        self.assertEqual(answer.status_code, 200, answer.content)
        rows = {row['day_of_week']: row for row in answer.json()}
        self.assertEqual(rows[SUNDAY]['capacity'], 4)
        self.assertIs(rows[SUNDAY]['trial_registration_open'], False)
        self.assertIsNone(rows[WEDNESDAY]['capacity'])
        self.assertIsNone(rows[WEDNESDAY]['trial_registration_open'])

    def test_the_lesson_says_what_it_really_takes(self):
        answer = self.office.get(f'/api/v1/courses/lessons/{self.sunday.id}/')

        self.assertEqual(answer.status_code, 200, answer.content)
        body = answer.json()
        self.assertEqual((body['capacity'], body['effective_capacity'], body['room_capacity']), (4, 4, 20))
