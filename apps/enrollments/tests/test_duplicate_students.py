from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, City, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.duplicate_students import duplicate_person_on_lesson, duplicate_roster_rows
from apps.enrollments.models import LessonEnrollment


class DuplicateRosterTests(TestCase):
    """The same person must not sit on one register twice."""

    def setUp(self):
        self.client = APIClient()
        manager = get_user_model().objects.create_user(
            username='dup-manager', password='pw-for-tests',
        )
        profile, _ = UserProfile.objects.get_or_create(user=manager)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        manager.refresh_from_db()
        self.client.force_authenticate(user=manager)

        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='מרכז', city=city)
        self.room = Room.objects.create(name='אולם', branch=self.branch, capacity=20)
        course_type = CourseType.objects.create(name='קפוארה')
        self.course = Course.objects.create(
            name='קפוארה ואקרובטיקה',
            course_type=course_type,
            branch=self.branch,
            price=300,
            capacity=20,
        )
        self.lesson = Lesson.objects.create(
            course=self.course,
            room=self.room,
            day_of_week=1,
            start_time='16:00',
            end_time='16:45',
            is_recurring=True,
        )

    def _child(self, *, first='דניאל', last='כהן', phone='052-265-9322', family_name='כהן'):
        family = Family.objects.create(name=family_name, phone=phone, branch=self.branch)
        return Child.objects.create(
            family=family,
            first_name=first,
            last_name=last,
            birth_date=date(2016, 5, 1),
            gender='male',
            status='active',
        )

    def test_same_name_and_phone_registered_twice_is_refused(self):
        first = self._child()
        LessonEnrollment.objects.create(lesson=self.lesson, child=first, status='active')
        twin_record = self._child(phone='0522659322')

        res = self.client.post(
            '/api/v1/enrollments/lesson-enrollments/',
            {'lesson': str(self.lesson.id), 'child': str(twin_record.id), 'status': 'active'},
            format='json',
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data['existing_child_id'], str(first.id))
        self.assertEqual(LessonEnrollment.objects.filter(lesson=self.lesson).count(), 1)

    def test_a_sibling_on_the_same_phone_still_registers(self):
        LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        sibling = self._child(first='נועה')

        res = self.client.post(
            '/api/v1/enrollments/lesson-enrollments/',
            {'lesson': str(self.lesson.id), 'child': str(sibling.id), 'status': 'active'},
            format='json',
        )

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(LessonEnrollment.objects.filter(lesson=self.lesson).count(), 2)

    def test_the_same_name_on_a_different_phone_still_registers(self):
        LessonEnrollment.objects.create(lesson=self.lesson, child=self._child(), status='active')
        namesake = self._child(phone='0501112233')

        res = self.client.post(
            '/api/v1/enrollments/lesson-enrollments/',
            {'lesson': str(self.lesson.id), 'child': str(namesake.id), 'status': 'active'},
            format='json',
        )

        self.assertEqual(res.status_code, 201, res.data)

    def test_registering_the_very_same_child_again_adds_no_row(self):
        child = self._child()
        LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active')

        res = self.client.post(
            '/api/v1/enrollments/lesson-enrollments/',
            {'lesson': str(self.lesson.id), 'child': str(child.id), 'status': 'active'},
            format='json',
        )

        # The table's own unique (lesson, child) answers this one before the
        # duplicate check is reached.
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(LessonEnrollment.objects.filter(lesson=self.lesson).count(), 1)

    def test_the_report_names_a_register_holding_one_person_twice(self):
        first = self._child()
        second = self._child(phone='+972 52-265-9322')
        LessonEnrollment.objects.create(lesson=self.lesson, child=first, status='active')
        LessonEnrollment.objects.create(lesson=self.lesson, child=second, status='active')

        # Written straight to the table, the way an older signup path could.
        rows = duplicate_roster_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['child_name'], 'דניאל כהן')
        self.assertEqual({row['child_id'] for row in rows[0]['children']}, {str(first.id), str(second.id)})

        res = self.client.get('/api/v1/enrollments/lesson-enrollments/duplicates/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['count'], 1)

    def test_a_child_with_no_phone_is_not_matched_to_anyone(self):
        family = Family.objects.create(name='ללא טלפון', phone='', branch=self.branch)
        nameless = Child.objects.create(
            family=family, first_name='דניאל', last_name='כהן',
            birth_date=date(2016, 5, 1), gender='male', status='active',
        )
        LessonEnrollment.objects.create(lesson=self.lesson, child=nameless, status='active')

        self.assertIsNone(
            duplicate_person_on_lesson(
                self.lesson, first_name='דניאל', last_name='כהן', phone='',
            )
        )
        self.assertEqual(duplicate_roster_rows(), [])
