"""
Price-aware lesson change.

`change_course.replace_unit` moves a child between lessons and leaves the
standing order alone — right when the price is the same, wrong otherwise
(a child moved onto a twice-a-week bundle kept paying for once a week).

This module puts the price next to the move:

* **quote** — what the child pays now, what the target costs, and the
  prorated difference for the rest of this month (remaining occurrences of
  the target lessons over the month's total, the same arithmetic as a
  mid-month signup).
* **up** (target costs more): the child moves now; the prorated difference
  is charged now on the saved card (or, with no saved card, added to next
  month's charge); the standing order's new amount is scheduled from the
  next billing cycle.
* **down** (target costs less): nothing moves this month — the parent paid
  for it. The move and the new amount are both scheduled for the next
  billing date, and the customers page shows the row as "מתוזמן". The
  monthly cron applies due changes before it charges.
* **same**: exactly as before.

Nothing here talks to Tranzila except the one prorated charge, and that
one runs only when the office confirmed the quoted figures.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.payment_service import (
    JERUSALEM_TZ,
    PaymentService,
    _compute_prorate,
    get_lesson_price_for_course_index,
)
from apps.core.tranzila_service import TranzilaService, is_tranzila_uncertain_gateway_error
from apps.courses.models import Lesson, LessonBundle
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.recurring_amount import (
    effective_date_for_amount_change,
    schedule_recurring_amount,
    set_month_override,
)
from apps.enrollments.change_course import (
    recurring_payments_for_unit,
    replace_unit,
    sibling_unit_enrollments,
)
from apps.enrollments.enrollment_counts import paying_enrollments
from apps.enrollments.models import LessonEnrollment, ScheduledUnitChange

logger = logging.getLogger(__name__)

DIFF_DESCRIPTION = 'הפרש יחסי — החלפת חוג'


class ChangePricingError(ValueError):
    def __init__(self, message: str, *, processing: bool = False):
        super().__init__(message)
        self.processing = processing


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('0.01'))


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

def unit_label(target_lessons: list[Lesson], target_bundle: LessonBundle | None) -> str:
    if not target_lessons:
        return ''
    course = target_lessons[0].course
    times = {1: 'פעם בשבוע', 2: 'פעמיים בשבוע', 3: 'שלוש פעמים בשבוע'}.get(len(target_lessons), f'{len(target_lessons)} פעמים בשבוע')
    days = ' · '.join(
        f"{lesson.get_day_of_week_display()} {lesson.start_time.strftime('%H:%M') if lesson.start_time else ''}".strip()
        for lesson in target_lessons
    )
    return f'{course.name} · {times} · {days}'


def _other_regular_lesson_ids(child, old_rows: list[LessonEnrollment]) -> set:
    """Paying lessons the child keeps regardless of this change."""
    unit_ids = {row.id for row in old_rows}
    return set(
        paying_enrollments(LessonEnrollment.objects.filter(child=child, status__in=('active', 'payments_problem')))
        .exclude(id__in=unit_ids)
        .values_list('lesson_id', flat=True)
    )


def target_base_price(child, old_rows: list[LessonEnrollment], target_lessons: list[Lesson], target_bundle: LessonBundle | None) -> Decimal:
    """The monthly list price of the target unit, priced as the child's Nth lesson."""
    if target_bundle is not None:
        course = target_bundle.course
        price = course.price if course.must_attend_all_lessons else target_bundle.combined_price
        return money(price or 0)
    lesson = target_lessons[0]
    if len(target_lessons) > 1 and lesson.course.must_attend_all_lessons:
        return money(lesson.course.price or 0)
    index = len(_other_regular_lesson_ids(child, old_rows)) + 1
    tier = get_lesson_price_for_course_index(lesson, index)
    regular = lesson.course.price or 0
    return money(tier if tier and tier > 0 else regular)


def _unit_prorate(target_lessons: list[Lesson], today: date) -> tuple[Decimal, int, int]:
    """Remaining / total occurrences of the target lessons this month (billing arithmetic)."""
    remaining = total = 0
    for lesson in target_lessons:
        _, lesson_remaining, lesson_total, _ = _compute_prorate(today, lesson.day_of_week)
        remaining += lesson_remaining
        total += lesson_total
    if total <= 0:
        return Decimal('1'), 0, 0
    return (Decimal(remaining) / Decimal(total)), remaining, total


def pending_change_for(enrollment: LessonEnrollment) -> ScheduledUnitChange | None:
    rows = sibling_unit_enrollments(enrollment)
    return (
        ScheduledUnitChange.objects
        .filter(enrollment_id__in=[row.id for row in rows], applied_at__isnull=True, cancelled_at__isnull=True)
        .select_related('target_bundle')
        .prefetch_related('target_lessons__course')
        .order_by('effective_date')
        .first()
    )


def quote_unit_change(
    *,
    enrollment: LessonEnrollment,
    target_lessons: list[Lesson],
    target_bundle: LessonBundle | None,
    today: date | None = None,
) -> dict:
    """Pure: nothing is written."""
    today = today or timezone.now().astimezone(JERUSALEM_TZ).date()
    child = enrollment.child
    old_rows = sibling_unit_enrollments(enrollment)

    stos = list(recurring_payments_for_unit(child, old_rows))
    recurring = stos[0] if len(stos) == 1 else None
    quote = {
        'target_label': unit_label(target_lessons, target_bundle),
        'current_amount': None,
        'new_base_price': None,
        'new_amount': None,
        'discount_amount': '0.00',
        'discounts': [],
        'direction': 'no_sto',
        'difference': '0.00',
        'prorated_difference': '0.00',
        'remaining_occurrences': 0,
        'total_occurrences': 0,
        'effective_date': None,
        'has_saved_card': False,
        'blocked': '',
        'pending_change': None,
    }
    pending = pending_change_for(enrollment)
    if pending is not None:
        quote['pending_change'] = _serialize_pending(pending)

    if enrollment.trial_lesson_date:
        quote['blocked'] = 'שיעור ניסיון — אין תמחור'
        return quote
    if len(stos) > 1:
        quote['blocked'] = 'לילד יותר מהוראת קבע אחת לחוג הזה — יש לטפל בהוראות הקבע ידנית'
        return quote
    if recurring is None:
        return quote  # no standing order: the move is free of money
    if recurring.tranzila_recurring_index:
        quote['blocked'] = 'הוראת הקבע הזאת מנוהלת בטרנזילה ולא נגבית מהמערכת — לא ניתן לשנות מחיר מכאן'
        return quote

    current = money(recurring.amount)
    new_base = target_base_price(child, old_rows, target_lessons, target_bundle)
    # Discounts are recomputed for the new price. The "additional lesson"
    # discount is asked for only when the child keeps another paying lesson —
    # during a change the old unit is still active and would count as one.
    other_ids = _other_regular_lesson_ids(child, old_rows)
    lesson_id_for_discount = None
    if target_bundle is None and len(target_lessons) == 1 and other_ids:
        lesson_id_for_discount = str(target_lessons[0].id)
    calc = PaymentService().discount_service.evaluate_discounts_for_payment(
        family_id=str(child.family_id),
        child_id=str(child.id),
        payment_date=today,
        base_price=new_base,
        lesson_id=lesson_id_for_discount,
    )
    new_amount = money(calc.final_price)
    factor, remaining, total = _unit_prorate(target_lessons, today)
    difference = new_amount - current
    prorated = money(difference * factor) if difference > 0 else Decimal('0.00')
    if difference > 0 and prorated < Decimal('1.00'):
        prorated = Decimal('0.00')
    direction = 'same' if difference == 0 else ('up' if difference > 0 else 'down')

    quote.update({
        'recurring_payment_id': str(recurring.id),
        'current_amount': str(current),
        'new_base_price': str(new_base),
        'new_amount': str(new_amount),
        'discount_amount': str(money(calc.total_discount_amount)),
        'discounts': [
            {'name': d.name, 'reason': d.reason, 'value': str(d.value)}
            for d in calc.applicable_discounts if Decimal(str(d.value or 0)) > 0
        ],
        'direction': direction,
        'difference': str(difference),
        'prorated_difference': str(prorated),
        'remaining_occurrences': remaining,
        'total_occurrences': total,
        'effective_date': effective_date_for_amount_change(recurring, today=today).isoformat(),
        'has_saved_card': bool(
            (recurring.tranzila_token or '').strip() and recurring.card_expire_month and recurring.card_expire_year
        ),
    })
    return quote


def _serialize_pending(change: ScheduledUnitChange) -> dict:
    return {
        'id': str(change.id),
        'effective_date': change.effective_date.isoformat(),
        'target_label': change.target_label,
        'old_amount': str(change.old_amount) if change.old_amount is not None else None,
        'new_amount': str(change.new_amount) if change.new_amount is not None else None,
        'target_lesson_ids': [str(pk) for pk in change.target_lessons.values_list('id', flat=True)],
        'target_bundle_id': str(change.target_bundle_id) if change.target_bundle_id else None,
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _charge_difference(recurring: RecurringPayment, enrollment: LessonEnrollment, target_lessons, target_bundle, amount: Decimal, quote: dict, today: date, created_by=None) -> Payment:
    """Charge the prorated difference on the saved card. Raises on decline; freezes on doubt."""
    child = enrollment.child
    family = child.family
    lesson = target_lessons[0]
    stuck = Payment.objects.filter(
        child=child, payment_type='one_time', status='processing',
        description__startswith=DIFF_DESCRIPTION,
    ).exists()
    if stuck:
        raise ChangePricingError(
            'חיוב הפרש קודם לילד הזה לא הסתיים בוודאות — יש לבדוק מול Tranzila ולסגור אותו לפני החלפה נוספת.',
            processing=True,
        )
    description = f'{DIFF_DESCRIPTION}: {quote["target_label"]} ({quote["remaining_occurrences"]}/{quote["total_occurrences"]} שיעורים החודש)'[:200]
    with transaction.atomic():
        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first() if family else None,
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=target_bundle,
            payment_type='one_time',
            status='processing',
            base_amount=amount,
            discount_amount=Decimal('0.00'),
            final_amount=amount,
            registration_fee=Decimal('0.00'),
            description=description,
        )
    tranzila = TranzilaService.production()
    result = tranzila.charge_with_token(
        token=recurring.tranzila_token,
        amount=amount,
        description=description,
        transaction_id=str(payment.id),
        items=[{
            'name': description[:60], 'type': 'I', 'unit_price': float(amount), 'units_number': 1,
            'unit_type': 1, 'price_type': 'G', 'currency_code': 'ILS',
        }],
        expire_month=recurring.card_expire_month,
        expire_year=recurring.card_expire_year,
        duplicate_guard_key=f'change-diff-{payment.id}',
    )
    if not result.get('success'):
        if is_tranzila_uncertain_gateway_error(result):
            payment.failure_reason = str(result.get('error') or 'uncertain')[:500]
            payment.save(update_fields=['failure_reason', 'updated_at'])   # stays 'processing' — a person checks
            logger.error('change diff charge %s uncertain: %s', payment.id, result.get('error'))
            raise ChangePricingError('החיוב לא אושר בוודאות. יש לבדוק מול Tranzila לפני ניסיון נוסף.', processing=True)
        payment.status = 'failed'
        payment.failure_reason = str(result.get('error') or 'התשלום נדחה')[:500]
        payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        raise ChangePricingError(f"חיוב ההפרש נדחה ({result.get('error') or 'ללא פירוט'}). ההחלפה לא בוצעה.")

    with transaction.atomic():
        payment.status = 'completed'
        payment.payment_date = timezone.now()
        payment.save(update_fields=['status', 'payment_date', 'updated_at'])
        txn, _ = TranzilaTransaction.objects.get_or_create(
            idempotency_key=f'change_diff_{payment.id}',
            defaults={
                'transaction_id': result.get('transaction_id', '') or '',
                'confirmation_code': result.get('confirmation_code', '') or '',
                'transaction_type': 'charge',
                'response_code': result.get('response_code', '000') or '000',
                'response_message': '',
                'request_data': {'enrollment_id': str(enrollment.id)},
                'response_data': result.get('raw_response', {}) or {},
                'is_successful': True,
                'response_timestamp': timezone.now(),
            },
        )
        payment.tranzila_transaction = txn
        payment.save(update_fields=['tranzila_transaction'])
    try:
        PaymentService()._create_invoice_from_payment(payment, txn)
    except Exception:
        logger.exception('change diff: invoice for payment %s failed', payment.id)
    return payment


def apply_unit_change(
    *,
    enrollment: LessonEnrollment,
    target_lessons: list[Lesson],
    target_bundle: LessonBundle | None,
    expected_new_amount: Decimal | None,
    created_by=None,
    today: date | None = None,
) -> dict:
    """
    Move the child with the price handled. `expected_new_amount` is what the
    office saw on the quote; a different figure now means something changed
    under them and the request is refused rather than charged blind.
    """
    today = today or timezone.now().astimezone(JERUSALEM_TZ).date()
    quote = quote_unit_change(enrollment=enrollment, target_lessons=target_lessons, target_bundle=target_bundle, today=today)
    if quote['blocked'] and quote['direction'] != 'no_sto':
        raise ChangePricingError(quote['blocked'])
    if quote['pending_change']:
        raise ChangePricingError('כבר מתוזמנת החלפה לחוג הזה — בטלו אותה לפני החלפה נוספת.')

    direction = quote['direction']
    if direction in ('same', 'no_sto'):
        result = replace_unit(enrollment=enrollment, target_lessons=target_lessons, target_bundle=target_bundle)
        return {**result, 'quote': quote, 'applied': 'now', 'charged': None}

    if expected_new_amount is None:
        raise ChangePricingError(
            f"המחיר החודשי משתנה ({quote['current_amount']} ← {quote['new_amount']}) — יש לאשר את הצעת המחיר לפני ההחלפה."
        )
    if money(expected_new_amount) != Decimal(quote['new_amount']):
        raise ChangePricingError('המחיר השתנה מאז שהוצג — יש לפתוח את ההחלפה מחדש ולאשר את הסכום העדכני.')

    recurring = RecurringPayment.objects.get(id=quote['recurring_payment_id'])
    new_amount = Decimal(quote['new_amount'])

    if direction == 'up':
        prorated = Decimal(quote['prorated_difference'])
        charged = None
        folded = False
        if prorated > 0:
            if quote['has_saved_card']:
                charged = _charge_difference(recurring, enrollment, target_lessons, target_bundle, prorated, quote, today, created_by)
            else:
                # No card to charge now: the difference rides on next month's charge, once.
                effective = date.fromisoformat(quote['effective_date'])
                set_month_override(
                    recurring, billing_month=effective, amount=new_amount + prorated,
                    reason=f'הפרש יחסי מהחלפת חוג ל{quote["target_label"]} (אין כרטיס שמור)', source='manual',
                    created_by=created_by,
                )
                folded = True
        result = replace_unit(enrollment=enrollment, target_lessons=target_lessons, target_bundle=target_bundle)
        schedule_recurring_amount(recurring, new_amount)
        return {
            **result, 'quote': quote, 'applied': 'now',
            'charged': str(charged.final_amount) if charged else None,
            'charge_payment_id': str(charged.id) if charged else None,
            'folded_into_next_month': folded,
        }

    # down: nothing moves this month; both the move and the amount wait for the next cycle.
    effective = date.fromisoformat(quote['effective_date'])
    old_rows = sibling_unit_enrollments(enrollment)
    with transaction.atomic():
        change = ScheduledUnitChange.objects.create(
            enrollment=old_rows[0],
            child=enrollment.child,
            target_bundle=target_bundle,
            recurring_payment=recurring,
            effective_date=effective,
            old_amount=Decimal(quote['current_amount']),
            new_amount=new_amount,
            target_label=quote['target_label'],
            created_by=created_by,
        )
        change.target_lessons.set(target_lessons)
        schedule_recurring_amount(recurring, new_amount)
    quote['pending_change'] = _serialize_pending(change)
    return {
        'unchanged': True, 'kept': old_rows, 'removed_ids': [],
        'quote': quote, 'applied': 'scheduled', 'scheduled_change': quote['pending_change'], 'charged': None,
    }


def cancel_scheduled_change(change: ScheduledUnitChange) -> None:
    """Undo a scheduled downgrade before it runs: the standing order keeps its current amount."""
    if not change.is_pending:
        raise ChangePricingError('ההחלפה המתוזמנת כבר בוצעה או בוטלה')
    with transaction.atomic():
        change.cancelled_at = timezone.now()
        change.save(update_fields=['cancelled_at'])
        recurring = change.recurring_payment
        if recurring and recurring.status == 'active' and change.new_amount is not None \
                and recurring.pending_amount is not None and money(recurring.pending_amount) == money(change.new_amount):
            schedule_recurring_amount(recurring, money(recurring.amount))


def apply_due_scheduled_unit_changes(*, today: date | None = None) -> dict:
    """Run by the billing cron before it charges: move children whose change date has come."""
    today = today or timezone.now().astimezone(JERUSALEM_TZ).date()
    due = (
        ScheduledUnitChange.objects
        .filter(applied_at__isnull=True, cancelled_at__isnull=True, effective_date__lte=today)
        .select_related('enrollment', 'enrollment__child', 'enrollment__lesson', 'target_bundle')
        .prefetch_related('target_lessons__course', 'target_lessons__room')
        .order_by('effective_date', 'created_at')
    )
    summary = {'applied': 0, 'failed': 0, 'errors': []}
    for change in due:
        enrollment = change.enrollment
        if enrollment.status not in ('active', 'payments_problem'):
            change.applied_at = timezone.now()
            change.last_error = 'ההרשמה כבר לא פעילה — לא בוצע'
            change.save(update_fields=['applied_at', 'last_error'])
            summary['failed'] += 1
            summary['errors'].append({'id': str(change.id), 'error': change.last_error})
            continue
        targets = [lesson for lesson in change.target_lessons.all() if lesson.status != 'cancelled']
        targets.sort(key=lambda lesson: (lesson.day_of_week, str(lesson.start_time), str(lesson.id)))
        try:
            replace_unit(enrollment=enrollment, target_lessons=targets, target_bundle=change.target_bundle)
        except Exception as exc:  # capacity, cancelled lesson — leave it for a person, do not retry forever
            logger.exception('scheduled unit change %s failed', change.id)
            change.last_error = str(exc)[:1000]
            change.applied_at = timezone.now()
            change.save(update_fields=['last_error', 'applied_at'])
            summary['failed'] += 1
            summary['errors'].append({'id': str(change.id), 'error': change.last_error})
            continue
        change.applied_at = timezone.now()
        change.last_error = ''
        change.save(update_fields=['applied_at', 'last_error'])
        summary['applied'] += 1
    return summary


# ---------------------------------------------------------------------------
# Read-only report: standing orders whose amount no longer matches their unit
# ---------------------------------------------------------------------------

def price_drift_report(*, today: date | None = None, limit: int = 500) -> list[dict]:
    """
    Active standing orders billed by our cron whose amount differs from what
    their current lessons cost today (a child moved to a dearer or cheaper
    unit before changes were priced, or a course price that moved).
    Nothing is written.
    """
    today = today or timezone.now().astimezone(JERUSALEM_TZ).date()
    rows = []
    qs = (
        RecurringPayment.objects
        .filter(status='active', tranzila_recurring_index='')
        .exclude(initial_payment__isnull=True)
        .exclude(initial_payment__lesson__isnull=True)
        .select_related('child', 'child__family', 'initial_payment', 'initial_payment__lesson',
                        'initial_payment__lesson__course', 'initial_payment__bundle')
        .order_by('child__last_name', 'child__first_name')[:limit]
    )
    for recurring in qs:
        initial = recurring.initial_payment
        enrollment = (
            LessonEnrollment.objects
            .filter(child_id=recurring.child_id, lesson_id=initial.lesson_id, status__in=('active', 'payments_problem'),
                    trial_lesson_date__isnull=True)
            .select_related('lesson', 'lesson__course', 'bundle', 'child', 'child__family')
            .first()
        )
        if enrollment is None:
            continue
        unit = sibling_unit_enrollments(enrollment)
        target_lessons = [row.lesson for row in unit]
        target_lessons.sort(key=lambda lesson: (lesson.day_of_week, str(lesson.start_time), str(lesson.id)))
        target_bundle = next((row.bundle for row in unit if row.bundle_id), None)
        try:
            quote = quote_unit_change(enrollment=enrollment, target_lessons=target_lessons, target_bundle=target_bundle, today=today)
        except Exception as exc:  # a broken unit must not hide the rest of the report
            logger.warning('price drift: %s could not be quoted: %s', recurring.id, exc)
            continue
        if quote['blocked'] or quote['direction'] in ('same', 'no_sto'):
            continue
        child = enrollment.child
        rows.append({
            'recurring_payment_id': str(recurring.id),
            'child_id': str(child.id),
            'child_name': child.full_name,
            'family_name': child.family.name if child.family_id else '',
            'unit_label': quote['target_label'],
            'current_amount': quote['current_amount'],
            'expected_amount': quote['new_amount'],
            'difference': quote['difference'],
            'direction': quote['direction'],
            'pending_amount': str(recurring.pending_amount) if recurring.pending_amount is not None else None,
            'next_billing_date': recurring.next_billing_date.isoformat() if recurring.next_billing_date else None,
        })
    return rows
