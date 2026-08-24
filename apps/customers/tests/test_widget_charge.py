"""Widget charge must enroll only after Tranzila approval, and never un-enroll a paid child."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment
from apps.customers.widget_views import WidgetChargeView
from apps.enrollments.models import LessonEnrollment


class WidgetChargeEnrollmentTest(TestCase):
    def setUp(self):
        self.child = TestDataFactory.create_child(status='pending')
        self.parent = TestDataFactory.create_parent(family=self.child.family)
        self.lesson = TestDataFactory.create_lesson()
        self.view = WidgetChargeView()
        self.card = {
            'card_number': '4580458045804580',
            'expiry_month': 12,
            'expiry_year': 2028,
            'cvv': '123',
            'card_holder_id': '012345678',
        }

    def _pending_payment(self, **kwargs):
        defaults = {
            'child': self.child,
            'family': self.child.family,
            'parent': self.parent,
            'lesson': self.lesson,
            'branch': self.lesson.course.branch,
            'payment_type': 'recurring_subscription',
            'status': 'pending',
            'base_amount': Decimal('5.00'),
            'discount_amount': Decimal('0.00'),
            'final_amount': Decimal('5.00'),
            'registration_fee': Decimal('0.00'),
            'description': 'בדיקה',
        }
        defaults.update(kwargs)
        return Payment.objects.create(**defaults)

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_declined_charge_does_not_enroll(self, mock_production):
        mock_production.return_value.charge_with_card.return_value = {
            'success': False,
            'error': 'הכרטיס נדחה',
            'response_code': '033',
        }
        payment = self._pending_payment()

        result = self.view._charge_one(str(payment.id), self.card, send_subscription_whatsapp=False)

        self.assertFalse(result['success'])
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'failed')
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'payment_problem')
        self.assertFalse(LessonEnrollment.objects.filter(child=self.child).exists())

    def test_retry_of_completed_payment_reports_success(self):
        payment = self._pending_payment()
        payment.status = 'completed'
        payment.payment_date = timezone.now()
        payment.save()
        self.child.status = 'active'
        self.child.save()
        LessonEnrollment.objects.create(
            child=self.child,
            lesson=self.lesson,
            status='active',
            start_date=date.today(),
        )

        result = self.view._charge_one(str(payment.id), self.card, send_subscription_whatsapp=False)

        self.assertTrue(result['success'])
        self.assertTrue(result.get('already_completed'))
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_timeout_after_notify_still_reports_success(self, mock_production):
        payment = self._pending_payment()

        def charge_then_notify(*args, **kwargs):
            payment.refresh_from_db()
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save(update_fields=['status', 'payment_date'])
            self.child.status = 'active'
            self.child.save(update_fields=['status'])
            LessonEnrollment.objects.get_or_create(
                child=self.child,
                lesson=self.lesson,
                defaults={'status': 'active', 'start_date': date.today()},
            )
            return {
                'success': False,
                'error': 'Request timed out',
                'indeterminate': True,
                'response_code': '999',
            }

        mock_production.return_value.charge_with_card.side_effect = charge_then_notify
        mock_production.return_value.find_successful_transaction.return_value = None

        result = self.view._charge_one(str(payment.id), self.card, send_subscription_whatsapp=False)

        self.assertTrue(result['success'])
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')
        self.assertTrue(
            LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson, status='active').exists()
        )

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_declined_retry_does_not_mark_already_paid_child(self, mock_production):
        paid = self._pending_payment()
        paid.status = 'completed'
        paid.payment_date = timezone.now()
        paid.save()
        self.child.status = 'active'
        self.child.save()
        LessonEnrollment.objects.create(
            child=self.child,
            lesson=self.lesson,
            status='active',
            start_date=date.today(),
        )
        retry = self._pending_payment()
        mock_production.return_value.charge_with_card.return_value = {
            'success': False,
            'error': 'הכרטיס נדחה',
            'response_code': '033',
        }

        result = self.view._charge_one(str(retry.id), self.card, send_subscription_whatsapp=False)

        self.assertFalse(result['success'])
        retry.refresh_from_db()
        self.assertEqual(retry.status, 'failed')
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')


APPROVED_CARD = {
    'success': True,
    'transaction_id': 'txn-family',
    'confirmation_code': '051',
    'response_code': '000',
    'token': 'tok_family',
    'raw_response': {},
}

VERIFY_SCHEMA_ERROR = {
    'success': False,
    'error': 'Json does not match validation schema',
    'response_code': '20004',
}


class WidgetMultiChildChargeTest(TestCase):
    """Three-kid signup: ₪120 + extra ₪0 day each must not abort after a verify 20004."""

    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.parent = TestDataFactory.create_parent(family=self.family)
        self.course = TestDataFactory.create_course()
        self.room = TestDataFactory.create_room(branch=self.course.branch)
        self.instructor = TestDataFactory.create_instructor(branch=self.course.branch)
        self.view = WidgetChargeView()
        self.card = {
            'card_number': '4580458045804580',
            'expiry_month': 12,
            'expiry_year': 2028,
            'cvv': '123',
            'card_holder_id': '012345678',
        }
        self.children = []
        self.payment_ids = []
        for index, name in enumerate(('שקד', 'מאור', 'נופר')):
            child, fee, extra = self._child_pair(name, day_fee=index, day_extra=index + 3)
            self.children.append(child)
            self.payment_ids.extend([str(fee.id), str(extra.id)])

    def _child_pair(self, first_name, day_fee, day_extra):
        child = TestDataFactory.create_child(
            first_name=first_name, family=self.family, status='pending',
        )
        fee_lesson = TestDataFactory.create_lesson(
            course=self.course, room=self.room, instructor=self.instructor, day_of_week=day_fee,
        )
        extra_lesson = TestDataFactory.create_lesson(
            course=self.course, room=self.room, instructor=self.instructor, day_of_week=day_extra,
        )
        fee = Payment.objects.create(
            child=child,
            family=self.family,
            parent=self.parent,
            lesson=fee_lesson,
            branch=self.course.branch,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('120.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('120.00'),
            registration_fee=Decimal('120.00'),
            description=f'דמי רישום {first_name}',
        )
        extra = Payment.objects.create(
            child=child,
            family=self.family,
            parent=self.parent,
            lesson=extra_lesson,
            branch=self.course.branch,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('0.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('0.00'),
            registration_fee=Decimal('0.00'),
            description=f'יום נוסף {first_name}',
        )
        return child, fee, extra

    def test_charge_order_puts_registration_fees_before_zero_rows(self):
        ordered = WidgetChargeView._charge_order(self.payment_ids)
        due = ordered[:3]
        free = ordered[3:]
        amounts = WidgetChargeView._payment_amounts(ordered)
        self.assertEqual(len(ordered), 6)
        self.assertTrue(all(amounts[pid] > 0 for pid in due))
        self.assertTrue(all(amounts[pid] == 0 for pid in free))
        self.assertEqual(set(ordered), set(self.payment_ids))

    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email')
    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_three_kids_all_enroll_when_verify_card_would_fail(
        self, mock_production, _mock_email, _mock_whatsapp,
    ):
        gateway = mock_production.return_value
        gateway.charge_with_card.return_value = APPROVED_CARD
        gateway.verify_card.return_value = VERIFY_SCHEMA_ERROR

        payload = self.view._charge_batch(self.payment_ids, self.card)

        self.assertTrue(payload['success'])
        self.assertEqual(gateway.charge_with_card.call_count, 3)
        gateway.verify_card.assert_not_called()
        self.assertFalse(any(r.get('skipped') for r in payload['results']))
        for child in self.children:
            child.refresh_from_db()
            self.assertEqual(child.status, 'active')
            self.assertEqual(
                LessonEnrollment.objects.filter(child=child, status='active').count(),
                2,
            )
        self.assertEqual(
            Payment.objects.filter(id__in=self.payment_ids, status='completed').count(),
            6,
        )

    @patch('apps.core.tranzila_service.TranzilaService.production')
    def test_first_registration_fee_decline_skips_remaining_kids(self, mock_production):
        gateway = mock_production.return_value
        gateway.charge_with_card.return_value = {
            'success': False,
            'error': 'הכרטיס נדחה',
            'response_code': '033',
        }
        gateway.verify_card.return_value = VERIFY_SCHEMA_ERROR

        payload = self.view._charge_batch(self.payment_ids, self.card)

        self.assertFalse(payload.get('success'))
        self.assertFalse(payload.get('partial'))
        self.assertEqual(gateway.charge_with_card.call_count, 1)
        gateway.verify_card.assert_not_called()
        skipped = [r for r in payload['results'] if r.get('skipped')]
        self.assertEqual(len(skipped), 5)
        for child in self.children:
            child.refresh_from_db()
            self.assertNotEqual(child.status, 'active')
            self.assertFalse(LessonEnrollment.objects.filter(child=child).exists())

    def test_precheck_allows_retry_after_failed_and_completed_rows(self):
        payments = list(Payment.objects.filter(id__in=self.payment_ids).order_by('created_at'))
        payments[0].status = 'completed'
        payments[0].save(update_fields=['status'])
        payments[1].status = 'failed'
        payments[1].save(update_fields=['status'])
        self.assertIsNone(self.view._precheck_bundle(self.payment_ids))
