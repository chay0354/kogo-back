"""
A trial never occupies a seat — on any screen that counts one.

The case that used to slip through: the office books a trial for a child who is
already an active paying student somewhere. The child's own status stays useful
elsewhere, but the trial ROW must not be counted, and four different counters
used to check only the child's status.
"""
from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent
from apps.enrollments.enrollment_counts import (
    count_capacity_enrollments,
    count_paying_enrollments,
    paying_enrollment_q,
)
from apps.enrollments.models import LessonEnrollment

User = get_user_model()


class TrialTakesNoSeatTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='סניף')
        self.room = Room.objects.create(branch=self.branch, name='אולם', capacity=20)
        self.course_type = CourseType.objects.create(name='מחול')
        self.course = Course.objects.create(
            course_type=self.course_type, name='מחול א', price=260, capacity=10, branch=self.branch,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, room=self.room, day_of_week=0,
            start_time=time(16, 0), end_time=time(17, 0), is_recurring=True,
        )
        self.family = Family.objects.create(name='כהן', phone='0501234567', branch=self.branch)
        Parent.objects.create(
            family=self.family, first_name='אבי', last_name='כהן', phone='0501234567', is_primary=True,
        )
        user = User.objects.create_user(username='mgr@t.com', email='mgr@t.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _child(self, name, status):
        return Child.objects.create(
            family=self.family, first_name=name, last_name='כהן',
            birth_date=date(2015, 1, 1), gender='female', status=status,
        )

    def _payer(self, name='נועה'):
        child = self._child(name, 'active')
        LessonEnrollment.objects.create(child=child, lesson=self.lesson, status='active')
        return child

    def _trial_of_an_active_child(self, name='דנה'):
        """The case the counters missed: an active child booked for a trial."""
        child = self._child(name, 'active')
        return LessonEnrollment.objects.create(
            child=child, lesson=self.lesson, status='active',
            trial_lesson_date=date.today() + timedelta(days=3),
        )

    def test_the_shared_rule_excludes_a_trial_of_an_active_child(self):
        self._payer()
        self._trial_of_an_active_child()
        counted = LessonEnrollment.objects.filter(paying_enrollment_q()).count()
        self.assertEqual(counted, 1)

    def test_the_capacity_check_counts_only_the_payer(self):
        self._payer()
        self._trial_of_an_active_child()
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 1)

    def test_the_schedule_card_counts_only_the_payer(self):
        self._payer()
        self._trial_of_an_active_child()
        res = self.client.get('/api/v1/scheduling/lessons/')
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body['results'] if isinstance(body, dict) else body
        row = next(r for r in rows if r['id'] == str(self.lesson.id))
        self.assertEqual(row.get('enrollment_count', row.get('paying_enrollment_count')), 1)

    def test_the_course_type_students_count_counts_only_the_payer(self):
        self._payer()
        self._trial_of_an_active_child()
        res = self.client.get('/api/v1/courses/types/')
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        rows = body['results'] if isinstance(body, dict) else body
        row = next(r for r in rows if r['id'] == str(self.course_type.id))
        self.assertEqual(row['students_count'], 1)

    def test_the_instructor_headcount_counts_only_the_payer(self):
        from apps.instructors.utils import _count_enrollments_for_period, _unique_students_for_period

        self._payer()
        self._trial_of_an_active_child()
        start, end = date.today() - timedelta(days=30), date.today() + timedelta(days=30)
        self.assertEqual(_count_enrollments_for_period(self.lesson, start, end, ('active',)), 1)
        self.assertEqual(len(_unique_students_for_period(self.lesson, start, end, ('active',))), 1)

    def test_a_trial_child_in_the_trial_flow_is_still_excluded(self):
        self._payer()
        child = self._child('רותם', 'trial_signed')
        LessonEnrollment.objects.create(
            child=child, lesson=self.lesson, status='active',
            trial_lesson_date=date.today() + timedelta(days=2),
        )
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)
        self.assertEqual(LessonEnrollment.objects.filter(paying_enrollment_q()).count(), 1)

    def test_a_converted_child_takes_their_seat_back(self):
        # Conversion clears trial_lesson_date; the seat must then count.
        row = self._trial_of_an_active_child()
        row.trial_lesson_date = None
        row.save(update_fields=['trial_lesson_date'])
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)

    def test_a_full_class_of_payers_still_reads_as_full(self):
        for i in range(3):
            self._payer(f'ילד{i}')
        self._trial_of_an_active_child()
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 3)
