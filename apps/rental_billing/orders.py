"""Opening and changing a tenant's standing order — the office's side, and the signing page's.

Nothing here reaches Tranzila, so all of it works with RENTAL_BILLING_ENABLED
off. The card is never written here: it arrives only through the tenant's card
page (card.py).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from apps.rental_billing import billing
from apps.rental_billing.errors import BillingError
from apps.rental_billing.links import cancel_live_links
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.schedule import add_months, first_of_month
from apps.rentals.models import BILLING_DAY_MAX, BILLING_DAY_MIN, Tenancy

Order = TenantStandingOrder

# What the office may change on an order. Never the card, the status or the tenancy.
EDITABLE_FIELDS = ('amount_before_vat', 'billing_day', 'end_date', 'notes')
_UNSET = object()


def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise BillingError('סכום לא תקין')
    if amount <= 0:
        raise BillingError('הסכום החודשי חייב להיות גדול מ־0')
    if amount >= Decimal('100000000'):
        raise BillingError('סכום לא תקין')
    return amount


def _billing_day(value) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError):
        raise BillingError('יום חיוב לא תקין')
    if not BILLING_DAY_MIN <= day <= BILLING_DAY_MAX:
        raise BillingError(f'יום החיוב חייב להיות בין {BILLING_DAY_MIN} ל־{BILLING_DAY_MAX}')
    return day


def default_start(tenancy, today) -> 'date':
    """
    Where a new order on the tenancy starts: the first month the tenancy has
    no charged, reserved or in-review charge for — by any earlier order — and
    never a month before the current one. A tenancy whose agreement began
    months ago is not charged for them; one whose September was charged by a
    standing order that ended starts its next order in October.
    """
    taken = billing.tenancy_periods(
        tenancy.pk, statuses=(TenantCharge.STATUS_CHARGED, TenantCharge.STATUS_RESERVED, TenantCharge.STATUS_REVIEW),
    )
    month = max(first_of_month(today), first_of_month(tenancy.start_date))
    while month in taken:
        month = add_months(month, 1)
    return max(tenancy.start_date, month)


def open_standing_order(
    tenancy,
    *,
    user=None,
    source: str = Order.SOURCE_OFFICE,
    amount_before_vat=None,
    billing_day=None,
    start_date=None,
    end_date=_UNSET,
    notes: str = '',
    business_category=None,
    today=None,
) -> Order:
    """
    A standing order for a tenancy, waiting for the tenant's card.

    Amount, billing day and end date default to the tenancy's; the start to
    default_start(). The business is the rental one (RENTAL_BILLING_BUSINESS_NAME)
    when it exists; when it does not, the order is still opened and charging
    refuses until it does.
    """
    if tenancy.status in (Tenancy.STATUS_CANCELLED, Tenancy.STATUS_ENDED):
        raise BillingError('ההסכם בוטל או הסתיים')
    if source not in dict(Order.SOURCE_CHOICES):
        raise BillingError('מקור לא מוכר')
    amount = _amount(tenancy.monthly_amount if amount_before_vat is None else amount_before_vat)
    day = _billing_day(tenancy.billing_day if billing_day is None else billing_day)
    if start_date:
        start = start_date
    elif tenancy.start_date:
        start = default_start(tenancy, today or billing.today_local())
    else:
        raise BillingError('יש להזין תאריך תחילה להסכם או להוראת הקבע')
    end = tenancy.end_date if end_date is _UNSET else end_date
    if end and end < start:
        raise BillingError('אין חודש פתוח לחיוב בתקופת ההסכם' if not start_date else 'תאריך הסיום לא יכול להיות לפני תאריך ההתחלה')
    business = billing.rental_business()
    if business_category is not None and (business is None or business_category.business_id != business.id):
        raise BillingError('הקטגוריה אינה שייכת לעסק של השכירויות')
    try:
        with transaction.atomic():
            return Order.objects.create(
                tenancy=tenancy,
                tenant_id=tenancy.tenant_id,
                branch_id=tenancy.branch_id,
                business=business,
                business_category=business_category,
                amount_before_vat=amount,
                billing_day=day,
                start_date=start,
                end_date=end,
                status=Order.STATUS_PENDING_CARD,
                source=source,
                created_by=user if getattr(user, 'is_authenticated', False) else None,
                notes=(notes or '').strip(),
            )
    except IntegrityError:
        raise BillingError('להסכם הזה כבר יש הוראת קבע פתוחה')


def update_standing_order(order, changes: dict) -> Order:
    """Change the amount, the billing day, the end date or the notes. Anything else is refused."""
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise BillingError(f'אי אפשר לשנות את השדות: {", ".join(unknown)}')
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status == Order.STATUS_ENDED and set(changes) - {'notes'}:
            raise BillingError('הוראת קבע שהסתיימה אי אפשר לשנות')
        fields = set()
        if 'amount_before_vat' in changes:
            locked.amount_before_vat = _amount(changes['amount_before_vat'])
            fields.add('amount_before_vat')
        if 'billing_day' in changes:
            locked.billing_day = _billing_day(changes['billing_day'])
            fields.add('billing_day')
            if locked.next_charge_date:
                # The same billing month, on its new day.
                nxt = locked.next_charge_date
                locked.next_charge_date = nxt.replace(day=locked.billing_day)
                fields.add('next_charge_date')
        if 'end_date' in changes:
            end = changes['end_date']
            if end and end < locked.start_date:
                raise BillingError('תאריך הסיום לא יכול להיות לפני תאריך ההתחלה')
            locked.end_date = end
            fields.add('end_date')
            if (
                end and locked.next_charge_date and locked.next_charge_date > end
                and locked.status in (Order.STATUS_ACTIVE, Order.STATUS_PAUSED, Order.STATUS_FAILED)
            ):
                locked.status = Order.STATUS_ENDED
                fields.add('status')
        if 'notes' in changes:
            locked.notes = str(changes['notes'] or '').strip()
            fields.add('notes')
        if fields:
            locked.save(update_fields=sorted(fields | {'updated_at'}))
        if locked.status == Order.STATUS_ENDED:
            cancel_live_links(locked)
    return locked


def pause_order(order) -> Order:
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.STATUS_ACTIVE:
            raise BillingError('אפשר להשהות רק הוראת קבע פעילה')
        locked.status = Order.STATUS_PAUSED
        locked.save(update_fields=['status', 'updated_at'])
    return locked


def resume_order(order, *, today=None) -> Order:
    """
    Back to active. The months that passed while it was paused are not
    charged: the next charge is the billing day of the first open month from
    the current one (or the date it had, if that is later).
    """
    today = today or billing.today_local()
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.STATUS_PAUSED:
            raise BillingError('אפשר לחדש רק הוראת קבע מושהית')
        computed = billing.next_open_billing_date(locked, today)
        stored = locked.next_charge_date
        locked.next_charge_date = stored if stored and stored > computed else computed
        locked.status = Order.STATUS_ACTIVE
        if locked.end_date and locked.next_charge_date > locked.end_date:
            locked.status = Order.STATUS_ENDED
        locked.save(update_fields=['status', 'next_charge_date', 'updated_at'])
        if locked.status == Order.STATUS_ENDED:
            cancel_live_links(locked)
    return locked


def end_order(order) -> Order:
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status == Order.STATUS_ENDED:
            raise BillingError('הוראת הקבע כבר הסתיימה')
        locked.status = Order.STATUS_ENDED
        locked.save(update_fields=['status', 'updated_at'])
        cancel_live_links(locked)
    return locked
