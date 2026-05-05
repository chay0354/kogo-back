from datetime import date, time
from decimal import Decimal

from django.test import TestCase

from apps.core.models import City, Branch
from apps.instructors.models import Instructor, InstructorSalaryTier
from apps.courses.models import CourseType, Course, Lesson
from apps.customers.models import Family, Child
from apps.enrollments.models import LessonEnrollment
from apps.instructors.utils import calculate_instructor_monthly_metrics


class InstructorMonthlySalaryTests(TestCase):
    def setUp(self):
        self.city = City.objects.create(name="Test City")
        self.branch = Branch.objects.create(
            name="Branch",
            address="Addr",
            phone="050-0000000",
            email="b@test.com",
            manager_name="Manager",
            city=self.city,
            is_active=True,
        )

        self.instructor = Instructor.objects.create(
            first_name="Yossi",
            last_name="Mizrahi",
            phone="050-1111111",
            email="yossi@test.com",
            salary_model_type="tiered_by_students",
            fixed_salary_per_lesson=Decimal("200.00"),
            is_active=True,
        )
        InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=1,
            max_students=None,
            salary_per_lesson=Decimal("150.00"),
        )

        self.course_type = CourseType.objects.create(name="CT", description="", is_active=True)
        self.course = Course.objects.create(
            course_type=self.course_type,
            name="Course",
            description="",
            price=Decimal("400.00"),
            capacity=20,
            branch=None,
            min_age=6,
            max_age=8,
            is_active=True,
        )

        self.lesson_1 = Lesson.objects.create(
            course=self.course,
            branch=self.branch,
            instructor=self.instructor,
            day_of_week=1,
            start_time=time(16, 0),
            end_time=time(16, 45),
            is_recurring=True,
            status="scheduled",
        )
        self.lesson_2 = Lesson.objects.create(
            course=self.course,
            branch=self.branch,
            instructor=self.instructor,
            day_of_week=3,
            start_time=time(17, 0),
            end_time=time(17, 45),
            is_recurring=True,
            status="scheduled",
        )

        self.family = Family.objects.create(
            name="Family",
            phone="050-2222222",
            email="family@test.com",
            address="Addr",
            parent_id_number="123",
            branch=self.branch,
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

    def test_salary_is_monthly_and_counts_all_recurring_lessons_including_zero_students(self):
        # Enroll child to only one lesson:
        # - lesson_1 salary tier applies: 150 (1 student in tier 1-1)
        # - lesson_2 has 0 students -> uses LOWEST tier (150, not default 250)
        # Monthly salary = (150 * 4) + (150 * 4) = 1200
        LessonEnrollment.objects.create(lesson=self.lesson_1, child=self.child, status="active")

        metrics = calculate_instructor_monthly_metrics(self.instructor)
        self.assertEqual(metrics["salary"], Decimal("1200.00"))

        # Add enrollment to second lesson => both lessons match tier (150) => 150 * 4 * 2 = 1200
        child2 = Child.objects.create(
            family=self.family,
            first_name="Kid",
            last_name="Two",
            id_number="998",
            birth_date=date(2016, 1, 1),
            gender="female",
            status="active",
        )
        LessonEnrollment.objects.create(lesson=self.lesson_2, child=child2, status="active")

        metrics2 = calculate_instructor_monthly_metrics(self.instructor)
        self.assertEqual(metrics2["salary"], Decimal("1200.00"))


