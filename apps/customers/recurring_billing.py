"""Charge due recurring lesson subscriptions and email invoices."""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.payment_service import JERUSALEM_TZ, subscription_tranzila_items
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.recurring_amount import apply_due_pending_recurring_amounts

logger = logging.getLogger(__name__)


def _next_month_first(from_day: date) -> date:
    if from_day.month == 12:
        return date(from_day.year + 1, 1, 1)
    return date(from_day.year, from_day.month + 1, 1)


def _paid_until(end_of_charge_month: date) -> date:
    last_day = calendar.monthrange(end_of_charge_month.year, end_of_charge_month.month)[1]
    return date(end_of_charge_month.year, end_of_charge_month.month, last_day)


def process_due_recurring_charges(*, dry_run: bool = False, limit: int = 40) -> dict:
    """
    Charge active RecurringPayment rows whose next_billing_date is today or earlier.
    Creates Payment + Invoice and sends email (via PaymentService._create_invoice_from_payment).

    `limit` keeps each Vercel cron invocation under the function timeout; leftover
    rows stay due and the next hourly run continues.
    """
    from apps.core.payment_service import PaymentService

    apply_due_pending_recurring_amounts()
    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    service = PaymentService()
    tranzila = TranzilaService.production()
    batch = max(1, min(int(limit or 40), 200))

    due = (
        RecurringPayment.objects
        .filter(status='active', tranzila_recurring_index='')
        .exclude(tranzila_token='')
        .filter(next_billing_date__lte=today)
        .select_related(
            'child',
            'child__family',
            'initial_payment',
            'initial_payment__lesson',
            'initial_payment__lesson__course',
            'initial_payment__lesson__course__branch',
            'initial_payment__bundle',
        )
        .order_by('next_billing_date', 'created_at')[:batch]
    )
    due_rows = list(due)

    summary = {'checked': len(due_rows), 'charged': 0, 'failed': 0, 'skipped': 0, 'errors': []}

    for recurring in due_rows:
        if recurring.last_charge_date and recurring.last_charge_date >= today:
            summary['skipped'] += 1
            continue

        idempotency_key = f"recurring_{recurring.id}_{today.isoformat()}"
        if TranzilaTransaction.objects.filter(idempotency_key=idempotency_key, is_successful=True).exists():
            charge_month = recurring.next_billing_date or today
            recurring.last_charge_date = today
            recurring.next_billing_date = _next_month_first(charge_month)
            recurring.save(update_fields=['last_charge_date', 'next_billing_date', 'updated_at'])
            summary['skipped'] += 1
            continue

        initial = recurring.initial_payment
        lesson = initial.lesson if initial else None
        if not lesson:
            summary['skipped'] += 1
            summary['errors'].append(f'{recurring.id}: no lesson on initial payment')
            continue

        child = recurring.child
        family = child.family
        amount = Decimal(str(recurring.amount)).quantize(Decimal('0.01'))
        if amount < Decimal('1.00'):
            summary['skipped'] += 1
            continue

        label = f'{lesson.course.name} - {child.full_name}'
        if dry_run:
            summary['charged'] += 1
            continue

        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first(),
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=initial.bundle if initial else None,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=recurring.base_amount or amount,
            discount_amount=recurring.discount_amount or Decimal('0.00'),
            final_amount=amount,
            registration_fee=Decimal('0.00'),
            description=f'מנוי חודשי - {lesson.course.name} - {child.full_name}',
        )

        items = subscription_tranzila_items(
            label=label,
            prorated_lesson=amount,
            registration_fee=Decimal('0'),
        )
        result = tranzila.charge_with_token(
            token=recurring.tranzila_token,
            amount=amount,
            description=payment.description,
            transaction_id=str(payment.id),
            items=items,
            expire_month=recurring.card_expire_month,
            expire_year=recurring.card_expire_year,
            # Scoped to the billing month so a re-run of the cron cannot bill twice.
            duplicate_guard_key=f'recurring-{recurring.id}-{today:%Y-%m}',
        )

        if not result.get('success'):
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'charge failed')
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            recurring.status = 'failed'
            recurring.save(update_fields=['status', 'updated_at'])
            child.status = 'payment_problem'
            child.save(update_fields=['status', 'updated_at'])
            summary['failed'] += 1
            summary['errors'].append(f'{recurring.id}: {payment.failure_reason}')
            continue

        try:
            with transaction.atomic():
                payment.status = 'completed'
                payment.payment_date = timezone.now()
                payment.save(update_fields=['status', 'payment_date', 'updated_at'])

                tranzila_txn = TranzilaTransaction.objects.create(
                    transaction_id=result.get('transaction_id', ''),
                    confirmation_code=result.get('confirmation_code', ''),
                    transaction_type='recurring_charge',
                    response_code=result.get('response_code', '000'),
                    response_message='',
                    request_data={},
                    response_data=result.get('raw_response', {}),
                    idempotency_key=idempotency_key,
                    is_successful=True,
                    response_timestamp=timezone.now(),
                )
                payment.tranzila_transaction = tranzila_txn
                payment.save(update_fields=['tranzila_transaction'])

                service._create_invoice_from_payment(payment, tranzila_txn)

                charge_month = recurring.next_billing_date or today
                recurring.last_charge_date = today
                recurring.next_billing_date = _next_month_first(charge_month)
                recurring.save(update_fields=['last_charge_date', 'next_billing_date', 'updated_at'])

                child.status = 'active'
                child.paid_until_date = _paid_until(charge_month)
                child.save(update_fields=['status', 'paid_until_date', 'updated_at'])

            summary['charged'] += 1
        except Exception as exc:
            logger.exception('Recurring charge post-processing failed for %s', recurring.id)
            summary['failed'] += 1
            summary['errors'].append(f'{recurring.id}: {exc}')

    try:
        from apps.documents.check_plans import issue_due_check_invoices
        from apps.documents.models import CheckItem

        if dry_run:
            due_checks = CheckItem.objects.filter(
                status='pending',
                due_date__lte=today,
                plan__status='active',
            ).count()
            summary['check_invoices'] = {
                'checked': due_checks,
                'issued': 0,
                'errors': [],
                'dry_run': True,
            }
        else:
            summary['check_invoices'] = issue_due_check_invoices(today=today, limit=batch)
    except Exception as exc:
        logger.exception('Check invoice issuance failed')
        summary['check_invoices'] = {'checked': 0, 'issued': 0, 'errors': [str(exc)]}

    return summary
