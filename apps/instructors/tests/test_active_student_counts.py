"""
Who counts as a student (owner, 24.9.2026): "פעיל זה פעיל — משלם", and a child
whose card failed is still פעיל.

So a count of students takes children whose own status is פעיל or בעיית תשלום,
and leaves out a sign-up nobody has paid for yet (ממתין) and a child who left
(לא פעיל) — even while their enrolment row still says active. Seats are a
different question: a sign-up waiting for its payment still holds its place.
"""
from datetime import date

from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.enrollment_counts import (
    active_student_enrollments,
    count_paying_enrollments,
    is_active_student,
)
from apps.enrollments.models import LessonEnrollment
from apps.instructors.utils import _count_enrollments_for_period, _unique_students_for_period

MONTH_START = date(2026, 9, 1)
MONTH_END = date(2026, 9, 30)
STATUSES = ('active', 'payments_problem')


class ActiveStudentCountTests(TestCase):
    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()
        self.family = TestDataFactory.create_family()

    def _enrol(self, name, status, **kwargs):
        child = TestDataFactory.create_child(family=self.family, first_name=name, status=status)
        LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active', **kwargs)
        return child

    def _counts(self):
        lesson = type(self.lesson).objects.get(pk=self.lesson.pk)
        plain = _count_enrollments_for_period(lesson, MONTH_START, MONTH_END, STATUSES)
        prefetched = type(self.lesson).objects.prefetch_related('enrollments__child').get(pk=self.lesson.pk)
        return plain, _count_enrollments_for_period(prefetched, MONTH_START, MONTH_END, STATUSES)

    def test_active_and_card_failed_both_count(self):
        self._enrol('משלם', 'active')
        self._enrol('אשראי נכשל', 'payment_problem')
        self.assertEqual(self._counts(), (2, 2))

    def test_a_sign_up_not_yet_paid_is_not_a_student(self):
        self._enrol('ממתין', 'pending')
        self.assertEqual(self._counts(), (0, 0))

    def test_a_child_who_left_is_not_a_student_even_with_a_live_row(self):
        self._enrol('עזב', 'inactive')
        self.assertEqual(self._counts(), (0, 0))

    def test_a_trial_is_not_a_student(self):
        self._enrol('ניסיון', 'trial_signed')
        self._enrol('ניסיון שהתקיים', 'trial_completed')
        self.assertEqual(self._counts(), (0, 0))

    def test_both_ways_of_counting_agree(self):
        """With and without prefetching, the same children — they used to be two rules."""
        for status in ('active', 'payment_problem', 'pending', 'inactive', 'trial_signed'):
            self._enrol(f'ילד {status}', status)
        plain, prefetched = self._counts()
        self.assertEqual(plain, prefetched)
        self.assertEqual(plain, 2)

    def test_unique_students_follow_the_same_rule(self):
        self._enrol('משלם', 'active')
        self._enrol('ממתין', 'pending')
        students = _unique_students_for_period(self.lesson, MONTH_START, MONTH_END, STATUSES)
        self.assertEqual(len(students), 1)

    def test_a_seat_is_still_held_by_a_sign_up_waiting_to_pay(self):
        """Capacity is not a count of students: the place is taken until they pay or leave."""
        self._enrol('ממתין', 'pending')
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 1)
        self.assertEqual(active_student_enrollments().filter(lesson=self.lesson).count(), 0)

    def test_the_rule_for_a_child_in_hand(self):
        family = self.family
        self.assertTrue(is_active_student(TestDataFactory.create_child(family=family, status='active')))
        self.assertTrue(is_active_student(TestDataFactory.create_child(family=family, status='payment_problem')))
        self.assertFalse(is_active_student(TestDataFactory.create_child(family=family, status='pending')))
        self.assertFalse(is_active_student(None))


class BranchPageCountTests(TestCase):
    def test_the_branch_page_counts_active_students_only(self):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        from apps.core.models import UserProfile

        lesson = TestDataFactory.create_lesson()
        family = TestDataFactory.create_family()
        for name, status in (('משלם', 'active'), ('נכשל', 'payment_problem'), ('ממתין', 'pending')):
            child = TestDataFactory.create_child(family=family, first_name=name, status=status)
            LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')

        user = get_user_model().objects.create_user(username='m-branch@x.com', email='m-branch@x.com', password='x12345678!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        branch = lesson.course.branch
        res = client.get(f'/api/v1/core/branches/{branch.id}/statistics/')
        self.assertEqual(res.status_code, 200, res.content)
        # פעיל and בעיית תשלום count; the unpaid sign-up does not.
        self.assertEqual(res.data['active_students'], 2)
