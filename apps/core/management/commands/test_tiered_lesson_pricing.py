from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import Branch, City, Room
from apps.core.payment_service import PaymentService
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.discount_service import DiscountCalculation
from apps.customers.models import Child, Family, Parent
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor


class RollbackDemo(Exception):
    """Internal sentinel used to rollback all demo data."""


class Command(BaseCommand):
    help = (
        "Create temporary data and verify tiered lesson pricing: first signed "
        "lesson uses regular price, second signed lesson uses the second tier."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-data",
            action="store_true",
            help="Do not rollback temporary data after the check.",
        )

    def handle(self, *args, **options):
        keep_data = options["keep_data"]

        try:
            with transaction.atomic():
                result = self._run_pricing_check()

                if not keep_data:
                    raise RollbackDemo

                self.stdout.write(self.style.WARNING("Temporary test data was kept because --keep-data was used."))
                return result
        except RollbackDemo:
            self.stdout.write(self.style.WARNING("Temporary test data rolled back."))

    def _run_pricing_check(self):
        suffix = date.today().strftime("%Y%m%d")

        city = City.objects.create(name=f"Tier Test City {suffix}")
        branch = Branch.objects.create(
            name=f"Tier Test Branch {suffix}",
            city=city,
            branch_codes=[f"TIER-{suffix}"],
            is_active=True,
        )
        room = Room.objects.create(
            branch=branch,
            name="Tier Test Room",
            capacity=20,
            is_active=True,
        )
        instructor = Instructor.objects.create(
            first_name="Tier",
            last_name="Tester",
            phone="050-0000000",
            email=f"tier-tester-{suffix}@example.com",
            primary_branch=branch,
            is_active=True,
        )
        course_type = CourseType.objects.create(name=f"Tier Test Type {suffix}")
        course = Course.objects.create(
            course_type=course_type,
            name="Tier Test Course",
            price=Decimal("360.00"),
            capacity=20,
            branch=branch,
            is_active=True,
        )
        first_lesson = Lesson.objects.create(
            course=course,
            branch=branch,
            room=room,
            instructor=instructor,
            day_of_week=0,
            start_time=time(16, 0),
            end_time=time(16, 45),
            price=Decimal("360.00"),
            additional_course_prices=[
                {"course_index": 2, "price": 250},
                {"course_index": 3, "price": 200},
            ],
            status="scheduled",
        )
        second_lesson = Lesson.objects.create(
            course=course,
            branch=branch,
            room=room,
            instructor=instructor,
            day_of_week=2,
            start_time=time(17, 0),
            end_time=time(17, 45),
            price=Decimal("360.00"),
            additional_course_prices=[
                {"course_index": 2, "price": 250},
                {"course_index": 3, "price": 200},
            ],
            status="scheduled",
        )
        family = Family.objects.create(
            name="Tier Test Family",
            phone="050-1111111",
            branch=branch,
        )
        Parent.objects.create(
            family=family,
            first_name="Tier",
            last_name="Parent",
            phone="050-2222222",
            is_primary=True,
        )
        child = Child.objects.create(
            family=family,
            first_name="Tier",
            last_name="Child",
            birth_date=date.today() - timedelta(days=365 * 8),
            gender="male",
            status="pending",
        )

        def passthrough_discount(**kwargs):
            base_price = Decimal(str(kwargs["base_price"]))
            return DiscountCalculation(
                applicable_discounts=[],
                total_discount_amount=Decimal("0.00"),
                final_price=base_price,
                base_price=base_price,
            )

        service = PaymentService()
        with patch(
            "apps.core.payment_service.TranzilaService.create_recurring_payment_request",
            return_value="https://tranzila.test/tier-pricing-check",
        ), patch(
            "apps.core.payment_service.DiscountService.evaluate_discounts_for_payment",
            side_effect=passthrough_discount,
        ):
            first_payment = service.initiate_subscription_payment(
                child_id=str(child.id),
                lesson_id=str(first_lesson.id),
                payment_date=date.today(),
            )

            LessonEnrollment.objects.create(
                lesson=first_lesson,
                child=child,
                status="active",
                start_date=date.today(),
            )

            second_payment = service.initiate_subscription_payment(
                child_id=str(child.id),
                lesson_id=str(second_lesson.id),
                payment_date=date.today(),
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Tiered lesson pricing check"))
        self.stdout.write(f"Child: {child.full_name}")
        self.stdout.write(f"First lesson index: {first_payment['course_index']}")
        self.stdout.write(f"First lesson charged price: {first_payment['base_amount']}")
        self.stdout.write(f"Second lesson index: {second_payment['course_index']}")
        self.stdout.write(f"Second lesson charged price: {second_payment['base_amount']}")

        expected_first = 360.00
        expected_second = 250.00
        if first_payment["course_index"] != 1 or first_payment["base_amount"] != expected_first:
            raise AssertionError(
                f"Expected first lesson to charge {expected_first}, got {first_payment}"
            )
        if second_payment["course_index"] != 2 or second_payment["base_amount"] != expected_second:
            raise AssertionError(
                f"Expected second lesson to charge {expected_second}, got {second_payment}"
            )

        self.stdout.write(self.style.SUCCESS("PASS: second lesson price changed as expected."))
