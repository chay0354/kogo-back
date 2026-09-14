"""
Cash paid up front, recognised month by month.

A parent pays the whole year in cash. Two facts follow, and they have different
dates: the money arrived once, and the income belongs to the months it covers.
So a receipt for the whole sum is issued at registration, and a document for the
regular monthly price is issued on the 1st of each month the payment covers.

This mirrors `check_plans` deliberately — it is the same shape the office
already knows — with one difference: a check carries its own date and amount,
while cash is one sum split into equal months.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from apps.core.payment_service import JERUSALEM_TZ
from apps.customers.models import Child
from apps.documents.models import CashPlan, CashPlanMonth
from apps.documents.service import create_invoice, create_receipt

MAX_MONTHS = 24


def _today() -> date:
    return timezone.now().astimezone(JERUSALEM_TZ).date()


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def month_first(day: date) -> date:
    return day.replace(day=1)


def next_month_first(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def schedule(start: date, months: int, monthly_amount: Decimal, total: Decimal) -> list[dict]:
    """
    The 1st of each covered month, and what each one carries.

    The months are the regular price; the last one takes whatever is left, so
    the documents add up to exactly the cash that was taken and never to a
    rounder number than the money.
    """
    rows: list[dict] = []
    cursor = month_first(start)
    remaining = _money(total)
    for index in range(months):
        last = index == months - 1
        amount = remaining if last else min(_money(monthly_amount), remaining)
        if amount <= 0:
            break
        rows.append({'due_date': cursor, 'amount': _money(amount)})
        remaining = _money(remaining - amount)
        cursor = next_month_first(cursor)
    return rows


def plan_months(total: Decimal, monthly_amount: Decimal) -> int:
    """How many months a sum covers at the regular price, rounding up."""
    total = _money(total)
    monthly = _money(monthly_amount)
    if monthly <= 0:
        raise ValueError('הסכום החודשי חייב להיות גדול מאפס')
    whole = int((total / monthly).to_integral_value(rounding=ROUND_HALF_UP))
    # Round up only when there is a real remainder, so 240×3 stays three months.
    if _money(monthly * whole) < total:
        whole += 1
    return max(1, min(whole, MAX_MONTHS))


def preview(*, total_amount, monthly_amount, start_month=None) -> dict:
    """What the screen shows before anything is issued. Reads nothing, writes nothing."""
    total = _money(total_amount)
    monthly = _money(monthly_amount)
    if total <= 0:
        raise ValueError('יש להזין את הסכום ששולם')
    months = plan_months(total, monthly)
    start = start_month or month_first(_today())
    rows = schedule(start, months, monthly, total)
    return {
        'total_amount': str(total),
        'monthly_amount': str(monthly),
        'months': len(rows),
        'schedule': [
            {'due_date': r['due_date'].isoformat(), 'label': f"{r['due_date']:%m/%Y}", 'amount': str(r['amount'])}
            for r in rows
        ],
    }


@transaction.atomic
def register_cash_plan(
    *,
    child_id: str,
    total_amount,
    monthly_amount,
    lesson_id: str | None = None,
    start_month: date | None = None,
    description: str = '',
    monthly_document_type: str = 'combined',
    actor=None,
) -> CashPlan:
    """
    Take the cash, issue the receipt for all of it, and lay out the months.

    The receipt is the first thing written: it is the document for money that
    has already changed hands, and a registration that fails after it still
    leaves the payer holding a valid receipt rather than nothing.
    """
    child = Child.objects.select_related('family', 'family__branch').get(id=child_id)
    total = _money(total_amount)
    monthly = _money(monthly_amount)
    if total <= 0:
        raise ValueError('יש להזין את הסכום ששולם במזומן')
    if monthly <= 0:
        raise ValueError('יש להזין את הסכום החודשי של החוג')
    if monthly_document_type not in dict(CashPlan.MONTHLY_DOCUMENT_CHOICES):
        raise ValueError('סוג מסמך חודשי לא מוכר')

    lesson = None
    branch = None
    if lesson_id:
        from apps.courses.models import Lesson
        lesson = Lesson.objects.select_related('course', 'course__branch').filter(id=lesson_id).first()
        if lesson:
            branch = lesson.course.branch
    if branch is None:
        branch = getattr(getattr(child, 'family', None), 'branch', None)

    label = description.strip() or (
        f'מנוי במזומן — {lesson.course.name}' if lesson and lesson.course_id
        else f'מנוי במזומן — {child.full_name}'
    )

    receipt = create_receipt({
        'document_type': 'receipt',
        'client_type': 'existing',
        'child_id': str(child.id),
        'branch_id': str(branch.id) if branch else None,
        'document_date': str(_today()),
        'receipt_details': {
            'payment_method': 'מזומן',
            'cash_amount': str(total),
            'cash_notes': label,
        },
    })

    plan = CashPlan.objects.create(
        child=child,
        lesson=lesson,
        description=label,
        status='active',
        total_amount=total,
        monthly_amount=monthly,
        monthly_document_type=monthly_document_type,
        receipt=receipt,
        branch=branch,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )

    start = start_month or month_first(_today())
    for row in schedule(start, plan_months(total, monthly), monthly, total):
        CashPlanMonth.objects.create(plan=plan, due_date=row['due_date'], amount=row['amount'])

    # Issued here and not on commit: the months that have already passed are
    # part of registering, so the response says what was actually produced and
    # a receipt can never be handed over while its first document is still
    # pending somewhere behind it.
    issue_due_cash_documents(today=_today(), plan_id=plan.id)
    plan.refresh_from_db()
    return plan


def _issue_month(month: CashPlanMonth) -> None:
    # Locked and re-checked: the hourly cron and beat can overlap, and a month
    # must never get two documents.
    month = (
        CashPlanMonth.objects
        .select_for_update(of=('self',), skip_locked=True)
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch')
        .filter(pk=month.pk, status='pending')
        .first()
    )
    if month is None:
        return

    plan = month.plan
    course_name = ''
    if plan.lesson_id and plan.lesson and plan.lesson.course:
        course_name = plan.lesson.course.name
    line = f'{plan.description or "מנוי במזומן"} · {month.due_date:%m/%Y}'
    if course_name:
        line = f'{course_name} · {month.due_date:%m/%Y}'

    document = create_invoice({
        'document_type': plan.monthly_document_type,
        'client_type': 'existing',
        'child_id': str(plan.child_id),
        'branch_id': str(plan.branch_id) if plan.branch_id else None,
        'invoice_details': {
            'document_date': str(month.due_date),
            'description': line,
            # Cash is a gross amount: VAT comes out of it, never on top.
            'prices_include_vat': True,
            'line_items': [{
                'description': line,
                'quantity': 1,
                'price': str(month.amount),
            }],
            'payment_methods': (
                [{'payment_method': 'מזומן', 'amount': str(month.amount)}]
                if plan.monthly_document_type == 'combined' else []
            ),
            'customer_notes': 'שולם במזומן מראש',
        },
    }, plan.monthly_document_type)

    month.status = 'invoiced'
    month.document = document
    month.invoiced_at = timezone.now()
    month.save(update_fields=['status', 'document', 'invoiced_at'])


def issue_due_cash_documents(*, today: date | None = None, plan_id=None, limit: int = 40) -> dict:
    """Issue the monthly document for every cash month whose 1st has arrived."""
    today = today or _today()
    qs = (
        CashPlanMonth.objects
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch')
        .filter(status='pending', due_date__lte=today, plan__status='active')
        .order_by('due_date', 'created_at')
    )
    if plan_id is not None:
        qs = qs.filter(plan_id=plan_id)
    rows = list(qs[: max(1, min(int(limit or 40), 200))])

    issued = 0
    errors: list[str] = []
    for month in rows:
        try:
            with transaction.atomic():
                _issue_month(month)
            issued += 1
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f'{month.id}: {exc}')

    affected = {month.plan_id for month in rows}
    if plan_id is not None:
        affected.add(plan_id)
    for plan in CashPlan.objects.filter(id__in=affected, status='active'):
        if not plan.months.filter(status='pending').exists():
            plan.status = 'completed'
            plan.save(update_fields=['status', 'updated_at'])

    return {'checked': len(rows), 'issued': issued, 'errors': errors}
