"""
One meaning for "how many students are in this class".

Written from a real case: on a Wednesday at קניון דמרי סנטר, a 4.5-6 class held
fourteen paying children and six trials all booked for the same day. Every
counter that asked only for ``status='active'`` answered twenty — the capacity —
so the class read as full while six places were free. Across production that day
it was 47 lessons of 156, and every branch total was inflated.

The trap is that a trial signup *is* an active enrolment. It has to be: it sits
on the roster and the instructor marks it. What separates it is the trial date on
the row and, usually but not always, the child's own status — and both halves are
needed, because the office books trials for children who already attend something
else and are therefore ``active``. ``paying_enrollments`` is the one place that
rule lives; these tests fail if a caller stops using it.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor

User = get_user_model()


class HeadcountTestBase(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=self.city)
        self.room = Room.objects.create(name='סטודיו 1', branch=self.branch, capacity=20)
        self.ctype = CourseType.objects.create(name='קפוארה')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='t@counts.test', primary_branch=self.branch,
        )
        self.course = Course.objects.create(
            name='קפוארה 4.5-6 יום רביעי', branch=self.branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20, instructor=self.instructor,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, instructor=self.instructor, day_of_week=3,
            start_time=time(17, 30), end_time=time(18, 15), is_recurring=True, room=self.room,
        )
        self.manager = User.objects.create_user(
            username='mgr@counts.test', email='mgr@counts.test', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.manager)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        token, _ = Token.objects.get_or_create(user=self.manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def enroll(self, name, *, child_status='active', trial_on=None):
        family = Family.objects.create(name=name, phone='0529999999', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name=name, last_name='כהן',
            birth_date=date(2016, 4, 4), gender='male', status=child_status,
        )
        return LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active', trial_lesson_date=trial_on,
        )

    def the_wednesday_class(self):
        """Fourteen paying, six trials — the shape that read as 20/20."""
        wednesday = date.today() + timedelta(days=(2 - date.today().weekday()) % 7 or 7)
        for i in range(14):
            self.enroll(f'משלם{i}')
        for i in range(6):
            self.enroll(f'ניסיון{i}', child_status='trial_signed', trial_on=wednesday)
        return wednesday


class PayingHeadcountTests(HeadcountTestBase):
    def test_the_class_that_read_as_full_reports_its_fourteen_payers(self):
        from apps.enrollments.enrollment_counts import count_paying_enrollments

        self.the_wednesday_class()
        self.assertEqual(LessonEnrollment.objects.filter(lesson=self.lesson, status='active').count(), 20)
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 14)

    def test_a_trial_for_a_child_who_already_pays_elsewhere_is_not_a_payer_here(self):
        """
        The case the child's status cannot catch.

        A child who attends another class is 'active'. Booking them a trial here
        creates an active row on an active child — and only the trial date on the
        row says it is not a subscription.
        """
        from apps.enrollments.enrollment_counts import count_paying_enrollments

        self.enroll('ותיקה', child_status='active', trial_on=date.today() + timedelta(days=3))
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 0)

    def test_the_branch_total_counts_payers_and_not_trials(self):
        self.the_wednesday_class()
        res = self.client.get(f'/api/v1/core/branches/{self.branch.id}/statistics/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['active_students'], 14)

    def test_the_lesson_payload_never_reports_two_different_headcounts(self):
        """
        Both count fields answer the same question, so neither can be picked by
        name and shown against a capacity.
        """
        from apps.courses.serializers import LessonWithEnrollmentsSerializer

        self.the_wednesday_class()
        data = LessonWithEnrollmentsSerializer(self.lesson).data
        self.assertEqual(data['enrolled_count'], 14)
        self.assertEqual(data['total_students_count'], 14)

    def test_the_course_headcount_on_the_course_type_page_counts_payers(self):
        """
        The number the courses page falls back to when it has no per-lesson
        breakdown. It sits beside the capacity, which is where "20/20, full"
        came from.
        """
        self.the_wednesday_class()
        res = self.client.get(f'/api/v1/courses/types/{self.ctype.id}/details/')
        self.assertEqual(res.status_code, 200, res.data)
        courses = res.data['courses'] if isinstance(res.data, dict) else res.data
        row = next(c for c in courses if str(c['id']) == str(self.course.id))
        self.assertEqual(row['course_enrollment_count'], 14)


class LessonCapacityTests(HeadcountTestBase):
    """
    One answer to "how many fit in here".

    The room and the course each set a limit and the tighter one wins. The
    schedule used to send the course figure on its own, so twelve lessons in a
    nineteen-seat studio were drawn with twenty places — a class that was full
    never looked it.
    """

    def test_the_tighter_of_the_two_limits_wins(self):
        from apps.enrollments.enrollment_counts import resolve_lesson_capacity

        self.room.capacity = 19
        self.room.save(update_fields=['capacity'])
        self.assertEqual(self.course.capacity, 20)
        self.assertEqual(resolve_lesson_capacity(self.lesson), 19)

    def test_a_course_smaller_than_its_room_still_wins(self):
        from apps.enrollments.enrollment_counts import resolve_lesson_capacity

        self.course.capacity = 10
        self.course.save(update_fields=['capacity'])
        self.assertEqual(resolve_lesson_capacity(self.lesson), 10)

    def test_the_schedule_sends_the_same_number_the_widget_uses(self):
        self.room.capacity = 19
        self.room.save(update_fields=['capacity'])
        wednesday = self.the_wednesday_class()
        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': wednesday.isoformat(), 'end_date': wednesday.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = next(item for item in res.data if item['id'] == str(self.lesson.id))
        self.assertEqual(row['room_capacity'], 19)
        # And the headcount drawn against it is the paying one, not the roster.
        self.assertEqual(row['enrollment_count'], 14)
        self.assertEqual(row['trial_student_count'], 6)
