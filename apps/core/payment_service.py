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
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.db.utils import OperationalError
from django.utils import timezone

JERUSALEM_TZ = ZoneInfo('Asia/Jerusalem')

from apps.customers.models import (
    Child, Family, Parent, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot
)
from apps.customers.financial_models import Invoice, InvoiceChild, Discount
from apps.customers.discount_service import DiscountService
from apps.customers.trial_credit import credit_for_lesson, describe as describe_trial_credit
from apps.core.card_validation import validate_card_details
from apps.core.tranzila_service import (
    TOKEN_CHARGED,
    TOKEN_DECLINED,
    TOKEN_REQUEST_REJECTED,
    TOKEN_SETUP_PROBLEM,
    TranzilaService,
    invoice_id_from_pdesc,
    is_tranzila_uncertain_gateway_error,
    token_charge_outcome,
)
from apps.courses.models import Lesson, LessonBundle, LessonPriceOption
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.enrollment_counts import count_capacity_enrollments, paying_enrollments
from apps.instructors.utils import get_lesson_price_for_course_index
from apps.store.stock_utils import (
    decrement_product_stock as _decrement_product_stock,
    restore_stock_for_sale as _restore_stock_for_sale,
    store_line_item_branch_id as _store_line_item_branch_id,
)
from apps.store.pricing import line_charge_amount, sale_unit_and_total, tranzila_items_for_cart_line

logger = logging.getLogger(__name__)

BILLING_ENROLLMENT_STATUSES = ("active", "payments_problem")
ALREADY_REGISTERED_LESSON_ERROR = 'הילד כבר רשום לחוג זה'


def _sign_store_sale(invoice) -> None:
    """
    The paid sale's signed original: recorded now, signed after the commit
    (apps/documents/signing; a no-op while DOCUMENT_SIGNING_ENABLED is off).
    Only a website order is mailed by kogo (apps/store/invoice_email.py); a
    till sale's original goes into the archive.
    """
    from apps.documents.models import SignedOriginal
    from apps.documents.signing.service import KIND_STORE, issue

    issue(
        KIND_STORE, invoice,
        channel=SignedOriginal.CHANNEL_STORE if invoice.website_order_number else '',
        email_to=(invoice.customer_email or '').strip(),
    )


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

    # Only a row from the same checkout counts as "in flight" (the same two
    # hours `get_child_lesson_index_for_billing` uses). An abandoned pending
    # row from a previous visit — a closed dialog, a cart never paid — used to
    # hide the fee from every later signup of this child on another lesson.
    in_flight = payments.filter(
        registration_fee__gt=0,
        status__in=('pending', 'processing'),
        created_at__gte=timezone.now() - timedelta(hours=2),
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
    """
    Pro-rated lesson portion of a pending/completed first subscription charge.

    A paid-trial credit is added back: it lowered what the card was charged, not
    the month the parent bought. Without this a credited signup would look
    fee-only and its paid-until date would be dropped.
    """
    fee = payment.registration_fee or Decimal('0.00')
    credit = getattr(payment, 'trial_credit_amount', None) or Decimal('0.00')
    return (payment.final_amount - fee + credit).quantize(Decimal('0.01'))


def enroll_child_in_paid_lessons(*, child, lesson, bundle=None) -> None:
    """Activate the paid lesson, and every other day of a twice/thrice-a-week bundle.

    Extra bundle days must not get their own Payment (that showed up as ₪0 דמי רישום).
    """
    members = list(bundle.lessons.all()) if bundle is not None else []
    lessons = members or ([lesson] if lesson is not None else [])
    if lesson is not None and lesson not in lessons:
        lessons = [lesson] + lessons
    today = timezone.now().astimezone(JERUSALEM_TZ).date()
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
        # A paid row runs until something ends it. The row reused here is often
        # the trial's, which the trial cron closed with end_date = the trial day;
        # left in place, that date ended a paying subscription the day it began.
        # The register ignores end_date, so nobody saw it — but the instructor's
        # dashboard, the salary tiers and the monthly snapshots read it, and all
        # three quietly dropped the child from the month after the trial. On
        # 23.9.2026 that was 82 paying children. A cancellation writes its own
        # end_date after this, so clearing it here takes nothing away.
        enrollment.end_date = None
        if bundle and not enrollment.bundle:
            enrollment.bundle = bundle
        # A trial row reused as the paying one kept its trial date, and the roster
        # shows a trial-dated enrollment on that one date only — so a child who
        # trialled here and then subscribed dropped off the register the next
        # week. The trial itself stays on record in trial_outcome, the attendance
        # rows and Child.trial_classes_attended — stamped here, because the
        # parent usually pays on the evening of the trial, before the cron that
        # would have recorded it runs.
        if enrollment.trial_lesson_date:
            _record_trial_outcome_before_conversion(enrollment, today)
            enrollment.trial_lesson_date = None
        enrollment.save()

    # Any other trial row this child still holds from a trial that already took
    # place is history now. Left 'active' it would start filling that lesson's
    # capacity the moment the child's status became active, since capacity
    # excludes children by status. A trial still booked for a coming date stays —
    # the parent was told about it.
    stale = LessonEnrollment.objects.filter(
        child=child, status='active', trial_lesson_date__isnull=False, trial_lesson_date__lt=today,
    ).exclude(lesson__in=lessons)
    for row in stale:
        _record_trial_outcome_before_conversion(row, today)
        row.status = 'inactive'
        row.end_date = row.trial_lesson_date
        row.save(update_fields=['status', 'end_date', 'trial_outcome', 'updated_at'])


def _record_trial_outcome_before_conversion(enrollment, today) -> None:
    """Write הגיע / לא הגיע on a trial row the conversion is about to close.

    Only for a trial that has happened: a past date, or today's date once the
    register was marked. A trial booked for a coming date has no outcome yet.
    """
    from apps.enrollments.trial_reminders import _trial_outcome_for
    from apps.customers.models import Child

    if enrollment.trial_outcome or not enrollment.trial_lesson_date:
        return
    outcome = _trial_outcome_for(enrollment)
    if enrollment.trial_lesson_date >= today and outcome == 'unmarked':
        return
    enrollment.trial_outcome = outcome
    if outcome == 'attended':
        Child.objects.filter(pk=enrollment.child_id).update(
            trial_classes_attended=F('trial_classes_attended') + 1,
        )


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


def lessons_covered_by_selection(*, lesson=None, bundle=None):
    """Lessons a widget/CRM signup will enroll: the picked lesson plus bundle members."""
    members = list(bundle.lessons.all()) if bundle is not None else []
    if lesson is not None and lesson not in members:
        members = [lesson] + members
    return members


def child_has_standing_order_for_lessons(child, lessons) -> bool:
    """True when an active/paused standing order already bills any of these lessons."""
    lesson_ids = [getattr(item, 'id', item) for item in lessons if item is not None]
    if not child or not lesson_ids:
        return False
    return RecurringPayment.objects.filter(
        child=child,
        status__in=('active', 'paused'),
    ).filter(
        Q(initial_payment__lesson_id__in=lesson_ids)
        | Q(initial_payment__bundle__lessons__id__in=lesson_ids)
    ).exists()


def child_already_registered_for_lessons(child, lessons) -> bool:
    """True when this child is already a paying student or has an STO for any lesson."""
    lesson_ids = [getattr(item, 'id', item) for item in lessons if item is not None]
    if not child or not lesson_ids:
        return False
    if paying_enrollments(
        LessonEnrollment.objects.filter(child=child, lesson_id__in=lesson_ids)
    ).exists():
        return True
    return child_has_standing_order_for_lessons(child, lesson_ids)


def enrolled_or_billed_lesson_ids(child) -> list[str]:
    """Lesson UUIDs this child already pays for (roster or standing order)."""
    if child is None:
        return []
    ids = {
        str(lesson_id)
        for lesson_id in paying_enrollments(
            LessonEnrollment.objects.filter(child=child)
        ).values_list('lesson_id', flat=True)
    }
    stos = (
        RecurringPayment.objects
        .filter(child=child, status__in=('active', 'paused'))
        .select_related('initial_payment')
        .prefetch_related('initial_payment__bundle__lessons')
    )
    for recurring in stos:
        payment = recurring.initial_payment
        if payment is None:
            continue
        if payment.lesson_id:
            ids.add(str(payment.lesson_id))
        bundle = payment.bundle
        if bundle is not None:
            ids.update(str(lesson_id) for lesson_id in bundle.lessons.values_list('id', flat=True))
    return list(ids)


def should_create_recurring_for_payment(*, child, bundle, monthly_amount: Decimal, lesson=None) -> bool:
    """One standing order per lesson (or twice/thrice-a-week bundle), at the full widget price."""
    if monthly_amount <= 0:
        return False
    covered = lessons_covered_by_selection(lesson=lesson, bundle=bundle)
    if child_has_standing_order_for_lessons(child, covered):
        return False
    if bundle is None:
        return True
    return not RecurringPayment.objects.filter(
        child=child,
        status='active',
        initial_payment__bundle=bundle,
    ).exists()


def saved_card_token_for_child(child, terminal: str = '') -> str:
    """
    Token already stored for this child from an earlier lesson in the same signup.

    A twice-a-week bundle charges דמי רישום on the first day only; the other days
    are ₪0 and must not call Tranzila verify (that path returned schema 20004).

    Only a card saved on `terminal` is reused ('' — the michal pair, where the
    signup's own card is made by production()). The new standing order is
    written with that terminal; a card from another terminal would be charged
    on the wrong one and declined.
    """
    if child is None:
        return ''
    rec = (
        RecurringPayment.objects
        .filter(child=child, status='active', tranzila_terminal=(terminal or '').strip())
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
    trial_credit: Decimal = Decimal('0.00'),
) -> list[dict]:
    """
    Tranzila line items for a first subscription charge.

    The lesson line is dropped when there is nothing to bill for it, which is how a
    registration whose monthly billing only starts later charges דמי רישום alone.
    A paid-trial credit comes off the lesson line first and then the fee, so the
    lines always add up to the amount the card is charged (the gateway is never
    sent a negative line).
    """
    credit = (trial_credit or Decimal('0.00')).quantize(Decimal('0.01'))
    credited_label = ''
    if credit > 0:
        take = min(credit, max(prorated_lesson, Decimal('0.00')))
        prorated_lesson = prorated_lesson - take
        registration_fee = max(Decimal('0.00'), registration_fee - (credit - take))
        credited_label = ' (בניכוי שיעור ניסיון)'
    items = []
    if prorated_lesson > 0:
        items.append({
            'name': (
                f'מנוי חודשי (יחסי){credited_label} - {label}' if prorated
                else f'מנוי חודשי{credited_label} - {label}'
            ),
            'type': 'I',
            'unit_price': float(prorated_lesson),
            'units_number': 1,
            'unit_type': 1,
            'price_type': 'G',
            'currency_code': 'ILS',
        })
    if registration_fee > 0:
        items.append({
            'name': f'דמי רישום{credited_label}' if credit > 0 and prorated_lesson <= 0 else 'דמי רישום',
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
    # Not `paying_enrollments`: that helper keeps only `active` rows, which
    # silently dropped the payments-problem half of BILLING_ENROLLMENT_STATUSES
    # and priced a child's second lesson as a first one while the first was
    # waiting on a card. A trial booking is still not a signed lesson.
    signed_lesson_ids = set(
        LessonEnrollment.objects.filter(
            child=child,
            status__in=BILLING_ENROLLMENT_STATUSES,
            trial_lesson_date__isnull=True,
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


def validate_bundle_capacity(bundle: 'LessonBundle', *, seated_child=None) -> None:
    """
    Raise ValueError naming the first lesson without capacity. Called before any
    charge is made for a bundle registration so the whole registration fails
    fast rather than leaving the family charged for only some of the lessons.

    Seat count matches the office roster and widget catalog: only paying
    students occupy a place. Trial signups never fill the class.

    `seated_child` is a child being billed again for seats they already hold
    (a card link, a replaced card). Their own seat is not a new place, so a
    full class must not refuse them.
    """
    for lesson in bundle.lessons.select_related('room', 'course').all():
        if seated_child is not None and paying_enrollments(
            LessonEnrollment.objects.filter(child=seated_child, lesson=lesson)
        ).exists():
            continue
        if not lesson.room:
            raise ValueError(f"לא ניתן להירשם למסלול — לשיעור {lesson} אין חדר מוגדר")
        caps = []
        if lesson.course and lesson.course.capacity:
            caps.append(int(lesson.course.capacity))
        if lesson.room.capacity:
            caps.append(int(lesson.room.capacity))
        capacity = min(caps) if caps else int(lesson.room.capacity)
        if count_capacity_enrollments(lesson=lesson) >= capacity:
            raise ValueError(f"השיעור {lesson} מלא - קיבולת מקסימלית: {capacity} תלמידים")


def resolve_billing_price(
    child: Child,
    lesson: Lesson,
    bundle_id: Optional[str] = None,
    price_option_id: Optional[str] = None,
    *,
    seated_child: Optional[Child] = None,
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
        validate_bundle_capacity(bundle, seated_child=seated_child)
        if bundle.course.must_attend_all_lessons:
            return bundle.course.price, True, course_index, bundle, None
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


def _expiry_and_token_from_tranzila_payload(payment):
    """Read expiry + token from the charge's own Tranzila response when the STO is gone."""
    txn = getattr(payment, 'tranzila_transaction', None)
    data = getattr(txn, 'response_data', None) if txn else None
    if not isinstance(data, dict):
        return None, None, None
    original = data.get('original_request') if isinstance(data.get('original_request'), dict) else {}
    result = data.get('transaction_result') if isinstance(data.get('transaction_result'), dict) else {}
    month = original.get('expire_month') or result.get('expiry_month')
    year = original.get('expire_year') or result.get('expiry_year')
    token = result.get('token') or original.get('card_number')
    try:
        month = int(month) if month not in (None, '') else None
    except (TypeError, ValueError):
        month = None
    try:
        year = int(year) if year not in (None, '') else None
    except (TypeError, ValueError):
        year = None
    token = str(token).strip() if token else None
    if token and set(token) <= set('*'):
        token = None
    return month, year, token


def terminal_for_payment_refund(payment):
    """Terminal that took the original charge — refunds must use the same one."""
    txn = getattr(payment, 'tranzila_transaction', None)
    data = getattr(txn, 'response_data', None) if txn else None
    if not isinstance(data, dict):
        return None
    original = data.get('original_request') if isinstance(data.get('original_request'), dict) else {}
    name = (original.get('terminal_name') or '').strip()
    return name or None


def card_details_for_payment_refund(payment, terminal: str = ''):
    """Card token + expiry to refund this Payment via Tranzila.

    Signup charges sit on RecurringPayment.initial_payment. Monthly cron
    charges do not — match the standing order for the same child + lesson.
    Cancelled standing orders still hold the card; fall back to the charge payload.
    Only a standing order whose card is on the charge's terminal ('' — the
    michal pair) can hold the card that paid.
    """
    month = year = token = None
    if getattr(payment, 'child_id', None):
        qs = payment.child.recurring_payments.filter(tranzila_terminal=(terminal or '').strip())
        recurring = qs.filter(initial_payment_id=payment.id).first()
        if not recurring and payment.lesson_id:
            recurring = (
                qs.filter(initial_payment__lesson_id=payment.lesson_id)
                .order_by('-updated_at')
                .first()
            )
        if not recurring:
            recurring = (
                qs.exclude(tranzila_token='')
                .order_by('-updated_at')
                .first()
            )
        if recurring:
            month = recurring.card_expire_month
            year = recurring.card_expire_year
            token = recurring.tranzila_token or None

    payload_month, payload_year, payload_token = _expiry_and_token_from_tranzila_payload(payment)
    return (
        month or payload_month,
        year or payload_year,
        token or payload_token,
    )


# A till charge Tranzila did not answer. Kept on the invoice so the office can
# find it, and so a repeat of the same checkout charges nothing.
TILL_CHARGE_UNCERTAIN_MARK = 'לא ודאי'
TILL_CHARGE_UNCERTAIN_MESSAGE = 'לא ידוע אם החיוב עבר. בדקו בטרנזילה לפני שמנסים שוב.'
TILL_TOKEN_CHARGE_BUSY_MESSAGE = (
    'לילד הזה יש קנייה בכרטיס השמור שעוד לא נסגרה (חשבונית {number}). '
    'בדקו בטרנזילה אם היא נגבתה לפני קנייה נוספת בכרטיס השמור.'
)

REFUND_ALREADY_CLAIMED = (
    'זיכוי קודם לתשלום הזה עדיין רץ, או שלא התקבלה עליו תשובה מטרנזילה. '
    'יש לבדוק בטרנזילה אם הזיכוי בוצע לפני ניסיון נוסף.'
)
REFUND_UNCERTAIN = (
    'לא התקבלה תשובה מטרנזילה, וייתכן שהזיכוי בוצע. אל תנסו שוב: '
    'יש לבדוק בטרנזילה. עד אז המערכת לא תאפשר זיכוי נוסף לתשלום הזה.'
)


def _claim_refund(key: str, *, terminal: str, request_data: dict):
    """
    The row that holds a refund while it runs, or None when another holds it.

    The key is unique: a second attempt loses the insert. An unanswered
    refund leaves the row unsettled (is_successful False), which keeps every
    later attempt away until someone has looked at the terminal.
    """
    from apps.customers.models import TranzilaTransaction

    try:
        with transaction.atomic():
            return TranzilaTransaction.objects.create(
                transaction_id='',
                confirmation_code='',
                transaction_type='refund',
                response_code='',
                response_message='',
                request_data=request_data,
                response_data={},
                idempotency_key=key,
                is_successful=False,
                tranzila_terminal=(terminal or '')[:40],
            )
    except IntegrityError:
        return None


def _settle_refund_claim(claim, result: dict):
    claim.transaction_id = str(result.get('transaction_id', '') or '')[:100]
    claim.confirmation_code = str(result.get('confirmation_code', '') or '')[:100]
    claim.response_code = str(result.get('response_code', '000') or '000')[:10]
    claim.response_message = str(result.get('message', '') or '')
    claim.response_data = result.get('raw_response', {}) or {}
    claim.is_successful = True
    claim.response_timestamp = timezone.now()
    claim.save(update_fields=[
        'transaction_id', 'confirmation_code', 'response_code', 'response_message',
        'response_data', 'is_successful', 'response_timestamp',
    ])
    return claim


def _alert_refund_uncertain(*, key, what, why, family, child, amount, terminal) -> None:
    """Tell the office at once: a refund with no answer blocks every retry until someone checks."""
    try:
        from apps.core.office_alerts import crm_child_link, describe_family, raise_office_alert

        raise_office_alert(
            kind='refund_uncertain', dedup_key=key,
            title='לא ידוע אם הזיכוי בוצע',
            where='זיכוי תשלום מהמערכת',
            what=what + ' המערכת לא תאפשר זיכוי נוסף עד בדיקה.',
            why=str(why)[:300],
            customer=describe_family(family, children=[child] if child else [], amount=amount),
            action=f'לבדוק בטרנזילה (מסוף {terminal}) אם הזיכוי בוצע, ולסגור את שורת הזיכוי.',
            link=crm_child_link(child.id if child else None),
        )
    except Exception:
        logger.exception('Refund alert failed (non-fatal)')


def _drop_refund_claim(claim) -> None:
    """Tranzila answered no: nothing was refunded, so the next attempt may run."""
    from apps.customers.models import TranzilaTransaction

    TranzilaTransaction.objects.filter(pk=claim.pk, is_successful=False).delete()


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
        quote_only: bool = False,
    ) -> Dict:
        """
        Initiate a recurring subscription payment for a child's lesson enrollment.

        Flow:
        1. Validate child and lesson
        2. Get lesson pricing
        3. Calculate discounts
        4. Create Payment record (pending)
        5. Return payment details for frontend (`tranzila_url` is always None;
           the card is charged afterwards through the REST API)

        With `quote_only` step 4 is skipped: the same figures come back
        with `payment_id` set to None, and nothing is written.
        The office's subscription dialog prices a lesson this way; the row it
        used to leave behind on every open carried a registration fee that hid
        the fee from the child's next signup and posed as a sibling signing up.

        Args:
            child_id: UUID of child
            lesson_id: UUID of lesson
            payment_date: Date of payment (default: today)
            success_url, error_url, callback_url: accepted for the callers that
                still send them; unused since no hosted-page link is built here.
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
            Dict with payment_id, tranzila_url (None), amount, discounts_applied
        """
        if payment_date is None:
            # The server clock is UTC; the discount ranges and the proration are
            # Israeli calendar days. `date.today()` here made an early-signup
            # range end three hours early and start three hours late.
            payment_date = timezone.now().astimezone(JERUSALEM_TZ).date()

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
        if child_already_registered_for_lessons(
            child,
            lessons_covered_by_selection(lesson=lesson, bundle=bundle),
        ):
            raise ValueError(ALREADY_REGISTERED_LESSON_ERROR)
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

        def first_charge_figures():
            charge_fee = resolve_include_registration_fee(child, lesson, include_registration_fee)
            fee = registration_fee_amount(lesson.course) if charge_fee else Decimal('0.00')
            # A paid trial already settled for this branch comes off this
            # first charge, once. It never touches the monthly amount.
            credit = credit_for_lesson(child, lesson, first_charge=prorated_lesson + fee, today=today_local)
            return fee, credit, prorated_lesson + fee - credit['amount']

        # Create Payment record (pending) with retry (SQLite can throw "database is locked" under concurrency).
        # A quote computes the same figures under no lock and writes nothing.
        payment = None
        registration_fee = Decimal('0.00')
        prorated_final = prorated_lesson
        if quote_only:
            registration_fee, trial_credit, prorated_final = first_charge_figures()
        max_attempts = 0 if quote_only else 5
        for attempt in range(1, max_attempts + 1):
            try:
                with transaction.atomic():
                    Child.objects.select_for_update().get(id=child.id)
                    registration_fee, trial_credit, prorated_final = first_charge_figures()
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
                        trial_credit_amount=trial_credit['amount'],
                        trial_credit_source=trial_credit['source'],
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

        if payment is None and not quote_only:
            raise RuntimeError("Failed to create payment record")

        # No hosted-page link is built here any more. Nothing opened it — the widget
        # and the office charge the card through the REST API on the production
        # terminals — yet building it made a handshake on TRANZILA_TOKEN_TERMINAL,
        # so a hosted-page terminal that refused the handshake failed every signup.
        # The key stays in the answer, always None, for callers that read it.
        tranzila_url = None
        if not quote_only:
            log_payment_operation(
                "SUBSCRIPTION_INITIATED",
                child=child.full_name,
                payment_id=payment.id,
                amount=discount_calculation.final_price
            )

        return {
            'payment_id': str(payment.id) if payment is not None else None,
            'tranzila_url': tranzila_url,
            'course_index': course_index,
            'bundle_id': str(bundle.id) if bundle else None,
            'base_amount': float(discount_calculation.base_price),
            'discount_amount': float(discount_calculation.total_discount_amount),
            'prorated_amount': float(prorated_lesson),
            'registration_fee': float(registration_fee),
            'final_amount': float(prorated_final),
            **describe_trial_credit(trial_credit),
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
                    lesson=payment.lesson,
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
            # The server clock is UTC; the discount ranges and the proration are
            # Israeli calendar days. `date.today()` here made an early-signup
            # range end three hours early and start three hours late.
            payment_date = timezone.now().astimezone(JERUSALEM_TZ).date()

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
        # Locked before the checks below, not after them. A second click on
        # "charge" waited for the first request to finish — the charge and the
        # registration included — and then went on with checks it had made
        # before that registration existed, charging the card again.
        Child.objects.select_for_update().get(id=child.id)
        if child_already_registered_for_lessons(
            child,
            lessons_covered_by_selection(lesson=lesson, bundle=bundle),
        ):
            raise ValueError(ALREADY_REGISTERED_LESSON_ERROR)
        # A charge for this lesson that never got its answer (a timeout after
        # Tranzila may already have taken the money) is still open. The office
        # settles it against the terminal before a second card request goes out.
        if Payment.objects.filter(
            child=child, lesson=lesson, payment_type='recurring_subscription', status='processing',
        ).exists():
            raise ValueError(
                'חיוב קודם לילד על השיעור הזה עדיין בבדיקה מול הסליקה — יש לברר בטרנזילה ולסגור אותו לפני חיוב נוסף'
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
        trial_credit_c = credit_for_lesson(
            child, lesson, first_charge=prorated_lesson_c + registration_fee_c, today=payment_date,
        )
        prorated_final_c = prorated_lesson_c + registration_fee_c - trial_credit_c['amount']

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
            trial_credit_amount=trial_credit_c['amount'],
            trial_credit_source=trial_credit_c['source'],
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
            trial_credit=trial_credit_c['amount'],
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
                    lesson=lesson,
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

            # Invoice (nothing to invoice when the card was only verified). The card
            # is charged by now and this method is one transaction: a receipt that
            # raised rolled back the payment, the standing order and the
            # enrollment, so the office saw an error for money already taken and
            # could charge again. The receipt's own savepoint fails alone;
            # `check_invoices` issues it later.
            invoice = None
            if payment.final_amount > 0:
                try:
                    invoice = self._create_invoice_from_payment(payment, tranzila_transaction)
                except Exception:
                    logger.exception('Receipt not issued for payment %s (the charge is recorded)', payment.id)

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
        elif is_tranzila_uncertain_gateway_error(result):
            # No answer came back: the card may already have been charged. Not a
            # decline — the row stays `processing` (the guard above keeps a second
            # charge for this lesson out) and the child is not flagged.
            payment.status = 'processing'
            payment.failure_reason = result.get('error', 'no answer from the gateway')
            payment.save()
            logger.error(
                'Subscription charge uncertain for payment %s — leaving processing: %s',
                payment.id, payment.failure_reason,
            )
            return {
                'success': False,
                'uncertain': True,
                'payment_id': str(payment.id),
                'error': 'לא התקבלה תשובה מהסליקה — ייתכן שהכרטיס חויב. אל תחייבו שוב לפני בדיקה בטרנזילה.',
            }
        else:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'Unknown error')
            payment.failure_code = str(result.get('response_code', ''))[:50]
            payment.save()
            # A first charge that failed booked nothing — no enrolment, no
            # standing order — so there is nothing to bill and no card problem
            # to chase. It used to set בעיה באשראי on every child, including one
            # who already pays for another course (27.9.2026). A new child is
            # בתהליך רישום; anyone else keeps the status they had.
            if child.status in ('pending', 'inactive'):
                if child.status != 'pending':
                    child.status = 'pending'
                    child.save(update_fields=['status', 'updated_at'])
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
        tranzila_transaction: Optional[TranzilaTransaction],
        *,
        send_email: bool = True,
        invoice_date=None,
    ) -> Invoice:
        """
        Issue the חשבונית מס / קבלה for a completed Payment.

        The number comes from the IR series, in the same transaction as the
        document, so a failed save gives the number back and the run stays
        gapless. `invoice_date` is when the money was received — it defaults to
        now; a late issue (`check_invoices --fix`) passes the original charge date.
        """
        from apps.documents.numbering import SERIES_SUBSCRIPTION, next_document_number

        issued_on = invoice_date or timezone.now()
        with transaction.atomic():
            invoice = Invoice.objects.create(
                invoice_number=next_document_number(SERIES_SUBSCRIPTION, issued_on),
                family=payment.family,
                parent=payment.parent,
                branch=payment.branch,
                payment=payment,
                amount=payment.final_amount,
                status='paid',
                payment_method='credit_card' if tranzila_transaction is not None else '',
                payment_type='recurring' if payment.payment_type == 'recurring_subscription' else 'one_time',
                payer_name=payment.family.name,
                payer_email=payment.family.email,
                payer_phone=payment.family.phone,
                tranzila_transaction_id=tranzila_transaction.transaction_id if tranzila_transaction is not None else '',
                invoice_date=issued_on,
            )

            # Link the child to the invoice with lesson/product details
            if payment.child:
                InvoiceChild.objects.create(
                    invoice=invoice,
                    child=payment.child,
                    course=payment.lesson.course if payment.lesson else None,
                    lesson=payment.lesson,
                    product=getattr(payment, 'product', None)  # Use getattr for safer access
                )

            # The signed original's row commits with the receipt; the signature
            # follows after the commit, never inside the charge (a no-op while
            # DOCUMENT_SIGNING_ENABLED is off).
            from apps.documents.models import SignedOriginal
            from apps.documents.signing.service import KIND_IR, issue as issue_signed_original

            issue_signed_original(KIND_IR, invoice, channel=SignedOriginal.CHANNEL_IR if send_email else '')

        logger.info(f"Created invoice: {invoice.invoice_number}")

        if send_email:
            def _email():
                try:
                    from apps.customers.subscription_invoice_email import send_subscription_invoice_email
                    send_subscription_invoice_email(invoice)
                except Exception:
                    logger.exception('Subscription invoice email failed for %s (non-fatal)', invoice.invoice_number)

            # After the commit, never before. Emailed from inside a transaction that
            # then rolled back, the parent held a number the run hands out again to
            # someone else; and the series' row lock stayed held for the email's
            # round trip. With no transaction open this runs at once.
            transaction.on_commit(_email)

        return invoice
    
    def cancel_subscription(
        self,
        recurring_payment_id: str,
        cancellation_reason: str = '',
        recheck_status: bool = True,
    ) -> Dict:
        """
        Cancel a recurring subscription locally and on Tranzila (/sto/update).

        The child's status is worked out again at once, unless the caller does
        that itself after more changes (`recheck_status=False`, change_course).
        """
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
            # The terminal the card was saved on, with its own keys.
            tranzila_service = TranzilaService.for_saved_card(recurring_payment.tranzila_terminal)
            if tranzila_service is None:
                tranzila_result = {
                    'success': False,
                    'error': f'למסוף {recurring_payment.tranzila_terminal} אין מפתחות בשרת',
                    'manual_cancellation_required': True,
                }
            else:
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

        from apps.customers.child_status import recheck_after_money_stopped

        if recheck_status:
            recheck_after_money_stopped(recurring_payment.child, reason='הוראת הקבע בוטלה')

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
                
                # A saved card the server can charge: on a terminal it has keys for.
                recurring = None
                for candidate in (
                    RecurringPayment.objects
                    .filter(child=child, status='active', tranzila_token__isnull=False)
                    .exclude(tranzila_token='')
                ):
                    if TranzilaService.for_saved_card(candidate.tranzila_terminal) is not None:
                        recurring = candidate
                        break
                
                if recurring and recurring.tranzila_token:
                    # SYNCHRONOUS TOKEN CHARGE
                    log_payment_operation("STORE_TOKEN_CHARGE", child=child.full_name, amount=total_amount)
                    
                    # Create invoice (pending). The till sends each line's branch as
                    # an id string (or 'delivery' / nothing), never a Branch instance.
                    first_item = product_items[0] if product_items else None
                    first_product = StoreProduct.objects.get(id=first_item['product_id']) if first_item else None
                    # One saved-card purchase per child at a time. The till sends
                    # no key of its own on this path, and a failure used to read
                    # "failed" even when Tranzila never answered — the seller
                    # pressed again and the card was charged twice. The child row
                    # is locked while the invoice is opened, so two presses at
                    # once meet here one after the other.
                    with transaction.atomic():
                        Child.objects.select_for_update().filter(id=child.id).first()
                        busy = (
                            StoreInvoice.objects
                            .filter(
                                child=child,
                                charged_with_token=True,
                                payment_status='pending',
                                created_at__gte=timezone.now() - timedelta(days=1),
                            )
                            .order_by('-created_at')
                            .first()
                        )
                        if busy is not None:
                            logger.error('Till token charge refused for child %s: invoice %s still open',
                                         child.id, busy.invoice_number)
                            return {
                                'requires_iframe': False,
                                'success': False,
                                'uncertain': True,
                                'error': TILL_TOKEN_CHARGE_BUSY_MESSAGE.format(number=busy.invoice_number),
                            }
                        invoice = StoreInvoice.objects.create(
                            child=child,
                            total_amount=total_amount,
                            payment_method='credit_card',
                            payment_status='pending',
                            charged_with_token=True,
                            branch_id=_store_line_item_branch_id(first_item, first_product) if first_item else None,
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
                        'uncertain': bool(result.get('uncertain')),
                        'error': result.get('error')
                    }
            except Child.DoesNotExist:
                logger.warning(f"Child not found: {child_id}")
                child_id = None  # Fall through to iframe
        
        # IFRAME FALLBACK (no token or walk-in customer)
        if not getattr(settings, 'TRANZILA_HOSTED_PAGE_ENABLED', False):
            # The hosted page is on a test terminal and charged nobody. The till
            # types the card instead (store/payment/charge-card/, the business
            # terminal). Refused before anything is written, so no pending
            # invoice is left behind.
            from apps.core.tranzila_service import HOSTED_PAGE_DISABLED_MESSAGE
            return {
                'requires_iframe': False,
                'success': False,
                'use_direct_card': True,
                'error': HOSTED_PAGE_DISABLED_MESSAGE,
            }
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
            transaction_id=str(invoice.id),
            offer_wallets=True,
        )
        
        # Store product items in invoice notes for webhook processing
        import json
        invoice.notes = json.dumps(product_items)
        # A hosted page was handed out for this invoice: only such an invoice
        # may be paid by a notify (complete_store_purchase_from_webhook).
        invoice.payment_page_opened_at = timezone.now()
        invoice.payment_page_first_opened_at = invoice.payment_page_opened_at
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
        from apps.store.stock_utils import available_stock_for_item as _available_stock_for_item
        
        # Build items list for Tranzila API
        # Only include required fields to avoid validation errors
        tranzila_items = []
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            # Asked before the card is charged, and of the row this line draws
            # on (a size at a location), not the product total: a size that had
            # run out passed on the strength of the other sizes, the card was
            # charged, and the sale was recorded for a unit that did not exist.
            if _available_stock_for_item(product, item) < int(item['quantity']):
                invoice.payment_status = 'failed'
                invoice.notes = f"Insufficient stock for {product.name}"
                invoice.save()
                log_payment_operation("STORE_CHARGE_FAILED", invoice=invoice.invoice_number, error='insufficient stock')
                return {
                    'success': False,
                    'error': f'אין מספיק מלאי עבור {product.name}'
                }
            tranzila_items.extend(tranzila_items_for_cart_line(product, item))
        
        # The card is charged on the terminal it was saved on, with its keys.
        client = TranzilaService.for_saved_card(recurring_payment.tranzila_terminal if recurring_payment else '')
        if client is None:
            invoice.payment_status = 'failed'
            invoice.notes = 'Payment not sent: the saved card\'s terminal has no keys here'
            invoice.save()
            return {'success': False, 'error': 'למסוף של הכרטיס השמור אין מפתחות בשרת. לא נשלח חיוב.'}

        result = client.charge_with_token(
            token=token,
            amount=invoice.total_amount,
            description=f"Store purchase - Invoice {invoice.invoice_number}",
            transaction_id=str(invoice.id),
            items=tranzila_items,
            expire_month=recurring_payment.card_expire_month if recurring_payment else None,
            expire_year=recurring_payment.card_expire_year if recurring_payment else None,
            duplicate_guard_key=f'store-{invoice.id}',
        )
        outcome = token_charge_outcome(result)

        if outcome not in (TOKEN_CHARGED, TOKEN_DECLINED, TOKEN_SETUP_PROBLEM, TOKEN_REQUEST_REJECTED):
            # No answer, or one that says nothing certain: the card may be
            # charged. Not a failure — the invoice stays pending and marked,
            # which also keeps the next saved-card purchase for this child away.
            invoice.tranzila_confirmation_code = TILL_CHARGE_UNCERTAIN_MARK
            invoice.tranzila_terminal = client.token_terminal  # where to look for it
            invoice.notes = f"Payment uncertain — check Tranzila before retrying: {result.get('error')}"
            invoice.save(update_fields=['tranzila_confirmation_code', 'tranzila_terminal', 'notes'])
            logger.error('Till token charge uncertain for invoice %s: %s', invoice.invoice_number, result.get('error'))
            try:
                from apps.core.office_alerts import crm_child_link, describe_family, raise_office_alert

                raise_office_alert(
                    kind='till_uncertain', dedup_key=f'till_uncertain:{invoice.id}',
                    title='לא ידוע אם הלקוח חויב בקופה',
                    where='קופה — קנייה בכרטיס השמור של הילד',
                    what=(f'נשלח חיוב של ₪{invoice.total_amount} (חשבונית {invoice.invoice_number}) ולא התקבלה תשובה. '
                          'החשבונית ממתינה, וקנייה נוספת בכרטיס השמור של הילד חסומה עד בדיקה.'),
                    why=str(result.get('error') or 'אין תשובה')[:300],
                    customer=describe_family(
                        invoice.child.family if invoice.child_id else None,
                        children=[invoice.child] if invoice.child_id else [],
                        amount=invoice.total_amount,
                    ),
                    action=f'לבדוק בטרנזילה (מסוף {client.token_terminal}) אם ירד הסכום, ולעדכן את החשבונית.',
                    link=crm_child_link(invoice.child_id),
                )
            except Exception:
                logger.exception('Till uncertain alert failed (non-fatal)')
            return {'success': False, 'uncertain': True, 'error': TILL_CHARGE_UNCERTAIN_MESSAGE}

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
                response_timestamp=timezone.now(),
                tranzila_terminal=(recurring_payment.tranzila_terminal if recurring_payment else '') or '',
            )
            
            # Update invoice
            invoice.payment_status = 'completed'
            invoice.tranzila_txn = tranzila_transaction
            invoice.tranzila_transaction_id = result.get('transaction_id', '')
            invoice.tranzila_confirmation_code = result.get('confirmation_code', '')
            # charge_with_token bills the token terminal.
            invoice.tranzila_terminal = client.token_terminal
            invoice.save()
            
            # Create sales records and update stock atomically
            with transaction.atomic():
                for item in product_items:
                    product = StoreProduct.objects.select_for_update().get(id=item['product_id'])
                    
                    # The card is charged: the sale is recorded whatever the
                    # shelf says now. Marking it failed hid a charge that
                    # happened; a unit sold twice is a stock count to fix.
                    if _available_stock_for_item(product, item) < int(item['quantity']):
                        logger.error(
                            "Stock for %s ran out between the check and the charge (invoice %s) — sale kept",
                            product.name, invoice.invoice_number,
                        )
                        invoice.notes = f"נמכר מעבר למלאי: {product.name} — לבדוק את המלאי"[:1000]
                        invoice.save(update_fields=['notes'])
                    
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

            _sign_store_sale(invoice)
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
        signature: Optional[str] = None,
        *,
        site_timeout: Optional[float] = None,
        source: str = 'notify',
    ) -> Dict:
        """
        Tranzila's notify for a store invoice paid on the hosted page (website or till).

        The notify POST is public and unsigned: anyone who knows an invoice id
        can send `Response=000`. Only Tranzila's own report — an approved
        charge with this number, this sum and this approval number, made after
        the invoice, the same check the payment links make — turns it into a
        sale (apps/store/payment_followup.report_answer). The website's report
        of the number it got back (widget/payment/returned/) comes here too,
        believed no more than a notify.

        A number the report does not confirm yet is kept and the invoice is in
        review (29.9.2026): pending whatever it read before, followed up by the
        site's poll and the morning sweep, and never failed by a later
        "declined" unless the report itself definitely says no. A second
        number for the same order is kept too, never written over the first.

        Args:
            invoice_id: UUID of StoreInvoice
            tranzila_response: Parsed webhook response
            signature: Optional webhook signature for verification
            site_timeout: how long a "paid" call to the website may wait, when
                the caller is the website itself, waiting
            source: 'notify', or 'returned' (the number the site got back
                from the page): a returned number the report disputes waits
                for the ordinary clock before the office hears — the real
                notify is usually on its way — and the approval number it
                brings only fills a blank, where a notify's own replaces
                whatever was kept

        Whatever the notify's own number ends as, the order's OTHER undecided
        numbers — a released one, one reported beside it — are asked about
        too, within the per-check limit of report reads.

        Returns:
            Dict with completion result
        """
        from apps.store import payment_followup as followup
        from apps.store.models import StoreInvoice

        # Verify webhook signature for security
        if signature and not self.tranzila_service.verify_webhook_signature(tranzila_response, signature):
            logger.error(f"Invalid webhook signature for store invoice {invoice_id}")
            return {'success': False, 'error': 'Invalid signature'}

        try:
            invoice = StoreInvoice.objects.get(id=invoice_id)
        except (StoreInvoice.DoesNotExist, ValidationError, ValueError):
            logger.error(f"Invoice not found: {invoice_id}")
            return {'success': False, 'error': 'Invoice not found'}

        # Tranzila's `index` only — parse_webhook_response falls back to the
        # card's token, which must never be kept as a transaction number.
        index = followup.notify_index(tranzila_response)
        code = str(tranzila_response.get('confirmation_code') or '').strip()[:100]

        if not tranzila_response.get('is_successful'):
            return self._store_notify_declined(invoice, index, tranzila_response)

        # Whether this notify may also ask about the order's other numbers is
        # decided BEFORE its own number is kept: keeping it stamps the
        # follow-up's pace-keeper, and the stamp of this very notify must not
        # be what stops the question (review of 1.10.2026). At most once per
        # RECHECK_INTERVAL per invoice: the notify address is public, and a
        # stream of made-up notifies must not turn into several report reads
        # each.
        may_ask_others = bool(index) and followup._claim_followup(invoice.pk, followup.RECHECK_INTERVAL)
        result = self._store_notify_approved(invoice, index, code, site_timeout=site_timeout, source=source)
        if may_ask_others and not result.pop('repeat', False):
            self._ask_other_store_numbers(invoice.pk, index, site_timeout=site_timeout)
        result.pop('repeat', None)
        return result

    def _store_notify_approved(self, invoice, index: str, code: str, *, site_timeout: Optional[float],
                               source: str) -> Dict:
        """An "approved" notify (or a returned number) for a store invoice: see complete_store_purchase_from_webhook."""
        from apps.store import payment_followup as followup
        from apps.store.models import StoreInvoice

        from_notify = source != 'returned'
        if not index:
            # "Approved" without a number proves nothing and leaves nothing to
            # ask the report about. Nothing is recorded; a number the invoice
            # already holds stays.
            logger.error('Store webhook for invoice %s: approved notify without a transaction number', invoice.invoice_number)
            return {'success': False, 'error': 'התשלום לא אומת מול טרנזילה',
                    'status': invoice.payment_status, 'verdict': 'unverified'}

        if invoice.payment_status in followup.PAID_STATUSES and index == (invoice.tranzila_transaction_id or '').strip():
            # Tranzila repeating the payment itself: never sold again, and
            # nothing new to ask the report. Any OTHER number on a paid order
            # is asked about — a released or ruled-out one included: "already
            # handled" without the report could hide a second charge.
            logger.info(f"Store webhook for invoice {invoice.invoice_number} already processed")
            return {'success': True, 'invoice_id': str(invoice.id), 'already_processed': True, 'repeat': True}

        # A till invoice no hosted page was ever opened for (a typed or saved
        # card, cash): Tranzila has no reason to notify about it, so a notify
        # that names it is not a payment of ours. As before this follow-up
        # existed, the report is still the judge — a payment it confirms is a
        # payment (a sale, or a second charge on a paid invoice) — but a
        # number it does not confirm is only logged: not kept, not followed,
        # no "double charge" told.
        never_had_page = not invoice.website_order_number and invoice.payment_page_opened_at is None

        number = followup.number_for(invoice, index, code, (self.iframe_tranzila_service.terminal or '').strip(),
                                     code_wins=from_notify)
        # Asked before the row is locked: the report may take up to 30 seconds,
        # and the lock would hold every other notify and poll of this order.
        answer, row, why = followup.report_answer(invoice, number)

        sold = None
        with transaction.atomic():
            # Locked for the whole decision, so two notifies arriving together
            # cannot both sell the cart.
            invoice = StoreInvoice.objects.select_for_update().get(id=invoice.id)
            if invoice.payment_status in followup.PAID_STATUSES:
                # Never sold again and never downgraded. A *different* number
                # is the same order paid twice (two tabs, a page opened twice):
                # kept and told, never swallowed.
                if never_had_page and answer != followup.ANSWER_VERIFIED:
                    logger.warning(
                        'Store webhook for till invoice %s: paid without a hosted page, and the report does not '
                        'confirm number %s (%s) — not kept', invoice.invoice_number, index, answer,
                    )
                    return {'success': True, 'invoice_id': str(invoice.id), 'already_processed': True}
                self._record_second_store_charge(invoice, number, answer, row, code_wins=from_notify)
                logger.info(f"Store webhook for invoice {invoice.invoice_number} already processed")
                return {'success': True, 'invoice_id': str(invoice.id), 'already_processed': True}

            if answer == followup.ANSWER_VERIFIED:
                sold = self._sell_store_invoice(invoice, number, row)
                if sold is None:
                    return {'success': True, 'invoice_id': str(invoice.id), 'already_processed': True}
            elif never_had_page:
                logger.error("Store webhook for till invoice %s not confirmed by Tranzila (%s: %s); no hosted page "
                             "was opened for it — number %s not kept", invoice.invoice_number, answer, why, index)
            elif followup.other_state(invoice, index) == followup.OTHER_REJECTED:
                # The report already definitely ruled this number out for this
                # order, and it still does not confirm it: a repeat changes
                # nothing.
                logger.error("Store webhook for invoice %s: number %s already ruled out (%s)",
                             invoice.invoice_number, index, answer)
            else:
                # Nothing sold, no stock moved, no document. The number is kept
                # — as the invoice's own, or beside it, a released one reopened
                # — and the invoice is in review; apps/store/payment_followup.py
                # asks the report again.
                own = self._keep_reported_number(invoice, number, code_wins=from_notify)
                logger.error(
                    "Store webhook for invoice %s not confirmed by Tranzila (%s: %s); in review",
                    invoice.invoice_number, answer, why,
                )
                # The report's disagreement goes to the office at once; a
                # report that cannot answer, or does not list the number yet —
                # or a number the site returned, before the notify — after ten
                # minutes from the payment. After commit, once per invoice. A
                # further number was told as a possible double charge already.
                if own and from_notify and answer in (followup.ANSWER_REJECTED, followup.ANSWER_DISPUTED):
                    followup.alert_payment_unverified(invoice, row, why)
                elif own:
                    followup.alert_if_stuck(invoice, why=why)
            if sold is None:
                return {
                    'success': False,
                    'error': 'התשלום לא אומת מול טרנזילה',
                    'status': invoice.payment_status,
                    'verdict': answer,
                }

        self._after_store_sale(invoice, sold, site_timeout=site_timeout)
        return {'success': True, 'invoice_id': str(invoice.id)}

    def _ask_other_store_numbers(self, invoice_id, index: str, *, site_timeout: Optional[float] = None) -> None:
        """
        After a notify (or a returned number) for `index`: the report is asked
        about the order's other undecided numbers too — a number a person
        released may be the real payment, and the one just reported the second
        (or the other way round). One report read went to the notify's own
        number; the rest of the per-check limit goes to these, in turns. The
        caller keeps the pace (once per RECHECK_INTERVAL per invoice); what
        is skipped is asked by the site's poll, the next notify, a retry, or
        the morning.
        """
        from apps.store import payment_followup as followup
        from apps.store.models import StoreInvoice

        invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
        if invoice is None:
            return
        if not any(n.index != index for n in followup.open_numbers(invoice, include_released=True)):
            return
        try:
            self.settle_reported_store_payment(
                invoice_id, skip=(index,), site_timeout=site_timeout,
                max_numbers=followup.MAX_REPORT_READS_PER_INVOICE - 1,
            )
        except Exception:  # noqa: BLE001 — the notify's own answer stands; the sweep asks again
            logger.exception('Store invoice %s: asking about its other numbers failed', invoice.invoice_number)

    def settle_reported_store_payment(
        self, invoice_id, *, complete: bool = True, decline: bool = False, write: bool = True,
        include_suspected: bool = False, evidence: Optional[dict] = None, site_timeout: Optional[float] = None,
        max_numbers: Optional[int] = None, skip: tuple = (),
    ) -> Dict:
        """
        Ask the report about the undecided numbers of a store invoice — the
        ones a person released included — and settle what it allows
        (apps/store/payment_followup.py).

        * A confirmed number on an unpaid invoice completes the sale — when
          `complete` — through the same locked sale the notify makes; any
          other number stays beside it. Without `complete` nothing is sold and
          the office is told the report confirms it.
        * On a paid invoice, a further confirmed number is a second charge:
          recorded and told as confirmed, in every mode.
        * `decline` — a "declined" notify came: the invoice is failed only
          when every number holding it in review at the moment of the lock was
          asked about, and the report gave each a definite no (declined, not a
          charge, another sum). A number reported while the report was being
          read, one it does not list, or one with another approval number,
          keeps the order in review — and so does a charge found in the
          report (suspected). Released numbers do not hold it.
        * Otherwise it stays in review (pending) — or, when only released
          numbers are left, as it is.
        * `write` off (the morning sweep while its switch is off): an unpaid
          invoice is read and told only — nothing sold, no status, no number
          moved.
        * `include_suspected` / `evidence` (confirmation_code / card_last4): a
          person's "complete after verification" with the customer's own
          evidence, which wins over any kept code; a suspected charge is
          asked about only with it (payment_followup.with_evidence).
        * `max_numbers`: report reads this call may make — never more than
          MAX_REPORT_READS_PER_INVOICE. The numbers take turns
          (payment_followup.next_to_ask): what the report really answered
          about is stamped on the invoice in every mode, and 'not_asked'
          says how many wait.
        * `skip`: numbers somebody is asking about right now (the notify's own).

        Reads the report before locking the invoice. Never charges.
        Returns {'outcome': completed | confirmed | paid | declined | in_review | released_open | nothing_open, ...}.
        """
        from apps.store import payment_followup as followup
        from apps.store.models import StoreInvoice

        invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
        if invoice is None:
            return {'outcome': 'nothing_open'}

        def numbers_of(inv):
            return [n for n in followup.open_numbers(inv, include_suspected=include_suspected, include_released=True)
                    if n.index not in skip]

        every = numbers_of(invoice)
        limit = min(max_numbers or followup.MAX_REPORT_READS_PER_INVOICE, followup.MAX_REPORT_READS_PER_INVOICE)
        numbers = [followup.with_evidence(n, **(evidence or {})) for n in followup.next_to_ask(every, limit)]
        asked = {n.index: (n, *followup.report_answer(invoice, n)) for n in numbers}
        if not asked:
            return {'outcome': 'nothing_open', 'status': invoice.payment_status}
        not_asked = len(every) - len(asked)

        sold = None
        with transaction.atomic():
            invoice = StoreInvoice.objects.select_for_update().get(pk=invoice.pk)
            # "Answered" is stamped only for what the report really answered
            # about: a number it could not be asked about, or does not list
            # yet, was asked — and is still unanswered.
            stamped = followup.mark_answered(
                invoice, [i for i, (_n, answer, _row, why) in asked.items() if not followup.is_no_answer(answer, why)])
            # Only numbers still undecided under the lock: another notify or
            # poll may have settled some while the report was being read — and
            # may have reported new ones, which nobody has asked about yet.
            still_open = {n.index: n for n in numbers_of(invoice)}
            answers = [(asked[i][0], answer, row, why)
                       for i, (_n, answer, row, why) in asked.items() if i in still_open]
            unasked_blocking = [n for i, n in still_open.items()
                                if i not in asked and not n.released and not n.suspected]

            if invoice.payment_status in followup.PAID_STATUSES:
                # In every mode: a second charge the report confirms is
                # recorded and told. No sale, no document, no email.
                recorded = [
                    number.index for number, answer, row, _why in answers
                    if not number.primary and self._record_second_store_charge(invoice, number, answer, row)
                ]
                if stamped:
                    invoice.save(update_fields=['other_transactions'])
                return {'outcome': 'paid', 'status': invoice.payment_status, 'second_charges': recorded,
                        'not_asked': not_asked}

            if not write:
                if stamped:
                    invoice.save(update_fields=['other_transactions'])
                return {**self._tell_reported_store_payment(invoice, answers), 'not_asked': not_asked}

            for number, answer, row, _why in answers:
                if answer == followup.ANSWER_REJECTED and not number.primary:
                    followup.keep_other_transaction(invoice, number, followup.OTHER_REJECTED)
            confirmed = next(((n, row) for n, answer, row, _why in answers if answer == followup.ANSWER_VERIFIED), None)
            blocking = [(n, answer) for n, answer, _r, _w in answers if not n.released and not n.suspected]

            if confirmed is not None and complete:
                sold = self._sell_store_invoice(invoice, *confirmed)
                if sold is None:
                    return {'outcome': 'completed', 'status': 'completed', 'not_asked': not_asked}
            elif confirmed is not None:
                invoice.save(update_fields=['other_transactions'])
                followup.alert_payment_confirmed(invoice, confirmed[0].index)
                return {'outcome': 'confirmed', 'status': invoice.payment_status, 'index': confirmed[0].index,
                        'not_asked': not_asked}
            elif (decline and blocking and not unasked_blocking
                  and all(answer == followup.ANSWER_REJECTED for _n, answer in blocking)):
                # Every reported number holding the order in review was asked
                # about, and the report definitely says none paid for it;
                # Tranzila says the attempt was declined. The numbers are
                # kept, marked, beside the invoice.
                for number, _answer in blocking:
                    if number.primary:
                        followup.keep_other_transaction(invoice, number, followup.OTHER_REJECTED)
                invoice.tranzila_transaction_id = ''
                invoice.tranzila_confirmation_code = ''
                fields = ['tranzila_transaction_id', 'tranzila_confirmation_code', 'payment_status', 'other_transactions']
                if followup.suspected_numbers(invoice):
                    # A charge found in the report still may be this order's
                    # payment: the decline rules out the reported numbers, not
                    # that one. In review for a person; the site hears nothing.
                    invoice.payment_status = 'pending'
                    invoice.save(update_fields=fields)
                    logger.warning('Store invoice %s declined, but a charge found in the report keeps it in review',
                                   invoice.invoice_number)
                    return {'outcome': 'in_review', 'status': 'pending', 'not_asked': not_asked}
                invoice.payment_status = 'failed'
                invoice.save(update_fields=fields)
                transaction.on_commit(lambda: self._tell_website_failed(invoice))
                logger.warning('Store invoice %s declined; the report rules out every reported number', invoice.invoice_number)
                return {'outcome': 'declined', 'status': 'failed', 'not_asked': not_asked}
            else:
                fields = ['other_transactions']
                if followup.holds_reported_payment(invoice) and invoice.payment_status != 'pending':
                    # Still in review: pending, whatever it read before.
                    invoice.payment_status = 'pending'
                    fields.append('payment_status')
                invoice.save(update_fields=fields)
                if not followup.holds_reported_payment(invoice):
                    # Only numbers a person released are left undecided: asked
                    # again next time, the order as it is.
                    return {'outcome': 'released_open', 'status': invoice.payment_status, 'not_asked': not_asked}
                primary = next(((n, answer, row, why) for n, answer, row, why in answers if n.primary), None)
                reasons = '; '.join(sorted({why for _n, _a, _r, why in answers if why}
                                           | ({'מספר עסקה נוסף דווח בזמן שהדוח נבדק'} if unasked_blocking else set())))
                if decline:
                    followup.alert_decline_conflict(invoice, reasons)
                elif primary is not None and primary[1] in (followup.ANSWER_REJECTED, followup.ANSWER_DISPUTED):
                    followup.alert_payment_unverified(invoice, primary[2], primary[3])
                else:
                    followup.alert_if_stuck(invoice, why=reasons)
                return {'outcome': 'in_review', 'status': 'pending', 'not_asked': not_asked}

        self._after_store_sale(invoice, sold, site_timeout=site_timeout)
        return {'outcome': 'completed', 'status': 'completed', 'not_asked': not_asked}

    def _tell_reported_store_payment(self, invoice, answers: list) -> Dict:
        """An unpaid invoice, without selling or changing it: what the report says, told to the office."""
        from apps.store import payment_followup as followup

        confirmed = next((n for n, answer, _r, _w in answers if answer == followup.ANSWER_VERIFIED), None)
        if confirmed is not None:
            followup.alert_payment_confirmed(invoice, confirmed.index)
            return {'outcome': 'confirmed', 'status': invoice.payment_status, 'index': confirmed.index}
        if not followup.holds_reported_payment(invoice):
            return {'outcome': 'released_open', 'status': invoice.payment_status}
        primary = next(((n, answer, row, why) for n, answer, row, why in answers if n.primary), None)
        if primary is not None and primary[1] in (followup.ANSWER_REJECTED, followup.ANSWER_DISPUTED):
            followup.alert_payment_unverified(invoice, primary[2], primary[3])
        else:
            followup.alert_if_stuck(invoice, why='; '.join(sorted({why for _n, _a, _r, why in answers if why})))
        return {'outcome': 'in_review', 'status': invoice.payment_status}

    def _store_notify_declined(self, invoice, index: str, tranzila_response: Dict) -> Dict:
        """
        A "declined" notify. It says one attempt failed — not that no attempt
        paid: another tab may have paid first. An invoice that holds a
        reported number is asked about again, and failed only on the report's
        definite "no" (settle_reported_store_payment); any other is failed as
        before.
        """
        from apps.store import payment_followup as followup
        from apps.store.models import StoreInvoice

        if invoice.payment_status not in followup.PAID_STATUSES and followup.holds_reported_payment(invoice):
            result = self.settle_reported_store_payment(invoice.pk, decline=True)
            if result['outcome'] == 'completed':
                return {'success': True, 'invoice_id': str(invoice.id)}
            if result['outcome'] == 'declined':
                return {'success': False, 'error': tranzila_response.get('error_message', 'Payment failed')}
            if result['outcome'] != 'nothing_open':
                return {'success': False, 'in_review': True, 'status': result.get('status', invoice.payment_status),
                        'error': 'התשלום בבדיקה מול טרנזילה'}

        # Locked and read again: a decline from one tab that arrives while
        # (or after) another tab's payment completes must not turn a paid
        # order into a failed one — nor one a payment was just reported for.
        with transaction.atomic():
            invoice = StoreInvoice.objects.select_for_update().get(id=invoice.id)
            if invoice.payment_status in followup.PAID_STATUSES:
                logger.info(
                    "Store webhook decline for invoice %s ignored — already paid", invoice.invoice_number,
                )
                return {'success': True, 'invoice_id': str(invoice.id), 'already_processed': True}
            if followup.holds_reported_payment(invoice):
                logger.warning(
                    "Store webhook decline for invoice %s left for review — a payment was reported meanwhile",
                    invoice.invoice_number,
                )
                return {'success': False, 'in_review': True, 'status': invoice.payment_status,
                        'error': 'התשלום בבדיקה מול טרנזילה'}
            # Keep cart JSON in notes so the customer can retry the same order.
            invoice.payment_status = 'failed'
            invoice.save(update_fields=['payment_status'])
        logger.warning(
            "Store iframe payment failed invoice=%s code=%s error=%s",
            invoice.invoice_number,
            tranzila_response.get('response_code'),
            tranzila_response.get('error_message', 'Unknown'),
        )
        self._tell_website_failed(invoice, provider_txn_id=index)
        return {
            'success': False,
            'error': tranzila_response.get('error_message', 'Payment failed')
        }

    @staticmethod
    def _tell_website_failed(invoice, provider_txn_id: str = '') -> None:
        if not invoice.website_order_number:
            return
        from apps.store.website_integration import notify_website_order_status

        notify_website_order_status(
            website_order_number=invoice.website_order_number,
            invoice_number=invoice.invoice_number,
            invoice_id=str(invoice.id),
            status='failed',
            provider_txn_id=provider_txn_id,
        )

    def _keep_reported_number(self, invoice, number, *, code_wins: bool = True) -> Optional[bool]:
        """
        A number the report does not confirm (yet), kept on the locked invoice:
        as its own when it has none, beside it otherwise — never over it. The
        invoice is in review, so it reads pending even if it read failed. A
        number a person released and Tranzila reports again is back in review.

        Each number has its own clock: one that becomes the invoice's own — the
        first, or one after a number that was ruled out — carries the time it
        was first reported, so it is not judged "missing from the report" by an
        older number's time. The approval number on `number` is the one
        payment_followup.number_for chose (a notify's own wins; the site's
        only fills a blank) and is kept with it.

        Every number is kept: more than MAX_UNDECIDED_NUMBERS unanswered ones
        are told to the office once, and only beyond MAX_KEPT_NUMBERS on one
        order is a new one refused (None). True when the number is the
        invoice's own, False when kept beside it.
        """
        from apps.store import payment_followup as followup

        now = timezone.now()
        index = number.index
        own = (invoice.tranzila_transaction_id or '').strip()
        known = index == own or bool(followup.other_state(invoice, index))
        if not known and not followup.has_room(invoice):
            logger.error('Store invoice %s: number %s not kept — %s numbers already',
                         invoice.invoice_number, index, followup.MAX_KEPT_NUMBERS)
            followup.alert_too_many_numbers(invoice, index, kept=False)
            return None
        further = False
        if not followup.is_transaction_number(own):
            followup.drop_other_transaction(invoice, index)  # a released one, reported again
            invoice.tranzila_transaction_id = index
            invoice.tranzila_confirmation_code = number.code
            invoice.tranzila_terminal = number.terminal
            invoice.payment_reported_at = number.reported_at
        elif own != index:
            further = followup.keep_other_transaction(invoice, number, followup.OTHER_OPEN, code_wins=code_wins)
        elif number.code:
            invoice.tranzila_confirmation_code = number.code
        if invoice.payment_reported_at is None:
            invoice.payment_reported_at = now
        invoice.payment_status = 'pending'
        invoice.payment_followup_at = now
        invoice.save(update_fields=[
            'tranzila_transaction_id', 'tranzila_confirmation_code', 'tranzila_terminal',
            'payment_reported_at', 'payment_followup_at', 'payment_status', 'other_transactions',
        ])
        if further:
            followup.alert_possible_double_charge(invoice, index)
        if followup.unanswered_count(invoice) > followup.MAX_UNDECIDED_NUMBERS:
            followup.alert_too_many_numbers(invoice, index)
        return invoice.tranzila_transaction_id.strip() == index

    def _sell_store_invoice(self, invoice, number, row=None) -> Optional[list]:
        """
        The sale, on the locked invoice, for the number the report confirmed.
        A number the invoice held before is kept beside it (and told to the
        office as a possible second charge), never written over. Returns the
        cart for _after_store_sale — or None when the invoice was sold
        already: then nothing is sold again, whatever its status read (a
        status written over by anything); the invoice reads completed again,
        and a different confirmed number is kept as a second charge.
        """
        from apps.store import payment_followup as followup
        from apps.store.models import StoreProduct, StoreSale
        from apps.store.stock_utils import available_stock_for_item as _available_stock_for_item

        if StoreSale.objects.filter(invoice=invoice).exists():
            logger.error(
                'Store invoice %s: already sold (status read %s) — not sold again for transaction %s',
                invoice.invoice_number, invoice.payment_status, number.index,
            )
            if followup.is_transaction_number(invoice.tranzila_transaction_id):
                invoice.payment_status = 'completed'
                invoice.save(update_fields=['payment_status'])
                self._record_second_store_charge(invoice, number, followup.ANSWER_VERIFIED, row)
            else:
                # Sold with no number kept (should not happen): the confirmed one is its payment.
                invoice.payment_status = 'completed'
                invoice.tranzila_transaction_id = number.index
                invoice.tranzila_confirmation_code = number.code
                invoice.tranzila_terminal = number.terminal
                followup.drop_other_transaction(invoice, number.index)
                invoice.save(update_fields=['payment_status', 'tranzila_transaction_id', 'tranzila_confirmation_code',
                                            'tranzila_terminal', 'other_transactions'])
            return None

        now = timezone.now()
        previous = (invoice.tranzila_transaction_id or '').strip()
        if followup.is_transaction_number(previous) and previous != number.index:
            moved_aside = followup.ReportedNumber(
                previous, (invoice.tranzila_confirmation_code or '').strip(), (invoice.tranzila_terminal or '').strip(),
                invoice.payment_reported_at or invoice.created_at, False,
            )
            followup.keep_other_transaction(invoice, moved_aside, followup.OTHER_OPEN)
            invoice.payment_reported_at = number.reported_at
        followup.drop_other_transaction(invoice, number.index)

        # Parse product items from invoice notes
        product_items = parse_store_cart_notes(invoice.notes) or []

        invoice.payment_status = 'completed'
        invoice.tranzila_transaction_id = number.index
        invoice.tranzila_confirmation_code = number.code
        # The hosted page charges on TRANZILA_TERMINAL — the one the report asked.
        invoice.tranzila_terminal = number.terminal
        if invoice.payment_reported_at is None:
            invoice.payment_reported_at = now
        invoice.save()

        oversold = []
        for item in product_items:
            product = StoreProduct.objects.select_for_update().get(id=item['product_id'])

            # The shelf was checked when the page opened, not now: the
            # customer has paid, so the sale is recorded whatever the
            # shelf says (refusing it would hide money that came in).
            # A unit sold that was not there is a stock count and a
            # customer to call — marked on the line and told to the
            # office. A size row cannot go below zero (stock_utils
            # stops it at 0), so it is asked before the decrement.
            available = _available_stock_for_item(product, item)
            short = available < int(item['quantity'])
            if short:
                oversold.append({
                    'name': product.name, 'size': item.get('size', ''),
                    'quantity': int(item['quantity']), 'available': available,
                })
                logger.error(
                    "Store invoice %s: %s sold beyond stock (%s ordered, %s on the shelf) — sale kept",
                    invoice.invoice_number, product.name, item['quantity'], available,
                )

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
                notes=f'נמכר מעבר למלאי (היו {available}) — לבדוק את המלאי' if short else ''
            )

            _decrement_product_stock(product, item)

        if oversold:
            followup.alert_oversold(invoice, oversold)
        # Every other undecided number on this order may be a second charge.
        others = [n.index for n in followup.open_numbers(invoice, include_released=True) if not n.primary]
        others += [n.index for n in followup.suspected_numbers(invoice)]
        if others:
            followup.alert_possible_double_charge(invoice, others[0], others=others[1:])
        logger.info(f"Successfully completed webhook purchase for invoice {invoice.invoice_number}")
        return product_items

    def _after_store_sale(self, invoice, product_items, *, site_timeout: Optional[float] = None) -> None:
        """What follows a sale, after its commit: never inside it, so a failure here cannot undo it."""
        from apps.store.models import StoreProduct

        product_items = product_items or []
        _sign_store_sale(invoice)

        if invoice.website_order_number:
            from apps.store.payment_followup import tell_website_paid
            from apps.store.website_integration import push_products_batch_to_website
            from apps.store.invoice_email import send_store_invoice_email
            # One call now; when the site does not acknowledge it, the
            # site's status poll and the morning sweep repeat it
            # (apps/store/payment_followup.py) and the office is told. From
            # the site's own poll the site is waiting: a short leash.
            try:
                tell_website_paid(invoice, timeout=site_timeout)
            except Exception:
                logger.exception('Telling the site about %s failed (non-fatal)', invoice.invoice_number)
            sold_products = list(
                StoreProduct.objects.filter(
                    id__in=[item['product_id'] for item in product_items]
                )
            )
            # One call for the whole order, not one per line item.
            push_products_batch_to_website(sold_products)
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

    def _record_second_store_charge(self, invoice, number, answer: str, row=None, *, alert: bool = True,
                                    code_wins: bool = False) -> bool:
        """
        A paid invoice reported paid again under another transaction number
        (the caller holds the row lock).

        Every such number is kept on the invoice (other_transactions) and the
        office is told, once per order, that it may have been paid twice. One
        the report confirms is also kept as its own transaction row and shown
        in the morning brief under "חיובים כפולים", so the office refunds it —
        and told as a confirmed second charge, per number; no document is
        made and the customer gets no email. One the report definitely rules
        out is marked so; any other stays undecided — a released one
        released — and is asked again. Kept up to MAX_KEPT_NUMBERS per order
        (one the report confirms, always). True when a confirmed second
        charge was recorded now.
        """
        from apps.store import payment_followup as followup

        if not number.index or number.index == (invoice.tranzila_transaction_id or '').strip():
            return False  # the same transaction reported again
        previous = followup.other_state(invoice, number.index)
        verified = answer == followup.ANSWER_VERIFIED
        if verified:
            state = followup.OTHER_SECOND_CHARGE
        elif answer == followup.ANSWER_REJECTED:
            state = followup.OTHER_REJECTED
        else:
            # Undecided: stays what it was (open, released, suspected), a new one open.
            state = previous if previous in followup.UNDECIDED_STATES else followup.OTHER_OPEN
            if not previous and not followup.has_room(invoice):
                logger.error('Store invoice %s: further number %s not kept — %s numbers already',
                             invoice.invoice_number, number.index, followup.MAX_KEPT_NUMBERS)
                followup.alert_too_many_numbers(invoice, number.index, kept=False)
                return False
        new = followup.keep_other_transaction(invoice, number, state, code_wins=code_wins or verified)
        invoice.save(update_fields=['other_transactions'])
        if verified:
            TranzilaTransaction.objects.get_or_create(
                idempotency_key=f'store_second_{invoice.id}_{number.index}'[:255],
                defaults={
                    'transaction_id': number.index[:100],
                    'confirmation_code': number.code[:100],
                    'transaction_type': 'charge',
                    'response_code': str((row or {}).get('processor_response_code') or '000')[:10],
                    'response_message': 'second charge on a paid invoice',
                    'request_data': {'invoice_id': str(invoice.id), 'invoice_number': invoice.invoice_number},
                    'response_data': {},
                    'is_successful': True,
                    'response_timestamp': timezone.now(),
                    'tranzila_terminal': (number.terminal or '')[:40],
                },
            )
            # Not written into invoice.notes: a hosted-page invoice keeps its cart
            # there as JSON.
            logger.error('Store invoice %s paid twice: second transaction %s recorded',
                         invoice.invoice_number, number.index)
        else:
            logger.error('Store invoice %s: another transaction %s reported (%s)',
                         invoice.invoice_number, number.index, answer)
        if alert and verified:
            followup.alert_second_charge_confirmed(invoice, number.index)
        elif alert and new:
            followup.alert_possible_double_charge(invoice, number.index, answer)
        if new and followup.unanswered_count(invoice) > followup.MAX_UNDECIDED_NUMBERS:
            followup.alert_too_many_numbers(invoice, number.index)
        return verified and previous != followup.OTHER_SECOND_CHARGE

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
        from apps.store.stock_utils import available_stock_for_item as _available_stock_for_item
        from apps.customers.recurring_amount import (
            active_recurring_for_child,
            add_to_month_override,
        )
        
        child = Child.objects.get(id=child_id)

        # Checked before any stock moves: a purchase told to ride on a standing
        # order that does not exist would otherwise be marked paid and collected
        # by no one, which is the exact hole this path was closing.
        recurring = None
        if payment_method == 'monthly_billing':
            recurring = active_recurring_for_child(child)
            if recurring is None:
                raise ValueError(
                    'לילד אין הוראת קבע פעילה, ולכן לא ניתן לגבות את הרכישה דרך הוראת קבע'
                )

        # Calculate total, and refuse before anything is written: the row each
        # line draws on (a size at a location) must hold the units, not just the
        # product total — a size that had run out passed on the strength of the
        # other sizes, and the sale was recorded for a unit that did not exist.
        total_amount = Decimal('0.00')
        for item in product_items:
            product = StoreProduct.objects.get(id=item['product_id'])
            if _available_stock_for_item(product, item) < int(item['quantity']):
                raise ValueError(f'אין מספיק מלאי עבור {product.name}')
            total_amount += line_charge_amount(product, item['quantity'], item)

        # Invoice, sales and stock commit together: the invoice used to be
        # created before this block, so a line refused inside it left a paid
        # invoice with no lines, and a consumed number, behind.
        with transaction.atomic():
            invoice = StoreInvoice.objects.create(
                child=child,
                total_amount=total_amount,
                payment_method=payment_method,
                payment_status='completed',
                charged_with_token=False
            )

            for item in product_items:
                product = StoreProduct.objects.select_for_update().get(id=item['product_id'])

                # Validate stock again under the lock
                if _available_stock_for_item(product, item) < int(item['quantity']):
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

            # "Standing order" used to be a label and nothing more: the invoice was
            # marked paid, the stock came down, and no one ever collected the money.
            # It now rides on the child's own standing order — added to the month it
            # is next charged in, on top of whatever that month already stood at.
            if payment_method == 'monthly_billing':
                names = ', '.join(
                    StoreProduct.objects.get(id=item['product_id']).name
                    for item in product_items
                )
                add_to_month_override(
                    recurring,
                    extra=total_amount,
                    reason=f'רכישה בחנות: {names} (חשבונית {invoice.invoice_number})',
                    source='store',
                    store_invoice=invoice,
                )

        logger.info(f"Created {payment_method} invoice {invoice.invoice_number}")
        _sign_store_sale(invoice)

        return StoreInvoiceSerializer(invoice).data
    
    # ============================================================================
    # Refund Methods
    # ============================================================================
    
    @staticmethod
    def _issue_payment_credit_note(payment: Payment, refund_amount: Decimal, reason: str) -> None:
        """
        The numbered הודעת זיכוי for a refunded lesson charge, mailed to the family.

        It names the receipt the family already holds — סעיף 9(ה)(4) wants that
        document identified by number and by date — and goes to the name on it.
        """
        from apps.core.computerized_docs import check_consent
        from apps.documents.service import issue_refund_credit_note

        original = payment.invoices.order_by('invoice_date').first()
        family = payment.family
        email = (family.email or '').strip()
        if not email and original:
            email = (original.payer_email or '').strip()
        check_consent(family, original.invoice_number if original else str(payment.id))

        course = payment.lesson.course if payment.lesson_id and payment.lesson.course_id else None
        issue_refund_credit_note(
            gross_amount=refund_amount,
            reason=reason,
            original_number=original.invoice_number if original else '',
            original_date=original.invoice_date if original else payment.payment_date,
            child=payment.child,
            customer_name=(original.payer_name if original else '') or family.name,
            email=email,
            branch_id=payment.branch_id,
            business_id=course.business_id if course else None,
        )

    @staticmethod
    def _issue_store_credit_note(invoice, refund_amount: Decimal, reason: str) -> None:
        """The numbered הודעת זיכוי for a refunded store sale — walk-in buyers included."""
        from apps.documents.service import issue_refund_credit_note

        email = (invoice.customer_email or '').strip()
        name = (invoice.customer_name or '').strip()
        if invoice.child:
            family = getattr(invoice.child, 'family', None)
            if family:
                email = email or (family.email or '').strip()
            name = name or invoice.child.full_name

        issue_refund_credit_note(
            gross_amount=refund_amount,
            reason=reason,
            original_number=invoice.invoice_number,
            original_date=invoice.issue_date,
            child=invoice.child,
            customer_name=name,
            email=email,
            branch_id=invoice.branch_id,
        )

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
        
        # The refund goes back to the terminal that took the charge, with that
        # terminal's keys. A charge recorded before 25.9.2026 carries no
        # terminal and keeps the old route.
        terminal = (payment.tranzila_transaction.tranzila_terminal or '').strip()
        refund_service = self.tranzila_service
        refund_terminal = terminal_for_payment_refund(payment)
        if terminal:
            refund_service = TranzilaService.for_saved_card(terminal)
            refund_terminal = terminal
            if refund_service is None:
                logger.error("Payment %s was charged on %s, which is not configured", payment_id, terminal)
                return {
                    'success': False,
                    'error': f'התשלום נגבה במסוף {terminal}, שאינו מוגדר במערכת. יש לזכות ידנית בטרנזילה.',
                }

        # Prefer the saved card from THIS payment's subscription. A child with two
        # lessons can have two tokens; .first() on the child would refund the wrong one.
        # Monthly cron charges are not the initial_payment — match by lesson too.
        card_expire_month, card_expire_year, token = card_details_for_payment_refund(payment, terminal)
        
        # Use full amount if not specified
        refund_amount = amount if amount else payment.final_amount
        # A partial refund is bounded by the charge it refunds. Anything else
        # (a typo of ₪2600 for ₪260, a negative figure) is stopped here, before
        # the gateway — a token credit has nothing tying it to the original sum.
        if refund_amount <= 0 or refund_amount > payment.final_amount:
            logger.warning(
                "Refund of %s refused for payment %s (charged %s)", refund_amount, payment_id, payment.final_amount,
            )
            return {
                'success': False,
                'error': f'סכום הזיכוי חייב להיות בין ₪0.01 ל־₪{payment.final_amount:.2f} (סכום החיוב)',
            }

        log_payment_operation(
            "REFUND_PAYMENT",
            payment_id=payment_id,
            amount=refund_amount,
            reason=reason
        )
        
        charged_at = payment.payment_date or payment.created_at
        same_day = False
        if charged_at:
            if timezone.is_naive(charged_at):
                charged_at = timezone.make_aware(charged_at)
            same_day = (
                charged_at.astimezone(JERUSALEM_TZ).date()
                == timezone.now().astimezone(JERUSALEM_TZ).date()
            )

        # One refund at a time per payment: a double click, or a second office
        # user, loses the insert and is sent away before the gateway is touched.
        # An answered refusal drops the claim again; an unanswered refund keeps
        # it, so nobody refunds a second time before Tranzila has been checked.
        claim = _claim_refund(
            f'refund_claim_payment_{payment.id}',
            terminal=terminal,
            request_data={
                'original_transaction_id': transaction_id,
                'authorization_number': authorization_number,
                'amount': str(refund_amount),
                'reason': reason,
                'token': token[:10] + '...' if token and len(token) > 10 else token
            },
        )
        if claim is None:
            return {'success': False, 'error': REFUND_ALREADY_CLAIMED}

        # A charge that paid for several payments (a course checkout's cart)
        # is refunded one payment at a time, as a credit: a cancel would void
        # every child's part of it.
        shared = Payment.objects.filter(
            tranzila_transaction_id=payment.tranzila_transaction_id,
        ).exclude(id=payment.id).exists()
        result = refund_service.refund_transaction(
            transaction_id=transaction_id,
            amount=refund_amount,
            reason=reason,
            authorization_number=authorization_number,
            card_expire_month=card_expire_month,
            card_expire_year=card_expire_year,
            token=token,
            prefer_cancel=same_day and not shared,
            terminal_name=refund_terminal,
            allow_cancel=not shared,
        )

        if result.get('uncertain'):
            logger.error("Refund of payment %s got no answer — claim kept: %s", payment_id, result.get('error'))
            _alert_refund_uncertain(
                key=f'refund_uncertain:payment:{payment.id}',
                what=f'נשלח זיכוי של ₪{refund_amount} לתשלום מקורי {transaction_id} ולא התקבלה תשובה.',
                why=str(result.get('error') or 'אין תשובה'),
                family=payment.family, child=payment.child, amount=refund_amount,
                terminal=refund_terminal or refund_service.token_terminal,
            )
            return {'success': False, 'uncertain': True, 'error': REFUND_UNCERTAIN}

        if result['success']:
            # The claim becomes the refund's record, together with the status.
            with transaction.atomic():
                _settle_refund_claim(claim, result)
                payment.status = 'refunded'
                payment.save()

            from apps.customers.child_status import recheck_after_money_stopped

            recheck_after_money_stopped(payment.child, reason='התשלום זוכה')
            
            log_payment_operation(
                "REFUND_PAYMENT_SUCCESS",
                payment_id=payment_id,
                transaction_id=result.get('transaction_id', ''),
                original_transaction_id=transaction_id
            )

            # The money is already back with the card issuer — a failed email
            # must never turn a successful refund into an error response.
            try:
                self._issue_payment_credit_note(payment, refund_amount, reason)
            except Exception:
                logger.exception('Credit note failed for payment %s (non-fatal)', payment_id)

            return {
                'success': True,
                'message': 'התשלום זוכה בהצלחה',
                'transaction_id': result.get('transaction_id', ''),
                'original_transaction_id': transaction_id,
                'refund_amount': float(refund_amount)
            }
        else:
            _drop_refund_claim(claim)
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
        
        # The refund goes back to the terminal that took the charge, with that
        # terminal's keys. Invoices from before 24.9.2026 carry no terminal and
        # keep the old route: the production token terminal.
        terminal = (invoice.tranzila_terminal or '').strip()
        refund_service = self.tranzila_service
        if terminal:
            refund_service = TranzilaService.for_terminal(terminal)
            if refund_service is None:
                logger.error(f"Invoice {invoice_id} was charged on {terminal}, which is not configured")
                return {
                    'success': False,
                    'error': f'החשבונית שולמה במסוף {terminal}, שאינו מוגדר עוד במערכת. יש לזכות ידנית בטרנזילה.',
                }

        # Get card expiration and token from child's active recurring payment
        card_expire_month = None
        card_expire_year = None
        token = None
        if terminal and not invoice.charged_with_token:
            # A typed card or the hosted page: the card that paid is on the
            # terminal's report, not on the child — who may have another card
            # on a standing order, or be no child at all.
            found = refund_service.find_transaction(transaction_id)
            paid_with = found.get('transaction') or {}
            card_expire_month = paid_with.get('expiration_month') or None
            card_expire_year = paid_with.get('expiration_year') or None
            token = str(paid_with.get('credit_card_token') or '').strip() or None
            if not card_expire_month or not card_expire_year:
                logger.error(
                    f"Invoice {invoice_id}: card details of transaction {transaction_id} "
                    f"not found on {terminal}: {found.get('error') or 'no row'}"
                )
                return {
                    'success': False,
                    'error': 'לא נמצאו בטרנזילה פרטי הכרטיס של העסקה. נסו שוב, או זכו ידנית בטרנזילה.',
                }
        elif invoice.child:
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
        
        issued = invoice.issue_date
        same_day = False
        if issued:
            issued_date = issued.date() if hasattr(issued, 'date') else issued
            same_day = issued_date == timezone.now().astimezone(JERUSALEM_TZ).date()

        # One refund at a time per invoice — see refund_payment.
        claim = _claim_refund(
            f'refund_claim_store_{invoice.id}',
            terminal=terminal,
            request_data={
                'original_transaction_id': transaction_id,
                'authorization_number': authorization_number,
                'amount': str(refund_amount),
                'reason': reason,
                'items': items,
                'token': token[:10] + '...' if token and len(token) > 10 else token
            },
        )
        if claim is None:
            return {'success': False, 'error': REFUND_ALREADY_CLAIMED}

        result = refund_service.refund_transaction(
            transaction_id=transaction_id,
            amount=refund_amount,
            reason=reason,
            authorization_number=authorization_number,
            card_expire_month=card_expire_month,
            card_expire_year=card_expire_year,
            token=token,
            items=items,
            prefer_cancel=same_day,
            terminal_name=terminal or None,
        )

        if result.get('uncertain'):
            # Not 'refund_failed': that status offers the retry button, and the
            # refund may already have been made. The claim holds every retry.
            logger.error("Refund of invoice %s got no answer — claim kept: %s", invoice_id, result.get('error'))
            invoice.notes = f"זיכוי ללא תשובה מטרנזילה — לבדוק בטרנזילה: {result.get('error') or ''} - {reason}"[:1000]
            invoice.save(update_fields=['notes'])
            _alert_refund_uncertain(
                key=f'refund_uncertain:store:{invoice.id}',
                what=f'נשלח זיכוי של ₪{refund_amount} לחשבונית {invoice.invoice_number} ולא התקבלה תשובה.',
                why=str(result.get('error') or 'אין תשובה'),
                family=invoice.child.family if invoice.child_id else None,
                child=invoice.child if invoice.child_id else None,
                amount=refund_amount,
                terminal=terminal or refund_service.token_terminal,
            )
            return {'success': False, 'uncertain': True, 'error': REFUND_UNCERTAIN}

        if result['success']:
            # Update invoice status to refunded and restore stock
            with transaction.atomic():
                _settle_refund_claim(claim, result)

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

            # Stock is back and the card is credited — email failures stay non-fatal.
            try:
                self._issue_store_credit_note(invoice, refund_amount, reason)
            except Exception:
                logger.exception(
                    'Credit note failed for store invoice %s (non-fatal)',
                    invoice.invoice_number,
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
            # Tranzila answered no: nothing was refunded, retry is safe.
            _drop_refund_claim(claim)
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

