"""
Discount Evaluation Service

This service handles the business logic for evaluating and applying discounts
to payments. It supports multiple discount types that can be combined additively.

Discount Types:
1. Early Sign-Up Discount: Applied when payment is made within specific date ranges
2. Second Child Discount: Applied automatically to 2nd child onwards in a family

Usage:
    service = DiscountService()
    result = service.evaluate_discounts_for_payment(
        family_id=family.id,
        child_id=child.id,
        payment_date=date.today(),
        base_price=500.00
    )
    # result.total_discount_amount = 100.00
    # result.final_price = 400.00
"""
from dataclasses import dataclass
from typing import List, Optional
from datetime import date
from decimal import Decimal

from django.db.models import Count, Q
from apps.customers.financial_models import Discount
from apps.customers.models import Family, Child


@dataclass
class ApplicableDiscount:
    """Represents a discount that applies to a payment"""
    discount_id: str
    name: str
    discount_type: str
    value: Decimal
    reason: str


@dataclass
class DiscountCalculation:
    """Result of discount evaluation"""
    applicable_discounts: List[ApplicableDiscount]
    total_discount_amount: Decimal
    final_price: Decimal
    base_price: Decimal


class DiscountService:
    """Service for evaluating and calculating discounts"""
    
    EARLY_SIGNUP_IDENTIFIER = "רישום מוקדם"
    SECOND_CHILD_IDENTIFIER = "ילד שני"
    ADDITIONAL_LESSON_IDENTIFIER = "שיעור נוסף"
    
    def evaluate_discounts_for_payment(
        self,
        family_id: str,
        child_id: str,
        payment_date: date,
        base_price: Decimal,
        lesson_id: Optional[str] = None
    ) -> DiscountCalculation:
        """
        Evaluate all applicable discounts for a payment.
        
        Args:
            family_id: UUID of the family
            child_id: UUID of the child
            payment_date: Date when payment is being made
            base_price: Original price before discounts
            lesson_id: Optional UUID of the lesson (needed for additional lesson discount)
            
        Returns:
            DiscountCalculation with all applicable discounts and final price
        """
        applicable_discounts = []
        
        # Check Additional Lesson Discount (takes precedence as it's most specific)
        if lesson_id:
            additional_lesson = self.check_additional_lesson_discount(child_id, lesson_id)
            if additional_lesson:
                # Fixed final price for additional lessons
                discount_amount = max(Decimal('0.00'), base_price - additional_lesson.value)
                return DiscountCalculation(
                    applicable_discounts=[ApplicableDiscount(
                        discount_id=str(additional_lesson.id),
                        name=additional_lesson.name,
                        discount_type='additional_lesson',
                        value=discount_amount,
                        reason=f"שיעור נוסף: ₪{additional_lesson.value} (במקום ₪{base_price})"
                    )],
                    total_discount_amount=discount_amount,
                    final_price=additional_lesson.value,
                    base_price=base_price
                )
        
        # Check Early Sign-Up Discount
        early_signup = self.check_early_signup_discount(payment_date)
        if early_signup:
            # Check if it's a fixed_final_price discount
            if early_signup.discount_type == 'fixed_final_price':
                # Fixed final price takes precedence - return immediately
                discount_amount = max(Decimal('0.00'), base_price - early_signup.value)
                return DiscountCalculation(
                    applicable_discounts=[ApplicableDiscount(
                        discount_id=str(early_signup.id),
                        name=early_signup.name,
                        discount_type='fixed_final_price',
                        value=discount_amount,
                        reason=f"מחיר קבוע: ₪{early_signup.value} (במקום ₪{base_price})"
                    )],
                    total_discount_amount=discount_amount,
                    final_price=early_signup.value,
                    base_price=base_price
                )
            else:
                applicable_discounts.append(ApplicableDiscount(
                    discount_id=str(early_signup.id),
                    name=early_signup.name,
                    discount_type='early_signup',
                    value=early_signup.value,
                    reason=f"תשלום בתאריך {payment_date.strftime('%d/%m/%Y')} נמצא בטווח רישום מוקדם"
                ))
        
        # Check Second Child Discount
        second_child = self.check_second_child_discount(family_id, child_id)
        if second_child:
            # Check if it's a fixed_final_price discount
            if second_child.discount_type == 'fixed_final_price':
                # Fixed final price takes precedence - return immediately
                discount_amount = max(Decimal('0.00'), base_price - second_child.value)
                return DiscountCalculation(
                    applicable_discounts=[ApplicableDiscount(
                        discount_id=str(second_child.id),
                        name=second_child.name,
                        discount_type='fixed_final_price',
                        value=discount_amount,
                        reason=f"מחיר קבוע לילד שני: ₪{second_child.value} (במקום ₪{base_price})"
                    )],
                    total_discount_amount=discount_amount,
                    final_price=second_child.value,
                    base_price=base_price
                )
            else:
                applicable_discounts.append(ApplicableDiscount(
                    discount_id=str(second_child.id),
                    name=second_child.name,
                    discount_type='second_child',
                    value=second_child.value,
                    reason="הנחה אוטומטית לילד שני ומעלה במשפחה"
                ))
        
        # Calculate total discount (additive for fixed/percentage types)
        total_discount = sum(
            discount.value for discount in applicable_discounts
        )
        
        # Ensure final price doesn't go negative
        final_price = max(Decimal('0.00'), base_price - total_discount)
        
        return DiscountCalculation(
            applicable_discounts=applicable_discounts,
            total_discount_amount=total_discount,
            final_price=final_price,
            base_price=base_price
        )
    
    def check_early_signup_discount(self, payment_date: date) -> Optional[Discount]:
        """
        Check if an early sign-up discount applies for the given payment date.
        
        Returns the first matching active discount if multiple ranges overlap.
        In practice, admins should avoid overlapping date ranges.
        
        Args:
            payment_date: Date when payment is being made
            
        Returns:
            Discount object if applicable, None otherwise
        """
        return Discount.objects.filter(
            is_active=True,
            is_built_in=True,
            name__contains=self.EARLY_SIGNUP_IDENTIFIER,
            start_date__lte=payment_date,
            end_date__gte=payment_date
        ).first()
    
    def check_second_child_discount(
        self,
        family_id: str,
        child_id: str
    ) -> Optional[Discount]:
        """
        Check if second child discount applies.
        
        Logic:
        - Family must have 2+ children
        - The specific child must be 2nd or later (by creation date)
        - Only applies to 2nd child onwards, not the first
        
        Args:
            family_id: UUID of the family
            child_id: UUID of the child being charged
            
        Returns:
            Discount object if applicable, None otherwise
        """
        try:
            family = Family.objects.prefetch_related('children').get(id=family_id)
            
            # Check if family has at least 2 children
            children_count = family.children.count()
            if children_count < 2:
                return None
            
            # Get all children ordered by creation date
            children = list(family.children.order_by('created_at'))
            
            # Find the position of this child (0-indexed)
            child_position = None
            for idx, child in enumerate(children):
                if str(child.id) == str(child_id):
                    child_position = idx
                    break
            
            # If this is NOT the first child (position > 0), apply discount
            if child_position is not None and child_position > 0:
                return Discount.objects.filter(
                    is_active=True,
                    is_built_in=True,
                    name__contains=self.SECOND_CHILD_IDENTIFIER
                ).first()
            
            return None
            
        except Family.DoesNotExist:
            return None
    
    def check_additional_lesson_discount(
        self,
        child_id: str,
        lesson_id: str
    ) -> Optional[Discount]:
        """
        Check if additional lesson discount applies.
        
        Logic:
        - Child must have status='active'
        - Child must be enrolled in at least 2 lessons
        - This lesson must NOT be the first lesson (by enrollment creation date)
        - Only applies to 2nd lesson onwards
        
        Args:
            child_id: UUID of the child
            lesson_id: UUID of the lesson being paid for
            
        Returns:
            Discount object if applicable, None otherwise
        """
        try:
            from apps.enrollments.models import LessonEnrollment
            
            child = Child.objects.get(id=child_id)
            
            # Check if child is active
            if child.status != 'active':
                return None
            
            # Get all active lesson enrollments for this child
            enrollments = LessonEnrollment.objects.filter(
                child=child,
                status='active'
            ).order_by('created_at')
            
            # Need at least 2 lessons
            if enrollments.count() < 2:
                return None
            
            # Find the position of this lesson (0-indexed)
            lesson_position = None
            for idx, enrollment in enumerate(enrollments):
                if str(enrollment.lesson_id) == str(lesson_id):
                    lesson_position = idx
                    break
            
            # If this is NOT the first lesson (position > 0), apply discount
            if lesson_position is not None and lesson_position > 0:
                return Discount.objects.filter(
                    is_active=True,
                    is_built_in=True,
                    name__contains=self.ADDITIONAL_LESSON_IDENTIFIER
                ).first()
            
            return None
            
        except Child.DoesNotExist:
            return None
    
    def get_discount_summary(self, discount_calculation: DiscountCalculation) -> dict:
        """
        Get a human-readable summary of the discount calculation.
        
        Args:
            discount_calculation: Result from evaluate_discounts_for_payment
            
        Returns:
            Dictionary with summary information
        """
        return {
            'base_price': float(discount_calculation.base_price),
            'discounts': [
                {
                    'name': d.name,
                    'type': d.discount_type,
                    'amount': float(d.value),
                    'reason': d.reason
                }
                for d in discount_calculation.applicable_discounts
            ],
            'total_discount': float(discount_calculation.total_discount_amount),
            'final_price': float(discount_calculation.final_price),
            'discount_count': len(discount_calculation.applicable_discounts)
        }

