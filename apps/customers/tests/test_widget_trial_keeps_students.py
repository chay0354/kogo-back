"""
A student who books a trial in the widget is still a student (27.9.2026).

Both widget paths — the free trial and the paid one, once its card goes
through — used to write נרשם לניסיון over whatever the child was. A paying
child who tried another course then vanished from their own course's register,
which hides the regular row of a trial_signed child. Five paying children in
production, until the office put them back.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Branch, City
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Parent, Payment
from apps.customers.tests.test_widget_charge import CARD, TRANZILA_OK, _payment_for
from apps.enrollments.models import LessonEnrollment

TRIAL_URL = '/api/v1/customers/widget/trial-register/'
WEDNESDAY = 3
PY_WEDNESDAY = 2


def _next_wednesday():
    today = date.today()
    return today + timedelta(days=(PY_WEDNESDAY - today.weekday()) % 7 or 7)


@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': True})
class WidgetFreeTrialTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        course_type = CourseType.objects.create(name='היפהופ')
        self.course = Course.objects.create(
            name='היפהופ', branch=self.branch, course_type=course_type,
            price=320, capacity=20, min_age=6, max_age=9, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, day_of_week=WEDNESDAY, start_time=time(17, 0), end_time=time(18, 0),
        )
        own_course = Course.objects.create(
            name='קפוארה', branch=self.branch, course_type=course_type,
            price=260, capacity=20, min_age=6, max_age=9, is_active=True,
        )
        self.own_lesson = Lesson.objects.create(
            course=own_course, day_of_week=1, start_time=time(16, 0), end_time=time(17, 0),
        )
        self.family = Family.objects.create(
            name='ניסן', phone='0521234567', parent_id_number='123456782', branch=self.branch,
        )
        Parent.objects.create(
            family=self.family, first_name='רות', last_name='ניסן', phone='0521234567', is_primary=True,
        )

    def child(self, status):
        return Child.objects.create(
            family=self.family, first_name='נועה', last_name='ניסן', id_number='111111118',
            birth_date=date(2018, 5, 5), gender='female', status=status,
        )

    def book(self):
        return self.client.post(TRIAL_URL, {
            'parent_id_number': '123456782', 'parent_first_name': 'רות', 'parent_last_name': 'ניסן',
            'parent_phone': '0521234567',
            'child_first_name': 'נועה', 'child_last_name': 'ניסן', 'child_id_number': '111111118',
            'child_birth_date': '2018-05-05', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'trial_lesson_date': _next_wednesday().isoformat(),
        }, format='json')

    def test_a_paying_child_who_books_a_free_trial_stays_active(self, _notify):
        child = self.child('active')
        LessonEnrollment.objects.create(child=child, lesson=self.own_lesson, status='active')
        res = self.book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(LessonEnrollment.objects.filter(child=child, lesson=self.lesson).exists())
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_a_child_in_registration_is_marked_for_the_trial(self, _notify):
        child = self.child('pending')
        res = self.book()
        self.assertEqual(res.status_code, 201, res.content)
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')


@override_settings(
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_PUBLIC_KEY='test_public_key',
    TRANZILA_SECRET_KEY='test_secret_key',
    TRANZILA_PROD_TERMINAL='test_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='test_terminal',
    TRANZILA_PROD_PUBLIC_KEY='test_public_key',
    TRANZILA_PROD_SECRET_KEY='test_secret_key',
    SUBSCRIPTION_FIRST_CHARGE_DATE='',
)
@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': False})
@patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
@patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
class WidgetPaidTrialTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=self.family)
        self.lesson = TestDataFactory.create_lesson()

    def pay_for_a_trial(self, child):
        payment = _payment_for(
            child, self.lesson,
            payment_type='one_time', trial_lesson_date=date.today() + timedelta(days=5),
            base_amount=Decimal('40.00'), final_amount=Decimal('40.00'),
            registration_fee=Decimal('0.00'), description='שיעור ניסיון',
        )
        self.client.post(
            '/api/v1/customers/widget/charge/',
            {'payment_ids': [str(payment.id)], 'card_details': CARD}, format='json',
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

    def test_a_paying_child_who_pays_for_a_trial_stays_active(self, _charge, _whatsapp, _stamp):
        child = TestDataFactory.create_child(family=self.family, status='active')
        self.pay_for_a_trial(child)
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_a_child_with_a_card_problem_stays_a_card_problem(self, _charge, _whatsapp, _stamp):
        child = TestDataFactory.create_child(family=self.family, status='payment_problem')
        self.pay_for_a_trial(child)
        child.refresh_from_db()
        self.assertEqual(child.status, 'payment_problem')

    def test_a_child_in_registration_is_marked_for_the_trial(self, _charge, _whatsapp, _stamp):
        child = TestDataFactory.create_child(family=self.family, status='pending')
        self.pay_for_a_trial(child)
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')
