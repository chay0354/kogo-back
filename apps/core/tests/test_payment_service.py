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
from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.payment_service import PaymentService
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.discount_service import DiscountCalculation, ApplicableDiscount
from apps.enrollments.models import LessonEnrollment


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
        self.assertEqual(result['final_amount'], 300.00)
        self.assertEqual(len(result['discounts_applied']), 1)
        
        # Verify Payment record created
        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.child, self.child)
        self.assertEqual(payment.lesson, self.lesson)
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.base_amount, Decimal('350.00'))
        self.assertEqual(payment.discount_amount, Decimal('50.00'))
        self.assertEqual(payment.final_amount, Decimal('300.00'))
        
        # Verify discount snapshots created
        snapshots = payment.discount_snapshots.all()
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.first().discount_name, "הנחת ילד שני")
    
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
    
    @patch('apps.core.payment_service.TranzilaService.parse_webhook_response')
    @patch('apps.core.payment_service.TranzilaService.verify_webhook_signature')
    def test_successful_webhook_processing(self, mock_verify, mock_parse):
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
        
        # Verify TranzilaTransaction created
        transaction = TranzilaTransaction.objects.filter(transaction_id='TRX123').first()
        self.assertIsNotNone(transaction)
        self.assertTrue(transaction.is_successful)
    
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
