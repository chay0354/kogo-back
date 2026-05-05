"""
Tests for Courses Catalog API endpoints.

Primary flow:
/api/v1/courses/types/ -> list course types with aggregated stats
/api/v1/courses/types/<id>/details/ -> course type details with nested courses/lessons
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework import status

from apps.core.models import City, Branch, Room, UserProfile
from apps.courses.models import CourseType, Course, Lesson
from apps.customers.models import Family, Child
from apps.instructors.models import Instructor, InstructorSalaryTier
from apps.enrollments.models import LessonEnrollment


class CourseCatalogAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager@test.com',
            email='manager@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        # Core data
        self.city = City.objects.create(name="Test City")
        self.branch_a = Branch.objects.create(
            name="Branch A",
            address="Addr A",
            phone="050-0000000",
            email="a@test.com",
            manager_name="Manager A",
            city=self.city,
            is_active=True,
        )
        self.branch_b = Branch.objects.create(
            name="Branch B",
            address="Addr B",
            phone="050-0000001",
            email="b@test.com",
            manager_name="Manager B",
            city=self.city,
            is_active=True,
        )
        self.room_a1 = Room.objects.create(
            branch=self.branch_a, name="Room A1", capacity=20, purpose="", notes="", is_active=True
        )

        # Instructors
        self.instructor_fixed = Instructor.objects.create(
            first_name="Fixed",
            last_name="Teacher",
            phone="050-1111111",
            email="fixed@test.com",
            salary_model_type="fixed_per_lesson",
            fixed_salary_per_lesson=200,
            is_active=True,
        )
        self.instructor_tiered = Instructor.objects.create(
            first_name="Tiered",
            last_name="Teacher",
            phone="050-2222222",
            email="tiered@test.com",
            salary_model_type="tiered_by_students",
            fixed_salary_per_lesson=250,
            is_active=True,
        )
        InstructorSalaryTier.objects.bulk_create(
            [
                InstructorSalaryTier(
                    instructor=self.instructor_tiered, min_students=0, max_students=5, salary_per_lesson=150
                ),
                InstructorSalaryTier(
                    instructor=self.instructor_tiered, min_students=6, max_students=10, salary_per_lesson=200
                ),
                InstructorSalaryTier(
                    instructor=self.instructor_tiered, min_students=11, max_students=None, salary_per_lesson=250
                ),
            ]
        )

        # Course type + courses
        self.course_type = CourseType.objects.create(
            name="קפוארה",
            description="Capoeira",
            is_active=True,
        )
        self.course_1 = Course.objects.create(
            course_type=self.course_type,
            name="מתחילים א-ג",
            description="Beginners",
            price=400,
            capacity=20,
            branch=None,
            min_age=6,
            max_age=8,
            is_active=True,
        )
        self.course_2 = Course.objects.create(
            course_type=self.course_type,
            name="מתקדמים ד-ו",
            description="Advanced",
            price=450,
            capacity=20,
            branch=None,
            min_age=9,
            max_age=11,
            is_active=True,
        )

        # Lessons: one recurring, one non-recurring (still counts towards branches list)
        self.lesson_recurring = Lesson.objects.create(
            course=self.course_1,
            branch=self.branch_a,
            room=self.room_a1,
            instructor=self.instructor_fixed,
            day_of_week=1,
            start_time=time(16, 0),
            end_time=time(16, 45),
            is_recurring=True,
            status="scheduled",
        )
        self.lesson_non_recurring = Lesson.objects.create(
            course=self.course_2,
            branch=self.branch_b,
            room=None,
            instructor=self.instructor_tiered,
            day_of_week=2,
            start_time=time(17, 0),
            end_time=time(17, 45),
            lesson_date=date.today(),
            is_recurring=False,
            status="scheduled",
        )

        # Child + enrollment (only active enrollments should be counted)
        self.family = Family.objects.create(
            name="Family",
            phone="050-3333333",
            email="family@test.com",
            address="Addr",
            parent_id_number="123",
            branch=self.branch_a,
        )
        self.child = Child.objects.create(
            family=self.family,
            first_name="Kid",
            last_name="One",
            id_number="999",
            birth_date=date(2017, 1, 1),
            gender="male",
            status="active",
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson_recurring,
            child=self.child,
            status="active",
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson_non_recurring,
            child=self.child,
            status="inactive",
        )

    def test_course_types_list_returns_stats(self):
        resp = self.client.get("/api/v1/courses/types/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 1)

        ct = resp.data[0]
        self.assertEqual(ct["id"], str(self.course_type.id))
        self.assertEqual(ct["name"], "קפוארה")
        self.assertEqual(ct["courses_count"], 2)
        # only recurring lessons count
        self.assertEqual(ct["lessons_count"], 1)
        # only active enrollments count
        self.assertEqual(ct["students_count"], 1)

        # branches where lessons occur (distinct)
        branch_names = sorted([b["name"] for b in ct["branches"]])
        self.assertEqual(branch_names, ["Branch A", "Branch B"])

    def test_course_type_details_returns_nested_courses_and_lessons(self):
        resp = self.client.get(f"/api/v1/courses/types/{self.course_type.id}/details/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["id"], str(self.course_type.id))
        self.assertIn("courses", resp.data)
        self.assertEqual(len(resp.data["courses"]), 2)

        # Flatten lessons
        lessons = []
        for course in resp.data["courses"]:
            lessons.extend(course.get("lessons", []))

        self.assertEqual(len(lessons), 2)

        # Ensure lesson includes instructor and enrolled_count
        recurring = next(l for l in lessons if l["id"] == str(self.lesson_recurring.id))
        self.assertEqual(recurring["enrolled_count"], 1)
        self.assertEqual(recurring["branch"]["name"], "Branch A")
        self.assertEqual(recurring["instructor"]["full_name"], self.instructor_fixed.full_name)
        self.assertEqual(recurring["instructor"]["salary_model_type"], "fixed_per_lesson")
        # instructor salary override should be present (null by default)
        self.assertIn("instructor_salary_override", recurring)
        self.assertIsNone(recurring["instructor_salary_override"])

        tiered = next(l for l in lessons if l["id"] == str(self.lesson_non_recurring.id))
        self.assertEqual(tiered["instructor"]["salary_model_type"], "tiered_by_students")
        # salary tiers should be included for tiered instructors
        self.assertIn("salary_tiers", tiered["instructor"])
        self.assertGreaterEqual(len(tiered["instructor"]["salary_tiers"]), 1)

    def test_students_count_updates_when_enrollment_status_changes(self):
        # initial: 1 active enrollment (recurring lesson)
        resp = self.client.get("/api/v1/courses/types/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data[0]["students_count"], 1)

        # add another active enrollment to non-recurring lesson - should count too
        child2 = Child.objects.create(
            family=self.family,
            first_name="Kid",
            last_name="Two",
            id_number="998",
            birth_date=date(2016, 1, 1),
            gender="female",
            status="active",
        )
        le = LessonEnrollment.objects.create(
            lesson=self.lesson_non_recurring,
            child=child2,
            status="active",
        )
        resp2 = self.client.get("/api/v1/courses/types/")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data[0]["students_count"], 2)

        # deactivate that enrollment - should drop back
        le.status = "inactive"
        le.save(update_fields=["status"])
        resp3 = self.client.get("/api/v1/courses/types/")
        self.assertEqual(resp3.status_code, status.HTTP_200_OK)
        self.assertEqual(resp3.data[0]["students_count"], 1)

    def test_enrolled_count_counts_only_active_enrollments(self):
        # add an inactive enrollment to recurring lesson (different child)
        child_inactive = Child.objects.create(
            family=self.family,
            first_name="Kid",
            last_name="Inactive",
            id_number="997",
            birth_date=date(2016, 2, 1),
            gender="male",
            status="active",
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson_recurring,
            child=child_inactive,
            status="inactive",
        )
        resp = self.client.get(f"/api/v1/courses/types/{self.course_type.id}/details/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        lessons = []
        for course in resp.data["courses"]:
            lessons.extend(course.get("lessons", []))

        recurring = next(l for l in lessons if l["id"] == str(self.lesson_recurring.id))
        self.assertEqual(recurring["enrolled_count"], 1)

    def test_instructor_salary_override_roundtrips(self):
        self.lesson_recurring.instructor_salary_override = 333
        self.lesson_recurring.save(update_fields=["instructor_salary_override"])

        resp = self.client.get(f"/api/v1/courses/types/{self.course_type.id}/details/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        lessons = []
        for course in resp.data["courses"]:
            lessons.extend(course.get("lessons", []))

        recurring = next(l for l in lessons if l["id"] == str(self.lesson_recurring.id))
        self.assertIsNotNone(recurring["instructor_salary_override"])
        # DRF DecimalField serializes as string by default
        self.assertIn(str(recurring["instructor_salary_override"]), ["333", "333.00"])

    def test_course_type_soft_delete(self):
        # soft delete
        resp = self.client.delete(f"/api/v1/courses/types/{self.course_type.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        self.course_type.refresh_from_db()
        self.assertFalse(self.course_type.is_active)

        # list should be empty now
        resp2 = self.client.get("/api/v1/courses/types/")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data, [])


