"""Charging a tenant's standing order — every path by which rental money moves.

    charge_due(today=, limit=)        the monthly cron: due active orders, one per transaction
    retry_charge(charge, user=)       the office's "retry now" on a failed month
    mark_charged(charge, ...)         the office's decision on a month in review: it went through
    void_charge(charge, ...)          ... or it is not charged
    reserve_month / reclaim_failed / call_gateway / record_result
                                      the rails the tenant's card page (card.py) runs on too

docs/12-RECURRING-BILLING-CHAIN.md lists how the courses' chain can charge a card
twice. Each of those weaknesses is closed here, and each has a test:

A. The row that says a month is taken is written and committed *before* the
   gateway is called: TenantCharge 'reserved', under UNIQUE(standing_order,
   period). The gateway's answer is written in a transaction of its own
   (record_result), and the receipt only after that, in its own try/except.
   Nothing that fails later can roll the reservation back, so nothing can
   make the month look unpaid again. The blocks are durable: they refuse to
   run inside an outer transaction that could still undo them.
B. A timeout, a connection error or an exception is 'review', never a
   decline. A month in review is never sent again by itself; the office checks
   Tranzila and decides. A reservation that never heard back goes to review too.
C. The cron takes one due order at a time under select_for_update(skip_locked=True),
   so overlapping runs never hold the same order, and an order is charged at
   most once a day.
E. A decline stops the order ('failed') and opens a card link for the tenant;
   the office can retry the month, or the tenant's new card pays it.

The month key is also sent to Tranzila as DCdisable (duplicate_guard_key). It
is off in production (docs/12), so nothing here relies on it.

Every Tranzila call goes through gateway(), which refuses while
RENTAL_BILLING_ENABLED is off.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.core.models import Business
from apps.core.tranzila_service import TranzilaService, extract_card_token, is_tranzila_uncertain_gateway_error
from apps.core.vat import add_vat
from apps.rental_billing.errors import DISABLED_MESSAGE, BillingDisabled, BillingError
from apps.rental_billing.links import cancel_live_links, ensure_card_link
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.schedule import (
    billing_date_after,
    first_billing_on_or_after,
    first_of_month,
    month_label,
)

logger = logging.getLogger(__name__)

Order = TenantStandingOrder
Charge = TenantCharge

# A reservation this old never heard back from the gateway: a function killed
# mid-call, a crash between the call and the write. Its outcome is unknown.
STALE_RESERVATION = timedelta(minutes=15)
STALE_MESSAGE = 'לא התקבלה תשובה מטרנזילה על החיוב — ייתכן שהכרטיס חויב. יש לבדוק בטרנזילה ולהכריע.'
CENT = Decimal('0.01')

OUTCOME_CHARGED = 'charged'
OUTCOME_FAILED = 'failed'
OUTCOME_REVIEW = 'review'
# The gateway answered after the month was already decided on (the stale sweep, the office).
OUTCOME_LATE = 'late'


# --------------------------------------------------------------- the switch

def billing_enabled() -> bool:
    return bool(getattr(settings, 'RENTAL_BILLING_ENABLED', False))


def business_name() -> str:
    return (getattr(settings, 'RENTAL_BILLING_BUSINESS_NAME', '') or '').strip()


def rental_business():
    """The Business every rental charge is tagged to, found by name — never created here."""
    name = business_name()
    return Business.objects.filter(name=name).first() if name else None


def missing_business_message() -> str:
    return f'העסק "{business_name()}" לא נמצא במערכת, ולכן לא בוצע חיוב. יש להקים אותו בהגדרות העסקים.'


def require_business() -> Business:
    business = rental_business()
    if business is None:
        raise BillingError(missing_business_message())
    return business


def gateway() -> TranzilaService:
    """The only way this app reaches Tranzila. Refuses while the switch is off."""
    if not billing_enabled():
        raise BillingDisabled()
    # The REST terminals the courses' standing orders charge on: charge_with_token
    # bills the token terminal, charge_with_card and verify_card the card terminal.
    return TranzilaService.production()


def today_local() -> date:
    return timezone.localdate()


# ------------------------------------------------------------------ amounts

def split_amount(amount_before_vat) -> tuple[int, int, int]:
    """(before VAT, VAT, total) in agorot for a monthly amount quoted before VAT, at today's rate."""
    net = Decimal(str(amount_before_vat)).quantize(CENT)
    total = add_vat(net)
    net_agorot = int(net * 100)
    total_agorot = int(total * 100)
    return net_agorot, total_agorot - net_agorot, total_agorot


def shekels(agorot) -> Decimal:
    return (Decimal(int(agorot or 0)) / 100).quantize(CENT)


def guard_key(order_id, period: date) -> str:
    """The month's key at Tranzila (DCdisable). The same for every attempt at the same month."""
    return f'rental-{order_id}-{period:%Y-%m}'


def charge_description(order, period: date) -> str:
    return f'שכירות סטודיו {month_label(period)} - {order.tenant.full_name}'.strip()


def tranzila_items(order, period: date, total: Decimal) -> list:
    return [{
        'name': charge_description(order, period)[:80],
        'type': 'I',
        'unit_price': float(total),
        'units_number': 1,
        'unit_type': 1,
        # Gross: the item price is what the card is charged, VAT included.
        'price_type': 'G',
        'currency_code': 'ILS',
    }]


def card_token(result: dict) -> str:
    raw = result.get('raw_response') if isinstance(result.get('raw_response'), dict) else {}
    return (result.get('token') or '').strip() or extract_card_token(raw, result)


# ----------------------------------------------------------------- schedule

def advance(order, period: date) -> None:
    """
    Move the order's next charge past `period`, and end it once that passes
    its end date. Changes the instance; the caller saves.
    """
    after = billing_date_after(period, order.billing_day)
    if order.next_charge_date is None or first_of_month(order.next_charge_date) <= period:
        order.next_charge_date = after
    if (
        order.end_date
        and order.next_charge_date
        and order.next_charge_date > order.end_date
        and order.status in Order.OPEN_STATUSES
    ):
        order.status = Order.STATUS_ENDED


def next_open_billing_date(order, on_or_after: date) -> date:
    """The first billing date on or after the day whose month this order has no charge for yet."""
    taken = set(Charge.objects.filter(standing_order_id=order.pk).values_list('period', flat=True))
    candidate = first_billing_on_or_after(on_or_after, order.billing_day)
    while first_of_month(candidate) in taken:
        candidate = billing_date_after(first_of_month(candidate), order.billing_day)
    return candidate


def is_undecided(charge, now=None) -> bool:
    """In review, or a reservation that never heard back: only the office may decide on it."""
    if charge.status == Charge.STATUS_REVIEW:
        return True
    return charge.status == Charge.STATUS_RESERVED and charge.reserved_at < (now or timezone.now()) - STALE_RESERVATION


def sweep_stale_reservations(now=None) -> int:
    """Reservations that never heard back, to review. Their outcome is unknown: the card may be charged."""
    now = now or timezone.now()
    return Charge.objects.filter(
        status=Charge.STATUS_RESERVED, reserved_at__lt=now - STALE_RESERVATION,
    ).update(status=Charge.STATUS_REVIEW, error=STALE_MESSAGE, updated_at=now)


# -------------------------------------------------------------------- rails

def _insert_reservation(order, period: date, *, business, trigger: str):
    """The month's row as 'reserved', or None when the month already has a row. Inside the caller's transaction."""
    net, vat, total = split_amount(order.amount_before_vat)
    category = order.business_category if order.business_category_id else None
    if category is not None and category.business_id != business.id:
        category = None
    if order.business_id is None:
        # Opened before the business existed: it carries the tag its charges carry from now on.
        Order.objects.filter(pk=order.pk, business__isnull=True).update(business=business)
    try:
        # A savepoint: a month that is taken must not break the caller's transaction.
        with transaction.atomic():
            return Charge.objects.create(
                standing_order=order,
                period=period,
                amount_before_vat=net,
                vat_amount=vat,
                total=total,
                business=business,
                business_category=category,
                status=Charge.STATUS_RESERVED,
                trigger=trigger,
                attempts=1,
                card_last4=order.card_last4,
                reserved_at=timezone.now(),
            )
    except IntegrityError:
        if not Charge.objects.filter(standing_order_id=order.pk, period=period).exists():
            raise  # not the month guard: a bug, never swallowed
        return None


def reserve_month(order, period: date, *, business, trigger: str):
    """Reserve the month and commit it, before anything calls the gateway. None when it is taken."""
    with transaction.atomic(durable=True):
        return _insert_reservation(order, period, business=business, trigger=trigger)


def reclaim_failed(charge_id, *, trigger: str):
    """A failed month back to 'reserved' for one more attempt, committed before the gateway is called."""
    with transaction.atomic(durable=True):
        charge = Charge.objects.select_for_update().get(pk=charge_id)
        if charge.status != Charge.STATUS_FAILED:
            return None
        charge.status = Charge.STATUS_RESERVED
        charge.trigger = trigger
        charge.attempts += 1
        charge.reserved_at = timezone.now()
        charge.error = ''
        charge.response_code = ''
        charge.save(update_fields=['status', 'trigger', 'attempts', 'reserved_at', 'error', 'response_code', 'updated_at'])
        return charge


def call_gateway(call) -> dict:
    """The gateway's answer. An exception is the uncertain answer it is: the card may be charged."""
    try:
        result = call()
    except Exception as exc:
        logger.exception('Rental billing: the Tranzila call raised')
        return {'success': False, 'error': str(exc) or exc.__class__.__name__, 'uncertain': True}
    if not isinstance(result, dict):
        return {'success': False, 'error': 'Invalid gateway response', 'uncertain': True}
    return result


def outcome_of(result: dict) -> str:
    if result.get('success'):
        return OUTCOME_CHARGED
    if is_tranzila_uncertain_gateway_error(result):
        return OUTCOME_REVIEW
    return OUTCOME_FAILED


def _error_text(result: dict) -> str:
    return str(result.get('error') or result.get('message') or 'החיוב נכשל')[:1000]


def record_result(charge_id, result: dict, *, on_charged=None, fail_order: bool = True, card_last4: str = '') -> str:
    """
    Write the gateway's answer, in a transaction of its own. Returns the outcome.

    on_charged(order) runs in the same transaction when the answer is a yes:
    the card page stores the new card there, so the order turns active together
    with the charge. Without it, a failed order that is charged is active again.

    fail_order: a decline on the monthly run or an office retry stops the order
    and opens a card link; one on the card page does not — the tenant may try
    another card.
    """
    outcome = outcome_of(result)
    now = timezone.now()
    with transaction.atomic(durable=True):
        charge = Charge.objects.select_for_update().get(pk=charge_id)
        order = Order.objects.select_for_update().get(pk=charge.standing_order_id)
        if charge.status != Charge.STATUS_RESERVED:
            # Decided on while the gateway was answering. That decision stands;
            # what Tranzila said is kept for whoever looks at it.
            note = f'תשובת טרנזילה הגיעה אחרי ההכרעה ({outcome}): {result.get("transaction_id") or _error_text(result)}'
            charge.error = f'{charge.error}\n{note}'.strip()[:2000]
            if result.get('success') and not charge.transaction_id:
                charge.transaction_id = str(result.get('transaction_id') or '')[:100]
                charge.confirmation_code = str(result.get('confirmation_code') or '')[:50]
            charge.save(update_fields=['error', 'transaction_id', 'confirmation_code', 'updated_at'])
            logger.error('Rental charge %s: Tranzila answered %s after the charge was %s', charge.pk, outcome, charge.status)
            return OUTCOME_LATE

        charge.response_code = str(result.get('response_code') or '')[:20]
        if outcome == OUTCOME_CHARGED:
            charge.status = Charge.STATUS_CHARGED
            charge.transaction_id = str(result.get('transaction_id') or '')[:100]
            charge.confirmation_code = str(result.get('confirmation_code') or '')[:50]
            charge.charged_at = now
            charge.error = ''
            if card_last4:
                charge.card_last4 = card_last4
            advance(order, charge.period)
            if on_charged is not None:
                on_charged(order)
            elif order.status == Order.STATUS_FAILED:
                order.status = Order.STATUS_ACTIVE
                order.last_error = ''
                order.failed_at = None
        elif outcome == OUTCOME_REVIEW:
            charge.status = Charge.STATUS_REVIEW
            charge.error = _error_text(result)
        else:
            charge.status = Charge.STATUS_FAILED
            charge.error = _error_text(result)
            if fail_order and order.status in (Order.STATUS_ACTIVE, Order.STATUS_FAILED):
                order.status = Order.STATUS_FAILED
                order.last_error = charge.error
                order.failed_at = now
        charge.save()
        order.save()
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
        elif outcome == OUTCOME_FAILED and fail_order and order.status == Order.STATUS_FAILED:
            ensure_card_link(order)
    return outcome


def issue_receipt_safely(charge_id) -> bool:
    """
    The receipt for a charge that is on record. Never raises: a receipt that
    fails leaves the charge charged, with the reason written on it, and the
    office sees "charged, no receipt" and can issue it again.
    """
    from apps.rental_billing.receipts import issue_receipt

    try:
        issue_receipt(charge_id)
        return True
    except Exception as exc:
        logger.exception('Rental receipt not issued for charge %s (the charge is recorded)', charge_id)
        Charge.objects.filter(pk=charge_id).update(
            receipt_error=(str(exc) or exc.__class__.__name__)[:1000], updated_at=timezone.now(),
        )
        return False


def _charge_saved_card(tranzila, order, charge) -> dict:
    total = shekels(charge.total)
    return call_gateway(lambda: tranzila.charge_with_token(
        token=order.tranzila_token,
        amount=total,
        description=charge_description(order, charge.period),
        transaction_id=str(charge.pk),
        items=tranzila_items(order, charge.period, total),
        expire_month=order.card_expire_month,
        expire_year=order.card_expire_year,
        duplicate_guard_key=guard_key(order.pk, charge.period),
    ))


# --------------------------------------------------------------------- cron

def _next_due_order(today: date, seen: list):
    """The next due active order nobody else holds, locked. Never one that was charged today already."""
    touched_today = Charge.objects.filter(standing_order=OuterRef('pk'), created_at__date=today)
    return (
        Order.objects.select_for_update(skip_locked=True, of=('self',))
        .select_related('tenant')
        .filter(status=Order.STATUS_ACTIVE, next_charge_date__lte=today)
        .exclude(pk__in=seen)
        .exclude(Exists(touched_today))
        .order_by('next_charge_date', 'created_at')
        .first()
    )


def _reserve_due_month(order, business, summary: dict):
    """Inside the order's transaction: end it, skip it, or reserve its month."""
    if not order.has_card:
        summary['skipped'] += 1
        summary['errors'].append(f'{order.pk}: אין כרטיס שמור בהוראת הקבע')
        return None
    if order.end_date and order.next_charge_date > order.end_date:
        order.status = Order.STATUS_ENDED
        order.save(update_fields=['status', 'updated_at'])
        cancel_live_links(order)
        summary['ended'] += 1
        return None
    period = first_of_month(order.next_charge_date)
    charge = _insert_reservation(order, period, business=business, trigger=Charge.TRIGGER_CRON)
    if charge is not None:
        charge.standing_order = order
        return charge
    existing = Charge.objects.get(standing_order=order, period=period)
    if existing.status in (Charge.STATUS_CHARGED, Charge.STATUS_VOIDED):
        # The month was settled elsewhere (the card page, the office) and the
        # schedule had not moved on: move it, charge nothing.
        advance(order, period)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
    else:
        # Reserved, in review or failed: an outcome nobody has decided on. Never sent again by itself.
        summary['errors'].append(f'{order.pk}: {period:%Y-%m} {existing.get_status_display()} — ממתין להכרעת המשרד')
    summary['skipped'] += 1
    return None


def charge_due(*, today: date | None = None, limit: int = 40) -> dict:
    """
    Charge every active standing order due on or before `today`, up to `limit`.

    Refuses as a whole — before any row is touched — while the switch is off,
    when the business to tag the charges to is missing, or when Tranzila is
    not configured. `limit` keeps one cron call under the function's time
    limit; what is left stays due for the next call.
    """
    summary = {
        'ok': True, 'enabled': billing_enabled(), 'checked': 0, 'charged': 0, 'failed': 0, 'review': 0,
        'skipped': 0, 'ended': 0, 'receipts': 0, 'stale_to_review': 0, 'errors': [],
    }
    if not summary['enabled']:
        summary.update(disabled=True, message=DISABLED_MESSAGE)
        return summary
    business = rental_business()
    if business is None:
        summary.update(ok=False, error=missing_business_message())
        return summary
    tranzila = gateway()
    credential_error = tranzila.credential_error()
    if credential_error:
        summary.update(ok=False, error=f'טרנזילה אינה מוגדרת: {credential_error}')
        return summary

    today = today or today_local()
    summary['stale_to_review'] = sweep_stale_reservations()
    batch = max(1, min(int(limit or 40), 200))
    seen: list = []
    while summary['checked'] < batch:
        # One order per transaction: locked, its month reserved, committed.
        with transaction.atomic(durable=True):
            order = _next_due_order(today, seen)
            if order is None:
                break
            seen.append(order.pk)
            summary['checked'] += 1
            charge = _reserve_due_month(order, business, summary)
        if charge is None:
            continue

        result = _charge_saved_card(tranzila, order, charge)
        outcome = record_result(charge.pk, result, fail_order=True)
        if outcome == OUTCOME_CHARGED:
            summary['charged'] += 1
            if issue_receipt_safely(charge.pk):
                summary['receipts'] += 1
            else:
                summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} חויב, הקבלה לא הופקה')
        elif outcome == OUTCOME_FAILED:
            summary['failed'] += 1
            summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} נדחה — {_error_text(result)}')
        else:
            summary['review'] += 1
            summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} בבדיקה — {_error_text(result)}')
    return summary


# ------------------------------------------------------ the office's decisions

def retry_charge(charge, *, user=None) -> tuple[str, Charge]:
    """
    "Retry now" on a failed month: reserve-first, on the card the order holds,
    under the same month key. Refused while the switch is off.
    """
    tranzila = gateway()
    require_business()
    order = Order.objects.select_related('tenant').get(pk=charge.standing_order_id)
    if charge.status != Charge.STATUS_FAILED:
        raise BillingError('אפשר לנסות שוב רק חיוב שנדחה')
    if order.status == Order.STATUS_ENDED:
        raise BillingError('הוראת הקבע הסתיימה')
    if not order.has_card:
        raise BillingError('אין כרטיס שמור בהוראת הקבע. יש לשלוח לשוכר קישור להזנת כרטיס.')
    credential_error = tranzila.credential_error()
    if credential_error:
        raise BillingError(f'טרנזילה אינה מוגדרת: {credential_error}')

    claimed = reclaim_failed(charge.pk, trigger=Charge.TRIGGER_RETRY)
    if claimed is None:
        raise BillingError('החיוב כבר אינו במצב נדחה', status_code=409)
    logger.info('Rental charge %s: retried by %s', claimed.pk, getattr(user, 'pk', None))
    result = _charge_saved_card(tranzila, order, claimed)
    outcome = record_result(claimed.pk, result, fail_order=True)
    if outcome == OUTCOME_CHARGED:
        issue_receipt_safely(claimed.pk)
    return outcome, Charge.objects.get(pk=claimed.pk)


def _resolve(charge, user, note: str, now) -> None:
    charge.resolved_by = user if getattr(user, 'is_authenticated', False) else None
    charge.resolved_at = now
    charge.resolution_note = note


def mark_charged(charge, *, transaction_id: str, confirmation_code: str = '', note: str = '', user=None) -> Charge:
    """
    The office found the charge in Tranzila: the month in review is charged,
    with the transaction id they found. Then its receipt is issued.
    """
    transaction_id = str(transaction_id or '').strip()
    if not transaction_id:
        raise BillingError('יש להזין את מזהה העסקה שנמצא בטרנזילה')
    if len(transaction_id) > 100:
        raise BillingError('מזהה העסקה ארוך מדי')
    now = timezone.now()
    with transaction.atomic(durable=True):
        locked = Charge.objects.select_for_update().get(pk=charge.pk)
        if not is_undecided(locked, now):
            raise BillingError('אפשר לסמן כחויב רק חיוב שנמצא בבדיקה')
        order = Order.objects.select_for_update().get(pk=locked.standing_order_id)
        locked.status = Charge.STATUS_CHARGED
        locked.transaction_id = transaction_id
        locked.confirmation_code = str(confirmation_code or '').strip()[:50]
        # The money moved when the attempt was made, not when the office found it.
        locked.charged_at = locked.reserved_at
        _resolve(locked, user, str(note or '').strip()[:1000], now)
        locked.save()
        advance(order, locked.period)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
    issue_receipt_safely(locked.pk)
    return Charge.objects.get(pk=locked.pk)


def void_charge(charge, *, reason: str, user=None) -> Charge:
    """
    The month is not charged by this order: a charge in review that the office
    found never went through, or a failed one they settle another way. Final:
    the month is not charged again, and the schedule moves past it.
    """
    reason = str(reason or '').strip()
    if not reason:
        raise BillingError('יש להזין סיבת ביטול')
    if len(reason) > 1000:
        raise BillingError('סיבת הביטול ארוכה מדי')
    now = timezone.now()
    with transaction.atomic(durable=True):
        locked = Charge.objects.select_for_update().get(pk=charge.pk)
        if not (is_undecided(locked, now) or locked.status == Charge.STATUS_FAILED):
            raise BillingError('אפשר לבטל רק חיוב שנמצא בבדיקה או שנדחה')
        order = Order.objects.select_for_update().get(pk=locked.standing_order_id)
        locked.status = Charge.STATUS_VOIDED
        _resolve(locked, user, reason, now)
        locked.save()
        advance(order, locked.period)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
    return Charge.objects.get(pk=locked.pk)
