from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.customers.models import RecurringChargeOverride, RecurringPayment


def effective_date_for_amount_change(recurring: RecurringPayment, *, today: date | None = None) -> date:
    """First billing cycle when a newly scheduled amount should take effect."""
    today = today or timezone.localdate()
    if recurring.next_billing_date and recurring.next_billing_date > today:
        return recurring.next_billing_date
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def schedule_recurring_amount(recurring: RecurringPayment, new_amount: Decimal) -> RecurringPayment:
    if recurring.status != 'active':
        raise ValueError('ניתן לעדכן סכום רק להוראת קבע פעילה')
    if new_amount <= 0:
        raise ValueError('הסכום חייב להיות גדול מ-0')

    effective_date = effective_date_for_amount_change(recurring)
    if Decimal(recurring.amount) == new_amount:
        recurring.pending_amount = None
        recurring.pending_amount_effective_date = None
    else:
        recurring.pending_amount = new_amount
        recurring.pending_amount_effective_date = effective_date

    recurring.save(update_fields=[
        'pending_amount',
        'pending_amount_effective_date',
        'updated_at',
    ])
    return recurring


def apply_due_pending_recurring_amounts(queryset=None) -> int:
    """Promote pending amounts whose effective date has arrived."""
    today = timezone.localdate()
    base = queryset if queryset is not None else RecurringPayment.objects.all()
    qs = base.filter(
        status='active',
        pending_amount__isnull=False,
        pending_amount_effective_date__lte=today,
    )
    updated = 0
    for recurring in qs:
        if recurring.pending_amount is None:
            continue
        recurring.amount = recurring.pending_amount
        if recurring.base_amount is not None:
            recurring.base_amount = recurring.pending_amount
        recurring.pending_amount = None
        recurring.pending_amount_effective_date = None
        recurring.save(update_fields=[
            'amount',
            'base_amount',
            'pending_amount',
            'pending_amount_effective_date',
            'updated_at',
        ])
        updated += 1
    return updated


def month_start(day: date) -> date:
    """The first of the month a date falls in — the key every override is filed under."""
    return day.replace(day=1)


def unspent_override(recurring: RecurringPayment, *, on_date: date) -> RecurringChargeOverride | None:
    """The override standing for that billing month, or None.

    Only rows that have not been spent are returned. An override whose charge went
    through carries `applied_at`, and a second charge in the same month — a cron
    re-run, a retry after a decline — must fall back to the regular amount rather
    than take the exceptional one twice.
    """
    return (
        RecurringChargeOverride.objects
        .filter(
            recurring_payment=recurring,
            billing_month=month_start(on_date),
            applied_at__isnull=True,
        )
        .first()
    )


def amount_for_charge(recurring: RecurringPayment, *, on_date: date) -> tuple[Decimal, RecurringChargeOverride | None]:
    """The sum to charge this month, and the override it came from if it did.

    Returns the standing amount untouched when no override is filed, so a month
    with nothing scheduled bills exactly as it did before overrides existed.
    """
    regular = Decimal(str(recurring.amount)).quantize(Decimal('0.01'))
    override = unspent_override(recurring, on_date=on_date)
    if override is None:
        return regular, None
    return Decimal(str(override.amount)).quantize(Decimal('0.01')), override


def set_month_override(
    recurring: RecurringPayment,
    *,
    billing_month: date,
    amount: Decimal,
    reason: str,
    source: str = 'manual',
    store_amount: Decimal | None = None,
    store_invoice=None,
    created_by=None,
) -> RecurringChargeOverride:
    """File (or replace) the amount for one month. The month must still be ahead.

    Replacing rather than adding keeps one row per month, which is what the unique
    constraint promises the cron: it never has to choose between two.
    """
    if recurring.status != 'active':
        raise ValueError('ניתן לקבוע סכום לחודש רק בהוראת קבע פעילה')
    # An order Tranzila holds the schedule for is skipped by our own cron
    # (recurring_billing filters on an empty index), so an override filed against
    # it would be shown in the CRM and never charged. Refusing beats promising a
    # change that silently does not happen.
    if (recurring.tranzila_recurring_index or '').strip():
        raise ValueError(
            'הוראת הקבע הזאת מנוהלת בטרנזילה ולא נגבית מהמערכת, ולכן לא ניתן לשנות בה חודש בודד'
        )
    if amount <= 0:
        raise ValueError('הסכום חייב להיות גדול מ-0')
    if not (reason or '').strip():
        raise ValueError('חובה לציין סיבה לשינוי')

    month = month_start(billing_month)
    if month < month_start(timezone.localdate()):
        raise ValueError('לא ניתן לשנות סכום לחודש שכבר עבר')

    spent = RecurringChargeOverride.objects.filter(
        recurring_payment=recurring,
        billing_month=month,
        applied_at__isnull=False,
    ).exists()
    if spent:
        raise ValueError('החודש הזה כבר חויב ולא ניתן לשנותו')

    total = Decimal(str(amount)).quantize(Decimal('0.01'))
    # A month may already carry purchases. Editing its total by hand does not say
    # how the new figure splits, so the till's part is kept and only capped by the
    # total — never letting the subscription line come out negative.
    existing = RecurringChargeOverride.objects.filter(
        recurring_payment=recurring, billing_month=month,
    ).first()
    carried_store = Decimal(str(existing.store_amount)) if existing else Decimal('0.00')
    if store_amount is None:
        store_part = min(carried_store, total)
    else:
        store_part = min(Decimal(str(store_amount)).quantize(Decimal('0.01')), total)

    override, _ = RecurringChargeOverride.objects.update_or_create(
        recurring_payment=recurring,
        billing_month=month,
        defaults={
            'amount': total,
            'original_amount': Decimal(str(recurring.amount)).quantize(Decimal('0.01')),
            'reason': reason.strip(),
            'source': source,
            'store_amount': store_part,
            'store_invoice': store_invoice,
            'created_by': created_by,
        },
    )
    return override


def clear_month_override(recurring: RecurringPayment, *, billing_month: date) -> bool:
    """Drop an unspent override so the month bills at the regular amount again."""
    deleted, _ = RecurringChargeOverride.objects.filter(
        recurring_payment=recurring,
        billing_month=month_start(billing_month),
        applied_at__isnull=True,
    ).delete()
    return bool(deleted)


def next_billable_month(recurring: RecurringPayment, *, today: date | None = None) -> date:
    """The month an amount added now would actually be taken in."""
    today = today or timezone.localdate()
    if recurring.next_billing_date and recurring.next_billing_date >= today:
        return month_start(recurring.next_billing_date)
    return month_start(effective_date_for_amount_change(recurring, today=today))


def add_to_month_override(
    recurring: RecurringPayment,
    *,
    extra: Decimal,
    reason: str,
    billing_month: date | None = None,
    source: str = 'store',
    store_invoice=None,
    created_by=None,
) -> RecurringChargeOverride:
    """Add a sum on top of what a month was already going to charge.

    The store needs adding rather than replacing: two shirts bought a week apart
    both belong on the same month, and the second must not erase the first. The
    base is whatever that month already stood at — an earlier override if one is
    filed, the regular amount otherwise.
    """
    month = month_start(billing_month) if billing_month else next_billable_month(recurring)
    existing = unspent_override(recurring, on_date=month)
    base = Decimal(str(existing.amount)) if existing else Decimal(str(recurring.amount))
    combined = (base + Decimal(str(extra))).quantize(Decimal('0.01'))

    note = reason.strip()
    if existing and existing.reason:
        note = f'{existing.reason}\n{note}'

    carried_store = Decimal(str(existing.store_amount)) if existing else Decimal('0.00')
    return set_month_override(
        recurring,
        billing_month=month,
        amount=combined,
        reason=note,
        source=source,
        store_amount=(carried_store + Decimal(str(extra))).quantize(Decimal('0.01')),
        store_invoice=store_invoice or (existing.store_invoice if existing else None),
        created_by=created_by or (existing.created_by if existing else None),
    )


def active_recurring_for_child(child) -> RecurringPayment | None:
    """The standing order a store purchase should ride on.

    A child on two courses has two; the one charged soonest is the one the buyer
    will see the addition on.
    """
    return (
        RecurringPayment.objects
        .filter(child=child, status='active', tranzila_recurring_index='')
        .exclude(tranzila_token='')
        .order_by('next_billing_date', 'created_at')
        .first()
    )
