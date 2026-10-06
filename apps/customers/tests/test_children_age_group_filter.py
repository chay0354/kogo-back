"""The customers list narrows by the course's age group, alone and inside a תחום."""
from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment

LIST_URL = '/api/v1/customers/children/'
IDS_URL = '/api/v1/customers/children/ids/'


class ChildrenAgeGroupFilterTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.room = Room.objects.create(branch=self.branch, name='A', capacity=20)
        self.family = Family.objects.create(name='Levi', phone='0521111111', branch=self.branch)
        self.capoeira = CourseType.objects.create(name='Capoeira')
        self.dance = CourseType.objects.create(name='Dance')

        # The age-group scale: 1 = ages 3–4.5, 3–4 = grades א–ב.
        self.capoeira_small = self._lesson(self.capoeira, 'Capoeira 3-4.5', 1, 1)
        self.capoeira_grades = self._lesson(self.capoeira, 'Capoeira A-B', 3, 4)
        self.dance_small = self._lesson(self.dance, 'Dance 3-4.5', 1, 1)
        self.dance_grades = self._lesson(self.dance, 'Dance A-B', 3, 4)
        self.ageless = self._lesson(self.dance, 'Open dance', None, None)

        User = get_user_model()
        manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')

    def _lesson(self, course_type, name, min_age, max_age):
        course = Course.objects.create(
            course_type=course_type, name=name, price=300, capacity=10, branch=self.branch,
            min_age=min_age, max_age=max_age,
        )
        return Lesson.objects.create(
            course=course, room=self.room, day_of_week=0,
            start_time=time(17, 0), end_time=time(18, 0), is_recurring=True,
        )

    def _child(self, first_name, *lessons, status='active'):
        child = Child.objects.create(
            family=self.family, first_name=first_name, last_name='Levi',
            birth_date=date(2016, 1, 1), gender='male', status='active',
        )
        for lesson in lessons:
            LessonEnrollment.objects.create(child=child, lesson=lesson, status=status, start_date=date(2026, 9, 1))
        return child

    def _names(self, **params):
        res = self.client.get(LIST_URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        return sorted(row['first_name'] for row in res.data['results'])

    def test_an_age_group_lists_the_children_of_every_course_of_that_group(self):
        self._child('Avi', self.capoeira_small)
        self._child('Beni', self.dance_small)
        self._child('Gal', self.capoeira_grades)
        self.assertEqual(self._names(age_group='1-1'), ['Avi', 'Beni'])
        self.assertEqual(self._names(age_group='3-4'), ['Gal'])

    def test_inside_a_course_type_only_that_types_group_is_listed(self):
        self._child('Avi', self.capoeira_small)
        self._child('Beni', self.dance_small)
        self.assertEqual(self._names(course_type=str(self.capoeira.id), age_group='1-1'), ['Avi'])

    def test_the_type_and_the_group_are_asked_of_the_same_course(self):
        # Capoeira for the small ones and dance for the grades: not "dance, small ones".
        self._child('Avi', self.capoeira_small, self.dance_grades)
        self.assertEqual(self._names(course_type=str(self.dance.id), age_group='1-1'), [])
        self.assertEqual(self._names(course_type=str(self.dance.id), age_group='3-4'), ['Avi'])

    def test_a_course_type_alone_still_lists_as_before(self):
        self._child('Avi', self.capoeira_small)
        self._child('Beni', self.dance_small)
        self.assertEqual(self._names(course_type=str(self.dance.id)), ['Beni'])

    def test_an_enrollment_that_is_not_active_does_not_count(self):
        self._child('Avi', self.capoeira_small, status='inactive')
        self.assertEqual(self._names(age_group='1-1'), [])

    def test_all_and_empty_do_not_narrow(self):
        self._child('Avi', self.capoeira_small)
        self._child('Gal', self.capoeira_grades)
        self.assertEqual(self._names(age_group='all'), ['Avi', 'Gal'])
        self.assertEqual(self._names(age_group=''), ['Avi', 'Gal'])

    def test_a_value_that_is_not_a_group_answers_empty(self):
        # Never the courses that have no ages — nobody chose those.
        self._child('Avi', self.ageless)
        self.assertEqual(self._names(age_group='abc'), [])

    def test_select_all_follows_the_same_filter(self):
        avi = self._child('Avi', self.capoeira_small)
        self._child('Gal', self.capoeira_grades)
        res = self.client.get(IDS_URL, {'course_type': str(self.capoeira.id), 'age_group': '1-1'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['ids'], [str(avi.id)])
