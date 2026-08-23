"""Staff can move a child to another lesson without changing billed amounts."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent, Payment, RecurringPayment
from apps.enrollments.models import LessonEnrollment


User = get_user_model()


class ChangeLessonEnrollmentTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        self.ct = CourseType.objects.create(name='Capoeira')
        self.course_a = Course.objects.create(
            course_type=self.ct, name='קפוארה 3-4.5 יום רביעי', price=260, capacity=10, branch=self.branch
        )
        self.course_b = Course.objects.create(
            course_type=self.ct, name='קפוארה 3-4.5 יום שני', price=400, capacity=10, branch=self.branch
        )
        self.lesson_a = Lesson.objects.create(
            course=self.course_a, room=self.room, day_of_week=3, start_time='16:45', end_time='17:30',
        )
        self.lesson_b = Lesson.objects.create(
            course=self.course_b, room=self.room, day_of_week=1, start_time='16:45', end_time='17:30',
        )
        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(
            family=self.family, first_name='Avi', last_name='Cohen', phone='0501234567', is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family,
            first_name='Noa',
            last_name='Cohen',
            birth_date=date(2018, 1, 1),
            gender='female',
            status='active',
        )
        self.enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson_a, child=self.child, status='active',
        )
        self.payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            branch=self.branch,
            lesson=self.lesson_a,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('260.00'),
        )
        self.recurring = RecurringPayment.objects.create(
            child=self.child,
            initial_payment=self.payment,
            status='active',
            base_amount=Decimal('260.00'),
            amount=Decimal('260.00'),
            billing_day=1,
            start_date=date.today(),
        )
        user = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_change_lesson_keeps_payment_amounts(self):
        res = self.client.post(
            f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/change-lesson/',
            {'lesson_id': str(self.lesson_b.id)},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.data)

        self.enrollment.refresh_from_db()
        self.payment.refresh_from_db()
        self.recurring.refresh_from_db()

        self.assertEqual(self.enrollment.lesson_id, self.lesson_b.id)
        self.assertEqual(self.payment.lesson_id, self.lesson_b.id)
        self.assertEqual(self.payment.final_amount, Decimal('260.00'))
        self.assertEqual(self.payment.base_amount, Decimal('260.00'))
        self.assertEqual(self.recurring.amount, Decimal('260.00'))
        self.assertEqual(self.recurring.base_amount, Decimal('260.00'))

    def test_change_lesson_rejects_duplicate(self):
        LessonEnrollment.objects.create(lesson=self.lesson_b, child=self.child, status='active')
        res = self.client.post(
            f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/change-lesson/',
            {'lesson_id': str(self.lesson_b.id)},
            format='json',
        )
        self.assertEqual(res.status_code, 400)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.lesson_a.id)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.final_amount, Decimal('260.00'))
        self.assertEqual(self.payment.lesson_id, self.lesson_a.id)
