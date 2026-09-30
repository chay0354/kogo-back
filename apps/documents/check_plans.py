"""
Office check series — a receipt when the checks arrive, a tax invoice for each check (D2).

Owner default D2 (25.9.2026), finding D of the documents program:

- When the checks arrive: one receipt (RC) listing every check — number, bank,
  branch, account and due date (הוראה 5(ב)).
- On each check's due date: a tax invoice (TI) for that check, issued by the
  hourly run on or after the day, dated the day it is issued. It is never
  back-dated — dating it on the check's day put numbers out of date order in
  the TI run — so it goes through the run's date rules like any document, and
  says the check's due date in its note instead.
- The invoice is paid by that check, which is on the plan's receipt: it is
  settled against the receipt (settlement.py) in the same transaction, so it
  never prints "ממתין לתשלום" and the collections tab never chases it.
- A check that bounced: marked, its settlement voided, and its invoice (if one
  was issued) credited in full. A replacement check, when one is given, is a
  new small plan of its own — its own receipt, its own invoice on its day.
- Cancelling a plan: the checks still pending are cancelled and get no
  invoice; an invoice already issued that nothing paid is credited for what it
  still owes.

Locks: a check's row first, then its plan's (the hourly run, bouncing and
cancelling all take them in that order), so a plan cancelled while a check's
invoice is being issued sees that invoice and credits it if it is unpaid.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.payment_service import JERUSALEM_TZ
from apps.customers.child_status import refresh_child_status
from apps.customers.models import Child
from apps.documents.models import CheckItem, CheckPlan, DocumentSettlement, FormalDocument
from apps.documents.numbering import israel_today
from apps.documents.service import create_invoice, create_receipt

logger = logging.getLogger(__name__)


class CheckPlanError(ValueError):
    """A request about a plan the rules refuse; the message is the office's, in Hebrew (400)."""


class CheckAlreadyBounced(CheckPlanError):
    """The check is marked as bounced already (409)."""


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
            # הוראה 18ב(ד)(2): crossed "לא סחיר" in the customer's name, or not.
            'check_crossed': row.get('check_crossed') is True,
        })
    return checks


def _plan_place(child, lesson_id):
    """(lesson, branch) of a plan: the lesson's branch, else the family's."""
    lesson = None
    branch = None
    if lesson_id:
        from apps.courses.models import Lesson
        lesson = Lesson.objects.select_related('course', 'course__branch').filter(id=lesson_id).first()
        if lesson:
            branch = lesson.course.branch
    if branch is None:
        branch = getattr(getattr(child, 'family', None), 'branch', None)
    return lesson, branch


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

    lesson, branch = _plan_place(child, lesson_id)
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
    # Cheques never write paid_until_date, so nothing else would tell the
    # child's status that the money is in: two cheque-paying children sat on
    # בתהליך רישום in production.
    refresh_child_status(child, reason="נרשם תשלום בצ'קים")
    return plan


@transaction.atomic
def plan_for_receipt(receipt: FormalDocument, *, description: str = '') -> CheckPlan:
    """
    "חשבונית מס לכל צ'ק" on a receipt issued from the form: its checks become a
    plan, and each one's tax invoice is issued on its day, as a registered
    plan's are. The receipt is the plan's receipt, so each invoice is settled
    against it. A private customer's (a child's) receipt only — a plan belongs
    to a child.
    """
    if receipt.document_type != 'receipt' or not receipt.child_id:
        raise CheckPlanError("חשבונית לכל צ'ק מופקת על קבלה של לקוח פרטי (ילד) בלבד")
    rows = list(receipt.payments.filter(payment_method='check').order_by('check_date', 'id'))
    if not rows:
        raise CheckPlanError("חשבונית לכל צ'ק — רק לקבלה על צ'קים")
    undated = [row.reference or '—' for row in rows if not row.check_date]
    if undated:
        raise CheckPlanError(f"לכל צ'ק צריך תאריך פירעון — חסר בצ'ק {', '.join(undated)}")

    child = Child.objects.select_related('family').get(pk=receipt.child_id)
    plan = CheckPlan.objects.create(
        child=child,
        description=description.strip() or receipt.customer_notes.strip()[:300] or f"צ'קים — {child.full_name}",
        status='active',
        receipt=receipt,
        branch_id=receipt.branch_id,
    )
    for row in rows:
        CheckItem.objects.create(
            plan=plan,
            due_date=row.check_date,
            amount=row.amount,
            bank=row.check_bank,
            bank_branch=row.check_branch,
            account_number=row.check_account,
            check_number=row.reference,
        )
    issue_due_check_invoices(today=_today(), plan=plan)
    refresh_child_status(child, reason="נרשם תשלום בצ'קים")
    return plan


def _check_text(item: CheckItem) -> str:
    """'צ'ק מס' 1001 · בנק לאומי · סניף 123 · לפירעון 01/10/2026' — what is known of it."""
    parts = [
        f"צ'ק מס' {item.check_number}" if item.check_number else "צ'ק",
        f'בנק {item.bank}' if item.bank else '',
        f'סניף {item.bank_branch}' if item.bank_branch else '',
        f'לפירעון {item.due_date:%d/%m/%Y}',
    ]
    return ' · '.join(part for part in parts if part)


def _invoice_note(item: CheckItem) -> str:
    receipt = item.plan.receipt
    paid_by = f' — שולם בקבלה {receipt.document_number}' if receipt is not None else ''
    return f'{_check_text(item)}{paid_by}'


@transaction.atomic
def _issue_item_invoice(item: CheckItem, *, today: date) -> FormalDocument | None:
    """
    The tax invoice of one check whose day has come, or None when another run
    has it, it was invoiced, bounced or cancelled, or its plan is not active.

    Dated the day it is issued (the TI run's date rules apply: never before
    the run's latest date), naming the check's due date in its note, and
    settled against the plan's receipt — the check that paid it is on it.
    """
    # Locked and re-checked: the hourly cron and beat can overlap, and a check
    # must never get two invoices. Then the plan, in the order cancelling takes them.
    item = CheckItem.objects.select_for_update(of=('self',), skip_locked=True).select_related(
        'plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch', 'plan__receipt',
    ).filter(pk=item.pk, status='pending', bounced_at__isnull=True, due_date__lte=today).first()
    if item is None:
        return None
    if CheckPlan.objects.select_for_update().filter(pk=item.plan_id, status='active').first() is None:
        return None

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
            'document_date': str(israel_today()),
            'description': line,
            # A check is a gross amount: VAT is taken out of it, never added on top.
            'prices_include_vat': True,
            'line_items': [{
                'description': line,
                'quantity': 1,
                'price': str(item.amount),
            }],
            'customer_notes': _invoice_note(item),
        },
    }, 'tax_invoice')

    receipt = item.plan.receipt
    if receipt is not None and receipt.document_type == 'receipt':
        from apps.documents.settlement import SettlementError, record_settlements

        try:
            record_settlements(receipt, [{'invoice_id': invoice.pk, 'amount': invoice.total_amount}])
        except SettlementError as exc:
            # The invoice stands (the VAT is due); it stays open on the
            # collections tab, where the office sees it, instead of blocking
            # every later run.
            logger.warning('Check %s: invoice %s issued but not settled against %s: %s',
                           item.pk, invoice.document_number, receipt.document_number, exc)
    item.status = 'invoiced'
    item.tax_invoice = invoice
    item.invoiced_at = timezone.now()
    item.save(update_fields=['status', 'tax_invoice', 'invoiced_at'])
    return invoice


def issue_due_check_invoices(*, today: date | None = None, plan: CheckPlan | None = None, limit: int = 40) -> dict:
    """
    Issue a tax invoice for each pending check whose date has arrived.

    "Arrived" is never later than today in Israel, whatever day the caller
    passes: an invoice is dated the day it is issued, so it is never dated
    before its check.
    """
    today = min(today or _today(), israel_today())
    qs = (
        CheckItem.objects
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch')
        .filter(status='pending', bounced_at__isnull=True, due_date__lte=today, plan__status='active')
        .order_by('due_date', 'created_at')
    )
    if plan is not None:
        qs = qs.filter(plan=plan)
    rows = list(qs[: max(1, min(int(limit or 40), 200))])
    issued = 0
    errors = []
    for item in rows:
        try:
            if _issue_item_invoice(item, today=today) is not None:
                issued += 1
        except Exception as exc:
            logger.exception('Check %s: tax invoice not issued', item.pk)
            errors.append(f'{item.id}: {exc}')
    affected_ids = {item.plan_id for item in rows}
    if plan is not None:
        affected_ids.add(plan.id)
    _complete_finished(affected_ids)
    return {'checked': len(rows), 'issued': issued, 'errors': errors}


def _complete_finished(plan_ids) -> None:
    """A plan with no check left pending is completed."""
    for active_plan in CheckPlan.objects.filter(id__in=plan_ids, status='active'):
        if not active_plan.items.filter(status='pending').exists():
            active_plan.status = 'completed'
            active_plan.save(update_fields=['status', 'updated_at'])


def credit_plan_invoice(original: FormalDocument, gross, *, reason: str) -> FormalDocument:
    """
    A credit note for `gross` (VAT included) of a plan's invoice — a bounced
    check's, an unpaid one of a cancelled plan, an unused part of a cash plan.

    Checked under a lock on the original: not past what is left of it (its
    total less the credit notes already against it). Issued as a refund's is
    (service.issue_refund_credit_note: dated today, VAT split out of the gross
    so it totals the sum exactly, signed and mailed after the commit), and
    linked to the original.
    """
    from apps.documents.service import issue_refund_credit_note
    from apps.documents.settlement import _credits, money, money_text

    original = FormalDocument.objects.select_for_update(of=('self',)).select_related('child__family').get(pk=original.pk)
    gross = money(gross)
    left = money(original.total_amount) - _credits([original]).get(original.pk, Decimal('0'))
    if gross <= 0:
        raise CheckPlanError('סכום הזיכוי חייב להיות גדול מאפס')
    if gross > left:
        raise CheckPlanError(
            f'אפשר לזכות את {original.document_number} עד {money_text(max(left, Decimal("0")))} '
            f'(כבר זוכו {money_text(money(original.total_amount) - left)}).'
        )
    family = getattr(original.child, 'family', None) if original.child_id else None
    note = issue_refund_credit_note(
        gross_amount=gross,
        reason=reason,
        original_number=original.document_number,
        original_date=original.document_date,
        child=original.child,
        email=(family.email or '').strip() if family else '',
        branch_id=original.branch_id,
        business_id=original.business_id,
    )
    FormalDocument.objects.filter(pk=note.pk).update(linked_document=original, internal_notes=reason)
    note.linked_document = original
    note.internal_notes = reason
    return note


def _void_check_settlements(invoice_id, *, user, reason: str) -> None:
    from apps.documents.settlement import void_settlement

    for row_id in DocumentSettlement.objects.filter(invoice_id=invoice_id, voided_at__isnull=True).values_list('id', flat=True):
        void_settlement(row_id, user=user, reason=reason)


@transaction.atomic
def bounce_check(plan_id, item_id, *, user=None, reason: str = '', replacement: dict | None = None) -> dict:
    """
    A check came back unpaid.

    Marked bounced (never deleted). If its tax invoice was issued, the check
    paid nothing: the invoice's settlement is voided and the invoice credited
    for what it then owes. If not, it is cancelled — no invoice will be issued
    for it. A replacement check, when given ({date, amount, bank, branch,
    account_number, check_number, check_crossed}), is registered as a plan of
    its own — its own receipt now, its own invoice on its day — and linked
    (replaced_by).

    Returns {'item', 'credit_note', 'replacement_plan'}.
    """
    item = (
        CheckItem.objects.select_for_update(of=('self',))
        .select_related('plan', 'tax_invoice').filter(pk=item_id, plan_id=plan_id).first()
    )
    if item is None:
        raise CheckItem.DoesNotExist
    plan = CheckPlan.objects.select_for_update().select_related('child').get(pk=plan_id)
    if item.bounced_at is not None:
        raise CheckAlreadyBounced(f"הצ'ק כבר סומן כחוזר ({timezone.localtime(item.bounced_at):%d/%m/%Y})")
    if item.status == 'cancelled':
        raise CheckPlanError("הצ'ק בוטל ולא הופקד — הוא לא יכול לחזור")

    replacement_rows = None
    if replacement:
        replacement_rows = _normalize_checks([replacement])
        if not replacement_rows:
            raise CheckPlanError("יש למלא לצ'ק החלופי תאריך וסכום")

    why = f"{_check_text(item)} חזר" + (f' — {reason.strip()}' if (reason or '').strip() else '')
    item.bounced_at = timezone.now()
    fields = ['bounced_at']
    credit = None
    if item.status == 'invoiced' and item.tax_invoice_id:
        item.save(update_fields=fields)
        _void_check_settlements(item.tax_invoice_id, user=user, reason=why)
        from apps.documents.settlement import balance_of

        owed = balance_of(item.tax_invoice).open
        if owed > 0:
            credit = credit_plan_invoice(item.tax_invoice, owed, reason=why)
            item.credit_note = credit
            fields.append('credit_note')
    elif item.status == 'pending':
        item.status = 'cancelled'
        fields.append('status')

    replacement_plan = None
    if replacement_rows:
        replacement_plan = register_check_plan(
            child_id=str(plan.child_id),
            checks=replacement_rows,
            description=f"צ'ק חלופי ל{_check_text(item)}",
            lesson_id=str(plan.lesson_id) if plan.lesson_id else None,
        )
        item.replaced_by = replacement_plan.items.get()
        fields.append('replaced_by')
    item.save(update_fields=fields)
    _complete_finished([plan.pk])
    logger.info('Check %s of plan %s bounced by %s; credit %s; replacement plan %s',
                item.pk, plan.pk, getattr(user, 'email', user),
                credit.document_number if credit else '—', replacement_plan.pk if replacement_plan else '—')
    return {'item': item, 'credit_note': credit, 'replacement_plan': replacement_plan}


@transaction.atomic
def cancel_check_plan(plan_id, *, user=None, reason: str = '') -> dict:
    """
    Stop a plan: its pending checks are cancelled (no invoice will be issued
    for them), and an invoice already issued that nothing paid is credited for
    what it still owes — normally none, since each is settled by its check.
    Cancelling a cancelled plan does nothing again.

    Returns {'plan', 'credit_notes', 'already_cancelled'}.
    """
    from apps.documents.settlement import balance_of

    # The checks first, then the plan: the order the hourly run takes them.
    items = list(CheckItem.objects.select_for_update().filter(plan_id=plan_id).order_by('pk'))
    plan = CheckPlan.objects.select_for_update().get(pk=plan_id)
    if plan.status == 'cancelled':
        return {'plan': plan, 'credit_notes': [], 'already_cancelled': True}
    plan.status = 'cancelled'
    plan.cancelled_at = timezone.now()
    plan.cancelled_by = user if getattr(user, 'is_authenticated', False) else None
    plan.save(update_fields=['status', 'cancelled_at', 'cancelled_by', 'updated_at'])

    why = "ביטול תוכנית צ'קים" + (f' — {reason.strip()}' if (reason or '').strip() else '')
    credit_notes = []
    for item in items:
        if item.status == 'pending':
            item.status = 'cancelled'
            item.save(update_fields=['status'])
            continue
        if item.status != 'invoiced' or not item.tax_invoice_id or item.credit_note_id:
            continue
        invoice = FormalDocument.objects.get(pk=item.tax_invoice_id)
        owed = balance_of(invoice).open
        if owed > 0:
            item.credit_note = credit_plan_invoice(invoice, owed, reason=f'{why} — {_check_text(item)}')
            item.save(update_fields=['credit_note'])
            credit_notes.append(item.credit_note)
    logger.info('Check plan %s cancelled by %s; %d credit notes', plan.pk, getattr(user, 'email', user), len(credit_notes))
    return {'plan': plan, 'credit_notes': credit_notes, 'already_cancelled': False}
