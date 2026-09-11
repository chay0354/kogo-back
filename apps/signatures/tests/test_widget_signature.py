"""
The registration widget keeps the parent's signature — and a registration never
depends on it. Every test here also checks the registration itself came out
as it would have without the signature.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Branch, City, RegistrationTerms
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Payment
from apps.enrollments.models import LessonEnrollment
from apps.signatures.models import Signature
from apps.signatures.tests.helpers import TERMS_HTML, png_bytes, png_data_url

REGISTER_URL = '/api/v1/customers/widget/register/'
TRIAL_URL = '/api/v1/customers/widget/trial-register/'


def _no_discount(**kwargs):
    from apps.customers.discount_service import DiscountCalculation

    return DiscountCalculation(
        applicable_discounts=[],
        total_discount_amount=Decimal('0.00'),
        final_price=kwargs['base_price'],
        base_price=kwargs['base_price'],
    )


def _register_payload(**overrides):
    base = {
        'parent_id_number': '123456782',
        'parent_first_name': 'רות',
        'parent_last_name': 'ניסן',
        'parent_phone': '0521234567',
        'parent_email': 'ruth@example.com',
        'child_first_name': 'נועה',
        'child_last_name': 'ניסן',
        'child_id_number': '234567892',
        'child_birth_date': '2015-01-01',
        'child_gender': 'female',
        'signature': png_data_url(),
        'terms_consent': True,
        'health_consent': True,
        'computerized_docs_consent': True,
    }
    base.update(overrides)
    return base


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment', side_effect=_no_discount)
@patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
class WidgetRegisterSignatureTests(TestCase):
    def setUp(self):
        RegistrationTerms.objects.update_or_create(pk=1, defaults={'content': TERMS_HTML})
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    def _register(self, **overrides):
        payload = _register_payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id))
        payload.update(overrides)
        return self.client.post(
            REGISTER_URL, payload, format='json',
            HTTP_X_FORWARDED_FOR='203.0.113.9, 10.0.0.1', HTTP_USER_AGENT='Mozilla/5.0 (Widget)',
        )

    def test_a_signed_registration_keeps_the_signature(self, *_mocks):
        res = self._register()

        self.assertEqual(res.status_code, 201, res.content)
        family = Family.objects.get(parent_id_number='123456782')
        child = Child.objects.get(pk=res.json()['child_id'])
        signature = Signature.objects.get()
        self.assertEqual(signature.family, family)
        self.assertEqual(list(signature.children.all()), [child])
        self.assertEqual(signature.branch, self.course.branch)
        self.assertEqual(signature.document_html, TERMS_HTML)
        self.assertEqual(len(signature.document_sha256), 64)
        self.assertEqual(bytes(signature.signature_png), png_bytes())
        self.assertEqual(signature.consents, {'health': True, 'terms': True, 'computerized_documents': True})
        self.assertEqual(signature.ip_address, '203.0.113.9')
        self.assertEqual(signature.user_agent, 'Mozilla/5.0 (Widget)')
        self.assertEqual(signature.signer_name, 'רות ניסן')
        self.assertEqual(signature.refs, {
            'payment_ids': [res.json()['payment_id']],
            'lesson_ids': [str(self.lesson.id)],
            'course_ids': [str(self.course.id)],
            'trial': False,
        })

    def test_a_registration_without_a_signature_stores_nothing(self, *_mocks):
        res = self._register(signature=None)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(Payment.objects.filter(pk=res.json()['payment_id']).exists())
        self.assertFalse(Signature.objects.exists())

    def test_a_bad_image_stores_nothing(self, *_mocks):
        res = self._register(signature='data:image/png;base64,AAAA')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(Signature.objects.exists())

    def test_an_oversized_image_stores_nothing(self, *_mocks):
        import base64
        big = b'\x89PNG\r\n\x1a\n' + b'\0' * (300 * 1024)
        res = self._register(signature='data:image/png;base64,' + base64.b64encode(big).decode())
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(Signature.objects.exists())

    def test_two_children_signed_for_once_are_one_signing(self, *_mocks):
        first = self._register()
        second = self._register(child_first_name='איתי', child_id_number='111111118')

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertNotEqual(first.json()['child_id'], second.json()['child_id'])
        signature = Signature.objects.get()
        self.assertEqual(
            {str(child.id) for child in signature.children.all()},
            {first.json()['child_id'], second.json()['child_id']},
        )
        self.assertEqual(signature.refs['payment_ids'], [first.json()['payment_id'], second.json()['payment_id']])

    def test_a_capture_that_raises_never_changes_the_response(self, *_mocks):
        control = self._register(parent_id_number='987654321')
        with patch('apps.signatures.capture.record_registration_signature', side_effect=RuntimeError('boom')):
            res = self._register()

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(set(res.json()), set(control.json()))
        self.assertEqual(res.json()['final_amount'], control.json()['final_amount'])
        self.assertTrue(Payment.objects.filter(pk=res.json()['payment_id'], status='pending').exists())
        self.assertFalse(Signature.objects.filter(family__parent_id_number='123456782').exists())

    def test_a_database_error_in_capture_leaves_the_registration_in_place(self, *_mocks):
        def failing_terms():
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1/0')

        with patch('apps.signatures.capture.get_registration_terms', side_effect=failing_terms):
            res = self._register()

        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(Payment.objects.filter(pk=res.json()['payment_id']).exists())
        self.assertFalse(Signature.objects.exists())

    def test_a_bundle_registration_keeps_the_signature(self, *_mocks):
        from apps.courses.models import LessonBundle

        other = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('500.00'))
        bundle.lessons.set([self.lesson, other])

        res = self._register(bundle_id=str(bundle.id))

        self.assertEqual(res.status_code, 201, res.content)
        signature = Signature.objects.get()
        self.assertEqual(signature.refs['bundle_ids'], [str(bundle.id)])
        self.assertEqual(signature.refs['payment_ids'], [res.json()['payments'][0]['payment_id']])
        self.assertEqual(set(signature.refs['lesson_ids']), {str(self.lesson.id), str(other.id)})


WEDNESDAY = 3
PY_WEDNESDAY = 2


def _next_wednesday():
    today = date.today()
    return today + timedelta(days=(PY_WEDNESDAY - today.weekday()) % 7 or 7)


@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': True})
class WidgetTrialSignatureTests(TestCase):
    def setUp(self):
        RegistrationTerms.objects.update_or_create(pk=1, defaults={'content': TERMS_HTML})
        self.client = APIClient()
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        self.course = Course.objects.create(
            name='היפהופ', branch=self.branch, course_type=CourseType.objects.create(name='היפהופ'),
            price=320, capacity=20, min_age=6, max_age=9, is_active=True,
        )
        self.lesson = Lesson.objects.create(
            course=self.course, day_of_week=WEDNESDAY, start_time=time(17, 0), end_time=time(18, 0),
        )
        self.trial_date = _next_wednesday()

    def _book(self, **overrides):
        payload = {
            'parent_id_number': '123456782', 'parent_first_name': 'רות', 'parent_last_name': 'ניסן',
            'parent_phone': '0521234567',
            'child_first_name': 'נועה', 'child_last_name': 'ניסן', 'child_id_number': '111111118',
            'child_birth_date': '2018-05-05', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'trial_lesson_date': self.trial_date.isoformat(),
            'computerized_docs_consent': False,
        }
        payload.update(overrides)
        return self.client.post(TRIAL_URL, payload, format='json', REMOTE_ADDR='198.51.100.4')

    def test_a_free_trial_without_a_signature_stores_nothing(self, _notify):
        res = self._book()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(LessonEnrollment.objects.filter(pk=res.json()['enrollment_id']).exists())
        self.assertFalse(Signature.objects.exists())

    def test_a_signed_free_trial_keeps_the_signature(self, _notify):
        res = self._book(signature=png_data_url(), terms_consent=True, health_consent=True)

        self.assertEqual(res.status_code, 201, res.content)
        signature = Signature.objects.get()
        self.assertEqual(signature.branch, self.branch)
        self.assertEqual([str(c.id) for c in signature.children.all()], [res.json()['child_id']])
        self.assertEqual(signature.ip_address, '198.51.100.4')
        self.assertEqual(signature.refs, {
            'enrollment_ids': [res.json()['enrollment_id']],
            'lesson_ids': [str(self.lesson.id)],
            'course_ids': [str(self.course.id)],
            'trial_lesson_dates': [self.trial_date.isoformat()],
            'trial': True,
        })

    def test_a_signed_paid_trial_keeps_the_signature(self, _notify):
        self.course.trial_lesson_is_paid = True
        self.course.trial_lesson_price = Decimal('40.00')
        self.course.save()

        res = self._book(signature=png_data_url(), terms_consent=True, health_consent=True)

        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.json()['requires_payment'])
        signature = Signature.objects.get()
        self.assertEqual(signature.refs['payment_ids'], [res.json()['payment_id']])
        self.assertTrue(signature.refs['trial'])

    def test_a_capture_that_raises_never_changes_the_trial_response(self, _notify):
        with patch('apps.signatures.capture.record_registration_signature', side_effect=RuntimeError('boom')):
            res = self._book(signature=png_data_url(), terms_consent=True, health_consent=True)

        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            set(res.json()), {'enrollment_id', 'child_id', 'trial_lesson_date', 'trial_applied', 'whatsapp'},
        )
        self.assertEqual(Child.objects.get(pk=res.json()['child_id']).status, 'trial_signed')
        self.assertFalse(Signature.objects.exists())
