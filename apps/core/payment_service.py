"""
Payment Service - Business Logic for Payment Processing

This service orchestrates the payment flow, including:
- Discount calculation
- Payment initiation
- Webhook processing
- Invoice creation
- Child subscription status updates
"""
import logging
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from apps.customers.models import (
    Child, Family, Parent, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot
)
from apps.customers.financial_models import Invoice, InvoiceChild, Discount
from apps.customers.discount_service import DiscountService
from apps.core.tranzila_service import TranzilaService
from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment
from apps.instructors.utils import get_lesson_price_for_course_index
from apps.store.stock_utils import (
    decrement_product_stock as _decrement_product_stock,
    restore_stock_for_sale as _restore_stock_for_sale,
)

logger = logging.getLogger(__name__)

BILLING_ENROLLMENT_STATUSES = ("active", "payments_problem")


def log_payment_operation(operation: str, **kwargs):
    """Centralized logging for payment operations."""
    log_parts = [f"[{operation}]"]
    for key, value in kwargs.items():
        log_parts.append(f"{key}={value}")
    logger.info(" ".join(log_parts))


def get_child_lesson_index_for_billing(child: Child, lesson: Lesson) -> int:
    """
    Return the 1-based lesson number this lesson will be for the child.

    Existing active/payment-problem enrollments still count as signed lessons.
    The selected lesson is excluded so re-opening payment for the same lesson
    does not incorrectly move the child into the next price tier.
    """
    signed_lessons_count = LessonEnrollment.objects.filter(
        child=child,
        status__in=BILLING_ENROLLMENT_STATUSES,
    ).exclude(lesson=lesson).count()
    return signed_lessons_count + 1


class PaymentService:
    """
    Service for managing payment operations and business logic.
    
    Coordinates between:
    - DiscountService (for calculating discounts)
    - TranzilaService (for payment gateway integration)
    - Database models (for persisting payment data)
    """
    
    def __init__(self):
        self.discount_service = DiscountService()
        self.tranzila_service = TranzilaService()
    
    def initiate_subscription_payment(
        self,
        child_id: str,
        lesson_id: str,
        payment_date: Optional[date] = None,
        success_url: str = '',
        error_url: str = '',
        callback_url: str = ''
    ) -> Dict:
        """
        Initiate a recurring subscription payment for a child's lesson enrollment.
        
        Flow:
        1. Validate child and lesson
        2. Get lesson pricing
        3. Calculate discounts
        4. Create Payment record (pending)
        5. Generate Tranzila payment URL
        6. Return payment details for frontend
        
        Args:
            child_id: UUID of child
            lesson_id: UUID of lesson
            payment_date: Date of payment (default: today)
            success_url: URL to redirect on success
            error_url: URL to redirect on error
            callback_url: Webhook callback URL
            
        Returns:
            Dict with payment_id, tranzila_url, amount, discounts_applied
        """
        if payment_date is None:
            payment_date = date.today()
        
        try:
            child = Child.objects.select_related('family').get(id=child_id)
            lesson = Lesson.objects.select_related('course', 'branch').get(id=lesson_id)
        except (Child.DoesNotExist, Lesson.DoesNotExist) as e:
            logger.error(f"Child or Lesson not found: {e}")
            raise ValueError("Child or Lesson not found")
        
        # Determine which lesson number this is for the child:
        # 1 = first signed lesson, 2 = second, 3 = third, etc.
        course_index = get_child_lesson_index_for_billing(child, lesson)

        # Lesson price: prefer the matching tier in `additional_course_prices`,
        # then fall back to lesson.price or course.price.
        tier_price = get_lesson_price_for_course_index(lesson, course_index)
        regular_price = lesson.price or lesson.course.price
        base_price = tier_price if tier_price and tier_price > 0 else regular_price
        if not base_price:
            raise ValueError("Lesson/Course price not configured")

        used_lesson_tier = (
            course_index >= 2
            and tier_price is not None
            and Decimal(str(tier_price)) != Decimal(str(regular_price or 0))
        )

        # Calculate discounts. If a per-lesson tier already kicked in for this
        # course-index, skip the global "additional_lesson" discount so the
        # price isn't reduced twice.
        if used_lesson_tier:
            discount_calculation = self.discount_service.evaluate_discounts_for_payment(
                family_id=str(child.family.id),
                child_id=str(child.id),
                payment_date=payment_date,
                base_price=base_price,
                lesson_id=None,
            )
        else:
            discount_calculation = self.discount_service.evaluate_discounts_for_payment(
                family_id=str(child.family.id),
                child_id=str(child.id),
                payment_date=payment_date,
                base_price=base_price,
                lesson_id=str(lesson.id),
            )

        # Create Payment record (pending) with retry (SQLite can throw "database is locked" under concurrency).
        payment = None
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                with transaction.atomic():
                    payment = Payment.objects.create(
                        child=child,
                        family=child.family,
                        parent=child.family.parents.filter(is_primary=True).first(),
                        branch=lesson.branch,
                        lesson=lesson,
                        payment_type='recurring_subscription',
                        status='pending',
                        base_amount=discount_calculation.base_price,
                        discount_amount=discount_calculation.total_discount_amount,
                        final_amount=discount_calculation.final_price,
                        description=f"מנוי חודשי - {lesson.course.name} - {child.full_name}"
                    )
                    
                    # Create discount snapshots
                    for applied_discount in discount_calculation.applicable_discounts:
                        discount_kwargs = {
                            'payment': payment,
                            'discount_name': applied_discount.name,
                            'discount_type': applied_discount.discount_type,
                            'discount_value': applied_discount.value,
                            'amount_deducted': applied_discount.value,
                            'reason': applied_discount.reason
                        }
                        
                        # Add discount FK if we can resolve it
                        if applied_discount.discount_id:
                            discount_kwargs['discount_id'] = applied_discount.discount_id
                        
                        PaymentDiscountSnapshot.objects.create(**discount_kwargs)

                break
            except OperationalError as e:
                msg = str(e).lower()
                if "database is locked" in msg and attempt < max_attempts:
                    sleep_s = 0.2 * attempt  # simple backoff
                    logger.warning(f"SQLite database is locked; retrying payment create (attempt {attempt}/{max_attempts}) after {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                raise

        if payment is None:
            raise RuntimeError("Failed to create payment record")
        
        # Generate Tranzila payment URL
        tranzila_url= self.tranzila_service.create_recurring_payment_request(
            amount=discount_calculation.final_price,
            currency='ILS',
            description=payment.description,
            customer_name=child.family.name,
            customer_email=child.family.email,
            customer_phone=child.family.phone,
            success_url=success_url,
            error_url=error_url,
            callback_url=callback_url,
            transaction_id=str(payment.id),
        )
        
        log_payment_operation(
            "SUBSCRIPTION_INITIATED",
            child=child.full_name,
            payment_id=payment.id,
            amount=discount_calculation.final_price
        )
        
        return {
            'payment_id': str(payment.id),
            'tranzila_url': tranzila_url,
            'course_index': course_index,
            'base_amount': float(discount_calculation.base_price),
            'discount_amount': float(discount_calculation.total_discount_amount),
            'final_amount': float(discount_calculation.final_price),
            'discounts_applied': [
                {
                    'name': d.name,
                    'type': d.discount_type,
                    'value': float(d.value),
                    'reason': d.reason
                }
                for d in discount_calculation.applicable_discounts
            ],
            'lesson': {
                'id': str(lesson.id),
                'name': lesson.course.name,
                'day_of_week': lesson.get_day_of_week_display(),
                'time': lesson.start_time.strftime('%H:%M')
            }
        }
    
    @transaction.atomic
    def process_webhook_callback(
        self,
        webhook_payload: Dict,
        signature: Optional[str] = None
    ) -> Dict:
        """
        Process a Tranzila webhook callback.
        
        Flow:
        1. Verify webhook signature
        2. Check idempotency (prevent duplicate processing)
        3. Parse transaction result
        4. On success:
           - Update Payment status
           - Create/update RecurringPayment
           - Create Invoice
           - Update Child status
           - Create LessonEnrollment if needed
        5. On failure:
           - Update Payment status
           - Store failure reason
        
        Args:
            webhook_payload: Raw webhook data from Tranzila
            signature: Webhook signature for verification
            
        Returns:
            Dict with processing result
        """
        # Verify signature
        if signature and not self.tranzila_service.verify_webhook_signature(webhook_payload, signature):
            logger.error("Invalid webhook signature")
            return {'success': False, 'error': 'Invalid signature'}
        
        # Parse webhook response
        parsed_response = self.tranzila_service.parse_webhook_response(webhook_payload)
        
        # Check idempotency
        idempotency_key = f"tranzila_{parsed_response['transaction_id']}_{parsed_response['timestamp'].isoformat()}"
        
        if TranzilaTransaction.objects.filter(idempotency_key=idempotency_key).exists():
            logger.warning(f"Duplicate webhook received: {idempotency_key}")
            return {'success': True, 'message': 'Already processed'}
        
        # Create TranzilaTransaction record
        tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id=parsed_response['transaction_id'],
            confirmation_code=parsed_response['confirmation_code'],
            transaction_type='recurring_setup' if parsed_response.get('token') else 'charge',
            response_code=parsed_response['response_code'],
            response_message=parsed_response.get('error_message', ''),
            request_data={},
            response_data=parsed_response['raw_payload'],
            idempotency_key=idempotency_key,
            is_successful=parsed_response['is_successful'],
            response_timestamp=parsed_response['timestamp']
        )
        
        # Find associated Payment record (using transaction_id which should be payment.id)
        payment_id = webhook_payload.get('pdesc', '')  # We sent payment.id as pdesc
        
        try:
            payment = Payment.objects.select_related('child', 'family').get(id=payment_id)
        except Payment.DoesNotExist:
            logger.error(f"Payment not found for webhook: {payment_id}")
            return {'success': False, 'error': 'Payment not found'}
        
        # Link transaction to payment
        payment.tranzila_transaction = tranzila_transaction
        
        if parsed_response['is_successful']:
            # SUCCESS FLOW
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save()
            
            # Create/update RecurringPayment if this is a subscription
            if payment.payment_type == 'recurring_subscription' and parsed_response.get('token'):
                # Build discount details from payment discount snapshots
                discount_details = []
                for snapshot in payment.discount_snapshots.all():
                    discount_details.append({
                        'name': snapshot.discount_name,
                        'type': snapshot.discount_type,
                        'value': str(snapshot.discount_value),
                        'amount_deducted': str(snapshot.amount_deducted),
                        'reason': snapshot.reason
                    })
                
                recurring_payment = RecurringPayment.objects.create(
                    child=payment.child,
                    initial_payment=payment,
                    tranzila_token=parsed_response['token'],
                    card_expire_month=parsed_response.get('card_expire_month'),
                    card_expire_year=parsed_response.get('card_expire_year'),
                    status='active',
                    base_amount=payment.base_amount,
                    discount_amount=payment.discount_amount,
                    amount=payment.final_amount,
                    discount_details=discount_details,
                    billing_day=date.today().day,
                    start_date=date.today(),
                    next_billing_date=date.today() + timedelta(days=30)
                )
                
                log_payment_operation(
                    "RECURRING_CREATED",
                    recurring_id=recurring_payment.id,
                    child_id=payment.child.id,
                    base_amount=payment.base_amount,
                    discount_amount=payment.discount_amount,
                    final_amount=payment.final_amount
                )
            
            # Create Invoice
            invoice = self._create_invoice_from_payment(payment, tranzila_transaction)
            
            # Update Child status and subscription dates
            child = payment.child
            child.status = 'active'
            child.subscription_start_date = date.today()
            child.paid_until_date = date.today() + timedelta(days=30)
            child.save()
            
            # Create LessonEnrollment if payment has an associated lesson
            if payment.lesson:
                enrollment, created = LessonEnrollment.objects.get_or_create(
                    child=child,
                    lesson=payment.lesson,
                    defaults={
                        'start_date': date.today(),
                        'status': 'active'
                    }
                )
                if created:
                    logger.info(f"Created LessonEnrollment: {enrollment.id} for child {child.id} and lesson {payment.lesson.id}")
                else:
                    # Update existing enrollment to active
                    enrollment.status = 'active'
                    if not enrollment.start_date:
                        enrollment.start_date = date.today()
                    enrollment.save()
                    logger.info(f"Updated existing LessonEnrollment: {enrollment.id}")
            else:
                logger.warning(f"Payment {payment.id} has no associated lesson - skipping enrollment creation")
            
            logger.info(f"Successfully processed payment webhook: {payment.id}")
            
            return {
                'success': True,
                'payment_id': str(payment.id),
                'invoice_id': str(invoice.id),
                'message': 'Payment processed successfully'
            }
        
        else:
            # FAILURE FLOW
            payment.status = 'failed'
            payment.failure_reason = parsed_response.get('error_message', 'Unknown error')
            payment.failure_code = parsed_response['response_code']
            payment.save()
            
            # Update child status to 'payment_problem' (בעיות באשראי)
            child = payment.child
            child.status = 'payment_problem'
            child.save()
            
            logger.warning(f"Payment failed: {payment.id}, reason: {payment.failure_reason}. Child {child.id} status updated to 'payment_problem'")
            
            return {
                'success': False,
                'payment_id': str(payment.id),
                'error': payment.failure_reason
            }
    
    def _create_invoice_from_payment(
        self,
        payment: Payment,
        tranzila_transaction: TranzilaTransaction
    ) -> Invoice:
        """
        Create an Invoice record from a completed Payment.
        
        Args:
            payment: Completed Payment object
            tranzila_transaction: Associated TranzilaTransaction
            
        Returns:
            Created Invoice object
        """
        # Generate invoice number
        invoice_number = f"INV-{timezone.now().strftime('%Y%m%d')}-{payment.id.hex[:8].upper()}"
        
        invoice = Invoice.objects.create(
            invoice_number=invoice_number,
            family=payment.family,
            parent=payment.parent,
            branch=payment.branch,
            payment=payment,
            amount=payment.final_amount,
            status='paid',
            payment_method='credit_card',
            payment_type='recurring' if payment.payment_type == 'recurring_subscription' else 'one_time',
            payer_name=payment.family.name,
            payer_email=payment.family.email,
            payer_phone=payment.family.phone,
            tranzila_transaction_id=tranzila_transaction.transaction_id,
            invoice_date=timezone.now()
        )
        
        # Link the child to the invoice with lesson/product details
        if payment.child:
            invoice_child = InvoiceChild.objects.create(
                invoice=invoice,
                child=payment.child,
                course=payment.lesson.course if payment.lesson else None,
                lesson=payment.lesson,
                product=getattr(payment, 'product', None)  # Use getattr for safer access
            )
            
            item_desc = ""
            if payment.lesson:
                item_desc = f"lesson: {payment.lesson.course.name if payment.lesson.course else 'N/A'}"
            elif getattr(payment, 'product', None):
                item_desc = f"product: {payment.product.name}"
            else:
                item_desc = "general payment"
            
            logger.info(f"Linked child {payment.child.full_name} to invoice {invoice.invoice_number} ({item_desc})")
        
        logger.info(f"Created invoice: {invoice.invoice_number}")
        
        return invoice
    
    def cancel_subscription(
        self,
        recurring_payment_id: str,
        cancellation_reason: str = ''
    ) -> Dict:
        """
        Cancel a recurring subscription.
        
        TODO: Needs to be implemented with https://api.tranzila.com/v2/sto/update
        as mentioned in Tranzila documentation.
        
        The implementation should:
        1. Use the STO (Secure Token Object) update endpoint
        2. Update the token status to cancelled/inactive
        3. Update the RecurringPayment status in the database
        
        Args:
            recurring_payment_id: UUID of RecurringPayment
            cancellation_reason: Reason for cancellation
            
        Returns:
            Dict with cancellation result
        """
        try:
            recurring_payment = RecurringPayment.objects.select_related('child').get(
                id=recurring_payment_id
            )
        except RecurringPayment.DoesNotExist:
            raise ValueError("Recurring payment not found")
        
        # TODO: Implement cancellation using Tranzila v2 STO API
        # Call: https://api.tranzila.com/v2/sto/update
        # Required parameters:
        # - terminal_name
        # - token (recurring_payment.tranzila_token)
        # - status: "cancelled" or appropriate status
        
        logger.warning(f"Cancel subscription not yet implemented for recurring payment: {recurring_payment.id}")
        
        return {
            'success': False,
            'error': 'Cancel subscription feature needs to be implemented using Tranzila v2 STO API'
        }
    
    def get_payment_status(self, payment_id: str) -> Dict:
        """
        Get the current status of a payment.
        
        Args:
            payment_id: UUID of Payment
            
        Returns:
            Dict with payment status details
        """
        try:
            payment = Payment.objects.select_related(
                'child', 'tranzila_transaction'
            ).prefetch_related('discount_snapshots').get(id=payment_id)
        except Payment.DoesNotExist:
            raise ValueError("Payment not found")
        
        return {
            'payment_id': str(payment.id),
            'status': payment.status,
            'payment_type': payment.payment_type,
            'base_amount': float(payment.base_amount),
            'discount_amount': float(payment.discount_amount),
            'final_amount': float(payment.final_amount),
            'payment_date': payment.payment_date.isoformat() if payment.payment_date else None,
            'child': {
                'id': str(payment.child.id),
                'name': payment.child.full_name
            },
            'discounts_applied': [
                {
                    'name': snapshot.discount_name,
                    'amount': float(snapshot.amount_deducted)
                }
                for snapshot in payment.discount_snapshots.all()
            ],
            'transaction': {
                'id': payment.tranzila_transaction.transaction_id,
                'confirmation_code': payment.tranzila_transaction.confirmation_code
            } if payment.tranzila_transaction else None
        }
    
    # ============================================================================
    # Store Payment Methods - Token-based charging with iframe fallback
    # ============================================================================
    
    def initiate_store_purchase(
        self,
        product_items: list,
        child_id: Optional[str] = None,
        customer_info: Optional[dict] = None,
        callback_url: str = ''
    ) -> Dict:
        """
        Initiate store purchase with smart payment routing.
        
        Routes to appropriate payment method:
        - Child WITH stored token → Direct API charge (synchronous)
        - Child WITHOUT token → Tranzila iframe (webhook callback)
        - Walk-in customer → Tranzila iframe
        
        Args:
            product_items: List of dicts with {product_id, quantity, size}
            child_id: UUID of child (optional for walk-in)
            customer_info: Dict with {name, phone} for walk-in customers
            callback_url: Webhook callback URL for iframe payments
            
        Returns:
            Dict with either:
            - {requires_iframe: False, invoice: obj, success: bool} for token charge
            - {requires_iframe: True, iframe_url: str, invoice_id: uuid} for iframe
        """
        from apps.store.models import StoreProduct, StoreInvoice, StoreSale
        
        # Calculate total from product items
        total_amount = Decimal('0.00')
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            total_amount += product.sale_price * item['quantity']
        
        # Check for stored token if child is registered
        if child_id:
            try:
                child = Child.objects.get(id=child_id)
                
                # Look for active recurring payment with token
                recurring = RecurringPayment.objects.filter(
                    child=child,
                    status='active',
                    tranzila_token__isnull=False
                ).exclude(tranzila_token='').first()
                
                if recurring and recurring.tranzila_token:
                    # SYNCHRONOUS TOKEN CHARGE
                    log_payment_operation("STORE_TOKEN_CHARGE", child=child.full_name, amount=total_amount)
                    
                    # Create invoice (pending)
                    invoice = StoreInvoice.objects.create(
                        child=child,
                        total_amount=total_amount,
                        payment_method='credit_card',
                        payment_status='pending',
                        charged_with_token=True,
                        branch=product_items[0].get('branch') if product_items else None
                    )
                    
                    # Charge token and complete purchase
                    result = self.charge_store_with_token(
                        token=recurring.tranzila_token,
                        invoice=invoice,
                        product_items=product_items,
                        recurring_payment=recurring
                    )
                    
                    # Serialize invoice for response
                    from apps.store.serializers import StoreInvoiceSerializer
                    invoice_data = StoreInvoiceSerializer(invoice).data
                    
                    return {
                        'requires_iframe': False,
                        'invoice': invoice_data,
                        'success': result['success'],
                        'error': result.get('error')
                    }
            except Child.DoesNotExist:
                logger.warning(f"Child not found: {child_id}")
                child_id = None  # Fall through to iframe
        
        # IFRAME FALLBACK (no token or walk-in customer)
        logger.info("No token found or walk-in customer, using iframe")
        
        invoice = StoreInvoice.objects.create(
            child_id=child_id if child_id else None,
            customer_name=customer_info.get('name', '') if customer_info else '',
            customer_phone=customer_info.get('phone', '') if customer_info else '',
            total_amount=total_amount,
            payment_method='credit_card',
            payment_status='pending',
            charged_with_token=False
        )
        
        # Generate Tranzila iframe URL
        customer_name = ''
        customer_email = ''
        customer_phone = ''
        
        if child_id:
            try:
                child = Child.objects.select_related('family').get(id=child_id)
                customer_name = child.family.name
                customer_email = child.family.email
                customer_phone = child.family.phone
            except Child.DoesNotExist:
                pass
        elif customer_info:
            customer_name = customer_info.get('name', '')
            customer_phone = customer_info.get('phone', '')
        
        iframe_url = self.tranzila_service.create_payment_request(
            amount=total_amount,
            currency='ILS',
            description=f"Store purchase - Invoice {invoice.invoice_number}",
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            callback_url=callback_url,
            transaction_id=str(invoice.id)
        )
        
        # Store product items in invoice notes for webhook processing
        import json
        invoice.notes = json.dumps(product_items)
        invoice.save()
        
        return {
            'requires_iframe': True,
            'iframe_url': iframe_url,
            'invoice_id': str(invoice.id)
        }
    
    def charge_store_with_token(
        self,
        token: str,
        invoice,
        product_items: list,
        recurring_payment=None
    ) -> Dict:
        """
        Charge a stored token and complete the store purchase synchronously.
        
        Args:
            token: Tranzila token
            invoice: StoreInvoice object
            product_items: List of {product_id, quantity, size}
            
        Returns:
            Dict with success status and transaction details
        """
        from apps.store.models import StoreProduct, StoreSale
        
        # Build items list for Tranzila API
        # Only include required fields to avoid validation errors
        tranzila_items = []
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            tranzila_items.append({
                'name': f"{product.name} {item.get('size', '')}".strip(),
                'type': 'I',  # Item/Product
                'unit_price': float(product.sale_price),
                'units_number': item['quantity']
            })
        
        # Charge the token using new REST API
        result = self.tranzila_service.charge_with_token(
            token=token,
            amount=invoice.total_amount,
            description=f"Store purchase - Invoice {invoice.invoice_number}",
            transaction_id=str(invoice.id),
            items=tranzila_items,
            expire_month=recurring_payment.card_expire_month if recurring_payment else None,
            expire_year=recurring_payment.card_expire_year if recurring_payment else None
        )
        
        if result['success']:
            # Create TranzilaTransaction record for audit trail
            tranzila_transaction = TranzilaTransaction.objects.create(
                transaction_id=result.get('transaction_id', ''),
                confirmation_code=result.get('confirmation_code', ''),
                transaction_type='charge',
                response_code=result.get('response_code', '000'),
                response_message=result.get('message', ''),
                request_data={
                    'token': token[:10] + '...' if len(token) > 10 else token,  # Masked token
                    'amount': str(invoice.total_amount),
                    'items': tranzila_items
                },
                response_data=result.get('raw_response', {}),
                idempotency_key=f"store_token_{invoice.id}_{result.get('transaction_id', '')}",
                is_successful=True,
                response_timestamp=timezone.now()
            )
            
            # Update invoice
            invoice.payment_status = 'completed'
            invoice.tranzila_txn = tranzila_transaction
            invoice.tranzila_transaction_id = result.get('transaction_id', '')
            invoice.tranzila_confirmation_code = result.get('confirmation_code', '')
            invoice.save()
            
            # Create sales records and update stock atomically
            with transaction.atomic():
                for item in product_items:
                    product = StoreProduct.objects.select_for_update().get(id=item['product_id'])
                    
                    # Validate stock
                    if product.stock_quantity < item['quantity']:
                        logger.error(f"Insufficient stock for product {product.name}")
                        # Refund if this fails mid-transaction
                        invoice.payment_status = 'failed'
                        invoice.notes = f"Insufficient stock for {product.name}"
                        invoice.save()
                        return {
                            'success': False,
                            'error': f'אין מספיק מלאי עבור {product.name}'
                        }
                    
                    # Create sale record
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=product.sale_price,
                        total_price=product.sale_price * item['quantity'],
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch=product.branch,
                        notes=''
                    )
                    
                    _decrement_product_stock(product, item)

                    logger.debug(f"Sold {item['quantity']}x {product.name}, new stock: {product.stock_quantity}")
            
            log_payment_operation("STORE_CHARGE_SUCCESS", invoice=invoice.invoice_number, total=invoice.total_amount)
            return {
                'success': True,
                'transaction_id': result.get('transaction_id'),
                'confirmation_code': result.get('confirmation_code')
            }
        else:
            # Update invoice to failed
            error_msg = result.get('error') or result.get('message') or 'Unknown error'
            invoice.payment_status = 'failed'
            invoice.notes = f"Payment failed: {error_msg}"
            invoice.save()
            
            log_payment_operation("STORE_CHARGE_FAILED", invoice=invoice.invoice_number, error=error_msg)
            return {
                'success': False,
                'error': error_msg
            }
    
    def complete_store_purchase_from_webhook(
        self,
        invoice_id: str,
        tranzila_response: Dict,
        signature: Optional[str] = None
    ) -> Dict:
        """
        Complete a store purchase after Tranzila iframe webhook callback.
        
        Args:
            invoice_id: UUID of StoreInvoice
            tranzila_response: Parsed webhook response
            signature: Optional webhook signature for verification
            
        Returns:
            Dict with completion result
        """
        from apps.store.models import StoreInvoice, StoreProduct, StoreSale
        import json
        
        # Verify webhook signature for security
        if signature and not self.tranzila_service.verify_webhook_signature(tranzila_response, signature):
            logger.error(f"Invalid webhook signature for store invoice {invoice_id}")
            return {'success': False, 'error': 'Invalid signature'}
        
        try:
            invoice = StoreInvoice.objects.get(id=invoice_id)
        except StoreInvoice.DoesNotExist:
            logger.error(f"Invoice not found: {invoice_id}")
            return {'success': False, 'error': 'Invoice not found'}
        
        if tranzila_response['is_successful']:
            # Parse product items from invoice notes
            try:
                product_items = json.loads(invoice.notes) if invoice.notes else []
            except json.JSONDecodeError:
                product_items = []
            
            # Update invoice
            invoice.payment_status = 'completed'
            invoice.tranzila_transaction_id = tranzila_response.get('transaction_id', '')
            invoice.tranzila_confirmation_code = tranzila_response.get('confirmation_code', '')
            invoice.save()
            
            # Create sales and update stock
            with transaction.atomic():
                for item in product_items:
                    product = StoreProduct.objects.select_for_update().get(id=item['product_id'])
                    
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=product.sale_price,
                        total_price=product.sale_price * item['quantity'],
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch=product.branch,
                        notes=''
                    )

                    _decrement_product_stock(product, item)

            logger.info(f"Successfully completed webhook purchase for invoice {invoice.invoice_number}")
            return {'success': True, 'invoice_id': str(invoice.id)}
        else:
            # Mark as failed
            invoice.payment_status = 'failed'
            invoice.notes = f"Payment failed: {tranzila_response.get('error_message', 'Unknown')}"
            invoice.save()
            
            return {
                'success': False,
                'error': tranzila_response.get('error_message', 'Payment failed')
            }
    
    def create_cash_invoice(
        self,
        product_items: list,
        child_id: str,
        payment_method: str
    ) -> Dict:
        """
        Create invoice and complete purchase immediately for cash/monthly billing.
        
        Args:
            product_items: List of {product_id, quantity, size}
            child_id: UUID of child
            payment_method: 'cash' or 'monthly_billing'
            
        Returns:
            Dict with invoice data
        """
        from apps.store.models import StoreProduct, StoreInvoice, StoreSale
        from apps.store.serializers import StoreInvoiceSerializer
        
        child = Child.objects.get(id=child_id)
        
        # Calculate total
        total_amount = Decimal('0.00')
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            total_amount += product.sale_price * item['quantity']
        
        # Create completed invoice
        invoice = StoreInvoice.objects.create(
            child=child,
            total_amount=total_amount,
            payment_method=payment_method,
            payment_status='completed',
            charged_with_token=False
        )
        
        # Create sales and update stock
        with transaction.atomic():
            for item in product_items:
                product = StoreProduct.objects.select_for_update().get(id=item['product_id'])
                
                # Validate stock
                if product.stock_quantity < item['quantity']:
                    raise ValueError(f'אין מספיק מלאי עבור {product.name}')
                
                StoreSale.objects.create(
                    invoice=invoice,
                    product=product,
                    child=child,
                    quantity=item['quantity'],
                    unit_price=product.sale_price,
                    total_price=product.sale_price * item['quantity'],
                    size=item.get('size', ''),
                    payment_method=payment_method,
                    branch=product.branch,
                    notes=''  # Empty notes for cash/monthly purchases
                )

                _decrement_product_stock(product, item)

        logger.info(f"Created {payment_method} invoice {invoice.invoice_number}")
        
        return StoreInvoiceSerializer(invoice).data
    
    # ============================================================================
    # Refund Methods
    # ============================================================================
    
    def refund_payment(
        self,
        payment_id: str,
        reason: str = 'זיכוי',
        amount: Optional[Decimal] = None
    ) -> Dict:
        """
        Refund a customer payment (lessons/subscriptions).
        
        Args:
            payment_id: UUID of Payment
            reason: Refund reason
            amount: Optional amount for partial refund (None = full refund)
            
        Returns:
            Dict with refund result
        """
        try:
            payment = Payment.objects.select_related(
                'tranzila_transaction',
                'child'
            ).get(id=payment_id)
        except Payment.DoesNotExist:
            logger.error(f"Payment not found: {payment_id}")
            return {
                'success': False,
                'error': 'לא נמצא תשלום'
            }
        
        # Validate payment status
        if payment.status != 'completed':
            logger.warning(f"Cannot refund payment {payment_id} - status: {payment.status}")
            return {
                'success': False,
                'error': 'ניתן לזכות רק תשלומים שהושלמו'
            }
        
        # Get Tranzila transaction details
        if not payment.tranzila_transaction:
            logger.error(f"Payment {payment_id} has no tranzila_transaction")
            return {
                'success': False,
                'error': 'לא נמצא מזהה עסקת טרנזילה'
            }
        
        transaction_id = payment.tranzila_transaction.transaction_id
        authorization_number = payment.tranzila_transaction.confirmation_code
        
        if not transaction_id:
            logger.error(f"Payment {payment_id} has no transaction_id")
            return {
                'success': False,
                'error': 'לא נמצא מזהה עסקת טרנזילה'
            }
        
        if not authorization_number:
            logger.error(f"Payment {payment_id} has no authorization_number")
            return {
                'success': False,
                'error': 'לא נמצא קוד אישור לעסקה'
            }
        
        # Get card expiration and token from active recurring payment
        card_expire_month = None
        card_expire_year = None
        token = None
        if payment.child:
            recurring = payment.child.recurring_payments.filter(
                status='active'
            ).first()
            if recurring:
                card_expire_month = recurring.card_expire_month
                card_expire_year = recurring.card_expire_year
                token = recurring.tranzila_token
        
        # Use full amount if not specified
        refund_amount = amount if amount else payment.final_amount
        
        log_payment_operation(
            "REFUND_PAYMENT",
            payment_id=payment_id,
            amount=refund_amount,
            reason=reason
        )
        
        # Call Tranzila refund
        result = self.tranzila_service.refund_transaction(
            transaction_id=transaction_id,
            amount=refund_amount,
            reason=reason,
            authorization_number=authorization_number,
            card_expire_month=card_expire_month,
            card_expire_year=card_expire_year,
            token=token
        )
        
        if result['success']:
            # Create TranzilaTransaction record for audit trail
            from apps.customers.models import TranzilaTransaction
            tranzila_transaction = TranzilaTransaction.objects.create(
                transaction_id=result.get('transaction_id', ''),
                confirmation_code=result.get('confirmation_code', ''),
                transaction_type='refund',
                response_code=result.get('response_code', '000'),
                response_message=result.get('message', ''),
                request_data={
                    'original_transaction_id': transaction_id,
                    'authorization_number': authorization_number,
                    'amount': str(refund_amount),
                    'reason': reason,
                    'token': token[:10] + '...' if token and len(token) > 10 else token
                },
                response_data=result.get('raw_response', {}),
                idempotency_key=f"refund_payment_{payment_id}_{result.get('transaction_id', '')}",
                is_successful=True,
                response_timestamp=timezone.now()
            )
            
            # Update payment status
            payment.status = 'refunded'
            payment.save()
            
            log_payment_operation(
                "REFUND_PAYMENT_SUCCESS",
                payment_id=payment_id,
                transaction_id=result.get('transaction_id', ''),
                original_transaction_id=transaction_id
            )
            
            return {
                'success': True,
                'message': 'התשלום זוכה בהצלחה',
                'transaction_id': result.get('transaction_id', ''),
                'original_transaction_id': transaction_id,
                'refund_amount': float(refund_amount)
            }
        else:
            error_msg = result.get('error', 'שגיאה בזיכוי התשלום')
            logger.error(f"Refund failed for payment {payment_id}: {error_msg}")
            return {
                'success': False,
                'error': error_msg
            }
    
    def refund_store_invoice(
        self,
        invoice_id: str,
        reason: str = 'זיכוי רכישה',
        amount: Optional[Decimal] = None
    ) -> Dict:
        """
        Refund a store invoice.
        
        Args:
            invoice_id: UUID of StoreInvoice
            reason: Refund reason
            amount: Optional amount for partial refund (None = full refund)
            
        Returns:
            Dict with refund result
        """
        from apps.store.models import StoreInvoice
        
        try:
            invoice = StoreInvoice.objects.select_related('child').get(id=invoice_id)
        except StoreInvoice.DoesNotExist:
            logger.error(f"Store invoice not found: {invoice_id}")
            return {
                'success': False,
                'error': 'לא נמצאה חשבונית'
            }
        
        # Validate invoice status - allow completed or refund_failed (for retry)
        if invoice.payment_status not in ['completed', 'refund_failed']:
            logger.warning(f"Cannot refund invoice {invoice_id} - status: {invoice.payment_status}")
            return {
                'success': False,
                'error': 'ניתן לזכות רק חשבוניות ששולמו או שזיכוי נכשל'
            }
        
        if invoice.payment_method != 'credit_card':
            logger.warning(f"Cannot refund invoice {invoice_id} - payment method: {invoice.payment_method}")
            return {
                'success': False,
                'error': 'ניתן לזכות רק תשלומי אשראי'
            }
        
        # Get Tranzila transaction details (try ForeignKey first, fallback to CharField)
        transaction_id = None
        authorization_number = None
        
        if invoice.tranzila_txn:
            # Use the linked TranzilaTransaction object (preferred)
            transaction_id = invoice.tranzila_txn.transaction_id
            authorization_number = invoice.tranzila_txn.confirmation_code
        else:
            # Fallback to CharField for older records
            transaction_id = invoice.tranzila_transaction_id
            authorization_number = invoice.tranzila_confirmation_code
        
        if not transaction_id:
            logger.error(f"Invoice {invoice_id} has no transaction_id")
            return {
                'success': False,
                'error': 'לא נמצא מזהה עסקת טרנזילה'
            }
        
        if not authorization_number:
            logger.error(f"Invoice {invoice_id} has no confirmation code")
            return {
                'success': False,
                'error': 'לא נמצא קוד אישור לעסקה'
            }
        
        # Get card expiration and token from child's active recurring payment
        card_expire_month = None
        card_expire_year = None
        token = None
        if invoice.child:
            recurring = invoice.child.recurring_payments.filter(
                status='active'
            ).first()
            if recurring:
                card_expire_month = recurring.card_expire_month
                card_expire_year = recurring.card_expire_year
                token = recurring.tranzila_token
        
        # Use full amount if not specified
        refund_amount = amount if amount else invoice.total_amount
        
        # Build items list from invoice sales
        from apps.store.models import StoreSale
        items = []
        sales = StoreSale.objects.filter(invoice=invoice).select_related('product')
        for sale in sales:
            items.append({
                'name': sale.product.name if sale.product else 'מוצר',
                'type': 'I',
                'unit_price': float(sale.unit_price),
                'units_number': sale.quantity
            })
        
        # If no sales found, create a single item with the total amount
        if not items:
            items = [{
                'name': f'זיכוי חשבונית {invoice.invoice_number}',
                'type': 'I',
                'unit_price': float(refund_amount),
                'units_number': 1
            }]
        
        log_payment_operation(
            "REFUND_STORE_INVOICE",
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            amount=refund_amount,
            reason=reason
        )
        
        # Call Tranzila refund
        result = self.tranzila_service.refund_transaction(
            transaction_id=transaction_id,
            amount=refund_amount,
            reason=reason,
            authorization_number=authorization_number,
            card_expire_month=card_expire_month,
            card_expire_year=card_expire_year,
            token=token,
            items=items
        )
        
        if result['success']:
            # Create TranzilaTransaction record for audit trail
            from apps.customers.models import TranzilaTransaction
            tranzila_transaction = TranzilaTransaction.objects.create(
                transaction_id=result.get('transaction_id', ''),
                confirmation_code=result.get('confirmation_code', ''),
                transaction_type='refund',
                response_code=result.get('response_code', '000'),
                response_message=result.get('message', ''),
                request_data={
                    'original_transaction_id': transaction_id,
                    'authorization_number': authorization_number,
                    'amount': str(refund_amount),
                    'reason': reason,
                    'items': items,
                    'token': token[:10] + '...' if token and len(token) > 10 else token
                },
                response_data=result.get('raw_response', {}),
                idempotency_key=f"refund_store_{invoice_id}_{result.get('transaction_id', '')}",
                is_successful=True,
                response_timestamp=timezone.now()
            )
            
            # Update invoice status to refunded and restore stock
            with transaction.atomic():
                # Restore stock for refunded products (per-size aware)
                from apps.store.models import StoreSale
                sales = StoreSale.objects.filter(invoice=invoice).select_related('product')

                for sale in sales:
                    _restore_stock_for_sale(sale)
                    logger.info(
                        f"Restored {sale.quantity} units to product {sale.product.name}"
                        f"{f' (size {sale.size})' if sale.size else ''}"
                    )

                # Update invoice
                invoice.payment_status = 'refunded'
                invoice.refunded_amount = refund_amount
                invoice.notes = f"זוכה: {reason}"
                invoice.save()
            
            log_payment_operation(
                "REFUND_STORE_INVOICE_SUCCESS",
                invoice_id=invoice_id,
                invoice_number=invoice.invoice_number,
                refunded_amount=refund_amount,
                new_transaction_id=result.get('transaction_id', ''),
                original_transaction_id=transaction_id
            )
            
            return {
                'success': True,
                'message': 'החשבונית זוכתה בהצלחה',
                'invoice_number': invoice.invoice_number,
                'refund_amount': float(refund_amount),
                'transaction_id': result.get('transaction_id', ''),
                'original_transaction_id': transaction_id
            }
        else:
            # Update invoice status to refund_failed (keep button for retry)
            error_msg = result.get('error', 'שגיאה בזיכוי החשבונית')
            invoice.payment_status = 'refund_failed'
            invoice.notes = f"זיכוי נכשל: {error_msg} - {reason}"
            invoice.save()
            
            logger.error(f"Refund failed for invoice {invoice_id}: {error_msg}")
            return {
                'success': False,
                'error': error_msg
            }

