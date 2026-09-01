"""
Unit tests for PaymentService.

Tests coverage:
- initiate_subscription_payment: validation, discount integration, payment creation
- process_webhook_callback: success/failure webhooks, child status updates, invoice creation
- Error handling and edge cases
"""
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.payment_service import PaymentService
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.discount_service import DiscountCalculation, ApplicableDiscount
from apps.courses.models import LessonBundle
from apps.enrollments.models import LessonEnrollment


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class PaymentServiceInitiateSubscriptionTest(TestCase):
    """Test PaymentService.initiate_subscription_payment"""
    
    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson()
        
        # Mock DiscountService
        self.mock_discount_calculation = DiscountCalculation(
            applicable_discounts=[
                ApplicableDiscount(
                    discount_id=None,
                    name="הנחת ילד שני",
                    discount_type="second_child",
                    value=Decimal('50.00'),
                    reason="ילד שני במשפחה"
                )
            ],
            total_discount_amount=Decimal('50.00'),
            final_price=Decimal('300.00'),
            base_price=Decimal('350.00')
        )
    
    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_successful_payment_initiation(self, mock_discount, mock_tranzila):
        """Test successful payment initiation"""
        mock_discount.return_value = self.mock_discount_calculation
        mock_tranzila.return_value = "https://tranzila.test/payment"
        
        result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
            success_url='http://success',
            error_url='http://error',
            callback_url='http://callback'
        )
        
        # Verify result structure
        self.assertIn('payment_id', result)
        self.assertIn('tranzila_url', result)
        self.assertEqual(result['base_amount'], 350.00)
        self.assertEqual(result['discount_amount'], 50.00)
        self.assertEqual(result['registration_fee'], 120.00)
        self.assertAlmostEqual(result['final_amount'], result['prorated_amount'] + 120.00, places=2)
        self.assertGreater(result['prorated_amount'], 0)
        self.assertEqual(len(result['discounts_applied']), 1)
        
        # Verify Payment record created
        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.child, self.child)
        self.assertEqual(payment.lesson, self.lesson)
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.base_amount, Decimal('350.00'))
        self.assertEqual(payment.discount_amount, Decimal('50.00'))
        self.assertEqual(payment.registration_fee, Decimal('120.00'))
        self.assertEqual(payment.final_amount, Decimal(str(result['final_amount'])))
        
        # Verify discount snapshots created
        snapshots = payment.discount_snapshots.all()
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.first().discount_name, "הנחת ילד שני")

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_registration_fee_once_per_child(self, mock_discount, mock_tranzila):
        """A child pays דמי רישום on the first course only — extra courses skip it."""
        mock_discount.return_value = self.mock_discount_calculation
        mock_tranzila.return_value = "https://tranzila.test/payment"

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(first['registration_fee'], 120.00)

        other_lesson = TestDataFactory.create_lesson(course=self.lesson.course, day_of_week=2)
        second = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(other_lesson.id),
        )
        self.assertEqual(second['registration_fee'], 0.00)
        self.assertEqual(second['final_amount'], second['prorated_amount'])

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_later_signup_skips_fee_after_completed_payment(self, mock_discount, mock_tranzila):
        mock_discount.return_value = self.mock_discount_calculation
        mock_tranzila.return_value = "https://tranzila.test/payment"

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        Payment.objects.filter(id=first['payment_id']).update(status='completed')

        other_lesson = TestDataFactory.create_lesson(course=self.lesson.course, day_of_week=2)
        later = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(other_lesson.id),
        )
        self.assertEqual(later['registration_fee'], 0.00)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_failed_first_payment_still_charges_fee_on_next_course(self, mock_discount, mock_tranzila):
        mock_discount.return_value = self.mock_discount_calculation
        mock_tranzila.return_value = "https://tranzila.test/payment"

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        Payment.objects.filter(id=first['payment_id']).update(status='failed')

        other_lesson = TestDataFactory.create_lesson(course=self.lesson.course, day_of_week=2)
        second = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(other_lesson.id),
        )
        self.assertEqual(second['registration_fee'], 120.00)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_siblings_each_pay_registration_fee(self, mock_discount, mock_tranzila):
        mock_discount.return_value = self.mock_discount_calculation
        mock_tranzila.return_value = "https://tranzila.test/payment"
        sibling = TestDataFactory.create_child(family=self.child.family, first_name='Sibling')

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        other = self.service.initiate_subscription_payment(
            child_id=str(sibling.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(first['registration_fee'], 120.00)
        self.assertEqual(other['registration_fee'], 120.00)
    
    def test_child_not_found_error(self):
        """Test error when child doesn't exist"""
        with self.assertRaises(ValueError) as context:
            self.service.initiate_subscription_payment(
                child_id='00000000-0000-0000-0000-000000000000',
                lesson_id=str(self.lesson.id)
            )
        
        self.assertIn("Child or Lesson not found", str(context.exception))
    
    def test_lesson_not_found_error(self):
        """Test error when lesson doesn't exist"""
        with self.assertRaises(ValueError) as context:
            self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id='00000000-0000-0000-0000-000000000000'
            )
        
        self.assertIn("Child or Lesson not found", str(context.exception))
    
    # Note: test_no_price_configured_error removed because Course.price is NOT NULL
    # in the database, making this scenario impossible in production
    
    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_uses_lesson_price_override(self, mock_discount, mock_tranzila):
        """Test uses lesson price override when set"""
        lesson = TestDataFactory.create_lesson(
            price=Decimal('400.00')  # Override course price
        )
        
        mock_discount.return_value = DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=Decimal('400.00'),
            base_price=Decimal('400.00')
        )
        mock_tranzila.return_value = "https://tranzila.test/payment"
        
        result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(lesson.id)
        )
        
        self.assertEqual(result['base_amount'], 400.00)
        
        # Verify discount service called with correct price
        mock_discount.assert_called_once()
        call_args = mock_discount.call_args[1]
        self.assertEqual(call_args['base_price'], Decimal('400.00'))

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_first_signed_lesson_uses_regular_price(self, mock_discount, mock_tranzila):
        """A child with no active lessons pays the regular first-lesson price."""
        self.lesson.course.price = Decimal('350.00')
        self.lesson.course.save()
        self.lesson.additional_course_prices = [
            {'course_index': 2, 'price': 250},
            {'course_index': 3, 'price': 200},
        ]
        self.lesson.save()

        def passthrough_discount(**kwargs):
            return DiscountCalculation(
                applicable_discounts=[],
                total_discount_amount=Decimal('0.00'),
                final_price=kwargs['base_price'],
                base_price=kwargs['base_price'],
            )

        mock_discount.side_effect = passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )

        self.assertEqual(result['course_index'], 1)
        self.assertEqual(result['base_amount'], 350.00)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_second_and_third_signed_lessons_use_matching_tiers(self, mock_discount, mock_tranzila):
        """Existing signed lessons advance the child through the lesson price tiers."""
        course = self.lesson.course
        course.price = Decimal('350.00')
        course.save()
        self.lesson.additional_course_prices = [
            {'course_index': 2, 'price': 250},
            {'course_index': 3, 'price': 200},
        ]
        self.lesson.save()

        def passthrough_discount(**kwargs):
            return DiscountCalculation(
                applicable_discounts=[],
                total_discount_amount=Decimal('0.00'),
                final_price=kwargs['base_price'],
                base_price=kwargs['base_price'],
            )

        mock_discount.side_effect = passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        existing_lesson = TestDataFactory.create_lesson(course=course, branch=course.branch)
        LessonEnrollment.objects.create(
            child=self.child,
            lesson=existing_lesson,
            status='active',
        )

        second_result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(second_result['course_index'], 2)
        self.assertEqual(second_result['base_amount'], 250.00)

        payment_problem_lesson = TestDataFactory.create_lesson(course=course, branch=course.branch)
        LessonEnrollment.objects.create(
            child=self.child,
            lesson=payment_problem_lesson,
            status='payments_problem',
        )

        third_result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(third_result['course_index'], 3)
        self.assertEqual(third_result['base_amount'], 200.00)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_second_lesson_in_same_checkout_uses_tier_price(self, mock_discount, mock_tranzila):
        """A second lesson added in the same widget form is billed as course_index 2."""
        course = self.lesson.course
        course.price = Decimal('350.00')
        course.save()
        extra = TestDataFactory.create_lesson(course=course, branch=course.branch, day_of_week=3)
        extra.additional_course_prices = [{'course_index': 2, 'price': 250}]
        extra.save()

        def passthrough_discount(**kwargs):
            return DiscountCalculation(
                applicable_discounts=[],
                total_discount_amount=Decimal('0.00'),
                final_price=kwargs['base_price'],
                base_price=kwargs['base_price'],
            )

        mock_discount.side_effect = passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
        )
        self.assertEqual(first['course_index'], 1)

        second = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(extra.id),
        )
        self.assertEqual(second['course_index'], 2)
        self.assertEqual(second['base_amount'], 250.00)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class PaymentServiceLessonBundleTest(TestCase):
    """Bundle-aware pricing and capacity guard (see resolve_billing_price)."""

    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.course = TestDataFactory.create_course(price=Decimal('200.00'))
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)

        self.bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        self.bundle.lessons.set([self.lesson_a, self.lesson_b])

        def passthrough_discount(**kwargs):
            return DiscountCalculation(
                applicable_discounts=[],
                total_discount_amount=Decimal('0.00'),
                final_price=kwargs['base_price'],
                base_price=kwargs['base_price'],
            )
        self.passthrough_discount = passthrough_discount

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_bundle_bills_full_combined_price_on_first_lesson_only(self, mock_discount, mock_tranzila):
        mock_discount.side_effect = self.passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        result_a = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson_a.id),
            bundle_id=str(self.bundle.id),
        )
        self.assertEqual(result_a['base_amount'], 300.00)
        self.assertEqual(result_a['monthly_amount'], 300.00)
        self.assertEqual(result_a['bundle_id'], str(self.bundle.id))

        payment = Payment.objects.get(id=result_a['payment_id'])
        self.assertEqual(payment.bundle, self.bundle)
        self.assertEqual(payment.base_amount, Decimal('300.00'))

        result_b = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson_b.id),
            bundle_id=str(self.bundle.id),
            include_registration_fee=False,
            include_monthly_amount=False,
        )
        self.assertTrue(result_b.get('enrollment_only'))
        self.assertIsNone(result_b['payment_id'])
        self.assertEqual(Payment.objects.filter(child=self.child).count(), 1)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_bundle_charges_registration_fee_once(self, mock_discount, mock_tranzila):
        mock_discount.side_effect = self.passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        first = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson_a.id),
            bundle_id=str(self.bundle.id),
        )
        second = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson_b.id),
            bundle_id=str(self.bundle.id),
            include_registration_fee=False,
        )
        self.assertEqual(first['registration_fee'], 120.00)
        self.assertEqual(second['registration_fee'], 0.00)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_bundle_registration_rejected_when_a_member_lesson_is_full(self, mock_discount, mock_tranzila):
        mock_discount.side_effect = self.passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"

        self.lesson_b.room.capacity = 1
        self.lesson_b.room.save()
        other_child = TestDataFactory.create_child(first_name="אחר")
        LessonEnrollment.objects.create(child=other_child, lesson=self.lesson_b, status='active')

        with self.assertRaises(ValueError) as ctx:
            self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson_a.id),
                bundle_id=str(self.bundle.id),
            )
        self.assertIn('מלא', str(ctx.exception))
        self.assertFalse(Payment.objects.filter(child=self.child).exists())

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_inactive_must_attend_bundle_still_bills(self, mock_discount, mock_tranzila):
        mock_discount.side_effect = self.passthrough_discount
        mock_tranzila.return_value = "https://tranzila.test/payment"
        self.course.must_attend_all_lessons = True
        self.course.save(update_fields=['must_attend_all_lessons'])
        self.bundle.is_active = False
        self.bundle.save(update_fields=['is_active'])

        result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson_a.id),
            bundle_id=str(self.bundle.id),
        )
        self.assertEqual(result['base_amount'], 300.00)
        self.assertEqual(result['bundle_id'], str(self.bundle.id))

    def test_inactive_bundle_rejected_when_course_is_not_must_attend(self):
        self.bundle.is_active = False
        self.bundle.save(update_fields=['is_active'])

        with self.assertRaises(ValueError) as ctx:
            self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson_a.id),
                bundle_id=str(self.bundle.id),
            )
        self.assertIn('מסלול משולב', str(ctx.exception))


class PaymentServiceWebhookTest(TestCase):
    """Test PaymentService.process_webhook_callback"""
    
    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson()
        self.parent = TestDataFactory.create_parent(family=self.child.family)
        
        # Create pending payment
        self.payment = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            parent=self.parent,
            lesson=self.lesson,
            branch=self.lesson.course.branch,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('350.00'),
            description="מנוי חודשי"
        )
        
        self.success_webhook_payload = {
            'Response': '000',
            'TranzilaTK': 'test_token_123',
            'ConfirmationCode': 'ABC123',
            'sum': '350.00',
            'tranmode': 'V',
            'index': '1',
            'ccno': '4580****1234',
            'expmonth': '12',
            'expyear': '2027',
            'pdesc': str(self.payment.id),
        }
    
    @patch('apps.core.manychat_service.ManyChatService')
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_successful_webhook_processing(self, mock_verify, mock_parse, mock_manychat_cls):
        """Test successful webhook processing"""
        mock_verify.return_value = True
        mock_parse.return_value = {
            'transaction_id': 'TRX123',
            'confirmation_code': 'ABC123',
            'response_code': '000',
            'is_successful': True,
            'token': 'test_token_123',
            'card_expire_month': 12,
            'card_expire_year': 2027,
            'timestamp': timezone.now(),
            'raw_payload': self.success_webhook_payload
        }
        mock_manychat_cls.return_value.notify_registration.return_value = {'sent': True}
        
        result = self.service.process_webhook_callback(
            webhook_payload=self.success_webhook_payload,
            signature='test_signature'
        )
        
        self.assertTrue(result['success'])
        
        # Verify payment updated
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'completed')
        self.assertIsNotNone(self.payment.payment_date)
        
        # Verify RecurringPayment created
        recurring = RecurringPayment.objects.filter(child=self.child).first()
        self.assertIsNotNone(recurring)
        self.assertEqual(recurring.tranzila_token, 'test_token_123')
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.amount, Decimal('350.00'))
        
        # Verify TranzilaTransaction created
        transaction = TranzilaTransaction.objects.filter(transaction_id='TRX123').first()
        self.assertIsNotNone(transaction)
        self.assertTrue(transaction.is_successful)

        from apps.core.manychat_service import ManyChatService
        mock_manychat_cls.return_value.notify_registration.assert_called()
        call_kwargs = mock_manychat_cls.return_value.notify_registration.call_args.kwargs
        self.assertEqual(call_kwargs['kind'], ManyChatService.REGISTRATION_KIND_SUBSCRIPTION)
    
    @patch('apps.core.manychat_service.ManyChatService')
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_failed_webhook_sends_payment_failed_whatsapp(self, mock_verify, mock_parse, mock_manychat_cls):
        """Failed Tranzila webhook (Response != 000) triggers payment-failed WhatsApp."""
        mock_verify.return_value = True
        mock_parse.return_value = {
            'transaction_id': 'TRX456',
            'confirmation_code': '',
            'response_code': '033',
            'is_successful': False,
            'error_message': 'הכרטיס נדחה',
            'timestamp': timezone.now(),
            'raw_payload': {},
        }
        from apps.core.manychat_service import ManyChatService
        mock_manychat_cls.return_value.notify_registration.return_value = {
            'sent': True,
            'method': 'flow',
            'kind': 'payment_failed',
        }

        result = self.service.process_webhook_callback(
            webhook_payload={'pdesc': str(self.payment.id), 'Response': '033'},
            signature='test_signature',
        )

        self.assertFalse(result['success'])
        mock_manychat_cls.return_value.notify_registration.assert_called_once()
        call_kwargs = mock_manychat_cls.return_value.notify_registration.call_args.kwargs
        self.assertEqual(call_kwargs['kind'], ManyChatService.REGISTRATION_KIND_PAYMENT_FAILED)

    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_failed_webhook_processing(self, mock_verify, mock_parse):
        """Test failed payment webhook processing"""
        mock_verify.return_value = True
        mock_parse.return_value = {
            'transaction_id': 'TRX456',
            'confirmation_code': '',
            'response_code': '033',
            'is_successful': False,
            'error_message': 'Card declined',
            'timestamp': timezone.now(),
            'raw_payload': {}
        }
        
        result = self.service.process_webhook_callback(
            webhook_payload={'pdesc': str(self.payment.id), 'Response': '033'},
            signature='test_signature'
        )
        
        self.assertFalse(result['success'])  # Failed payment returns success=False
        
        # Verify payment marked as failed
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'failed')
        
        # Verify no RecurringPayment created
        self.assertEqual(RecurringPayment.objects.filter(child=self.child).count(), 0)
        
        # Verify child status updated to payment_problem
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'payment_problem')
    
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_invalid_signature_rejected(self, mock_verify):
        """Test webhook with invalid signature is rejected"""
        mock_verify.return_value = False
        
        result = self.service.process_webhook_callback(
            webhook_payload=self.success_webhook_payload,
            signature='invalid_signature'
        )
        
        self.assertFalse(result['success'])
        self.assertIn('Invalid signature', result['error'])
    
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_duplicate_webhook_detection(self, mock_verify, mock_parse):
        """Test duplicate webhook is detected and ignored"""
        mock_verify.return_value = True
        
        timestamp = timezone.now()
        mock_parse.return_value = {
            'transaction_id': 'TRX789',
            'confirmation_code': 'ABC789',
            'response_code': '000',
            'is_successful': True,
            'token': 'token_789',
            'timestamp': timestamp,
            'raw_payload': self.success_webhook_payload
        }
        
        # Process webhook first time
        result1 = self.service.process_webhook_callback(
            webhook_payload=self.success_webhook_payload,
            signature='test_signature'
        )
        self.assertTrue(result1['success'])
        
        # Process same webhook again (duplicate)
        result2 = self.service.process_webhook_callback(
            webhook_payload=self.success_webhook_payload,
            signature='test_signature'
        )
        
        self.assertTrue(result2['success'])
        self.assertIn('Already processed', result2['message'])
        
        # Verify only one transaction created
        self.assertEqual(TranzilaTransaction.objects.filter(transaction_id='TRX789').count(), 1)
    
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_payment_not_found_error(self, mock_verify, mock_parse):
        """Test error when payment not found for webhook"""
        mock_verify.return_value = True
        mock_parse.return_value = {
            'transaction_id': 'TRX999',
            'confirmation_code': '',
            'response_code': '000',
            'is_successful': True,
            'timestamp': timezone.now(),
            'raw_payload': {}
        }
        
        result = self.service.process_webhook_callback(
            webhook_payload={'pdesc': '00000000-0000-0000-0000-000000000000'},
            signature='test_signature'
        )
        
        self.assertFalse(result['success'])
        self.assertIn('Payment not found', result['error'])


@override_settings(REGISTRATION_FEE_ILS=120)
class DeferredSubscriptionStartTest(TestCase):
    """
    Registrations taken before the season starts charge דמי רישום only, and the monthly
    subscription is first billed on SUBSCRIPTION_FIRST_CHARGE_DATE.
    """

    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson()
        # Relative to today so the suite keeps testing deferral after the real date passes.
        today = date.today()
        self.start_date = (
            date(today.year + 1, 1, 1) if today.month == 12
            else date(today.year, today.month + 1, 1)
        )
        self.discount_calculation = DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=Decimal('260.00'),
            base_price=Decimal('260.00'),
        )

    def test_setting_stops_applying_once_the_date_arrives(self):
        from apps.core.payment_service import deferred_first_charge_date

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE='2026-09-01'):
            self.assertEqual(
                deferred_first_charge_date(date(2026, 8, 18)),
                date(2026, 9, 1),
            )
            self.assertIsNone(deferred_first_charge_date(date(2026, 9, 1)))
            self.assertIsNone(deferred_first_charge_date(date(2026, 9, 2)))

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=''):
            self.assertIsNone(deferred_first_charge_date(date(2026, 8, 18)))

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_signup_charges_registration_fee_only(self, mock_discount, mock_tranzila):
        mock_discount.return_value = self.discount_calculation
        mock_tranzila.return_value = 'https://tranzila.test/payment'

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=self.start_date.isoformat()):
            result = self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson.id),
            )

        self.assertEqual(result['prorated_amount'], 0.00)
        self.assertEqual(result['registration_fee'], 120.00)
        self.assertEqual(result['final_amount'], 120.00)
        self.assertEqual(result['monthly_amount'], 260.00)
        self.assertEqual(result['next_billing_date'], self.start_date.isoformat())
        self.assertEqual(result['subscription_start_date'], self.start_date.isoformat())

        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.final_amount, Decimal('120.00'))
        self.assertEqual(payment.registration_fee, Decimal('120.00'))
        self.assertEqual(payment.base_amount, Decimal('260.00'))
        self.assertTrue(payment.description.startswith('דמי רישום'))

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_immediate_standing_order_course_bills_custom_fee_and_starts_today(
        self, mock_discount, mock_tranzila,
    ):
        mock_discount.return_value = DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=Decimal('5.00'),
            base_price=Decimal('5.00'),
        )
        mock_tranzila.return_value = 'https://tranzila.test/payment'
        self.lesson.course.price = Decimal('5.00')
        self.lesson.course.registration_fee_override = Decimal('5.00')
        self.lesson.course.charge_standing_order_immediately = True
        self.lesson.course.save(update_fields=[
            'price', 'registration_fee_override', 'charge_standing_order_immediately',
        ])
        other = TestDataFactory.create_lesson()

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=self.start_date.isoformat()):
            immediate = self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson.id),
            )
            regular = self.service.initiate_subscription_payment(
                child_id=str(self.child.id),
                lesson_id=str(other.id),
            )

        today = date.today()
        self.assertEqual(immediate['registration_fee'], 5.00)
        self.assertEqual(immediate['final_amount'], 5.00)
        self.assertEqual(immediate['monthly_amount'], 5.00)
        self.assertEqual(immediate['next_billing_date'], today.isoformat())
        self.assertEqual(immediate['subscription_start_date'], today.isoformat())

        self.assertEqual(regular['registration_fee'], 0.00)
        self.assertEqual(regular['final_amount'], 0.00)
        self.assertEqual(regular['next_billing_date'], self.start_date.isoformat())
        self.assertEqual(regular['subscription_start_date'], self.start_date.isoformat())

    @patch('apps.core.payment_service.TranzilaService.charge_with_card')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_card_charge_bills_fee_now_and_starts_monthly_on_the_date(self, mock_discount, mock_charge):
        mock_discount.return_value = self.discount_calculation
        mock_charge.return_value = {
            'success': True,
            'transaction_id': 'TRX_FEE',
            'confirmation_code': 'CONF1',
            'token': 'card_token_1',
            'response_code': '000',
            'raw_response': {},
        }

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=self.start_date.isoformat()):
            result = self.service.charge_subscription_with_card(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson.id),
                card_number='4580458045804580',
                expiry_month=12,
                expiry_year=2030,
                cvv='123',
                card_holder_id='123456782',
            )

        self.assertTrue(result['success'])
        self.assertEqual(mock_charge.call_args.kwargs['amount'], Decimal('120.00'))

        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.final_amount, Decimal('120.00'))

        recurring = RecurringPayment.objects.get(child=self.child)
        self.assertEqual(recurring.amount, Decimal('260.00'))
        self.assertEqual(recurring.next_billing_date, self.start_date)

        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')
        self.assertEqual(self.child.subscription_start_date, self.start_date)
        self.assertIsNone(self.child.paid_until_date)
        self.assertTrue(
            LessonEnrollment.objects.filter(
                child=self.child, lesson=self.lesson, status='active'
            ).exists()
        )

    @patch('apps.core.payment_service.TranzilaService.verify_card')
    @patch('apps.core.payment_service.TranzilaService.charge_with_card')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_bundle_member_without_fee_only_verifies_the_card(self, mock_discount, mock_charge, mock_verify):
        mock_discount.return_value = self.discount_calculation
        mock_verify.return_value = {
            'success': True,
            'transaction_id': 'TRX_VERIFY',
            'confirmation_code': 'CONF2',
            'token': 'card_token_2',
            'response_code': '000',
            'raw_response': {},
        }

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=self.start_date.isoformat()):
            result = self.service.charge_subscription_with_card(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson.id),
                card_number='4580458045804580',
                expiry_month=12,
                expiry_year=2030,
                cvv='123',
                card_holder_id='123456782',
                include_registration_fee=False,
            )

        self.assertTrue(result['success'])
        mock_charge.assert_not_called()
        mock_verify.assert_called_once()
        self.assertIsNone(result['invoice_number'])

        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.final_amount, Decimal('0.00'))
        self.assertFalse(payment.invoices.exists())

        recurring = RecurringPayment.objects.get(child=self.child)
        self.assertEqual(recurring.amount, Decimal('260.00'))
        self.assertEqual(recurring.next_billing_date, self.start_date)

    @patch('apps.core.payment_service.TranzilaService.verify_card')
    @patch('apps.core.payment_service.TranzilaService.charge_with_card')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_zero_amount_reuses_existing_token_without_verify(self, mock_discount, mock_charge, mock_verify):
        """Second bundle day is ₪0; do not call Tranzila verify if a token already exists."""
        mock_discount.return_value = self.discount_calculation
        RecurringPayment.objects.create(
            child=self.child,
            tranzila_token='already-saved-token',
            card_expire_month=12,
            card_expire_year=2030,
            status='active',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            amount=Decimal('260.00'),
            billing_day=1,
            start_date=date.today(),
            next_billing_date=self.start_date,
        )

        with override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE=self.start_date.isoformat()):
            result = self.service.charge_subscription_with_card(
                child_id=str(self.child.id),
                lesson_id=str(self.lesson.id),
                card_number='4580458045804580',
                expiry_month=12,
                expiry_year=2030,
                cvv='123',
                card_holder_id='123456782',
                include_registration_fee=False,
            )

        self.assertTrue(result['success'])
        mock_charge.assert_not_called()
        mock_verify.assert_not_called()
        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.final_amount, Decimal('0.00'))
        self.assertEqual(
            RecurringPayment.objects.filter(initial_payment=payment).get().tranzila_token,
            'already-saved-token',
        )

    def test_first_monthly_charge_bills_the_full_month(self):
        """On the start date the recurring cron charges the plain monthly price."""
        from apps.customers.recurring_billing import process_due_recurring_charges

        payment = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.lesson.course.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('120.00'),
            registration_fee=Decimal('120.00'),
            description='דמי רישום',
        )
        today = date.today()
        recurring = RecurringPayment.objects.create(
            child=self.child,
            initial_payment=payment,
            tranzila_token='card_token_1',
            card_expire_month=12,
            card_expire_year=2030,
            status='active',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            amount=Decimal('260.00'),
            billing_day=1,
            start_date=today,
            next_billing_date=today,
        )

        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token') as mock_token_charge, \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'):
            mock_token_charge.return_value = {
                'success': True,
                'transaction_id': 'TRX_MONTH',
                'confirmation_code': 'CONF3',
                'response_code': '000',
                'raw_response': {},
            }
            summary = process_due_recurring_charges()

        self.assertEqual(summary['charged'], 1)
        self.assertEqual(mock_token_charge.call_args.kwargs['amount'], Decimal('260.00'))

        monthly = Payment.objects.filter(child=self.child, payment_type='recurring_subscription').exclude(id=payment.id).get()
        self.assertEqual(monthly.final_amount, Decimal('260.00'))
        self.assertEqual(monthly.registration_fee, Decimal('0.00'))

        recurring.refresh_from_db()
        self.assertGreater(recurring.next_billing_date, today)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')
        self.assertIsNotNone(self.child.paid_until_date)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class PaymentServiceIntegrationTest(TestCase):
    """Integration tests for PaymentService end-to-end flows"""
    
    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_full_subscription_payment_flow(self, mock_verify, mock_parse, mock_tranzila):
        """Test complete subscription payment flow from initiation to completion"""
        service = PaymentService()
        child = TestDataFactory.create_child(status='pending')
        lesson = TestDataFactory.create_lesson()
        
        # Mock Tranzila URL generation
        mock_tranzila.return_value = "https://tranzila.test/payment"
        
        # Step 1: Initiate payment
        result = service.initiate_subscription_payment(
            child_id=str(child.id),
            lesson_id=str(lesson.id),
            callback_url='http://callback'
        )
        
        payment_id = result['payment_id']
        self.assertIsNotNone(payment_id)
        
        # Verify payment created
        payment = Payment.objects.get(id=payment_id)
        self.assertEqual(payment.status, 'pending')
        
        # Step 2: Process successful webhook
        mock_verify.return_value = True
        mock_parse.return_value = {
            'transaction_id': 'TRX_INTEGRATION',
            'confirmation_code': 'CONF123',
            'response_code': '000',
            'is_successful': True,
            'token': 'recurring_token_123',
            'card_expire_month': 12,
            'card_expire_year': 2027,
            'timestamp': timezone.now(),
            'raw_payload': {'pdesc': payment_id}
        }
        
        webhook_result = service.process_webhook_callback(
            webhook_payload={'pdesc': payment_id, 'Response': '000'},
            signature='test_sig'
        )
        
        self.assertTrue(webhook_result['success'])
        
        # Step 3: Verify final state
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')
        
        # Verify recurring payment created
        recurring = RecurringPayment.objects.filter(child=child).first()
        self.assertIsNotNone(recurring)
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.tranzila_token, 'recurring_token_123')


class PaymentRefundTokenTest(TestCase):
    """Refunds of monthly standing-order charges must use that course's card."""

    def setUp(self):
        self.service = PaymentService()
        self.child = TestDataFactory.create_child()
        self.lesson_a = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='חוג א'),
        )
        self.lesson_b = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='חוג ב', branch=self.lesson_a.course.branch),
        )
        self.signup_a = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.lesson_a.course.branch,
            lesson=self.lesson_a,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('200.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('200.00'),
            description='מנוי חודשי - חוג א',
        )
        self.signup_b = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.lesson_b.course.branch,
            lesson=self.lesson_b,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('235.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('235.00'),
            description='מנוי חודשי - חוג ב',
        )
        self.sto_a = RecurringPayment.objects.create(
            child=self.child,
            initial_payment=self.signup_a,
            tranzila_token='token_course_a',
            card_expire_month=1,
            card_expire_year=2030,
            status='active',
            amount=Decimal('200.00'),
            billing_day=1,
            start_date=date.today(),
        )
        self.sto_b = RecurringPayment.objects.create(
            child=self.child,
            initial_payment=self.signup_b,
            tranzila_token='token_course_b',
            card_expire_month=6,
            card_expire_year=2031,
            status='active',
            amount=Decimal('235.00'),
            billing_day=1,
            start_date=date.today(),
        )
        self.monthly_b = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.lesson_b.course.branch,
            lesson=self.lesson_b,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('235.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('235.00'),
            description='מנוי חודשי - חוג ב - ילד',
        )
        txn = TranzilaTransaction.objects.create(
            transaction_id='TRX_MONTH_B',
            confirmation_code='AUTH_B',
            transaction_type='recurring_charge',
            is_successful=True,
            idempotency_key='refund-token-test-month-b',
        )
        self.monthly_b.tranzila_transaction = txn
        self.monthly_b.save(update_fields=['tranzila_transaction'])

    def test_monthly_charge_uses_same_lesson_token(self):
        from apps.core.payment_service import card_details_for_payment_refund

        month, year, token = card_details_for_payment_refund(self.monthly_b)
        self.assertEqual(token, 'token_course_b')
        self.assertEqual(month, 6)
        self.assertEqual(year, 2031)

    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction')
    def test_refund_payment_sends_matching_token(self, mock_refund):
        mock_refund.return_value = {
            'success': True,
            'transaction_id': 'REFUND_1',
            'confirmation_code': 'R1',
            'response_code': '000',
            'message': 'ok',
            'raw_response': {},
        }

        result = self.service.refund_payment(str(self.monthly_b.id), reason='בדיקה')

        self.assertTrue(result['success'])
        mock_refund.assert_called_once()
        kwargs = mock_refund.call_args.kwargs
        self.assertEqual(kwargs['token'], 'token_course_b')
        self.assertEqual(kwargs['card_expire_month'], 6)
        self.assertEqual(kwargs['transaction_id'], 'TRX_MONTH_B')
        self.monthly_b.refresh_from_db()
        self.assertEqual(self.monthly_b.status, 'refunded')

    def test_cancelled_sto_without_lesson_still_uses_card(self):
        from apps.core.payment_service import card_details_for_payment_refund

        self.sto_a.status = 'cancelled'
        self.sto_a.save(update_fields=['status'])
        self.sto_b.status = 'cancelled'
        self.sto_b.save(update_fields=['status'])
        self.sto_b.save(update_fields=['updated_at'])
        self.monthly_b.lesson = None
        self.monthly_b.save(update_fields=['lesson'])

        month, year, token = card_details_for_payment_refund(self.monthly_b)
        self.assertEqual(token, 'token_course_b')
        self.assertEqual(month, 6)
        self.assertEqual(year, 2031)

    def test_card_from_tranzila_payload_when_no_standing_order(self):
        from apps.core.payment_service import card_details_for_payment_refund

        self.child.recurring_payments.all().delete()
        self.monthly_b.lesson = None
        self.monthly_b.save(update_fields=['lesson'])
        txn = self.monthly_b.tranzila_transaction
        txn.response_data = {
            'original_request': {
                'expire_month': 2,
                'expire_year': 2032,
                'card_number': 'Y0payloadtoken',
            },
            'transaction_result': {
                'token': 'Y0payloadtoken',
                'expiry_month': '02',
                'expiry_year': '32',
            },
        }
        txn.save(update_fields=['response_data'])

        month, year, token = card_details_for_payment_refund(self.monthly_b)
        self.assertEqual(token, 'Y0payloadtoken')
        self.assertEqual(month, 2)
        self.assertEqual(year, 2032)
