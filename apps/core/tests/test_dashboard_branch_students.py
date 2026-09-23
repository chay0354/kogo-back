"""
The branch tab's student counts.

A student is a child with a paying enrollment, and belongs to the branch of the
lesson they attend. The tab used to count every child not ghost/inactive by the
family's branch — trials and pending sign-ups included — and on 23.9.2026 it
read 1181 against 614 paying.
"""
from datetime import date

from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.core.models import BranchMonthlySnapshot, UserProfile
from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.enrollments.models import LessonEnrollment


class BranchTabStudentCounts(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.other_branch = TestDataFactory.create_branch(name='סניף צפון', city=self.city)
        self.lesson = TestDataFactory.create_lesson(course=TestDataFactory.create_course(branch=self.branch))
        self.second_lesson = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='מתקדמים', branch=self.branch), day_of_week=2,
        )
        self.other_lesson = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='צפון', branch=self.other_branch),
        )
        month = timezone.now().date().strftime('%Y-%m')
        for branch in (self.branch, self.other_branch):
            BranchMonthlySnapshot.objects.create(branch=branch, month=month)
        self.family = TestDataFactory.create_family(branch=self.branch)

    def child(self, status='active', family=None):
        return TestDataFactory.create_child(family=family or self.family, status=status)

    def enroll(self, child, lesson=None, **kwargs):
        return LessonEnrollment.objects.create(
            lesson=lesson or self.lesson, child=child, status=kwargs.pop('status', 'active'),
            start_date=date(2026, 9, 1), **kwargs,
        )

    def tab(self, **params):
        res = self.client.get('/api/v1/core/dashboard/branches/', params)
        self.assertEqual(res.status_code, 200, res.data)
        by_branch = {row['branch_id']: row['students'] for row in res.data['branch_list']}
        return res.data['kpis']['total_students'], by_branch

    def test_counts_paying_children_only(self):
        self.enroll(self.child())
        # A trial: booked on the row and on the child.
        self.enroll(self.child('trial_signed'), trial_lesson_date=date(2026, 9, 30))
        # An active child whose only row here is a trial booked by the office.
        self.enroll(self.child(), trial_lesson_date=date(2026, 9, 30))
        # A trial the parent did not go on with, and a sign-up that never paid.
        self.child('trial_completed')
        self.child('pending')
        # Left: the row was closed.
        self.enroll(self.child('inactive'), status='inactive')

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.branch.id)], 1)

    def test_a_child_belongs_to_the_branch_of_the_lesson_not_of_the_family(self):
        self.enroll(self.child(), lesson=self.other_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.other_branch.id)], 1)
        self.assertEqual(by_branch[str(self.branch.id)], 0)

    def test_two_lessons_in_one_branch_are_one_student(self):
        child = self.child()
        self.enroll(child)
        self.enroll(child, lesson=self.second_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.branch.id)], 1)

    def test_a_child_in_two_branches_is_in_each_but_once_in_the_total(self):
        child = self.child()
        self.enroll(child)
        self.enroll(child, lesson=self.other_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.branch.id)], 1)
        self.assertEqual(by_branch[str(self.other_branch.id)], 1)

    def test_the_branch_filter_counts_that_branch(self):
        self.enroll(self.child())
        self.enroll(self.child(), lesson=self.other_lesson)

        total, _ = self.tab(branch_id=str(self.other_branch.id))

        self.assertEqual(total, 1)

    def test_a_partner_sees_only_the_children_of_their_branches(self):
        self.enroll(self.child())
        self.enroll(self.child(), lesson=self.other_lesson)
        self.enroll(self.child(), lesson=self.other_lesson)
        partner = TestDataFactory.create_user(username='partner@example.com', role=UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.set([self.other_branch])
        token, _ = Token.objects.get_or_create(user=partner)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        total, by_branch = self.tab()

        self.assertEqual(total, 2)
        self.assertEqual(set(by_branch), {str(self.other_branch.id)})
