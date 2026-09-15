"""
Trial availability is a question about a date, not about a class.

The case this came from: a Wednesday class held fourteen paying children and six
trials, all six booked for the same date. The room holds twenty. Because the
widget summed every trial ahead into one number, the class reported itself full
for trials outright — and the parent never reached the date picker, where the
following Wednesday was sitting empty.

Two things had to change and both are pinned here: the class is closed to trials
only when *every* offered date is full, and a full date is shown as full rather
than quietly dropped from the list. The second matters more than it sounds. A
shortened list is indistinguishable from "this class has no dates", and when the
nearest date was the full one, the parent was told to come back later about a
class that had room the week after.
"""
from datetime import date, time
from decimal import Decimal

from rest_framework.test import APITestCase

from apps.core.models import Branch, City, Room
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor

CATALOG = '/api/v1/customers/widget/courses/'
DATES = '/api/v1/customers/widget/lesson-occurrences/'


class WidgetTrialDateTests(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=self.city)
        self.room = Room.objects.create(name='סטודיו', branch=self.branch, capacity=20)
        self.ctype = CourseType.objects.create(name='קפוארה')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='t@dates.test', primary_branch=self.branch,
        )
        self.course = Course.objects.create(
            name='קפוארה 4.5-6 יום רביעי', branch=self.branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20, instructor=self.instructor,
            show_in_widget=True, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, instructor=self.instructor, day_of_week=3,
            start_time=time(17, 30), end_time=time(18, 15), is_recurring=True, room=self.room,
        )
        for i in range(14):
            self.child(f'משלם{i}')

    def child(self, name, *, status='active', trial_on=None):
        family = Family.objects.create(name=name, phone='0529999999', branch=self.branch)
        kid = Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2016, 4, 4), gender='male', status=status,
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=kid, status='active', trial_lesson_date=trial_on,
        )
        return kid

    def offered_dates(self):
        res = self.client.get(DATES, {'lesson_id': str(self.lesson.id)})
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def lesson_payload(self):
        res = self.client.get(CATALOG, {'branch_id': str(self.branch.id)})
        self.assertEqual(res.status_code, 200, res.data)
        for course in res.data:
            for row in course.get('lessons', []):
                if row['id'] == str(self.lesson.id):
                    return row
        self.fail('lesson missing from the catalogue')

    def fill(self, when, how_many=6):
        for i in range(how_many):
            self.child(f'ניסיון{when}{i}', status='trial_signed', trial_on=when)


class ADateBeingFullDoesNotCloseTheClass(WidgetTrialDateTests):
    def test_the_class_still_offers_a_trial_when_a_later_date_has_room(self):
        dates = self.offered_dates()
        self.assertGreaterEqual(len(dates), 2, 'need at least two offered dates')
        self.fill(date.fromisoformat(dates[0]['date']))

        row = self.lesson_payload()
        self.assertFalse(row['trial_is_full'])
        # The best remaining date, not the sum of everything booked ahead.
        self.assertEqual(row['trial_spots_left'], 6)

    def test_the_full_date_is_listed_as_full_and_the_next_one_is_not(self):
        dates = self.offered_dates()
        first = date.fromisoformat(dates[0]['date'])
        self.fill(first)

        after = self.offered_dates()
        self.assertEqual(len(after), len(dates), 'a full date must not vanish from the list')
        self.assertTrue(after[0]['is_full'])
        self.assertEqual(after[0]['seats_left'], 0)
        self.assertFalse(after[1]['is_full'])
        self.assertEqual(after[1]['seats_left'], 6)

    def test_the_class_closes_only_when_every_offered_date_is_full(self):
        for row in self.offered_dates():
            self.fill(date.fromisoformat(row['date']))
        self.assertTrue(self.lesson_payload()['trial_is_full'])
        self.assertTrue(all(row['is_full'] for row in self.offered_dates()))

    def test_a_class_with_room_everywhere_reports_every_date_open(self):
        row = self.lesson_payload()
        self.assertFalse(row['trial_is_full'])
        self.assertEqual(row['trial_spots_left'], 6)
        self.assertTrue(all(not d['is_full'] for d in self.offered_dates()))


class TheServerStillRefusesAFullDate(WidgetTrialDateTests):
    def test_booking_a_trial_on_a_full_date_is_rejected(self):
        """
        The list marks a date full; this is what stops it being booked anyway —
        a stale page, a resubmit, or someone calling the endpoint directly.
        """
        first = date.fromisoformat(self.offered_dates()[0]['date'])
        self.fill(first)
        res = self.client.post('/api/v1/customers/widget/trial-register/', {
            'course_id': str(self.course.id),
            'lesson_id': str(self.lesson.id),
            'trial_lesson_date': first.isoformat(),
            'parent_first_name': 'הורה', 'parent_last_name': 'בדיקה',
            'parent_id_number': '123456782', 'parent_phone': '0501234567',
            'phone': '0501234567', 'email': 'p@dates.test',
            'child_first_name': 'ילד', 'child_last_name': 'בדיקה',
            'child_id_number': '123456782',
            'child_birth_date': '2017-01-01', 'child_gender': 'male',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('התפוסה מלאה', str(res.data))
