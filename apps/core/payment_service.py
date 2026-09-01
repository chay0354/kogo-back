"""
Payment Service - Business Logic for Payment Processing

This service orchestrates the payment flow, including:
- Discount calculation
- Payment initiation
- Webhook processing
- Invoice creation
- Child subscription status updates
"""
import calendar
import logging
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

JERUSALEM_TZ = ZoneInfo('Asia/Jerusalem')

from apps.customers.models import (
    Child, Family, Parent, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot
)
from apps.customers.financial_models import Invoice, InvoiceChild, Discount
from apps.customers.discount_service import DiscountService
from apps.core.card_validation import validate_card_details
from apps.core.tranzila_service import TranzilaService, invoice_id_from_pdesc
from apps.courses.models import Lesson, LessonBundle, LessonPriceOption
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.enrollment_counts import paying_enrollments
from apps.instructors.utils import get_lesson_price_for_course_index
from apps.store.stock_utils import (
    decrement_product_stock as _decrement_product_stock,
    restore_stock_for_sale as _restore_stock_for_sale,
    store_line_item_branch_id as _store_line_item_branch_id,
)
from apps.store.pricing import line_charge_amount, sale_unit_and_total, tranzila_items_for_cart_line

logger = logging.getLogger(__name__)

BILLING_ENROLLMENT_STATUSES = ("active", "payments_problem")


def parse_store_cart_notes(notes: Optional[str]) -> Optional[list]:
    """Return cart line items stored on a StoreInvoice.notes JSON blob, or None."""
    import json

    try:
        data = json.loads(notes or '')
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(row, dict) and row.get('product_id') for row in data):
        return None
    return data


def registration_fee_amount(course=None) -> Decimal:
    """One-time registration fee added to a child's first subscription."""
    override = getattr(course, 'registration_fee_override', None) if course is not None else None
    if override is not None:
        try:
            return max(Decimal('0.00'), Decimal(str(override)).quantize(Decimal('0.01')))
        except Exception:
            pass
    raw = getattr(settings, 'REGISTRATION_FEE_ILS', 120)
    try:
        fee = Decimal(str(raw or 0))
    except Exception:
        fee = Decimal('0')
    return max(Decimal('0.00'), fee.quantize(Decimal('0.01')))


def child_already_has_registration_fee(child, current_lesson=None) -> bool:
    """True when this child should not be charged דמי רישום again.

    The fee is once per child: extra courses in the same checkout, and later
    signups for the same child, all skip it. A retry of the *same* unpaid
    lesson still charges, so an abandoned pending row cannot hide the fee.
    Existing paying students (completed subscription or active standing order)
    also skip, including older rows that never split the fee onto the field.
    """
    payments = Payment.objects.filter(child=child)
    if payments.filter(registration_fee__gt=0, status='completed').exists():
        return True

    in_flight = payments.filter(
        registration_fee__gt=0,
        status__in=('pending', 'processing'),
    )
    if current_lesson is not None:
        in_flight = in_flight.exclude(lesson=current_lesson)
    if in_flight.exists():
        return True

    if payments.filter(payment_type='recurring_subscription', status='completed').exists():
        return True

    return RecurringPayment.objects.filter(
        child=child,
        status__in=('active', 'paused'),
    ).exists()


def resolve_include_registration_fee(child, lesson, requested: bool) -> bool:
    """Honor an explicit opt-out, otherwise charge only if this child has not paid yet."""
    if not requested:
        return False
    return not child_already_has_registration_fee(child, current_lesson=lesson)


def standing_order_next_billing_date(*, today: date, lesson) -> date:
    """When the monthly standing order should first run for this lesson."""
    course = getattr(lesson, 'course', None) if lesson is not None else None
    if course is not None and getattr(course, 'charge_standing_order_immediately', False):
        return today
    deferred = deferred_first_charge_date(today)
    if deferred:
        return deferred
    day_of_week = lesson.day_of_week if lesson is not None else 1
    _, _, _, nxt = _compute_prorate(today, day_of_week)
    return nxt


def deferred_first_charge_date(today: Optional[date] = None) -> Optional[date]:
    """
    Date the monthly subscription starts, or None to bill the first month on signup.

    While this date is in the future a registration only charges דמי רישום, and the
    monthly price is first billed by the recurring cron on that date. Once the date
    arrives the setting stops applying by itself, so registrations go back to being
    billed for the signup month without a code change.
    """
    raw = getattr(settings, 'SUBSCRIPTION_FIRST_CHARGE_DATE', '') or ''
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        charge_date = date.fromisoformat(raw)
    except ValueError:
        logger.error("Invalid SUBSCRIPTION_FIRST_CHARGE_DATE=%r — ignoring", raw)
        return None
    if today is None:
        today = timezone.now().astimezone(JERUSALEM_TZ).date()
    return charge_date if charge_date > today else None


def subscription_payment_description(
    *,
    child: Child,
    lesson: Lesson,
    bundle=None,
    price_option=None,
    fee_only: bool = False,
) -> str:
    """Parent-facing description of a first subscription charge."""
    if price_option:
        subject = price_option.display_title
    elif bundle:
        subject = f"{lesson.course.name} ({bundle.name or 'מסלול משולב'})"
    else:
        subject = lesson.course.name
    prefix = 'דמי רישום' if fee_only else 'מנוי חודשי'
    return f"{prefix} - {subject} - {child.full_name}"


def payment_full_monthly_amount(payment: Payment) -> Decimal:
    """Full recurring monthly lesson price (excludes proration and registration fee)."""
    return (payment.base_amount - payment.discount_amount).quantize(Decimal('0.01'))


def payment_prorated_lesson_amount(payment: Payment) -> Decimal:
    """Pro-rated lesson portion of a pending/completed first subscription charge."""
    fee = payment.registration_fee or Decimal('0.00')
    return (payment.final_amount - fee).quantize(Decimal('0.01'))


def enroll_child_in_paid_lessons(*, child, lesson, bundle=None) -> None:
    """Activate the paid lesson, and every other day of a twice/thrice-a-week bundle.

    Extra bundle days must not get their own Payment (that showed up as ₪0 דמי רישום).
    """
    members = list(bundle.lessons.all()) if bundle is not None else []
    lessons = members or ([lesson] if lesson is not None else [])
    if lesson is not None and lesson not in lessons:
        lessons = [lesson] + lessons
    today = date.today()
    for member in lessons:
        enrollment, created = LessonEnrollment.objects.get_or_create(
            child=child,
            lesson=member,
            defaults={'start_date': today, 'status': 'active', 'bundle': bundle},
        )
        if created:
            logger.info(
                "Created LessonEnrollment %s for child %s lesson %s",
                enrollment.id, child.id, member.id,
            )
            continue
        enrollment.status = 'active'
        if not enrollment.start_date:
            enrollment.start_date = today
        if bundle and not enrollment.bundle:
            enrollment.bundle = bundle
        enrollment.save()


def heal_missing_bundle_enrollments() -> dict:
    """Enroll every other day of a twice/thrice-a-week bundle the child is already billed for.

    The first widget split charged the first day, failed (or skipped) the ₪0 extra day,
    and left the standing order on the combined price. New signups enroll all members
    in enroll_child_in_paid_lessons; this catches leftovers.
    """
    jobs: dict[tuple[str, str], tuple] = {}

    enrollments = (
        LessonEnrollment.objects.filter(status='active', bundle_id__isnull=False)
        .select_related('child', 'bundle', 'lesson')
        .prefetch_related('bundle__lessons')
    )
    for enrollment in enrollments:
        bundle = enrollment.bundle
        if bundle is None:
            continue
        members = list(bundle.lessons.all())
        if len(members) < 2:
            continue
        jobs[(str(enrollment.child_id), str(bundle.id))] = (
            enrollment.child,
            enrollment.lesson,
            bundle,
        )

    stos = (
        RecurringPayment.objects.filter(
            status='active',
            initial_payment__bundle_id__isnull=False,
        )
        .select_related('child', 'initial_payment__bundle', 'initial_payment__lesson')
        .prefetch_related('initial_payment__bundle__lessons')
    )
    for rp in stos:
        payment = rp.initial_payment
        bundle = payment.bundle if payment else None
        lesson = payment.lesson if payment else None
        if bundle is None or lesson is None:
            continue
        members = list(bundle.lessons.all())
        if len(members) < 2:
            continue
        jobs.setdefault((str(rp.child_id), str(bundle.id)), (rp.child, lesson, bundle))

    children_healed = 0
    enrollments_created = 0
    for child, lesson, bundle in jobs.values():
        member_ids = list(bundle.lessons.values_list('id', flat=True))
        active_count = LessonEnrollment.objects.filter(
            child=child,
            lesson_id__in=member_ids,
            status='active',
        ).count()
        if active_count >= len(member_ids):
            continue
        enroll_child_in_paid_lessons(child=child, lesson=lesson, bundle=bundle)
        after = LessonEnrollment.objects.filter(
            child=child,
            lesson_id__in=member_ids,
            status='active',
        ).count()
        added = max(0, after - active_count)
        if added:
            children_healed += 1
            enrollments_created += added

    logger.info(
        "Healed missing bundle enrollments: %s children, %s rows",
        children_healed,
        enrollments_created,
    )
    return {
        'children': children_healed,
        'enrollments_created': enrollments_created,
    }


def should_create_recurring_for_payment(*, child, bundle, monthly_amount: Decimal) -> bool:
    """One standing order per twice/thrice-a-week bundle, at the full widget price."""
    if monthly_amount <= 0:
        return False
    if bundle is None:
        return True
    return not RecurringPayment.objects.filter(
        child=child,
        status='active',
        initial_payment__bundle=bundle,
    ).exists()


def saved_card_token_for_child(child) -> str:
    """
    Token already stored for this child from an earlier lesson in the same signup.

    A twice-a-week bundle charges דמי רישום on the first day only; the other days
    are ₪0 and must not call Tranzila verify (that path returned schema 20004).
    """
    if child is None:
        return ''
    rec = (
        RecurringPayment.objects
        .filter(child=child, status='active')
        .exclude(tranzila_token='')
        .order_by('-created_at')
        .first()
    )
    return (rec.tranzila_token or '').strip() if rec else ''


def payment_is_fee_only(payment: Payment) -> bool:
    """
    True when a signup charge covers no lesson month, only דמי רישום (or nothing).

    Such a registration has its monthly billing start on a later date, so the signup
    month must not be treated as paid for.
    """
    return payment_prorated_lesson_amount(payment) <= 0


def subscription_tranzila_items(
    *,
    label: str,
    prorated_lesson: Decimal,
    registration_fee: Decimal,
    prorated: bool = True,
) -> list[dict]:
    """
    Tranzila line items for a first subscription charge.

    The lesson line is dropped when there is nothing to bill for it, which is how a
    registration whose monthly billing only starts later charges דמי רישום alone.
    """
    items = []
    if prorated_lesson > 0:
        items.append({
            'name': f'מנוי חודשי (יחסי) - {label}' if prorated else f'מנוי חודשי - {label}',
            'type': 'I',
            'unit_price': float(prorated_lesson),
            'units_number': 1,
            'unit_type': 1,
            'price_type': 'G',
            'currency_code': 'ILS',
        })
    if registration_fee > 0:
        items.append({
            'name': 'דמי רישום',
            'type': 'I',
            'unit_price': float(registration_fee),
            'units_number': 1,
            'unit_type': 1,
            'price_type': 'G',
            'currency_code': 'ILS',
        })
    return items


def log_payment_operation(operation: str, **kwargs):
    """Centralized logging for payment operations."""
    log_parts = [f"[{operation}]"]
    for key, value in kwargs.items():
        log_parts.append(f"{key}={value}")
    logger.info(" ".join(log_parts))


_DJANGO_DOW_TO_PYTHON = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
# Django: Sun=0..Sat=6  →  Python date.weekday(): Mon=0..Sun=6


def _compute_prorate(enrollment_date: date, day_of_week: int) -> tuple:
    """
    Compute pro-rata values based on remaining lesson occurrences this month.

    day_of_week: Django convention (Sunday=0 … Saturday=6).
    Returns (factor, lessons_remaining, total_lessons, next_billing_date).
    next_billing_date is always the 1st of the following month.
    """
    days_in_month = calendar.monthrange(enrollment_date.year, enrollment_date.month)[1]
    if enrollment_date.month == 12:
        next_billing_date = date(enrollment_date.year + 1, 1, 1)
    else:
        next_billing_date = date(enrollment_date.year, enrollment_date.month + 1, 1)

    python_wd = _DJANGO_DOW_TO_PYTHON[day_of_week]

    total_lessons = sum(
        1 for d in range(1, days_in_month + 1)
        if date(enrollment_date.year, enrollment_date.month, d).weekday() == python_wd
    )
    remaining_lessons = sum(
        1 for d in range(enrollment_date.day, days_in_month + 1)
        if date(enrollment_date.year, enrollment_date.month, d).weekday() == python_wd
    )

    if total_lessons == 0:
        return Decimal('1'), 0, 0, next_billing_date

    factor = Decimal(remaining_lessons) / Decimal(total_lessons)
    return factor, remaining_lessons, total_lessons, next_billing_date


def get_child_lesson_index_for_billing(child: Child, lesson: Lesson) -> int:
    """
    Return the 1-based lesson number this lesson will be for the child.

    Existing active/payment-problem enrollments still count as signed lessons.
    Pending widget/CRM payments from this checkout also count, so a second
    lesson added in the same form uses the 2nd-lesson price tier instead of
    being billed as another first lesson.

    The selected lesson is excluded so re-opening payment for the same lesson
    does not incorrectly move the child into the next price tier.
    """
    signed_lesson_ids = set(
        paying_enrollments(
            LessonEnrollment.objects.filter(
                child=child,
                status__in=BILLING_ENROLLMENT_STATUSES,
            )
        ).exclude(lesson=lesson).values_list('lesson_id', flat=True)
    )
    recent_pending = timezone.now() - timedelta(hours=2)
    inflight_lesson_ids = set(
        Payment.objects.filter(
            child=child,
            payment_type='recurring_subscription',
            status__in=('pending', 'processing'),
            created_at__gte=recent_pending,
        )
        .exclude(lesson=lesson)
        .exclude(lesson_id__isnull=True)
        .values_list('lesson_id', flat=True)
    )
    return len(signed_lesson_ids | inflight_lesson_ids) + 1


def validate_bundle_capacity(bundle: 'LessonBundle') -> None:
    """
    Raise ValueError naming the first lesson without capacity. Called before any
    charge is made for a bundle registration so the whole registration fails
    fast rather than leaving the family charged for only some of the lessons.
    """
    for lesson in bundle.lessons.select_related('room', 'course').all():
        if not lesson.room:
            raise ValueError(f"לא ניתן להירשם למסלול — לשיעור {lesson} אין חדר מוגדר")
        active_count = LessonEnrollment.objects.filter(lesson=lesson, status='active').count()
        if active_count >= lesson.room.capacity:
            raise ValueError(f"השיעור {lesson} מלא - קיבולת מקסימלית: {lesson.room.capacity} תלמידים")


def resolve_billing_price(
    child: Child,
    lesson: Lesson,
    bundle_id: Optional[str] = None,
    price_option_id: Optional[str] = None,
) -> Tuple[Decimal, bool, int, Optional['LessonBundle'], Optional['LessonPriceOption']]:
    """
    Resolve the monthly base price to bill for a lesson, and whether the
    generic "additional lesson" discount should be skipped because a
    per-child/per-lesson discount already applied.

    When bundle_id is given, the monthly price is the widget combined_price
    (not combined_price / lesson count). Extra days of the same bundle are
    billed at ₪0 via include_monthly_amount=False so there is one standing
    order for the amount the parent saw in the widget.

    When price_option_id is given, bill the catalog monthly_price chosen in
    the widget (same physical lesson, different marketing title/price).

    Returns: (base_price, used_lesson_tier, course_index, bundle, price_option)
    """
    course_index = get_child_lesson_index_for_billing(child, lesson)

    if price_option_id:
        try:
            price_option = LessonPriceOption.objects.get(
                id=price_option_id,
                lesson=lesson,
                is_active=True,
            )
        except LessonPriceOption.DoesNotExist:
            raise ValueError("מחיר נוסף לא נמצא או לא פעיל")
        return price_option.monthly_price, True, course_index, None, price_option

    if bundle_id:
        from apps.courses.bundles import resolve_registration_bundle

        bundle = resolve_registration_bundle(course=lesson.course, bundle_id=str(bundle_id))
        if bundle is None:
            raise ValueError("מסלול משולב לא נמצא או לא פעיל")
        if not bundle.lessons.filter(pk=lesson.pk).exists():
            raise ValueError("השיעור אינו חלק מהמסלול המשולב")
        validate_bundle_capacity(bundle)
        return bundle.combined_price, True, course_index, bundle, None

    tier_price = get_lesson_price_for_course_index(lesson, course_index)
    regular_price = lesson.course.price
    base_price = tier_price if tier_price and tier_price > 0 else regular_price
    used_lesson_tier = (
        course_index >= 2
        and tier_price is not None
        and Decimal(str(tier_price)) != Decimal(str(regular_price or 0))
    )
    return base_price, used_lesson_tier, course_index, None, None


def card_details_for_payment_refund(payment):
    """Card token + expiry to refund this Payment via Tranzila.

    Signup charges sit on RecurringPayment.initial_payment. Monthly cron
    charges do not — match the standing order for the same child + lesson
    so a child with two courses is not refunded on the wrong token.
    """
    if not getattr(payment, 'child_id', None):
        return None, None, None

    qs = payment.child.recurring_payments.all()
    recurring = qs.filter(initial_payment_id=payment.id).first()
    if not recurring and payment.lesson_id:
        recurring = (
            qs.filter(initial_payment__lesson_id=payment.lesson_id)
            .exclude(status='cancelled')
            .order_by('-updated_at')
            .first()
        )
    if not recurring:
        recurring = qs.filter(status='active').order_by('-updated_at').first()
    if not recurring:
        return None, None, None
    return recurring.card_expire_month, recurring.card_expire_year, recurring.tranzila_token


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
        # REST token/card charges.
        self.tranzila_service = TranzilaService.production()
        # Hosted iframe checkout — TRANZILA_TERMINAL, not the REST production terminal.
        self.iframe_tranzila_service = TranzilaService.iframe()
    
    def initiate_subscription_payment(
        self,
        child_id: str,
        lesson_id: str,
        payment_date: Optional[date] = None,
        success_url: str = '',
        error_url: str = '',
        callback_url: str = '',
        bundle_id: Optional[str] = None,
        price_option_id: Optional[str] = None,
        include_registration_fee: bool = True,
        include_monthly_amount: bool = True,
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
            bundle_id: when set, bill the widget combined_price on the first member
                lesson (see resolve_billing_price). Caller is responsible for calling
                this once per member lesson of the bundle.
            price_option_id: when set, bill the widget catalog price for this lesson.
            include_registration_fee: pass False for extra days of a twice/thrice-a-week
                bundle. Default is true, but the fee is still skipped when this child
                already paid (or has an in-flight first-course charge). One fee per child.
            include_monthly_amount: pass False for extra days of a twice/thrice-a-week
                bundle so only one standing order is created at the full widget price.

        Returns:
            Dict with payment_id, tranzila_url, amount, discounts_applied
        """
        if payment_date is None:
            payment_date = date.today()

        try:
            child = Child.objects.select_related('family').get(id=child_id)
            lesson = Lesson.objects.select_related('course__branch').get(id=lesson_id)
        except (Child.DoesNotExist, Lesson.DoesNotExist) as e:
            logger.error(f"Child or Lesson not found: {e}")
            raise ValueError("Child or Lesson not found")

        if not include_monthly_amount and not include_registration_fee:
            # Extra bundle day: enrollment happens when the real payment completes.
            return {
                'success': True,
                'enrollment_only': True,
                'payment_id': None,
                'child_id': str(child.id),
                'lesson_id': str(lesson.id),
                'bundle_id': bundle_id,
                'base_amount': 0.0,
                'discount_amount': 0.0,
                'prorated_amount': 0.0,
                'registration_fee': 0.0,
                'final_amount': 0.0,
                'monthly_amount': 0.0,
                'discounts_applied': [],
            }

        base_price, used_lesson_tier, course_index, bundle, price_option = resolve_billing_price(
            child, lesson, bundle_id, price_option_id
        )
        if not include_monthly_amount:
            base_price = Decimal('0.00')
        elif not base_price:
            raise ValueError("Lesson/Course price not configured")

        # Calculate discounts. If a per-lesson tier (or bundle price) already kicked in for this
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

        # Pro-rate the first payment to the remaining lessons of the current month.
        today_local = timezone.now().astimezone(JERUSALEM_TZ).date()
        prorate_factor, prorate_lessons_remaining, total_lessons_this_month, next_billing_date = _compute_prorate(
            today_local, lesson.day_of_week
        )
        full_monthly_amount = discount_calculation.final_price

        # When monthly billing only starts later, signup charges דמי רישום alone and the
        # full monthly price is first billed by the recurring cron on that date.
        deferred_charge_date = deferred_first_charge_date(today_local)
        next_billing_date = standing_order_next_billing_date(today=today_local, lesson=lesson)
        if deferred_charge_date:
            prorate_factor = Decimal('0')
            prorate_lessons_remaining = 0
            prorated_lesson = Decimal('0.00')
        elif full_monthly_amount <= 0:
            prorated_lesson = Decimal('0.00')
        else:
            prorated_lesson = max(
                Decimal('1.00'),
                (full_monthly_amount * prorate_factor).quantize(Decimal('0.01'))
            )

        # Create Payment record (pending) with retry (SQLite can throw "database is locked" under concurrency).
        payment = None
        registration_fee = Decimal('0.00')
        prorated_final = prorated_lesson
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                with transaction.atomic():
                    Child.objects.select_for_update().get(id=child.id)
                    charge_fee = resolve_include_registration_fee(child, lesson, include_registration_fee)
                    registration_fee = (
                        registration_fee_amount(lesson.course) if charge_fee else Decimal('0.00')
                    )
                    prorated_final = prorated_lesson + registration_fee
                    payment = Payment.objects.create(
                        child=child,
                        family=child.family,
                        parent=child.family.parents.filter(is_primary=True).first(),
                        branch=lesson.course.branch,
                        lesson=lesson,
                        bundle=bundle,
                        price_option=price_option,
                        payment_type='recurring_subscription',
                        status='pending',
                        base_amount=discount_calculation.base_price,
                        discount_amount=discount_calculation.total_discount_amount,
                        final_amount=prorated_final,
                        registration_fee=registration_fee,
                        description=subscription_payment_description(
                            child=child,
                            lesson=lesson,
                            bundle=bundle,
                            price_option=price_option,
                            fee_only=bool(deferred_charge_date),
                        ),
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
        tranzila_url= self.iframe_tranzila_service.create_recurring_payment_request(
            amount=prorated_final,
            currency='ILS',
            description=payment.description,
            customer_name=child.family.name,
            customer_email=child.family.email,
            customer_phone=child.family.phone,
            success_url=success_url,
            error_url=error_url,
            callback_url=callback_url,
            transaction_id=str(payment.id),
            # The initial charge carries דמי רישום (plus the pro-rated month unless
            # monthly billing starts later); the standing order itself must run at the
            # plain monthly price from the next billing date.
            recur_sum=full_monthly_amount,
            recur_start_date=next_billing_date.isoformat(),
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
            'bundle_id': str(bundle.id) if bundle else None,
            'base_amount': float(discount_calculation.base_price),
            'discount_amount': float(discount_calculation.total_discount_amount),
            'prorated_amount': float(prorated_lesson),
            'registration_fee': float(registration_fee),
            'final_amount': float(prorated_final),
            'prorate_factor': float(prorate_factor),
            'prorate_lessons_remaining': prorate_lessons_remaining,
            'total_lessons_this_month': total_lessons_this_month,
            'next_billing_date': next_billing_date.isoformat(),
            'monthly_amount': float(full_monthly_amount),
            'subscription_start_date': (
                next_billing_date.isoformat()
                if deferred_charge_date or getattr(lesson.course, 'charge_standing_order_immediately', False)
                else None
            ),
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
        
        # Find associated Payment record (we sent payment.id as pdesc)
        payment_id = invoice_id_from_pdesc(webhook_payload.get('pdesc', ''))

        try:
            payment = Payment.objects.select_for_update(of=('self',)).select_related(
                'child', 'family', 'lesson', 'lesson__course', 'lesson__course__branch',
            ).get(id=payment_id)
        except (Payment.DoesNotExist, ValidationError, ValueError):
            logger.error(f"Payment not found for webhook: {payment_id}")
            return {'success': False, 'error': 'Payment not found'}

        # Tranzila retries the notify callback, so the key must be stable across
        # deliveries — anything time-based would let a retry create a second
        # subscription, invoice and WhatsApp message.
        idempotency_key = f"tranzila_{payment.id}_{parsed_response['transaction_id']}"

        if TranzilaTransaction.objects.filter(idempotency_key=idempotency_key).exists():
            logger.warning(f"Duplicate webhook received: {idempotency_key}")
            return {'success': True, 'message': 'Already processed'}

        if payment.status == 'completed':
            logger.warning(f"Webhook for already completed payment {payment.id}; ignoring")
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
        
        # Link transaction to payment
        payment.tranzila_transaction = tranzila_transaction
        
        if parsed_response['is_successful']:
            # SUCCESS FLOW
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save()
            
            # Create/update RecurringPayment if this is a subscription
            if payment.payment_type == 'recurring_subscription' and parsed_response.get('token'):
                full_monthly_amount = payment_full_monthly_amount(payment)
                if should_create_recurring_for_payment(
                    child=payment.child,
                    bundle=payment.bundle,
                    monthly_amount=full_monthly_amount,
                ):
                    discount_details = []
                    for snapshot in payment.discount_snapshots.all():
                        discount_details.append({
                            'name': snapshot.discount_name,
                            'type': snapshot.discount_type,
                            'value': str(snapshot.discount_value),
                            'amount_deducted': str(snapshot.amount_deducted),
                            'reason': snapshot.reason
                        })

                    enrollment_date = payment.created_at.astimezone(JERUSALEM_TZ).date()
                    lesson_dow = payment.lesson.day_of_week if payment.lesson else 1
                    _, _, _, next_billing_date = _compute_prorate(enrollment_date, lesson_dow)
                    if payment_is_fee_only(payment):
                        next_billing_date = standing_order_next_billing_date(
                            today=enrollment_date, lesson=payment.lesson,
                        )

                    recurring_payment = RecurringPayment.objects.create(
                        child=payment.child,
                        initial_payment=payment,
                        tranzila_token=parsed_response['token'],
                        card_expire_month=parsed_response.get('card_expire_month'),
                        card_expire_year=parsed_response.get('card_expire_year'),
                        status='active',
                        base_amount=payment.base_amount,
                        discount_amount=payment.discount_amount,
                        amount=full_monthly_amount,
                        discount_details=discount_details,
                        billing_day=1,
                        start_date=enrollment_date,
                        next_billing_date=next_billing_date,
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
            enrollment_date_child = payment.created_at.astimezone(JERUSALEM_TZ).date()
            lesson_dow_child = payment.lesson.day_of_week if payment.lesson else 1
            _, _, _, next_billing_date_child = _compute_prorate(enrollment_date_child, lesson_dow_child)
            child.status = 'active'
            deferred_start_child = (
                standing_order_next_billing_date(today=enrollment_date_child, lesson=payment.lesson)
                if payment.payment_type == 'recurring_subscription' and payment_is_fee_only(payment)
                else None
            )
            if deferred_start_child:
                # No lesson month is paid for yet; the charge on that date sets it.
                child.subscription_start_date = deferred_start_child
                child.paid_until_date = None
            else:
                child.subscription_start_date = enrollment_date_child
                child.paid_until_date = next_billing_date_child - timedelta(days=1)
            child.save()
            
            # Create LessonEnrollment if payment has an associated lesson
            if payment.lesson:
                enroll_child_in_paid_lessons(
                    child=child,
                    lesson=payment.lesson,
                    bundle=payment.bundle,
                )

                # Notify parent on WhatsApp (non-fatal — never block payment processing).
                try:
                    self._send_registration_whatsapp(payment)
                except Exception:
                    logger.exception("ManyChat registration notification failed (non-fatal)")
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
            
            logger.warning(
                "Payment failed: %s, code=%s, reason=%s. Child %s → payment_problem",
                payment.id,
                payment.failure_code,
                payment.failure_reason,
                child.id,
            )

            # WhatsApp when Tranzila notify reports failure (Response != 000) on subscription enrollment.
            if payment.payment_type == 'recurring_subscription' and payment.lesson_id:
                try:
                    self._send_payment_failed_whatsapp(payment)
                except Exception:
                    logger.exception("ManyChat payment-failed notification failed (non-fatal)")

            return {
                'success': False,
                'payment_id': str(payment.id),
                'error': payment.failure_reason
            }

    @transaction.atomic
    def charge_subscription_with_card(
        self,
        child_id: str,
        lesson_id: str,
        card_number: str,
        expiry_month: int,
        expiry_year: int,
        cvv: str,
        card_holder_id: str = '',
        payment_date: Optional[date] = None,
        bundle_id: Optional[str] = None,
        price_option_id: Optional[str] = None,
        include_registration_fee: bool = True,
        include_monthly_amount: bool = True,
    ) -> Dict:
        """
        Charge a subscription payment directly with card details (synchronous, no iframe/webhook).
        Reuses the same pricing/discount logic as initiate_subscription_payment and the same
        post-success logic as process_webhook_callback.

        bundle_id: when set, bill the widget combined_price on the first member lesson.
        price_option_id: when set, bill the widget catalog price for this lesson.
        include_registration_fee: pass False only when explicitly opting out (rare).
            Default is true, but the fee is still once per child — later lessons skip it.
        include_monthly_amount: pass False for extra bundle days so one standing order
            is created at the full widget price.
        """
        if payment_date is None:
            payment_date = date.today()

        try:
            child = Child.objects.select_related('family').get(id=child_id)
            lesson = Lesson.objects.select_related('course__branch').get(id=lesson_id)
        except (Child.DoesNotExist, Lesson.DoesNotExist) as e:
            raise ValueError("Child or Lesson not found")

        if not include_monthly_amount and not include_registration_fee:
            _, _, _, bundle, _ = resolve_billing_price(child, lesson, bundle_id, price_option_id)
            enroll_child_in_paid_lessons(child=child, lesson=lesson, bundle=bundle)
            return {
                'success': True,
                'enrollment_only': True,
                'payment_id': None,
                'invoice_number': None,
            }

        card = validate_card_details({
            'card_number': card_number,
            'expiry_month': expiry_month,
            'expiry_year': expiry_year,
            'cvv': cvv,
            'card_holder_id': card_holder_id,
        })
        card_number = card['card_number']
        expiry_month = card['expiry_month']
        expiry_year = card['expiry_year']
        cvv = card['cvv']
        card_holder_id = card['card_holder_id']

        # Pricing (identical to initiate_subscription_payment)
        base_price, used_lesson_tier, course_index, bundle, price_option = resolve_billing_price(
            child, lesson, bundle_id, price_option_id
        )
        if not include_monthly_amount:
            base_price = Decimal('0.00')
        elif not base_price:
            raise ValueError("Lesson/Course price not configured")

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

        # Pro-rate the first payment to the remaining lessons of the current month.
        prorate_factor_c, _, _, next_billing_date_c = _compute_prorate(payment_date, lesson.day_of_week)
        full_monthly_amount_c = discount_calculation.final_price
        Child.objects.select_for_update().get(id=child.id)
        charge_fee_c = resolve_include_registration_fee(child, lesson, include_registration_fee)
        registration_fee_c = (
            registration_fee_amount(lesson.course) if charge_fee_c else Decimal('0.00')
        )

        # When monthly billing only starts later, signup charges דמי רישום alone.
        deferred_charge_date_c = deferred_first_charge_date(payment_date)
        next_billing_date_c = standing_order_next_billing_date(today=payment_date, lesson=lesson)
        if deferred_charge_date_c:
            prorated_lesson_c = Decimal('0.00')
        elif full_monthly_amount_c <= 0:
            prorated_lesson_c = Decimal('0.00')
        else:
            prorated_lesson_c = max(
                Decimal('1.00'),
                (full_monthly_amount_c * prorate_factor_c).quantize(Decimal('0.01'))
            )
        prorated_final_c = prorated_lesson_c + registration_fee_c

        # Create Payment (pending)
        payment = Payment.objects.create(
            child=child,
            family=child.family,
            parent=child.family.parents.filter(is_primary=True).first(),
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=bundle,
            price_option=price_option,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=discount_calculation.base_price,
            discount_amount=discount_calculation.total_discount_amount,
            final_amount=prorated_final_c,
            registration_fee=registration_fee_c,
            description=subscription_payment_description(
                child=child,
                lesson=lesson,
                bundle=bundle,
                price_option=price_option,
                fee_only=bool(deferred_charge_date_c),
            ),
        )

        for applied_discount in discount_calculation.applicable_discounts:
            discount_kwargs = {
                'payment': payment,
                'discount_name': applied_discount.name,
                'discount_type': applied_discount.discount_type,
                'discount_value': applied_discount.value,
                'amount_deducted': applied_discount.value,
                'reason': applied_discount.reason
            }
            if applied_discount.discount_id:
                discount_kwargs['discount_id'] = applied_discount.discount_id
            PaymentDiscountSnapshot.objects.create(**discount_kwargs)

        # Charge card via Tranzila REST API
        label = f"{lesson.course.name} - {child.full_name}"
        items = subscription_tranzila_items(
            label=label,
            prorated_lesson=prorated_lesson_c,
            registration_fee=registration_fee_c,
            prorated=not deferred_charge_date_c,
        )

        if prorated_final_c > 0:
            result = self.tranzila_service.charge_with_card(
                card_number=card_number,
                expiry_month=expiry_month,
                expiry_year=expiry_year,
                cvv=cvv,
                card_holder_id=card_holder_id,
                amount=prorated_final_c,
                description=payment.description,
                items=items,
                duplicate_guard_key=f'payment-{payment.id}',
            )
        else:
            reused = saved_card_token_for_child(child)
            if reused:
                result = {
                    'success': True,
                    'token': reused,
                    'transaction_id': '',
                    'confirmation_code': '',
                    'response_code': '000',
                    'raw_response': {'reused_saved_card': True},
                }
            else:
                result = self.tranzila_service.verify_card(
                    card_number=card_number,
                    expiry_month=expiry_month,
                    expiry_year=expiry_year,
                    cvv=cvv,
                    card_holder_id=card_holder_id,
                    amount=full_monthly_amount_c,
                    description=payment.description,
                    duplicate_guard_key=f'verify-{payment.id}',
                )

        if result['success']:
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save()

            # TranzilaTransaction audit record
            tranzila_transaction = TranzilaTransaction.objects.create(
                transaction_id=result.get('transaction_id', ''),
                confirmation_code=result.get('confirmation_code', ''),
                transaction_type='recurring_setup',
                response_code=result.get('response_code', '000'),
                response_message='',
                request_data={},
                response_data=result.get('raw_response', {}),
                # Keyed on the payment so the unique constraint itself prevents a
                # second completed charge for it.
                idempotency_key=f"card_{payment.id}",
                is_successful=True,
                response_timestamp=timezone.now(),
            )
            payment.tranzila_transaction = tranzila_transaction
            payment.save(update_fields=['tranzila_transaction'])

            # RecurringPayment (store token for future charges)
            token = result.get('token', '')
            if not token:
                # Charge succeeded but no saved card came back, so nothing will bill
                # this subscription next month.
                logger.error(
                    "Tranzila returned no card token for payment %s (child=%s) — "
                    "monthly billing will not run until a token is stored",
                    payment.id, child.full_name,
                )
            if token:
                discount_details = [
                    {
                        'name': s.discount_name,
                        'type': s.discount_type,
                        'value': str(s.discount_value),
                        'amount_deducted': str(s.amount_deducted),
                        'reason': s.reason
                    }
                    for s in payment.discount_snapshots.all()
                ]
                if should_create_recurring_for_payment(
                    child=child,
                    bundle=bundle,
                    monthly_amount=full_monthly_amount_c,
                ):
                    RecurringPayment.objects.create(
                        child=child,
                        initial_payment=payment,
                        tranzila_token=token,
                        card_expire_month=expiry_month,
                        card_expire_year=expiry_year,
                        status='active',
                        base_amount=payment.base_amount,
                        discount_amount=payment.discount_amount,
                        amount=full_monthly_amount_c,
                        discount_details=discount_details,
                        billing_day=1,
                        start_date=payment_date,
                        next_billing_date=next_billing_date_c,
                    )

            # Invoice (nothing to invoice when the card was only verified)
            invoice = (
                self._create_invoice_from_payment(payment, tranzila_transaction)
                if payment.final_amount > 0 else None
            )

            # Child status
            child.status = 'active'
            if deferred_charge_date_c:
                # The subscription itself has not started and no month is paid for yet;
                # the recurring charge on that date fills paid_until_date in.
                child.subscription_start_date = next_billing_date_c
                child.paid_until_date = None
            else:
                child.subscription_start_date = payment_date
                child.paid_until_date = next_billing_date_c - timedelta(days=1)
            child.save()

            # LessonEnrollment — a bundle payment enrolls every member day, so extra
            # days never need their own ₪0 Payment row.
            enroll_child_in_paid_lessons(child=child, lesson=lesson, bundle=bundle)

            log_payment_operation("SUBSCRIPTION_CHARGED", child=child.full_name, payment_id=payment.id, amount=payment.final_amount)

            try:
                self._send_registration_whatsapp(payment)
            except Exception:
                logger.exception("Registration WhatsApp failed after card charge (non-fatal)")

            return {
                'success': True,
                'payment_id': str(payment.id),
                'invoice_number': invoice.invoice_number if invoice else None,
                'token_saved': bool(token),
                'bundle_id': str(bundle.id) if bundle else None,
                'base_amount': float(payment.base_amount),
                'discount_amount': float(payment.discount_amount),
                'final_amount': float(payment.final_amount),
                'monthly_amount': float(full_monthly_amount_c),
                'subscription_start_date': (
                    deferred_charge_date_c.isoformat() if deferred_charge_date_c else None
                ),
                'discounts_applied': [
                    {'name': d.name, 'type': d.discount_type, 'value': float(d.value), 'reason': d.reason}
                    for d in discount_calculation.applicable_discounts
                ],
            }
        else:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'Unknown error')
            payment.save()
            child.status = 'payment_problem'
            child.save()
            return {'success': False, 'error': result.get('error', 'התשלום נכשל')}

    @staticmethod
    def _enrollment_whatsapp_context(payment: 'Payment') -> Optional[dict]:
        """Build parent/lesson fields for enrollment-related WhatsApp messages."""
        from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context_from_payment

        return build_enrollment_whatsapp_context_from_payment(payment)

    def _send_registration_whatsapp(self, payment: 'Payment') -> None:
        """WhatsApp confirmation after successful subscription payment (Tranzila Response 000)."""
        from apps.core.manychat_service import ManyChatService

        ctx = self._enrollment_whatsapp_context(payment)
        if not ctx:
            logger.info("Skipping registration WhatsApp: no phone/lesson for payment %s", payment.id)
            return

        lookup_names = ctx.pop('lookup_names', None)
        result = ManyChatService().notify_registration(
            kind=ManyChatService.REGISTRATION_KIND_SUBSCRIPTION,
            lookup_names=lookup_names,
            **ctx,
        )
        self._log_whatsapp_result('registration', ctx['phone'], result)

    def _send_payment_failed_whatsapp(self, payment: 'Payment') -> None:
        """WhatsApp notice after failed subscription payment (Tranzila Response != 000)."""
        from apps.core.manychat_service import ManyChatService

        ctx = self._enrollment_whatsapp_context(payment)
        if not ctx:
            logger.info("Skipping payment-failed WhatsApp: no phone/lesson for payment %s", payment.id)
            return

        lookup_names = ctx.pop('lookup_names', None)
        result = ManyChatService().notify_registration(
            kind=ManyChatService.REGISTRATION_KIND_PAYMENT_FAILED,
            lookup_names=lookup_names,
            **ctx,
        )
        self._log_whatsapp_result(
            f"payment_failed (Tranzila {payment.failure_code or '?'})",
            ctx['phone'],
            result,
        )

    @staticmethod
    def _log_whatsapp_result(label: str, phone: str, result: dict) -> None:
        if result.get('sent'):
            logger.info(
                "WhatsApp %s sent to %s via %s (sub %s)",
                label,
                phone,
                result.get('method'),
                result.get('subscriber_id'),
            )
        else:
            logger.warning("WhatsApp %s NOT sent to %s: %s", label, phone, result.get('reason'))

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

        try:
            from apps.customers.subscription_invoice_email import send_subscription_invoice_email
            send_subscription_invoice_email(invoice)
        except Exception:
            logger.exception('Subscription invoice email failed for %s (non-fatal)', invoice.invoice_number)

        return invoice
    
    def cancel_subscription(
        self,
        recurring_payment_id: str,
        cancellation_reason: str = ''
    ) -> Dict:
        """Cancel a recurring subscription locally and on Tranzila (/sto/update)."""
        try:
            recurring_payment = RecurringPayment.objects.select_related('child').get(
                id=recurring_payment_id
            )
        except RecurringPayment.DoesNotExist:
            raise ValueError("Recurring payment not found")

        if recurring_payment.status == 'cancelled':
            return {'success': True, 'message': 'Subscription already cancelled'}

        tranzila_result = {'success': True}
        if recurring_payment.tranzila_token:
            tranzila_service = TranzilaService.production()
            tranzila_result = tranzila_service.cancel_recurring_payment(
                token=recurring_payment.tranzila_token
            )
            if not tranzila_result.get('success'):
                logger.warning(
                    f"Tranzila STO cancel failed for recurring {recurring_payment.id}: "
                    f"{tranzila_result.get('error')}. Cancelling locally only."
                )

        recurring_payment.status = 'cancelled'
        recurring_payment.cancelled_at = timezone.now()
        if cancellation_reason:
            recurring_payment.cancellation_reason = cancellation_reason
        recurring_payment.save(update_fields=['status', 'cancelled_at', 'cancellation_reason'])

        logger.info(f"Recurring payment {recurring_payment.id} cancelled")
        return {
            'success': True,
            'recurring_id': str(recurring_payment.id),
            'tranzila_cancelled': tranzila_result.get('success', False),
            'manual_cancellation_required': tranzila_result.get('manual_cancellation_required', False),
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
            total_amount += line_charge_amount(product, item['quantity'], item)
        
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
        
        iframe_url = self.iframe_tranzila_service.create_payment_request(
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
            tranzila_items.extend(tranzila_items_for_cart_line(product, item))
        
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
                    unit, total = sale_unit_and_total(product, item)
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=unit,
                        total_price=total,
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch_id=_store_line_item_branch_id(item, product),
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
            product_items = parse_store_cart_notes(invoice.notes) or []
            
            # Update invoice
            invoice.payment_status = 'completed'
            invoice.tranzila_transaction_id = tranzila_response.get('transaction_id', '')
            invoice.tranzila_confirmation_code = tranzila_response.get('confirmation_code', '')
            invoice.save()
            
            # Create sales and update stock
            with transaction.atomic():
                for item in product_items:
                    product = StoreProduct.objects.select_for_update().get(id=item['product_id'])
                    
                    unit, total = sale_unit_and_total(product, item)
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=invoice.child,
                        quantity=item['quantity'],
                        unit_price=unit,
                        total_price=total,
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch_id=_store_line_item_branch_id(item, product),
                        notes=''
                    )

                    _decrement_product_stock(product, item)

            logger.info(f"Successfully completed webhook purchase for invoice {invoice.invoice_number}")

            if invoice.website_order_number:
                from apps.store.website_integration import notify_website_order_status, push_product_to_website
                from apps.store.invoice_email import send_store_invoice_email
                notify_website_order_status(
                    website_order_number=invoice.website_order_number,
                    invoice_number=invoice.invoice_number,
                    invoice_id=str(invoice.id),
                    status='paid',
                    provider_txn_id=tranzila_response.get('transaction_id', ''),
                )
                for item in product_items:
                    try:
                        product = StoreProduct.objects.get(id=item['product_id'])
                        push_product_to_website(product)
                    except StoreProduct.DoesNotExist:
                        pass
                try:
                    from apps.store.tranzila_store_invoice import issue_store_tranzila_document
                    issue_store_tranzila_document(invoice)
                except Exception:
                    logger.exception(
                        'Tranzila store document failed for %s (non-fatal)',
                        invoice.invoice_number,
                    )
                try:
                    send_store_invoice_email(invoice)
                except Exception:
                    logger.exception(
                        'Store invoice email failed for %s (non-fatal)',
                        invoice.invoice_number,
                    )

            return {'success': True, 'invoice_id': str(invoice.id)}
        else:
            # Keep cart JSON in notes so the customer can retry the same order.
            invoice.payment_status = 'failed'
            invoice.save(update_fields=['payment_status'])
            logger.warning(
                "Store iframe payment failed invoice=%s code=%s error=%s",
                invoice.invoice_number,
                tranzila_response.get('response_code'),
                tranzila_response.get('error_message', 'Unknown'),
            )

            if invoice.website_order_number:
                from apps.store.website_integration import notify_website_order_status
                notify_website_order_status(
                    website_order_number=invoice.website_order_number,
                    invoice_number=invoice.invoice_number,
                    invoice_id=str(invoice.id),
                    status='failed',
                    provider_txn_id=tranzila_response.get('transaction_id', ''),
                )
            
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
            total_amount += line_charge_amount(product, item['quantity'], item)
        
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
                
                unit, total = sale_unit_and_total(product, item)
                StoreSale.objects.create(
                    invoice=invoice,
                    product=product,
                    child=child,
                    quantity=item['quantity'],
                    unit_price=unit,
                    total_price=total,
                    size=item.get('size', ''),
                    payment_method=payment_method,
                    branch_id=_store_line_item_branch_id(item, product),
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
        
        # Prefer the saved card from THIS payment's subscription. A child with two
        # lessons can have two tokens; .first() on the child would refund the wrong one.
        # Monthly cron charges are not the initial_payment — match by lesson too.
        card_expire_month, card_expire_year, token = card_details_for_payment_refund(payment)
        
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

