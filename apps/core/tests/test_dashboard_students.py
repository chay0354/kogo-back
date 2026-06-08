"""
Tests for dashboard students endpoint (churn + abnormal attendance charts).
"""
from datetime import date, timedelta

from django.utils import timezone
from rest_framework import status

from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.customers.status_history_models import ChildStatusHistory
from apps.enrollments.models import LessonEnrollment


class DashboardStudentsDataTests(BaseAPITestCase):
    """Students dashboard quit and abnormal attendance data."""

    def setUp(self):
        super().setUp()
        self.family = TestDataFactory.create_family(branch=self.branch)
        self.course_type = TestDataFactory.create_course_type(name='אקרובטיקה')
        self.course = TestDataFactory.create_course(
            branch=self.branch,
            course_type=self.course_type,
        )
        self.lesson = TestDataFactory.create_lesson(course=self.course)

    def _get_students_dashboard(self, **params):
        return self.client.get('/api/v1/core/dashboard/students/', params)

    def test_quit_all_time_without_explicit_quit_dates(self):
        """Churn uses all-time window when quit_date_* params are omitted."""
        child = TestDataFactory.create_child(
            family=self.family,
            first_name='נועם',
            last_name='נשר',
            status='inactive',
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=child,
            status='inactive',
        )
        changed_at = timezone.now() - timedelta(days=400)
        ChildStatusHistory.objects.create(
            child=child,
            previous_status='active',
            new_status='inactive',
            changed_at=changed_at,
        )

        response = self._get_students_dashboard(
            date_from='2026-06-01',
            date_to='2026-06-08',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        quit = response.data['quit_percentage']
        self.assertEqual(quit['total_quit'], 1)
        self.assertEqual(len(quit['by_status']), 1)
        self.assertEqual(quit['by_status'][0]['status_key'], 'inactive')
        self.assertEqual(len(quit['by_course_type']), 1)
        self.assertEqual(quit['by_course_type'][0]['course_type_name'], 'אקרובטיקה')
        self.assertEqual(len(quit['by_course']), 1)
        self.assertEqual(quit['by_course'][0]['course_name'], self.course.name)

    def test_quit_breakdown_by_course_with_filter(self):
        """Course-level quit breakdown respects quit_chart_filter_id."""
        child = TestDataFactory.create_child(
            family=self.family,
            status='inactive',
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=child,
            status='inactive',
        )
        ChildStatusHistory.objects.create(
            child=child,
            previous_status='active',
            new_status='inactive',
            changed_at=timezone.now(),
        )

        response = self._get_students_dashboard(
            quit_chart_breakdown='course',
            quit_chart_filter_id=str(self.course.id),
            quit_date_from='2020-01-01',
            quit_date_to=date.today().isoformat(),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_course = response.data['quit_percentage']['by_course']
        self.assertEqual(len(by_course), 1)
        self.assertEqual(by_course[0]['course_id'], str(self.course.id))
        self.assertEqual(by_course[0]['count'], 1)

    def test_quit_includes_child_with_inactive_enrollment_when_branch_filtered(self):
        """Branch filter must not exclude quitters who no longer have active enrollments."""
        child = TestDataFactory.create_child(
            family=self.family,
            first_name='יעל',
            last_name='עזבה',
            status='ghost',
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=child,
            status='inactive',
        )
        ChildStatusHistory.objects.create(
            child=child,
            previous_status='active',
            new_status='ghost',
            changed_at=timezone.now(),
        )

        response = self._get_students_dashboard(
            branch_id=str(self.branch.id),
            quit_date_from='2020-01-01',
            quit_date_to=date.today().isoformat(),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['quit_percentage']['total_quit'], 1)

    def test_abnormal_attendance_by_enrollment_branch(self):
        """Irregular attendance is counted by lesson branch, not family branch."""
        other_branch = TestDataFactory.create_branch(name='סניף צפון', city=self.city)
        other_family = TestDataFactory.create_family(name='משפחה אחרת', branch=other_branch)
        child = TestDataFactory.create_child(
            family=other_family,
            first_name='רoni',
            last_name='חריג',
            status='active',
            absent_irregularly=True,
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=child,
            status='active',
        )

        response = self._get_students_dashboard()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        abnormal = response.data['abnormal_attendance_by_branch']
        branch_counts = {item['branch_id']: item['count'] for item in abnormal}
        self.assertEqual(branch_counts.get(str(self.branch.id)), 1)
        self.assertEqual(branch_counts.get(str(other_branch.id), 0), 0)
