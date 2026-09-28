"""
"תלמידים בכל קבוצה" on the instructor app counts students, not paying rows.

Owner's rule (24.9.2026): a student is a child whose own status is פעיל or
בעיית תשלום. The dashboard counted every paying row, so a sign-up nobody had
paid for yet (בתהליך רישום) and a child who left while their row stayed active
were in the group's number — its docstring already said they were out. On
27.9.2026 pending children were behind six of the seven groups whose number
disagreed with the office's.

External-branch children stay counted: the screen asks how many children are
in the room. The attendance check keeps every paying row — a child who sits in
the class unpaid is exactly one whose register matters.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment
from apps.external_students.models import ExternalStudent
from apps.instructors.models import Instructor

User = get_user_model()

URL = '/api/v1/instructors/my-dashboard/'


class MyDashboardStudentCountTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.external_branch = Branch.objects.create(name='סניף עירייה', city=city, is_external=True)
        self.ctype = CourseType.objects.create(name='קפוארה')
        user = User.objects.create_user(
            username='teacher@groups.test', email='teacher@groups.test', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_WORKER
        profile.save(update_fields=['role'])
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='teacher@groups.test', primary_branch=self.branch,
        )
        self.lesson = self._lesson('קפוארה א-ב', self.branch)
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def _lesson(self, name, branch):
        course = Course.objects.create(
            name=name, branch=branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20, instructor=self.instructor, is_active=True,
        )
        return Lesson.objects.create(
            course=course, instructor=self.instructor,
            # day_of_week counts from Sunday and weekday() from Monday, so this
            # is yesterday: the lesson has already been taught this week.
            day_of_week=date.today().weekday(),
            start_time=time(16, 0), end_time=time(16, 45), is_recurring=True,
        )

    def enrol(self, name, child_status, lesson=None):
        family = Family.objects.create(name=name, phone='0501234567', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name=name, last_name='כהן', birth_date=date(2017, 1, 1),
            gender='female', status=child_status,
        )
        LessonEnrollment.objects.create(
            lesson=lesson or self.lesson, child=child, status='active',
            start_date=date.today() - timedelta(days=60),
        )
        return child

    def dashboard(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def group(self, data, lesson=None):
        return next(g for g in data['groups'] if g['lesson_id'] == str((lesson or self.lesson).id))

    def test_the_group_counts_active_and_card_failed_children_only(self):
        self.enrol('משלם', 'active')
        self.enrol('אשראי נכשל', 'payment_problem')
        self.enrol('ממתין', 'pending')
        self.enrol('עזב', 'inactive')
        data = self.dashboard()
        self.assertEqual(self.group(data)['active_students'], 2)
        self.assertEqual(data['total_active_students'], 2)

    def test_a_group_of_sign_ups_alone_reads_empty_and_low(self):
        for i in range(9):
            self.enrol(f'ממתין {i}', 'pending')
        row = self.group(self.dashboard())
        self.assertEqual(row['active_students'], 0)
        self.assertTrue(row['is_low'])

    def test_the_headline_counts_a_child_in_two_groups_once(self):
        second = self._lesson('קפוארה ג-ד', self.branch)
        child = self.enrol('בשתי קבוצות', 'active')
        LessonEnrollment.objects.create(
            lesson=second, child=child, status='active', start_date=date.today() - timedelta(days=60),
        )
        self.enrol('ממתין', 'pending', lesson=second)
        data = self.dashboard()
        self.assertEqual(self.group(data)['active_students'], 1)
        self.assertEqual(self.group(data, second)['active_students'], 1)
        self.assertEqual(data['total_active_students'], 1)

    def test_the_monthly_trend_follows_the_same_rule(self):
        self.enrol('משלם', 'active')
        self.enrol('ממתין', 'pending')
        data = self.dashboard()
        this_month = date.today().strftime('%Y-%m')
        point = next(p for p in data['monthly_trend'] if p['month'] == this_month)
        self.assertEqual(point['students'], 1)

    def test_external_students_still_count(self):
        external_lesson = self._lesson('קפוארה עירייה', self.external_branch)
        ExternalStudent.objects.create(lesson=external_lesson, first_name='נועם', last_name='כהן')
        ExternalStudent.objects.create(lesson=external_lesson, first_name='יעל', last_name='לוי')
        self.enrol('ממתין', 'pending', lesson=external_lesson)
        data = self.dashboard()
        row = self.group(data, external_lesson)
        self.assertEqual(row['active_students'], 2)
        self.assertFalse(row['roster_unknown'])
        self.assertEqual(data['total_active_students'], 2)

    def test_a_trial_is_still_not_a_student(self):
        self.enrol('ניסיון', 'trial_signed')
        self.assertEqual(self.group(self.dashboard())['active_students'], 0)

    def test_an_unpaid_child_on_the_register_still_needs_marking(self):
        """
        The attendance check reads the register, not the headcount: a sign-up
        sitting in the class unpaid is on it, and an unmarked day is still
        reported for them.
        """
        self.enrol('ממתין', 'pending')
        data = self.dashboard()
        self.assertEqual(self.group(data)['active_students'], 0)
        self.assertIn(str(self.lesson.id), {u['lesson_id'] for u in data['unmarked_lessons']})
