"""
The instructor page lists every group the instructor teaches (25.9.2026).

It used to list the stored monthly rows instead. The morning recount that
writes them was cut off by the hosting after 300 seconds, every day at the same
point, so most groups never got a row: the pages showed 111 of the 236 groups
in the schedule. The list now comes from the groups themselves, and a stored
row only supplies the figures when it is recent and belongs to this instructor.

The recount itself now runs in slices that carry on from where the last one
stopped (refresh_month_snapshots).
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import (
    BranchMonthlySnapshot,
    InstructorMonthlySnapshot,
    LessonMonthlySnapshot,
    UserProfile,
)
from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import LessonEnrollment
from apps.instructors.utils import refresh_month_snapshots

MONTH = timezone.now().strftime('%Y-%m')


def _last_month() -> str:
    first = timezone.now().date().replace(day=1)
    return (first - timedelta(days=1)).strftime('%Y-%m')


class _Groups(TestCase):
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.family = TestDataFactory.create_family()

    def _lesson(self, instructor=None, course=None, **kwargs):
        kwargs.setdefault('is_recurring', True)
        return TestDataFactory.create_lesson(
            course=course or self.course, instructor=instructor or self.instructor, **kwargs,
        )

    def _enrol(self, lesson, status):
        child = TestDataFactory.create_child(family=self.family, first_name=status, status=status)
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')
        return child

    def _stored_row(self, lesson, instructor=None, month=MONTH, **kwargs):
        return LessonMonthlySnapshot.objects.create(
            lesson=lesson, instructor=instructor or self.instructor, course=lesson.course,
            branch=lesson.course.branch, month=month, **kwargs,
        )


class InstructorPageListsEveryGroupTests(_Groups):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user(username='mgr@x.com', email='mgr@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

    def _page(self, instructor=None, **params):
        res = self.client.get(f'/api/v1/instructors/{(instructor or self.instructor).id}/', params)
        self.assertEqual(res.status_code, 200, res.content)
        return {row['lesson_id']: row for row in res.data['lessons']}

    def test_a_group_the_morning_recount_never_reached_is_on_the_page(self):
        reached = self._lesson(day_of_week=0)
        never_reached = self._lesson(day_of_week=1)
        self._stored_row(reached)
        self.assertEqual(set(self._page()), {str(reached.id), str(never_reached.id)})

    def test_a_group_handed_over_shows_under_its_new_instructor_only(self):
        before = TestDataFactory.create_instructor(first_name='קודם', branch=self.branch)
        lesson = self._lesson()
        self._stored_row(lesson, instructor=before, enrolled_students=9)
        self.assertIn(str(lesson.id), self._page())
        self.assertNotIn(str(lesson.id), self._page(before))

    def test_groups_the_schedule_does_not_show_are_left_out(self):
        running = self._lesson()
        self._lesson(status='cancelled')
        self._lesson(course=TestDataFactory.create_course(branch=self.branch, is_active=False))
        self._lesson(is_recurring=False, lesson_date=date.today())
        self.assertEqual(set(self._page()), {str(running.id)})

    def test_a_recent_stored_row_supplies_the_figures(self):
        lesson = self._lesson()
        self._stored_row(lesson, enrolled_students=7)
        self.assertEqual(self._page()[str(lesson.id)]['student_count'], 7)

    def test_a_stale_stored_row_is_counted_again(self):
        lesson = self._lesson()
        self._enrol(lesson, 'active')
        self._enrol(lesson, 'payment_problem')
        row = self._stored_row(lesson, enrolled_students=7)
        LessonMonthlySnapshot.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(days=3))
        self.assertEqual(self._page()[str(lesson.id)]['student_count'], 2)

    def test_a_group_counted_live_counts_active_students_only(self):
        lesson = self._lesson()
        for status in ('active', 'payment_problem', 'pending', 'inactive', 'trial_signed'):
            self._enrol(lesson, status)
        self.assertEqual(self._page()[str(lesson.id)]['student_count'], 2)

    def test_asking_for_a_refresh_counts_everything_live(self):
        lesson = self._lesson()
        self._enrol(lesson, 'active')
        self._stored_row(lesson, enrolled_students=7)
        self.assertEqual(self._page(refresh='1')[str(lesson.id)]['student_count'], 1)

    def test_a_closed_month_still_reads_its_stored_rows(self):
        """A month that is over is the record: what was stored is what it was."""
        on_record = self._lesson(day_of_week=0)
        self._lesson(day_of_week=1)
        self._stored_row(on_record, month=_last_month(), enrolled_students=4)
        page = self._page(month=_last_month())
        self.assertEqual(set(page), {str(on_record.id)})
        self.assertEqual(page[str(on_record.id)]['student_count'], 4)

    def test_the_courses_card_lists_each_course_once_with_its_type(self):
        self._lesson(day_of_week=0)
        self._lesson(day_of_week=1)
        res = self.client.get(f'/api/v1/instructors/{self.instructor.id}/')
        self.assertEqual(len(res.data['courses']), 1)
        self.assertEqual(res.data['courses'][0]['course_type'], self.course.course_type.name)


class MonthRecountInSlicesTests(_Groups):
    def test_a_full_run_stores_every_group_instructor_and_branch(self):
        first, second = self._lesson(day_of_week=0), self._lesson(day_of_week=1)
        result = refresh_month_snapshots(MONTH)
        self.assertTrue(result['finished'])
        self.assertEqual(
            set(LessonMonthlySnapshot.objects.filter(month=MONTH).values_list('lesson_id', flat=True)),
            {first.id, second.id},
        )
        self.assertTrue(InstructorMonthlySnapshot.objects.filter(instructor=self.instructor, month=MONTH).exists())
        self.assertTrue(BranchMonthlySnapshot.objects.filter(branch=self.branch, month=MONTH).exists())

    def test_when_the_time_is_up_it_stops_and_says_it_has_not_finished(self):
        self._lesson()
        result = refresh_month_snapshots(MONTH, budget_seconds=-1)
        self.assertFalse(result['finished'])
        self.assertEqual(result['lessons_done'], 0)
        self.assertEqual(result['lessons_total'], 1)
        # Branches are sums of the groups, so none is stored from half a month.
        self.assertFalse(BranchMonthlySnapshot.objects.exists())

    def test_the_next_call_carries_on_instead_of_starting_over(self):
        done = self._lesson(day_of_week=0)
        left = self._lesson(day_of_week=1)
        self._stored_row(done, enrolled_students=77)
        result = refresh_month_snapshots(MONTH)
        self.assertTrue(result['finished'])
        self.assertEqual(LessonMonthlySnapshot.objects.get(lesson=done).enrolled_students, 77)
        self.assertTrue(LessonMonthlySnapshot.objects.filter(lesson=left, month=MONTH).exists())

    def test_a_row_from_before_today_is_counted_again(self):
        lesson = self._lesson()
        self._enrol(lesson, 'active')
        row = self._stored_row(lesson, enrolled_students=77)
        LessonMonthlySnapshot.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(days=1, hours=1))
        refresh_month_snapshots(MONTH)
        self.assertEqual(LessonMonthlySnapshot.objects.get(pk=row.pk).enrolled_students, 1)

    def test_a_group_handed_over_is_stored_under_its_new_instructor(self):
        before = TestDataFactory.create_instructor(first_name='קודם', branch=self.branch)
        lesson = self._lesson()
        row = self._stored_row(lesson, instructor=before)
        LessonMonthlySnapshot.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(days=2))
        refresh_month_snapshots(MONTH)
        self.assertEqual(LessonMonthlySnapshot.objects.get(pk=row.pk).instructor_id, self.instructor.id)

    def test_the_running_month_is_never_locked(self):
        self._lesson()
        refresh_month_snapshots(MONTH)
        self.assertFalse(LessonMonthlySnapshot.objects.filter(is_finalized=True).exists())
        self.assertFalse(InstructorMonthlySnapshot.objects.filter(is_finalized=True).exists())
        self.assertFalse(BranchMonthlySnapshot.objects.filter(is_finalized=True).exists())
