"""Office check-series registration and monthly tax invoices."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.payment_service import JERUSALEM_TZ
from apps.customers.models import Child
from apps.documents.models import CheckItem, CheckPlan
from apps.documents.service import create_invoice, create_receipt


def _today() -> date:
    return timezone.now().astimezone(JERUSALEM_TZ).date()


def _normalize_checks(raw_checks: list) -> list[dict]:
    checks = []
    for row in raw_checks or []:
        try:
            amount = Decimal(str(row.get('amount') or 0))
        except Exception:
            amount = Decimal('0')
        due = row.get('date') or row.get('due_date')
        if amount <= 0 or not due:
            continue
        checks.append({
            'date': str(due)[:10],
            'bank': (row.get('bank') or '').strip(),
            'branch': (row.get('branch') or row.get('bank_branch') or '').strip(),
            'account_number': (row.get('account_number') or '').strip(),
            'check_number': (row.get('check_number') or '').strip(),
            'amount': amount,
            'confirmed': True,
        })
    return checks


@transaction.atomic
def register_check_plan(
    *,
    child_id: str,
    checks: list,
    description: str = '',
    lesson_id: str | None = None,
) -> CheckPlan:
    child = Child.objects.select_related('family', 'family__branch').get(id=child_id)
    normalized = _normalize_checks(checks)
    if not normalized:
        raise ValueError('יש למלא לפחות צ׳ק אחד עם תאריך וסכום')

    branch = None
    lesson = None
    if lesson_id:
        from apps.courses.models import Lesson
        lesson = Lesson.objects.select_related('course', 'course__branch').filter(id=lesson_id).first()
        if lesson:
            branch = lesson.course.branch
    if branch is None:
        branch = getattr(getattr(child, 'family', None), 'branch', None)

    label = description.strip() or (
        f"מנוי צ'קים — {lesson.course.name}" if lesson and lesson.course_id else f"מנוי צ'קים — {child.full_name}"
    )

    receipt = create_receipt({
        'document_type': 'receipt',
        'client_type': 'existing',
        'child_id': str(child.id),
        'branch_id': str(branch.id) if branch else None,
        'document_date': str(_today()),
        'receipt_details': {
            'payment_method': "צ'ק",
            'checks': normalized,
            'check_notes': label,
        },
    })

    plan = CheckPlan.objects.create(
        child=child,
        lesson=lesson,
        description=label,
        status='active',
        receipt=receipt,
        branch=branch,
    )
    for row in normalized:
        CheckItem.objects.create(
            plan=plan,
            due_date=date.fromisoformat(row['date']),
            amount=row['amount'],
            bank=row['bank'],
            bank_branch=row['branch'],
            account_number=row['account_number'],
            check_number=row['check_number'],
        )

    issue_due_check_invoices(today=_today(), plan=plan)
    return plan


@transaction.atomic
def _issue_item_invoice(item: CheckItem) -> None:
    # Locked and re-checked: the hourly cron and beat can overlap, and a check
    # must never get two invoices.
    item = CheckItem.objects.select_for_update(skip_locked=True).select_related(
        'plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch'
    ).filter(pk=item.pk, status='pending').first()
    if item is None:
        return
    child = item.plan.child
    month_label = item.due_date.strftime('%m/%Y')
    course_name = ''
    if item.plan.lesson_id and item.plan.lesson and item.plan.lesson.course:
        course_name = item.plan.lesson.course.name
    description = item.plan.description or "תשלום צ'ק"
    line = f"{description} · {month_label}"
    if course_name:
        line = f"{course_name} · {line}"

    invoice = create_invoice({
        'document_type': 'tax_invoice',
        'client_type': 'existing',
        'child_id': str(child.id),
        'branch_id': str(item.plan.branch_id) if item.plan.branch_id else None,
        'invoice_details': {
            'document_date': str(item.due_date),
            'description': line,
            'vat_exempt': True,
            'line_items': [{
                'description': line,
                'quantity': 1,
                'price': str(item.amount),
            }],
            'customer_notes': (
                f"צ'ק {item.check_number}" if item.check_number else "תשלום בצ'ק"
            ),
        },
    }, 'tax_invoice')
    item.status = 'invoiced'
    item.tax_invoice = invoice
    item.invoiced_at = timezone.now()
    item.save(update_fields=['status', 'tax_invoice', 'invoiced_at'])


def issue_due_check_invoices(*, today: date | None = None, plan: CheckPlan | None = None, limit: int = 40) -> dict:
    """Issue a tax invoice for each pending check whose date has arrived."""
    today = today or _today()
    qs = (
        CheckItem.objects
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch')
        .filter(status='pending', due_date__lte=today, plan__status='active')
        .order_by('due_date', 'created_at')
    )
    if plan is not None:
        qs = qs.filter(plan=plan)
    rows = list(qs[: max(1, min(int(limit or 40), 200))])
    issued = 0
    errors = []
    for item in rows:
        try:
            _issue_item_invoice(item)
            issued += 1
        except Exception as exc:
            errors.append(f'{item.id}: {exc}')
    affected_ids = {item.plan_id for item in rows}
    if plan is not None:
        affected_ids.add(plan.id)
    if affected_ids:
        for active_plan in CheckPlan.objects.filter(id__in=affected_ids, status='active'):
            if not active_plan.items.filter(status='pending').exists():
                active_plan.status = 'completed'
                active_plan.save(update_fields=['status', 'updated_at'])
    return {'checked': len(rows), 'issued': issued, 'errors': errors}
