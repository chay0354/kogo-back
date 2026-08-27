"""One checkout receipt for every child/lesson charged on the same card request."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from apps.customers.financial_models import Invoice, InvoiceActivityLog, InvoiceChild
from apps.customers.models import Payment

logger = logging.getLogger(__name__)


def _lesson_slot_label(lesson) -> str:
    if lesson is None:
        return ''
    day = lesson.get_day_of_week_display() if hasattr(lesson, 'get_day_of_week_display') else ''
    start = lesson.start_time.strftime('%H:%M') if getattr(lesson, 'start_time', None) else ''
    return ' '.join(part for part in (day, start) if part)


def _lessons_for_payment(payment: Payment) -> list:
    lesson = payment.lesson
    members = list(payment.bundle.lessons.all()) if payment.bundle_id else []
    if lesson is not None and lesson not in members:
        members = [lesson] + members
    if not members and lesson is not None:
        members = [lesson]
    return members


def payment_checkout_line(payment: Payment) -> dict:
    child = payment.child
    lesson = payment.lesson
    lessons = _lessons_for_payment(payment)
    course_name = ''
    if lesson and lesson.course_id:
        course_name = lesson.course.name
    elif lessons and lessons[0].course_id:
        course_name = lessons[0].course.name
    slots = ', '.join(_lesson_slot_label(item) for item in lessons)
    description = f'{course_name} — {slots}' if slots else (course_name or 'מנוי חוג')
    fee = payment.registration_fee or Decimal('0.00')
    return {
        'child_name': child.full_name if child else '',
        'description': description,
        'registration_fee': str(fee),
        'amount': str(payment.final_amount or Decimal('0.00')),
    }


def issue_widget_checkout_invoice(payments, *, send_email: bool = True) -> Invoice | None:
    """
    Create a single receipt covering every paid row in this checkout.

    Extra ₪0 bundle-day rows are skipped. Several children or several lessons
    still produce one Invoice and one email.
    """
    paid: list[Payment] = []
    seen: set[str] = set()
    for payment in payments:
        if payment is None or payment.status != 'completed':
            continue
        if (payment.final_amount or Decimal('0')) <= 0:
            continue
        key = str(payment.id)
        if key in seen:
            continue
        seen.add(key)
        paid.append(payment)
    if not paid:
        return None

    paid = list(
        Payment.objects.filter(id__in=[p.id for p in paid])
        .select_related(
            'child', 'family', 'parent', 'branch',
            'lesson__course', 'bundle', 'tranzila_transaction',
        )
        .prefetch_related('bundle__lessons')
    )
    paid.sort(key=lambda row: row.created_at or timezone.now())

    first = paid[0]
    family = first.family
    total = sum((p.final_amount or Decimal('0')) for p in paid)
    txn = ''
    for row in paid:
        gateway = getattr(row, 'tranzila_transaction', None)
        if gateway and (gateway.transaction_id or '').strip():
            txn = gateway.transaction_id.strip()
            break

    stamp = timezone.now()
    invoice_number = f"INV-{stamp.strftime('%Y%m%d')}-FAM-{first.id.hex[:8].upper()}"
    invoice = Invoice.objects.create(
        invoice_number=invoice_number,
        family=family,
        parent=first.parent,
        branch=first.branch,
        payment=first,
        amount=total,
        status='paid',
        payment_method='credit_card',
        payment_type='recurring',
        payer_name=(family.name if family else '') or '',
        payer_email=(family.email if family and family.email else '') or '',
        payer_phone=(family.phone if family else '') or '',
        tranzila_transaction_id=txn,
        invoice_date=stamp,
    )

    lines = []
    for payment in paid:
        child = payment.child
        lesson = payment.lesson
        if child_id := (child.id if child else None):
            InvoiceChild.objects.create(
                invoice=invoice,
                child=child,
                course=lesson.course if lesson and lesson.course_id else None,
                lesson=lesson,
            )
        lines.append(payment_checkout_line(payment))

    InvoiceActivityLog.objects.create(
        invoice=invoice,
        action='checkout_lines',
        details={
            'lines': lines,
            'payment_ids': [str(p.id) for p in paid],
        },
    )

    if send_email:
        try:
            from apps.customers.subscription_invoice_email import send_subscription_invoice_email
            send_subscription_invoice_email(invoice)
        except Exception:
            logger.exception('Checkout invoice email failed for %s (non-fatal)', invoice.invoice_number)

    logger.info(
        'Issued checkout invoice %s for %s payments total=%s',
        invoice.invoice_number,
        len(paid),
        total,
    )
    return invoice
