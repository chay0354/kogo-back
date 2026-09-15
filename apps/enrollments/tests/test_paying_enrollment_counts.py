"""Tests for paying vs trial enrollment counts."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.enrollment_counts import count_capacity_enrollments, count_paying_enrollments
from apps.enrollments.models import LessonEnrollment


User = get_user_model()


class PayingEnrollmentCountTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        self.ct = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(
            course_type=self.ct, name='Kids', price=400, capacity=10, branch=self.branch
        )
        self.lesson = Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        self.paying_child = Child.objects.create(
            family=self.family,
            first_name='Paid',
            last_name='Kid',
            birth_date=date(2015, 1, 1),
            gender='female',
            status='active',
        )
        self.trial_child = Child.objects.create(
            family=self.family,
            first_name='Trial',
            last_name='Kid',
            birth_date=date(2016, 1, 1),
            gender='male',
            status='trial_signed',
        )
        LessonEnrollment.objects.create(lesson=self.lesson, child=self.paying_child, status='active')
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.trial_child,
            status='active',
            trial_lesson_date=date(2026, 6, 10),
        )

        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_count_paying_enrollments_excludes_trial_signed(self):
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 1)

    def test_course_details_enrolled_count_excludes_trial(self):
        """
        Lessons moved off the course-type payload to their own endpoint, so this
        asks the one that serves them now.

        Both count fields answer the same question on purpose. This test used to
        expect total_students_count to be 2 — the roster, trial included — and
        that second meaning is what let a class of paying students be shown
        against its capacity as full. The roster is a per-date question and is
        still answered, with the date, by the schedule endpoint; see
        test_schedule_occurrence_returns_roster_and_trial_counts below.
        """
        res = self.client.get(f'/api/v1/courses/courses/{self.course.id}/lessons_detail/')
        self.assertEqual(res.status_code, 200, res.data)
        lessons = res.data['lessons'] if isinstance(res.data, dict) else res.data
        row = next(l for l in lessons if str(l['id']) == str(self.lesson.id))
        self.assertEqual(row['enrolled_count'], 1)
        self.assertEqual(row['total_students_count'], 1)

    def test_trial_child_counts_after_converting_to_active(self):
        self.trial_child.status = 'active'
        self.trial_child.save(update_fields=['status'])
        # Leftover trial_lesson_date still marks the row as a trial, not a seat.
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 1)
        LessonEnrollment.objects.filter(child=self.trial_child).update(trial_lesson_date=None)
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 2)

    def test_trial_never_counts_toward_capacity(self):
        trial_date = date.today() + timedelta(days=7)
        LessonEnrollment.objects.filter(child=self.trial_child).update(trial_lesson_date=trial_date)
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=trial_date),
            1,
        )
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)

    def test_finished_trial_does_not_count_toward_capacity(self):
        past_trial = date(2026, 6, 10)
        LessonEnrollment.objects.filter(child=self.trial_child).update(trial_lesson_date=past_trial)
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=past_trial),
            1,
        )
        self.trial_child.status = 'trial_completed'
        self.trial_child.save(update_fields=['status'])
        self.assertEqual(count_capacity_enrollments(lesson=self.lesson), 1)
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=past_trial),
            1,
        )

    def test_schedule_occurrence_returns_roster_and_trial_counts(self):
        trial_date = date.today() + timedelta(days=1)
        while trial_date.weekday() != 2:  # Wednesday
            trial_date += timedelta(days=1)
        LessonEnrollment.objects.filter(child=self.trial_child).update(trial_lesson_date=trial_date)
        self.lesson.day_of_week = 3
        self.lesson.save(update_fields=['day_of_week'])

        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': trial_date.isoformat(), 'end_date': trial_date.isoformat()},
        )

        self.assertEqual(res.status_code, 200, res.data)
        row = next(item for item in res.data if item['id'] == str(self.lesson.id))
        self.assertEqual(row['enrollment_count'], 1)
        self.assertEqual(row['student_count'], 2)
        # The instructor card shows these two apart: one paying, one on trial.
        self.assertEqual(row['active_student_count'], 1)
        self.assertEqual(row['trial_student_count'], 1)

    def test_course_enrollment_count_excludes_trials(self):
        """
        The course headcount shown beside the capacity on the courses page.

        It used to include trial signups — this test asserted it, and was named
        for it. That is what produced the report this change came from: a
        Wednesday class of fourteen paying children with six trials booked for
        one day read as "20/20", full, with six places actually free.
        """
        Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=2,
            start_time='18:00',
            end_time='19:00',
        )
        res = self.client.get(f'/api/v1/courses/types/{self.ct.id}/details/')
        self.assertEqual(res.status_code, 200)
        course = res.data['courses'][0]
        self.assertEqual(course['course_enrollment_count'], 1)
