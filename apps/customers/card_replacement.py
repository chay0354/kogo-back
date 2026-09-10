"""
Replace the card behind every standing order a family holds, and collect what
the old card never paid.

Why this exists as its own module rather than an extension of `card_update`:

* `card_update` repairs **one** standing order, and only after it has already
  failed. A family with two children in three courses holds three standing
  orders, each with its own token; a stolen card, an expired card or a brand the
  terminal cannot clear invalidates all of them at once, and the parent should
  type a card once, not three times.
* `card_link` deliberately refuses a child who already has a standing order
  (`card_link.py:_prevalidate`). It opens new subscriptions; it was never a
  replacement tool. That refusal is the error the office actually hits when it
  tries to change somebody's card.

Money rails, in order:

1. Validate the card and refuse a blocked brand before anything leaves us.
2. `verify_card` **once** — a token, no money. A card that cannot be tokenised
   never gets to charge anybody.
3. Save that token on every target, under a row lock, and commit. The card is
   fixed for the future even if a catch-up charge is later declined.
4. Only then charge the arrears, month by month, each one guarded by its own
   idempotency key committed immediately after the gateway answers — never
   inside the block that also writes invoices and statuses. `docs/12` records
   what happens when those share a transaction: the money moves and the
   database keeps no trace of it.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.core.card_validation import CardValidationError, validate_card_details
from apps.core.payment_service import (
    JERUSALEM_TZ,
    PaymentService,
    subscription_tranzila_items,
)
from apps.core.tranzila_service import (
    TranzilaService,
    extract_card_token,
    is_tranzila_uncertain_gateway_error,
)
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.recurring_amount import amount_for_charge, month_start

logger = logging.getLogger(__name__)

# A standing order in these states belongs to a paying customer and takes the
# new card. 'cancelled' is a decision somebody made; we do not undo it here.
REPLACEABLE_STATUSES = ('active', 'paused', 'failed')

# Arrears are never collected further back than this. A card fixed after a long
# silence must not surprise a parent with a year of charges in one click.
MAX_ARREARS_MONTHS = 12


class CardReplacementError(ValueError):
    """Raised with a message meant for whoever is looking at the screen."""


@dataclass
class MonthDue:
    month: date
    amount: Decimal
    override_id: str | None = None

    def as_dict(self) -> dict:
        return {
            'month': self.month.isoformat(),
            'label': f'{self.month:%m/%Y}',
            'amount': str(self.amount),
        }


@dataclass
class Target:
    recurring: RecurringPayment
    due: list[MonthDue] = field(default_factory=list)
    skip_reason: str = ''

    @property
    def total_due(self) -> Decimal:
        return sum((row.amount for row in self.due), Decimal('0.00'))

    def as_dict(self) -> dict:
        rec = self.recurring
        lesson = rec.initial_payment.lesson if rec.initial_payment else None
        course = lesson.course if lesson else None
        return {
            'recurring_id': str(rec.id),
            'child_id': str(rec.child_id),
            'child_name': rec.child.full_name if rec.child_id else '',
            'course_name': course.name if course else '',
            'branch_name': course.branch.name if course and course.branch_id else '',
            'status': rec.status,
            'monthly_amount': str(rec.amount),
            'card_last4': (rec.tranzila_token or '')[-4:],
            'months_due': [row.as_dict() for row in self.due],
            'total_due': str(self.total_due),
            'skip_reason': self.skip_reason,
            'will_update_card': not self.skip_reason,
        }


def _recurring_qs():
    return RecurringPayment.objects.select_related(
        'child',
        'child__family',
        'initial_payment',
        'initial_payment__lesson',
        'initial_payment__lesson__course',
        'initial_payment__lesson__course__branch',
        'initial_payment__bundle',
    )


def _today() -> date:
    return timezone.now().astimezone(JERUSALEM_TZ).date()


def _next_month_first(from_day: date) -> date:
    if from_day.month == 12:
        return date(from_day.year + 1, 1, 1)
    return date(from_day.year, from_day.month + 1, 1)


def _paid_until(charge_month: date) -> date:
    last_day = calendar.monthrange(charge_month.year, charge_month.month)[1]
    return date(charge_month.year, charge_month.month, last_day)


def family_standing_orders(family) -> list[RecurringPayment]:
    """Every standing order of every child in the family, newest child first."""
    if family is None:
        return []
    return list(
        _recurring_qs()
        .filter(child__family=family, status__in=REPLACEABLE_STATUSES)
        .order_by('child__first_name', 'created_at')
    )


def months_outstanding(recurring: RecurringPayment, *, today: date | None = None) -> list[MonthDue]:
    """
    Months this standing order should have billed and did not.

    `next_billing_date` is the authority: the cron advances it only after a
    charge succeeds, so on a failed order it still points at the first month
    nobody paid for. A month is dropped when a completed subscription payment
    for that child and lesson already exists — which is what makes running this
    twice harmless.
    """
    today = today or _today()
    lesson = recurring.initial_payment.lesson if recurring.initial_payment else None
    if lesson is None:
        return []

    cursor = month_start(recurring.next_billing_date or recurring.start_date or today)
    limit = month_start(today)
    if recurring.end_date:
        limit = min(limit, month_start(recurring.end_date))

    paid_months = {
        (row.year, row.month)
        for row in Payment.objects.filter(
            child_id=recurring.child_id,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='completed',
            payment_date__isnull=False,
        ).values_list('payment_date', flat=True)
    }

    due: list[MonthDue] = []
    while cursor <= limit and len(due) < MAX_ARREARS_MONTHS:
        if (cursor.year, cursor.month) not in paid_months:
            amount, override = amount_for_charge(recurring, on_date=cursor)
            if amount >= Decimal('1.00'):
                due.append(MonthDue(
                    month=cursor,
                    amount=amount,
                    override_id=str(override.id) if override else None,
                ))
        cursor = _next_month_first(cursor)
    return due


def build_targets(family, *, today: date | None = None) -> list[Target]:
    """Every standing order plus what it owes, including the ones we will not charge."""
    today = today or _today()
    targets: list[Target] = []
    for recurring in family_standing_orders(family):
        target = Target(recurring=recurring)
        if (recurring.tranzila_recurring_index or '').strip():
            # Billed by Tranzila's own standing-order engine, not by our cron.
            # Its card lives on their side; touching our token would do nothing.
            target.skip_reason = 'הוראת קבע המנוהלת בטרנזילה — יש לעדכן אותה מול טרנזילה'
        elif recurring.initial_payment is None or recurring.initial_payment.lesson is None:
            target.skip_reason = 'אין שיעור משויך להוראת הקבע'
        else:
            target.due = months_outstanding(recurring, today=today)
        targets.append(target)
    return targets


def quote(family, *, today: date | None = None) -> dict:
    """What the screen shows before anybody types a card. Reads only."""
    targets = build_targets(family, today=today or _today())
    chargeable = [t for t in targets if not t.skip_reason]
    total = sum((t.total_due for t in chargeable), Decimal('0.00'))
    return {
        'family_id': str(family.id) if family else '',
        'family_name': family.name if family else '',
        'targets': [t.as_dict() for t in targets],
        'standing_orders': len(chargeable),
        'total_due': str(total),
        'will_charge': total >= Decimal('1.00'),
        'blocked': [t.as_dict() for t in targets if t.skip_reason],
    }


def _charge_one_month(
    *,
    tranzila: TranzilaService,
    recurring: RecurringPayment,
    row: MonthDue,
    token: str,
    expire_month: int,
    expire_year: int,
) -> dict:
    """
    One month, one charge. Returns a result row; never raises for a decline.

    The gateway fact is committed on its own the moment the answer arrives, so a
    failure while writing the invoice cannot erase the evidence that money moved
    and let the next run bill the same month again.
    """
    lesson = recurring.initial_payment.lesson
    child = recurring.child
    family = child.family
    idempotency_key = f'cardrepl-{recurring.id}-{row.month:%Y-%m}'
    outcome = {
        'recurring_id': str(recurring.id),
        'child_name': child.full_name,
        'month': row.month.isoformat(),
        'amount': str(row.amount),
    }

    if TranzilaTransaction.objects.filter(idempotency_key=idempotency_key, is_successful=True).exists():
        return {**outcome, 'status': 'skipped', 'reason': 'החודש הזה כבר נגבה'}

    payment = Payment.objects.create(
        child=child,
        family=family,
        parent=family.parents.filter(is_primary=True).first() if family else None,
        branch=lesson.course.branch,
        lesson=lesson,
        bundle=recurring.initial_payment.bundle if recurring.initial_payment else None,
        payment_type='recurring_subscription',
        status='pending',
        base_amount=recurring.base_amount or row.amount,
        discount_amount=recurring.discount_amount or Decimal('0.00'),
        final_amount=row.amount,
        registration_fee=Decimal('0.00'),
        payment_date=None,
        description=f'מנוי חודשי {row.month:%m/%Y} - {lesson.course.name} - {child.full_name}',
    )

    result = tranzila.charge_with_token(
        token=token,
        amount=row.amount,
        description=payment.description,
        transaction_id=str(payment.id),
        items=subscription_tranzila_items(
            label=f'{lesson.course.name} - {child.full_name}',
            prorated_lesson=row.amount,
            registration_fee=Decimal('0'),
        ),
        expire_month=expire_month,
        expire_year=expire_year,
        duplicate_guard_key=idempotency_key,
    )

    if is_tranzila_uncertain_gateway_error(result):
        # The card may already have been charged. Leaving the row 'processing'
        # keeps the month unpaid-but-untouchable rather than declaring a failure
        # that would let tomorrow's run bill it a second time.
        payment.status = 'processing'
        payment.failure_reason = result.get('error', '')[:500]
        payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        logger.error('Card replacement uncertain for %s %s', recurring.id, row.month)
        return {**outcome, 'status': 'uncertain',
                'reason': 'תשובת הסליקה לא ודאית — החודש הזה בבדיקה, אל תחייבו שוב'}

    if not result.get('success'):
        payment.status = 'failed'
        payment.failure_reason = (result.get('error') or 'החיוב נדחה')[:500]
        payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        return {**outcome, 'status': 'declined', 'reason': payment.failure_reason}

    # Committed alone, immediately. This is the anti-double-charge record.
    with transaction.atomic():
        txn = TranzilaTransaction.objects.create(
            transaction_id=result.get('transaction_id', ''),
            confirmation_code=result.get('confirmation_code', ''),
            transaction_type='recurring_charge',
            response_code=result.get('response_code', '000'),
            response_message='',
            request_data={},
            response_data=result.get('raw_response', {}) or {},
            idempotency_key=idempotency_key,
            is_successful=True,
            response_timestamp=timezone.now(),
        )

    try:
        with transaction.atomic():
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.tranzila_transaction = txn
            payment.save(update_fields=['status', 'payment_date', 'tranzila_transaction', 'updated_at'])
            PaymentService()._create_invoice_from_payment(payment, txn)

            locked = RecurringPayment.objects.select_for_update(of=('self',)).get(id=recurring.id)
            locked.last_charge_date = _today()
            locked.next_billing_date = _next_month_first(row.month)
            locked.save(update_fields=['last_charge_date', 'next_billing_date', 'updated_at'])

            child.status = 'active'
            child.paid_until_date = _paid_until(row.month)
            child.save(update_fields=['status', 'paid_until_date', 'updated_at'])

            if row.override_id:
                from apps.customers.models import RecurringChargeOverride
                RecurringChargeOverride.objects.filter(
                    id=row.override_id, applied_at__isnull=True,
                ).update(applied_at=timezone.now())
    except Exception as exc:  # pragma: no cover - defensive
        # The money moved and the gateway record stands, so the month can never
        # be billed twice. What failed here is bookkeeping, and it is reported.
        logger.exception('Card replacement post-processing failed for %s %s', recurring.id, row.month)
        return {**outcome, 'status': 'charged_with_errors', 'reason': str(exc)}

    return {**outcome, 'status': 'charged'}


def replace_card(
    family,
    card_details: dict[str, Any],
    *,
    actor=None,
    source: str = 'crm',
    charge: bool = True,
    today: date | None = None,
) -> dict:
    """
    One card in, every standing order of the family repointed at it, arrears collected.

    The card is saved before a single charge is attempted. A parent whose second
    month is declined still ends the call with a working standing order, which is
    the outcome that matters most: next month bills itself.
    """
    today = today or _today()
    if family is None:
        raise CardReplacementError('לא נמצאה משפחה')

    card = validate_card_details(card_details)  # raises CardValidationError

    targets = build_targets(family, today=today)
    live = [t for t in targets if not t.skip_reason]
    if not live:
        raise CardReplacementError('אין למשפחה הזאת הוראת קבע שניתן לעדכן')

    tranzila = TranzilaService.production()
    verify = tranzila.verify_card(
        card_number=card['card_number'],
        expiry_month=card['expiry_month'],
        expiry_year=card['expiry_year'],
        cvv=card['cvv'],
        card_holder_id=card.get('card_holder_id') or '',
        description=f'עדכון כרטיס - {family.name}',
        duplicate_guard_key=f'cardrepl-verify-{family.id}-{today.isoformat()}',
    )
    if not verify.get('success'):
        raise CardReplacementError(verify.get('error') or 'הכרטיס לא אושר. נסו כרטיס אחר.')

    token = (verify.get('token') or '').strip() or extract_card_token(
        verify.get('raw_response') if isinstance(verify.get('raw_response'), dict) else {},
        verify,
    )
    if not token:
        # Exactly the Diners shape: the gateway is content and hands back nothing
        # to bill with. Saving a blank token would leave the family looking fixed
        # while every future month silently skips them.
        raise CardReplacementError(
            'הכרטיס אומת אך לא התקבל טוקן לחיוב חוזר. לא ניתן להשתמש בו להוראת קבע — נסו כרטיס אחר.'
        )

    # Step one, on its own: the card is now the family's card.
    updated: list[str] = []
    with transaction.atomic():
        for target in live:
            locked = RecurringPayment.objects.select_for_update(of=('self',)).get(id=target.recurring.id)
            locked.tranzila_token = token
            locked.card_expire_month = card['expiry_month']
            locked.card_expire_year = card['expiry_year']
            locked.status = 'active'
            locked.cancellation_reason = ''
            locked.card_update_reminders_sent = 0
            locked.card_update_last_reminder_at = None
            locked.save(update_fields=[
                'tranzila_token', 'card_expire_month', 'card_expire_year', 'status',
                'cancellation_reason', 'card_update_reminders_sent',
                'card_update_last_reminder_at', 'updated_at',
            ])
            updated.append(str(locked.id))

    # Step two: the arrears, one month at a time, each standing alone.
    results: list[dict] = []
    charged_total = Decimal('0.00')
    if charge:
        for target in live:
            for row in target.due:
                outcome = _charge_one_month(
                    tranzila=tranzila,
                    recurring=target.recurring,
                    row=row,
                    token=token,
                    expire_month=card['expiry_month'],
                    expire_year=card['expiry_year'],
                )
                results.append(outcome)
                if outcome['status'] == 'charged':
                    charged_total += row.amount

    for target in live:
        child = target.recurring.child
        if child and child.status == 'payment_problem' and not target.due:
            child.status = 'active'
            child.save(update_fields=['status', 'updated_at'])

    record = _record(
        family=family,
        card=card,
        actor=actor,
        source=source,
        recurring_ids=updated,
        charged_total=charged_total,
        results=results,
    )

    return {
        'success': True,
        'replacement_id': str(record.id) if record else '',
        'standing_orders_updated': len(updated),
        'charged_total': str(charged_total),
        'card_last4': card['card_number'][-4:],
        'card_brand': card.get('brand', ''),
        'results': results,
        'declined': [r for r in results if r['status'] in ('declined', 'uncertain', 'charged_with_errors')],
    }


def _record(*, family, card, actor, source, recurring_ids, charged_total, results):
    """Who replaced the card, when, and what it collected. Never the card number."""
    from apps.customers.models import CardReplacement

    return CardReplacement.objects.create(
        family=family,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        source=source,
        card_last4=card['card_number'][-4:],
        card_brand=card.get('brand', ''),
        card_expire_month=card['expiry_month'],
        card_expire_year=card['expiry_year'],
        recurring_ids=list(recurring_ids),
        charged_amount=charged_total,
        results=list(results),
    )
