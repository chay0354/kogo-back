"""
The widget books a child's first trial only. A child who already had one is
sent to the office, which books the repeat from the CRM.
"""
from datetime import date, time, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, City
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.repeat_trial import REPEAT_TRIAL_WIDGET_ERROR

TRIAL_URL = '/api/v1/customers/widget/trial-register/'
WEDNESDAY = 3
PY_WEDNESDAY = 2


def _wednesday(offset_weeks):
    today = date.today()
    ahead = (PY_WEDNESDAY - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead + 7 * offset_weeks)


@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': True})
class WidgetRepeatTrialTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.course = Course.objects.create(
            name='היפהופ', branch=self.branch, course_type=CourseType.objects.create(name='היפהופ'),
            price=320, capacity=20, min_age=6, max_age=9, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, day_of_week=WEDNESDAY, start_time=time(17, 0), end_time=time(18, 0),
        )
        self.family = Family.objects.create(
            name='ניסן', phone='0521234567', parent_id_number='123456782', branch=self.branch,
        )
        Parent.objects.create(
            family=self.family, first_name='רות', last_name='ניסן', phone='0521234567', is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='ניסן', id_number='111111118',
            birth_date=date(2018, 5, 5), gender='female', status='trial_completed', trial_classes_attended=1,
        )

    def _book(self, trial_date):
        return self.client.post(TRIAL_URL, {
            'parent_id_number': '123456782', 'parent_first_name': 'רות', 'parent_last_name': 'ניסן',
            'parent_phone': '0521234567',
            'child_first_name': 'נועה', 'child_last_name': 'ניסן', 'child_id_number': '111111118',
            'child_birth_date': '2018-05-05', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'trial_lesson_date': trial_date.isoformat(),
        }, format='json')

    def test_a_child_who_had_a_trial_is_sent_to_the_office(self, notify):
        row = LessonEnrollment.objects.create(
            child=self.child, lesson=self.lesson, status='inactive',
            trial_lesson_date=_wednesday(-2), trial_outcome='attended',
        )
        res = self._book(_wednesday(0))
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()['error'], REPEAT_TRIAL_WIDGET_ERROR)
        row.refresh_from_db()
        self.assertEqual((row.status, row.trial_number), ('inactive', 1))
        self.assertEqual(Child.objects.filter(family=self.family).count(), 1)
        notify.assert_not_called()

    def test_a_trial_whose_date_passed_without_a_mark_also_counts(self, notify):
        # The cron has not retired the row yet: the date went by, the outcome is empty.
        Child.objects.filter(pk=self.child.pk).update(status='trial_signed', trial_classes_attended=0)
        LessonEnrollment.objects.create(
            child=self.child, lesson=self.lesson, status='active', trial_lesson_date=_wednesday(-2),
        )
        res = self._book(_wednesday(0))
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()['error'], REPEAT_TRIAL_WIDGET_ERROR)

    def test_an_upcoming_trial_can_still_be_moved(self, notify):
        Child.objects.filter(pk=self.child.pk).update(status='trial_signed', trial_classes_attended=0)
        row = LessonEnrollment.objects.create(
            child=self.child, lesson=self.lesson, status='active', trial_lesson_date=_wednesday(0),
        )
        res = self._book(_wednesday(1))
        self.assertEqual(res.status_code, 201, res.content)
        row.refresh_from_db()
        self.assertEqual((row.trial_lesson_date, row.trial_number), (_wednesday(1), 1))

    def test_a_first_trial_still_books(self, notify):
        Child.objects.filter(pk=self.child.pk).update(status='pending', trial_classes_attended=0)
        res = self._book(_wednesday(0))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(LessonEnrollment.objects.get(child=self.child).trial_number, 1)
