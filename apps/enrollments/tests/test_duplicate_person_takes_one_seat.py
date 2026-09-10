"""
A child registered twice is one person: one seat, one line on the register,
one row on their card.

The office sees this when a parent registers again with a slightly different
record — a second family row, a nickname — and the child ends up with two Child
cards, each with its own enrolment on the same lesson.
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.duplicate_students import collapse_duplicate_people
from apps.enrollments.enrollment_counts import count_capacity_enrollments
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
PHONE = '0501234567'


class DuplicatePersonTakesOneSeatTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='כפר סבא')
        self.room = Room.objects.create(branch=self.branch, name='אולם', capacity=20)
        course_type = CourseType.objects.create(name='ריקוד')
        self.course = Course.objects.create(
            course_type=course_type, name='ג-ד ריקוד', price=260, capacity=10, branch=self.branch,
        )
        self.monday = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=1,
            start_time=time(18, 15), end_time=time(19, 15), is_recurring=True,
        )
        self.thursday = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=4,
            start_time=time(18, 0), end_time=time(19, 0), is_recurring=True,
        )
        # The same child, entered twice, under two family rows with one phone.
        self.family_a = Family.objects.create(name='מירון', phone=PHONE, parent_id_number='111111118', branch=self.branch)
        Parent.objects.create(family=self.family_a, first_name='רון', last_name='מירון', phone=PHONE, is_primary=True)
        self.family_b = Family.objects.create(name='מירון', phone=PHONE, parent_id_number='11111118', branch=self.branch)
        Parent.objects.create(family=self.family_b, first_name='רון', last_name='מירון', phone=PHONE, is_primary=True)
        self.card_a = self._child(self.family_a)
        self.card_b = self._child(self.family_b)
        for card in (self.card_a, self.card_b):
            for lesson in (self.monday, self.thursday):
                LessonEnrollment.objects.create(child=card, lesson=lesson, status='active')

        user = User.objects.create_user(username='mgr@t.com', email='mgr@t.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _child(self, family, first='אדר'):
        return Child.objects.create(
            family=family, first_name=first, last_name='מירון',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )

    def _other_child(self, first):
        family = Family.objects.create(name=first, phone='0509998887', branch=self.branch)
        Parent.objects.create(family=family, first_name='הורה', last_name=first, phone='0509998887', is_primary=True)
        return self._child(family, first=first)

    def test_two_cards_of_one_child_hold_one_seat(self):
        self.assertEqual(count_capacity_enrollments(lesson=self.monday), 1)
        self.assertEqual(count_capacity_enrollments(lesson=self.thursday), 1)

    def test_two_different_children_still_hold_two_seats(self):
        other = self._other_child('נועה')
        LessonEnrollment.objects.create(child=other, lesson=self.monday, status='active')
        self.assertEqual(count_capacity_enrollments(lesson=self.monday), 2)

    def test_the_register_shows_the_child_once(self):
        res = self.client.get(f'/api/v1/scheduling/lessons/{self.monday.id}/', {'date': self._next_monday().isoformat()})
        self.assertEqual(res.status_code, 200, res.content)
        names = [row['child_name'] for row in res.json()['enrollments']]
        self.assertEqual(names.count('אדר מירון'), 1)

    def test_the_card_shows_two_meetings_not_four(self):
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family_a.id)})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body['results'] if isinstance(body, dict) else body
        card = next(r for r in rows if r['id'] == str(self.card_a.id))
        slots = [e for e in card['enrollments'] if e.get('course_id') == str(self.course.id)]
        self.assertEqual(len(slots), 2)
        self.assertEqual({s['day_of_week'] for s in slots}, {1, 4})

    def test_a_row_with_no_phone_is_never_folded(self):
        # Half a key is not evidence of identity.
        nameless_family = Family.objects.create(name='ללא', phone='', branch=self.branch)
        loose = Child.objects.create(
            family=nameless_family, first_name='אדר', last_name='מירון',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        row = LessonEnrollment.objects.create(child=loose, lesson=self.monday, status='active')
        kept = collapse_duplicate_people(list(
            LessonEnrollment.objects.filter(lesson=self.monday).select_related('child', 'child__family')
        ))
        self.assertIn(row.id, {e.id for e in kept})
        self.assertEqual(len(kept), 2)

    def test_a_paying_row_wins_over_a_trial_row_of_the_same_person(self):
        LessonEnrollment.objects.filter(child=self.card_b, lesson=self.monday).update(
            trial_lesson_date=date.today() + timedelta(days=3),
        )
        kept = collapse_duplicate_people(list(
            LessonEnrollment.objects.filter(lesson=self.monday).select_related('child', 'child__family')
        ))
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0].trial_lesson_date)

    def _next_monday(self):
        today = date.today()
        return today + timedelta(days=(0 - today.weekday()) % 7 or 7)
