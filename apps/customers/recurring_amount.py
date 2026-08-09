from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from apps.customers.models import RecurringPayment


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
