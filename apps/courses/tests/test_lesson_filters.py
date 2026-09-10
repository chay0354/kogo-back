"""
The CRM enrolment dialog narrows by branch and by course type, and asks for the
lessons of the whole narrowed set in one request so it can show days and times.
"""
from datetime import time

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson

User = get_user_model()
URL = '/api/v1/courses/lessons/'


class LessonFilterTests(TestCase):
    def setUp(self):
        self.north = Branch.objects.create(name='צפון')
        self.south = Branch.objects.create(name='דרום')
        self.dance = CourseType.objects.create(name='מחול')
        self.judo = CourseType.objects.create(name="ג'ודו")
        self.north_dance = self._lesson(self.north, self.dance, 0)
        self.north_judo = self._lesson(self.north, self.judo, 1)
        self.south_dance = self._lesson(self.south, self.dance, 2)
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _lesson(self, branch, course_type, day):
        room = Room.objects.create(branch=branch, name=f'אולם {day}', capacity=20)
        course = Course.objects.create(
            course_type=course_type, name=f'{course_type.name} {branch.name}',
            price=200, capacity=20, branch=branch,
        )
        return Lesson.objects.create(
            course=course, room=room, day_of_week=day, start_time=time(16, 0), end_time=time(17, 0),
        )

    def _ids(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body['results'] if isinstance(body, dict) else body
        return {row['id'] for row in rows}

    def test_a_branch_narrows_the_lessons(self):
        self.assertEqual(self._ids(branch_id=str(self.north.id)), {str(self.north_dance.id), str(self.north_judo.id)})

    def test_a_course_type_narrows_the_lessons(self):
        self.assertEqual(self._ids(course_type=str(self.dance.id)), {str(self.north_dance.id), str(self.south_dance.id)})

    def test_branch_and_type_together_narrow_to_one(self):
        self.assertEqual(
            self._ids(branch_id=str(self.north.id), course_type=str(self.dance.id)),
            {str(self.north_dance.id)},
        )

    def test_no_filter_still_returns_everything(self):
        self.assertEqual(len(self._ids()), 3)

    def test_an_unknown_branch_returns_nothing_rather_than_everything(self):
        self.assertEqual(self._ids(branch_id='00000000-0000-0000-0000-000000000000'), set())
