"""
The courses page counts students by the owner's rule, and seats stay seats.

Owner's rule (24.9.2026): a student is a child whose own status is פעיל or
בעיית תשלום. The course card header ("פעילים לפי שיעור") and its fallbacks
counted every paying row, so a sign-up nobody had paid for yet and a child who
left while their row stayed active were counted as students.

A seat is a different question — a sign-up waiting for its payment still holds
its place — so `enrolled_count` (seats, and the salary estimate's tier) keeps
counting paying rows.
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment

User = get_user_model()


class CoursePageStudentCountTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.ctype = CourseType.objects.create(name='קפוארה')
        self.course = Course.objects.create(
            name='קפוארה א-ב', branch=self.branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20,
        )
        self.monday = self._lesson(1, time(16, 0))
        self.thursday = self._lesson(4, time(17, 0))
        manager = User.objects.create_user(
            username='mgr@coursecounts.test', email='mgr@coursecounts.test', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=manager)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        token, _ = Token.objects.get_or_create(user=manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _lesson(self, day, start, status='scheduled', course=None):
        return Lesson.objects.create(
            course=course or self.course, day_of_week=day, start_time=start,
            end_time=time(start.hour, 45), is_recurring=True, status=status,
        )

    def enrol(self, name, child_status, lesson, *, trial_on=None):
        family = Family.objects.create(name=name, phone='0501234567', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name=name, last_name='כהן', birth_date=date(2017, 1, 1),
            gender='male', status=child_status,
        )
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active', trial_lesson_date=trial_on)
        return child

    def a_mixed_monday(self):
        """Two students, and three paying rows that are not students."""
        self.enrol('משלם', 'active', self.monday)
        self.enrol('אשראי נכשל', 'payment_problem', self.monday)
        self.enrol('ממתין', 'pending', self.monday)
        self.enrol('עזב', 'inactive', self.monday)
        self.enrol('ניסיון', 'trial_signed', self.monday, trial_on=date(2026, 10, 5))

    def details_row(self, course=None):
        res = self.client.get(f'/api/v1/courses/types/{self.ctype.id}/details/')
        self.assertEqual(res.status_code, 200, res.data)
        return next(c for c in res.data['courses'] if str(c['id']) == str((course or self.course).id))

    def lessons_detail(self, course=None):
        res = self.client.get(f'/api/v1/courses/courses/{(course or self.course).id}/lessons_detail/')
        self.assertEqual(res.status_code, 200, res.data)
        return {row['id']: row for row in res.data}

    def test_the_header_counts_students_per_lesson(self):
        self.a_mixed_monday()
        self.enrol('משלם בחמישי', 'active', self.thursday)
        self.enrol('ממתין בחמישי', 'pending', self.thursday)
        row = self.details_row()
        self.assertEqual(
            [(h['lesson_id'], h['count']) for h in row['lesson_headcounts']],
            [(str(self.monday.id), 2), (str(self.thursday.id), 1)],
        )

    def test_the_header_fallback_counts_students_too(self):
        """A course with no scheduled lesson shows its distinct-students total instead."""
        self.a_mixed_monday()
        self.assertEqual(self.details_row()['course_enrollment_count'], 2)

    def test_a_child_in_two_lessons_is_one_student_of_the_course(self):
        child = self.enrol('פעמיים', 'active', self.monday)
        LessonEnrollment.objects.create(lesson=self.thursday, child=child, status='active')
        row = self.details_row()
        self.assertEqual(row['course_enrollment_count'], 1)
        self.assertEqual([h['count'] for h in row['lesson_headcounts']], [1, 1])

    def test_a_lesson_row_the_header_does_not_list_counts_students(self):
        """A cancelled lesson is not in the header; its row reads active_students_count."""
        cancelled = self._lesson(2, time(18, 0), status='cancelled')
        self.enrol('משלם', 'active', cancelled)
        self.enrol('ממתין', 'pending', cancelled)
        self.assertNotIn(str(cancelled.id), [h['lesson_id'] for h in self.details_row()['lesson_headcounts']])
        self.assertEqual(self.lessons_detail()[str(cancelled.id)]['active_students_count'], 1)

    def test_seats_still_count_every_paying_row(self):
        """Capacity and the salary tier: a sign-up waiting to pay still holds its place."""
        self.a_mixed_monday()
        monday = self.lessons_detail()[str(self.monday.id)]
        self.assertEqual(monday['enrolled_count'], 4)
        self.assertEqual(monday['total_students_count'], 4)
        self.assertEqual(monday['active_students_count'], 2)

    def test_the_lesson_rows_read_the_child_without_a_query_each(self):
        """Both counts read the child's status; the query count must not grow with the class."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries():
            with CaptureQueriesContext(connection) as ctx:
                self.lessons_detail()
            return len(ctx.captured_queries)

        self.enrol('ראשון', 'active', self.monday)
        self.lessons_detail()  # warm-up
        small = queries()
        for i in range(6):
            self.enrol(f'ילד {i}', 'active' if i % 2 else 'pending', self.monday)
        self.assertEqual(queries(), small)
