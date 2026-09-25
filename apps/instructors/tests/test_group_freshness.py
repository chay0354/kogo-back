"""
A plain refresh of the instructor page shows the numbers as they are now.

Owner, 25.9.2026: "למה רענון נתונים הוא לא פשוט לרענן את הדף". The page reads
each group's month from a stored row; whatever changes a group's numbers now
marks that row out of date, and the next look at the page counts the group
again and stores the answer — no waiting for the morning, no button.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import LessonMonthlySnapshot, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment
from apps.enrollments.models import LessonEnrollment
from apps.instructors.group_freshness import STALE_MARK, mark_groups_stale
from apps.instructors.models import InstructorSalaryTier
from apps.scheduling.models import LessonCancellation

MONTH = timezone.now().strftime('%Y-%m')


def _last_month() -> str:
    return (timezone.now().date().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')


class _Group(TestCase):
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor, is_recurring=True)
        self.family = TestDataFactory.create_family()

    def _row(self, lesson=None, month=MONTH, **kwargs):
        lesson = lesson or self.lesson
        return LessonMonthlySnapshot.objects.create(
            lesson=lesson, instructor=lesson.instructor, course=lesson.course,
            branch=lesson.course.branch, month=month, **kwargs,
        )

    def _is_stale(self, row) -> bool:
        return LessonMonthlySnapshot.objects.get(pk=row.pk).updated_at == STALE_MARK

    def committed(self):
        """The marks are made once a change is committed; a TestCase never commits, so run them here."""
        return self.captureOnCommitCallbacks(execute=True)

    def _child(self, status='active'):
        return TestDataFactory.create_child(family=self.family, first_name=status, status=status)


class WhatMarksAGroupTests(_Group):
    def test_a_registration(self):
        row = self._row()
        with self.committed():
            LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self.assertTrue(self._is_stale(row))

    def test_a_registration_removed(self):
        enrolment = LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        row = self._row()
        with self.committed():
            enrolment.delete()
        self.assertTrue(self._is_stale(row))

    def test_a_payment_on_the_group(self):
        child = self._child()
        row = self._row()
        with self.committed():
            Payment.objects.create(
                child=child, family=self.family, lesson=self.lesson, base_amount=Decimal('250'),
                final_amount=Decimal('250'), status='completed',
            )
        self.assertTrue(self._is_stale(row))

    def test_a_childs_new_status_marks_every_group_they_are_in(self):
        other = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor, is_recurring=True)
        child = self._child('active')
        for lesson in (self.lesson, other):
            LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')
        rows = [self._row(), self._row(other)]
        child.status = 'payment_problem'
        with self.committed():
            child.save(update_fields=['status', 'updated_at'])
        self.assertTrue(all(self._is_stale(row) for row in rows))

    def test_a_save_that_leaves_the_status_alone_marks_nothing(self):
        child = self._child()
        LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active')
        row = self._row()
        child.first_name = 'שם חדש'
        with self.committed():
            child.save(update_fields=['first_name'])
        self.assertFalse(self._is_stale(row))

    def test_an_occurrence_cancelled(self):
        row = self._row()
        with self.committed():
            LessonCancellation.objects.create(lesson=self.lesson, occurrence_date=date.today())
        self.assertTrue(self._is_stale(row))

    def test_the_course_price_marks_every_group_of_the_course(self):
        other = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor, is_recurring=True)
        rows = [self._row(), self._row(other)]
        self.course.price = Decimal('300')
        with self.committed():
            self.course.save()
        self.assertTrue(all(self._is_stale(row) for row in rows))

    def test_a_new_group_in_a_course_paid_by_the_month_marks_its_siblings(self):
        """The course's monthly pay is split across its groups, so one more group changes each share."""
        row = self._row()
        with self.committed():
            TestDataFactory.create_lesson(course=self.course, instructor=self.instructor, is_recurring=True)
        self.assertTrue(self._is_stale(row))

    def test_the_instructors_pay(self):
        row = self._row()
        self.instructor.fixed_salary_per_lesson = Decimal('120')
        with self.committed():
            self.instructor.save()
        self.assertTrue(self._is_stale(row))

    def test_a_salary_tier(self):
        row = self._row()
        with self.committed():
            InstructorSalaryTier.objects.create(
                instructor=self.instructor, min_students=0, max_students=5, salary_per_lesson=Decimal('100'),
            )
        self.assertTrue(self._is_stale(row))

    def test_a_closed_month_is_never_marked(self):
        closed = self._row(month=_last_month())
        with self.committed():
            LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self.assertFalse(self._is_stale(closed))

    def test_everything_one_transaction_changes_is_marked_in_one_statement(self):
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext

        other = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor, is_recurring=True)
        rows = [self._row(), self._row(other)]
        # The commit's work runs as committed() closes, so the capture has to outlast it.
        with CaptureQueriesContext(connection) as queries, self.committed():
            with transaction.atomic():
                for lesson in (self.lesson, other):
                    LessonEnrollment.objects.create(lesson=lesson, child=self._child(), status='active')
        marks = [q for q in queries.captured_queries if 'UPDATE' in q['sql'] and 'lesson_monthly' in q['sql'].lower()]
        self.assertEqual(len(marks), 1)
        self.assertTrue(all(self._is_stale(row) for row in rows))

    def test_nothing_is_marked_when_the_change_is_rolled_back(self):
        from django.db import transaction

        row = self._row()
        with self.committed():
            try:
                with transaction.atomic():
                    LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
                    raise RuntimeError('the registration failed')
            except RuntimeError:
                pass
        self.assertFalse(self._is_stale(row))

    def test_a_failure_to_mark_never_stops_the_registration(self):
        with patch('apps.core.models.LessonMonthlySnapshot.objects.filter', side_effect=RuntimeError('db')):
            with self.committed():
                enrolment = LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self.assertTrue(LessonEnrollment.objects.filter(pk=enrolment.pk).exists())


class PlainRefreshShowsTodayTests(_Group):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user(username='mgr@x.com', email='mgr@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

    def _group_on_page(self):
        res = self.client.get(f'/api/v1/instructors/{self.instructor.id}/')
        self.assertEqual(res.status_code, 200, res.content)
        return next(row for row in res.data['lessons'] if row['lesson_id'] == str(self.lesson.id))

    def test_a_child_who_joins_at_noon_is_on_the_page_at_the_next_refresh(self):
        with self.committed():
            LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self.assertEqual(self._group_on_page()['student_count'], 1)

        with self.committed():
            LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self.assertEqual(self._group_on_page()['student_count'], 2)

    def test_the_count_made_by_the_page_is_kept_for_the_next_look(self):
        LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        self._group_on_page()
        row = LessonMonthlySnapshot.objects.get(lesson=self.lesson, month=MONTH)
        self.assertEqual(row.enrolled_students, 1)
        self.assertNotEqual(row.updated_at, STALE_MARK)
        self.assertEqual(self._group_on_page()['revenue_calculation_method'], 'snapshot')

    def test_a_child_whose_card_failed_still_counts_and_one_who_left_does_not(self):
        staying, leaving = self._child('active'), self._child('active')
        for child in (staying, leaving):
            LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active')
        self.assertEqual(self._group_on_page()['student_count'], 2)

        staying.status = 'payment_problem'
        leaving.status = 'inactive'
        with self.committed():
            staying.save(update_fields=['status'])
            leaving.save(update_fields=['status'])
        self.assertEqual(self._group_on_page()['student_count'], 1)

    def test_marking_by_hand_is_what_the_hooks_do(self):
        self._row(enrolled_students=9)
        self.assertEqual(self._group_on_page()['student_count'], 9)
        with self.committed():
            mark_groups_stale([self.lesson.id])
        self.assertEqual(self._group_on_page()['student_count'], 0)
