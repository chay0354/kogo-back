"""
The widget hides the trial button for a closed lesson; the server refuses the
booking the button was hidden for. Both read the same rule, so a lesson the
office closed cannot be booked through an old tab or a hand-made request.
"""
from datetime import time

from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, City
from apps.courses.models import Course, CourseType, Lesson
from apps.enrollments.models import TrialRegistrationPolicy

COURSES_URL = '/api/v1/customers/widget/courses/'
TRIAL_URL = '/api/v1/customers/widget/trial-register/'
CLOSED = 'ההרשמה לשיעור ניסיון סגורה לשיעור זה'


class WidgetTrialPolicyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.course = Course.objects.create(
            name='היפהופ', branch=self.branch, course_type=CourseType.objects.create(name='היפהופ'),
            price=320, capacity=20, min_age=6, max_age=9, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0),
        )

    def _flags(self):
        res = self.client.get(COURSES_URL, {'branch_id': str(self.branch.id)})
        self.assertEqual(res.status_code, 200, res.content)
        return {
            lesson['id']: lesson['trial_registration_open']
            for course in res.json() for lesson in course.get('lessons', [])
        }

    def _book_trial(self):
        return self.client.post(TRIAL_URL, {
            'parent_id_number': '123456782', 'parent_first_name': 'רות', 'parent_last_name': 'ניסן',
            'parent_phone': '0521234567',
            'child_first_name': 'נועה', 'child_last_name': 'ניסן', 'child_id_number': '111111118',
            'child_birth_date': '2018-05-05', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'trial_lesson_date': '2030-01-06',
        }, format='json')

    def test_the_catalog_says_whether_a_trial_may_be_booked(self):
        self.assertIs(self._flags()[str(self.lesson.id)], True)

        policy = TrialRegistrationPolicy.current()
        policy.trials_open = False
        policy.save()
        self.assertIs(self._flags()[str(self.lesson.id)], False)

        self.lesson.trial_registration_open = True
        self.lesson.save()
        self.assertIs(self._flags()[str(self.lesson.id)], True)

    def test_a_closed_lesson_refuses_the_booking_the_button_was_hidden_for(self):
        self.lesson.trial_registration_open = False
        self.lesson.save()
        res = self._book_trial()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json().get('error'), CLOSED)

    def test_an_open_lesson_gets_past_that_check(self):
        res = self._book_trial()
        self.assertNotEqual(res.json().get('error'), CLOSED, res.content)
