"""A month the tenant paid at the office — in cash, by check or by bank transfer.

    parse_offline_payment(body, today=)   the office's form, checked: (OfflinePayment, amount, note)
    record_offline_payment(charge, ...)   the month is paid, and its RT receipt says how — one commit
    offline_payment_of(charge)            the receipt's cash / check / transfer line, or None
    late_card_charge(charge)              Tranzila confirmed a card charge on a month settled otherwise

Until this, a tenant who paid a month in cash or by check had nowhere to go:
the office voided the month ("settled another way") and issued a receipt by
hand from the documents page. Nothing tied that document to the month, the
tenant's page showed "בוטל" with no receipt, and the period report counted the
month's calendar income again as income with no document behind it.

Now the month itself is paid. It turns 'charged' — the status every reader
already takes for "this month is paid and must never be charged again": the
monthly run moves past it, a new order on the tenancy starts after it, the card
page never charges it — and its receipt is the RT receipt a card charge gets,
numbered, signed and delivered the same way, with a payment line that names
the means. That line is how a month paid at the office is told from one paid
by card (offline_payment_of); no column was added for it.

Which months may be paid at the office: a failed one (the card said no —
nothing moved), and a voided one (the office settled it and nothing was
charged). Never one whose card outcome is unknown (reserved, review): the card
may have been charged, and money taken twice is the one thing this must not
do. Never one Tranzila charged, before or after a decision on it.

Idempotent: a second call on a month already paid at the office returns it
as it is. The charge and the order are locked, and the status, the order's
schedule and the receipt are written in one transaction: either the month is
paid and has its receipt, or nothing happened.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.rental_billing.billing import advance, charged_after_void, shekels
from apps.rental_billing.errors import BillingError
from apps.rental_billing.links import cancel_live_links
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.receipts import (
    METHOD_CHECK,
    OFFLINE_METHOD_LABELS,
    OFFLINE_METHODS,
    OfflinePayment,
    issue_receipt_in_transaction,
    lock_charge,
)

Charge = TenantCharge
Order = TenantStandingOrder

# The months the office may take a payment for. Failed: the card said no, in so
# many words. Voided: the office decided the month is not charged.
PAYABLE_STATUSES = (Charge.STATUS_FAILED, Charge.STATUS_VOIDED)

UNDECIDED_MESSAGE = (
    'התוצאה של החיוב בכרטיס על החודש הזה עוד לא ידועה — ייתכן שהכרטיס חויב. '
    'בדקו בטרנזילה והכריעו (סימון כחויב או ביטול) לפני שרושמים תשלום אחר.'
)
CHARGED_MESSAGE = 'החודש הזה כבר חויב בכרטיס. אין לרשום עליו תשלום נוסף.'
LATE_CHARGE_MESSAGE = (
    'טרנזילה אישרה חיוב בכרטיס על החודש הזה אחרי שבוטל (עסקה {transaction}). '
    'בדקו בטרנזילה לפני שרושמים תשלום אחר — אחרת השוכר ישלם פעמיים.'
)


# ------------------------------------------------------------------ reading

def offline_payment_of(charge):
    """
    The payment line of a month paid at the office — cash, a check or a
    transfer on its receipt — or None (no receipt, or a card receipt). Reads
    receipt.payments through .all(), so a prefetched list is used as is.
    """
    if not charge.receipt_id:
        return None
    for payment in charge.receipt.payments.all():
        if payment.payment_method in OFFLINE_METHODS:
            return payment
    return None


def late_card_charge(charge) -> bool:
    """
    Tranzila answered yes on this month after it had been settled otherwise —
    voided, or paid at the office (billing.record_result keeps the transaction
    id it answered with). The card was charged: with no receipt, or on top of
    the money the office took. Nothing is refunded or charged about it by
    itself; the office is shown it and decides.
    """
    if charged_after_void(charge):
        return True
    if not (charge.transaction_id or '').strip():
        return False
    return charge.status == Charge.STATUS_CHARGED and offline_payment_of(charge) is not None


# ------------------------------------------------------------------ the form

def _text(body: dict, key: str, limit: int, label: str, errors: list) -> str:
    value = str(body.get(key) or '').strip()
    if len(value) > limit:
        errors.append(f'{label} ארוך מדי')
        return ''
    return value


def _date(raw, label: str, errors: list) -> date | None:
    if raw in (None, ''):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        errors.append(f'{label} לא תקין')
        return None


def _amount(raw) -> Decimal | None:
    if raw in (None, '') or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value.quantize(Decimal('0.01'))


def parse_offline_payment(body: dict, *, today: date) -> tuple[OfflinePayment, Decimal, str]:
    """
    {method, amount, paid_on?, reference?, note?, check?: {number, bank, branch,
    account, date, crossed}} → (payment, amount, note). Every problem at once,
    in one Hebrew message (BillingError, 400).

    A check needs its number, bank, branch, account and due date — הוראה 5(ב)
    has a receipt for a check name them. `crossed` is true only when sent as
    true: an unmarked check sends the signed original on paper (18ב(ד)(2)).
    """
    body = body if isinstance(body, dict) else {}
    errors: list[str] = []
    method = str(body.get('method') or '').strip()
    if method not in OFFLINE_METHODS:
        errors.append("יש לבחור אמצעי תשלום: מזומן, צ'ק או העברה בנקאית")
    amount = _amount(body.get('amount'))
    if amount is None:
        errors.append('יש להזין את הסכום ששולם')
    paid_on = _date(body.get('paid_on'), 'תאריך התשלום', errors) or today
    if paid_on > today:
        errors.append('תאריך התשלום לא יכול להיות בעתיד')
    reference = _text(body, 'reference', 200, 'האסמכתא', errors)
    note = _text(body, 'note', 1000, 'ההערה', errors)

    check = body.get('check') if isinstance(body.get('check'), dict) else {}
    check_fields = {}
    if method == METHOD_CHECK:
        for key, limit, label in (
            ('number', 50, "מספר הצ'ק"), ('bank', 100, 'הבנק'), ('branch', 50, 'הסניף'), ('account', 50, 'מספר החשבון'),
        ):
            value = str(check.get(key) or '').strip()
            if not value:
                errors.append(f'יש להזין את {label}')
            elif len(value) > limit:
                errors.append(f'{label} ארוך מדי')
            check_fields[key] = value[:limit]
        if check.get('date') in (None, ''):
            errors.append("יש להזין את תאריך הפירעון של הצ'ק")
        check_fields['date'] = _date(check.get('date'), "תאריך הפירעון של הצ'ק", errors)
        check_fields['crossed'] = check.get('crossed') is True

    if errors:
        raise BillingError('\n'.join(errors))
    payment = OfflinePayment(
        method=method,
        paid_on=paid_on,
        reference=reference,
        check_number=check_fields.get('number', ''),
        check_bank=check_fields.get('bank', ''),
        check_branch=check_fields.get('branch', ''),
        check_account=check_fields.get('account', ''),
        check_date=check_fields.get('date'),
        check_crossed=check_fields.get('crossed', False),
    )
    return payment, amount, note


# ------------------------------------------------------------------ the write

def _refuse(charge) -> None:
    """Why this month cannot be paid at the office, if it cannot."""
    if charge.status in (Charge.STATUS_RESERVED, Charge.STATUS_REVIEW):
        raise BillingError(UNDECIDED_MESSAGE, status_code=409)
    if charge.status == Charge.STATUS_CHARGED:
        raise BillingError(CHARGED_MESSAGE, status_code=409)
    if (charge.transaction_id or '').strip():
        # Only a late yes from Tranzila leaves a transaction on a month that is not charged.
        raise BillingError(LATE_CHARGE_MESSAGE.format(transaction=charge.transaction_id), status_code=409)
    if charge.receipt_id:
        raise BillingError('לחודש הזה כבר יש קבלה', status_code=409)
    if charge.status not in PAYABLE_STATUSES:
        raise BillingError('אפשר לרשום תשלום במשרד רק על חודש שנדחה או שבוטל', status_code=409)


def _paid_moment(paid_on: date, now):
    """When the money came, as the charge keeps it: now for today, midday of an earlier day otherwise."""
    if paid_on == timezone.localdate(now):
        return now
    return timezone.make_aware(datetime.combine(paid_on, time(12, 0)))


def resolution_text(payment: OfflinePayment, note: str, previous_void: str = '') -> str:
    parts = [f'שולם במשרד ב{OFFLINE_METHOD_LABELS[payment.method]}']
    if payment.method == METHOD_CHECK and payment.check_number:
        parts.append(f"צ'ק {payment.check_number}")
    if payment.reference:
        parts.append(f'אסמכתא {payment.reference}')
    if note:
        parts.append(note)
    if previous_void:
        parts.append(f'(החודש בוטל קודם לכן: {previous_void})')
    return ' · '.join(parts)[:1000]


def record_offline_payment(charge, *, payment: OfflinePayment, amount: Decimal, note: str = '',
                           user=None) -> tuple[Charge, bool]:
    """
    The month is paid at the office: charged, with an RT receipt whose payment
    line is `payment`. (fresh charge, True) — or (the charge, False) when it was
    already paid at the office, whatever this call says. BillingError when the
    month cannot be paid this way or `amount` is not the month's total.
    """
    now = timezone.now()
    with transaction.atomic(durable=True):
        locked = lock_charge(charge.pk)
        if locked.status == Charge.STATUS_CHARGED and offline_payment_of(locked) is not None:
            created = False
        else:
            _refuse(locked)
            total = shekels(locked.total)
            if amount != total:
                raise BillingError(
                    f'הסכום ששולם (₪{amount}) שונה מסכום החודש (₪{total}). '
                    'קבלה על החודש מופקת על הסכום המלא שלו בלבד.'
                )
            order = Order.objects.select_for_update().get(pk=locked.standing_order_id)
            previous_void = locked.resolution_note if locked.status == Charge.STATUS_VOIDED else ''
            locked.status = Charge.STATUS_CHARGED
            locked.charged_at = _paid_moment(payment.paid_on, now)
            locked.receipt_error = ''
            locked.resolved_by = user if getattr(user, 'is_authenticated', False) else None
            locked.resolved_at = now
            locked.resolution_note = resolution_text(payment, note, previous_void)
            locked.save()
            # A month paid is a month the order is past, as it is when a card pays it.
            # The order's status is left alone: a card that was declined is still declined.
            advance(order, locked.period)
            order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
            if order.status == Order.STATUS_ENDED:
                cancel_live_links(order)
            issue_receipt_in_transaction(locked, payment=payment, user=user)
            created = True
    return Charge.objects.get(pk=locked.pk), created
