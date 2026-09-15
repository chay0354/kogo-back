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
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Q
from django.utils import timezone
from apps.customers.financial_models import Discount
from apps.customers.models import Child


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
    """Service for evaluating and calculating discounts.

    A built-in discount sitting at value 0 is one nobody has configured yet —
    the CRM creates the rows on first view with value 0, and setting a שקל
    discount back to 0 is how the CRM documents turning it off. Either way it
    is not a discount: a 0 here must never reach a payment as a ₪0 discount
    line, and a 0 on a "מחיר סופי קבוע" discount must never make the lesson free.
    """

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
            if additional_lesson and self._fixed_price_lowers(additional_lesson, base_price):
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
        if early_signup and early_signup.discount_type == 'fixed_final_price':
            # Fixed final price takes precedence - return immediately
            if self._fixed_price_lowers(early_signup, base_price):
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
        elif early_signup:
            applicable_discounts.append(ApplicableDiscount(
                discount_id=str(early_signup.id),
                name=early_signup.name,
                discount_type='early_signup',
                value=self._amount_off(early_signup, base_price),
                reason=self._with_percent(
                    early_signup,
                    f"תשלום בתאריך {payment_date.strftime('%d/%m/%Y')} נמצא בטווח רישום מוקדם",
                ),
            ))
        
        # Check Second Child Discount
        second_child = self.check_second_child_discount(family_id, child_id)
        if second_child and second_child.discount_type == 'fixed_final_price':
            # Fixed final price takes precedence - return immediately
            if self._fixed_price_lowers(second_child, base_price):
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
        elif second_child:
            applicable_discounts.append(ApplicableDiscount(
                discount_id=str(second_child.id),
                name=second_child.name,
                discount_type='second_child',
                value=self._amount_off(second_child, base_price),
                reason=self._with_percent(second_child, "הנחה אוטומטית לילד שני ומעלה במשפחה"),
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
            value__gt=0,
            name__contains=self.EARLY_SIGNUP_IDENTIFIER,
            start_date__lte=payment_date,
            end_date__gte=payment_date
        ).first()
    
    def check_second_child_discount(
        self,
        family_id: str,
        child_id: Optional[str] = None
    ) -> Optional[Discount]:
        """
        Check if second child discount applies.

        Logic:
        - Another child in the same family must already be signed to a team:
          an active LessonEnrollment that is NOT a trial (trial_lesson_date empty).
        - A sibling with a pending/processing/completed subscription payment from
          the same checkout also counts — widget multi-child registration prices
          the second child before the first enrollment is created.
        - Trial-lesson enrollments don't count — a sibling who only did/booked
          a trial does not make this child eligible.
        - Order of registration doesn't matter; whichever sibling pays while
          another sibling is already on a team (or signing up) gets the discount.

        Args:
            family_id: UUID of the family
            child_id: UUID of the child being charged. None when the child does
                not exist yet (the widget's lookup asks before creating them), in
                which case every child of the family is a potential sibling.

        Returns:
            Discount object if applicable, None otherwise
        """
        from apps.enrollments.models import LessonEnrollment
        from apps.customers.models import Payment

        siblings_on_team = LessonEnrollment.objects.filter(
            child__family_id=family_id,
            status='active',
            trial_lesson_date__isnull=True,
        )
        recent = timezone.now() - timedelta(hours=2)
        siblings_paying = Payment.objects.filter(
            family_id=family_id,
            payment_type='recurring_subscription',
        ).filter(
            Q(status='completed')
            | Q(status__in=('pending', 'processing'), created_at__gte=recent)
        )
        if child_id:
            siblings_on_team = siblings_on_team.exclude(child_id=child_id)
            siblings_paying = siblings_paying.exclude(child_id=child_id)

        if not (siblings_on_team.exists() or siblings_paying.exists()):
            return None

        return Discount.objects.filter(
            is_active=True,
            is_built_in=True,
            value__gt=0,
            name__contains=self.SECOND_CHILD_IDENTIFIER
        ).first()
    
    def check_additional_lesson_discount(
        self,
        child_id: str,
        lesson_id: str
    ) -> Optional[Discount]:
        """
        Check if additional lesson discount applies.
        
        Logic:
        - Child must have status='active'
        - Re-billing a lesson the child already sits on (card link, replaced
          card): this lesson must NOT be the first lesson (by enrollment
          creation date) — the first lesson stays at full price.
        - Buying the lesson (widget / CRM signup): its enrollment is only
          created once the payment completes, so the lesson is additional when
          the child already pays for another lesson, or a payment for another
          lesson is in flight from the same checkout — the same way the lesson
          price tiers count it. Without this the discount the CRM configures
          never reached a signup, only a later card link, and the same lesson
          was billed at two prices.

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

            own = enrollments.filter(lesson_id=lesson_id).first()
            if own is not None:
                is_additional = enrollments.filter(created_at__lt=own.created_at).exists()
            else:
                is_additional = self._child_pays_for_another_lesson(child, lesson_id)

            if is_additional:
                return Discount.objects.filter(
                    is_active=True,
                    is_built_in=True,
                    value__gt=0,
                    name__contains=self.ADDITIONAL_LESSON_IDENTIFIER
                ).first()

            return None

        except Child.DoesNotExist:
            return None

    @staticmethod
    def _child_pays_for_another_lesson(child: Child, lesson_id: str) -> bool:
        """Another lesson the child is signed to (not a trial) or is paying for right now."""
        from apps.enrollments.models import LessonEnrollment
        from apps.customers.models import Payment

        if LessonEnrollment.objects.filter(
            child=child,
            status__in=('active', 'payments_problem'),
            trial_lesson_date__isnull=True,
        ).exclude(lesson_id=lesson_id).exists():
            return True
        recent = timezone.now() - timedelta(hours=2)
        return Payment.objects.filter(
            child=child,
            payment_type='recurring_subscription',
            status__in=('pending', 'processing'),
            created_at__gte=recent,
        ).exclude(lesson_id=lesson_id).exclude(lesson_id__isnull=True).exists()

    @staticmethod
    def _fixed_price_lowers(discount: Discount, base_price: Decimal) -> bool:
        """A "מחיר סופי קבוע" is a discount only while it is below the lesson's own price.

        One global figure serves every course; on a course cheaper than it, applying
        it would charge the parent more than the lesson costs — so it does not apply.
        """
        return discount.value < base_price

    @staticmethod
    def _amount_off(discount: Discount, base_price: Decimal) -> Decimal:
        """Shekels a fixed or percentage discount takes off base_price (rounded to אגורות)."""
        if discount.discount_type == 'percentage':
            return (base_price * discount.value / Decimal('100')).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        return discount.value

    @staticmethod
    def _with_percent(discount: Discount, reason: str) -> str:
        if discount.discount_type == 'percentage':
            return f"{reason} ({discount.value.normalize():f}%)"
        return reason
    
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

