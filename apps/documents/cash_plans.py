"""
Cash paid up front for a run of months (finding E, owner default D1).

A parent pays the whole year in cash. For a service the VAT falls due when the
money is received (חוק מע"מ ס' 24, 29), so the moment the cash is taken ONE
חשבונית מס/קבלה (IRM) is issued for the whole sum, with a cash payment line
for all of it. The months are laid out as the schedule of lessons it covers;
no further fiscal document is issued for them. Every month is marked covered
by that IRM when the plan is registered (none is left pending for a monthly
run); the plan completes when its last month begins, and the child's status
reads it as before.

`CashPlan.mode` says which design a plan was registered under:

- 'upfront' — every plan registered from now on (D1).
- NULL — the original design, still running in production: a receipt (RC)
  for the whole sum at registration, and a document on the 1st of each month.
  Those plans are not re-issued or reversed: the receipt and the documents
  already issued stay as they are. Their remaining months still get a
  document each — the VAT on them has to be invoiced — but a tax invoice
  (TI), dated the day it is issued and settled against the plan's receipt,
  not a second חשבונית מס/קבלה: the cash is on the receipt already, and a
  monthly IRM counted it again.

Cancelling (cancel_cash_plan): the months not yet begun stop; for an 'upfront'
plan the IRM is credited for them (or for the amount refunded), and for an
older plan nothing was invoiced for them, so there is nothing to credit.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.payment_service import JERUSALEM_TZ
from apps.customers.child_status import refresh_child_status
from apps.customers.models import Child
from apps.documents.models import CashPlan, CashPlanMonth
from apps.documents.numbering import israel_today
from apps.documents.service import create_combined, create_invoice

logger = logging.getLogger(__name__)

MODE_UPFRONT = 'upfront'
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


class CashPlanError(ValueError):
    """A request about a cash plan the rules refuse; the message is the office's, in Hebrew (400)."""


def _period(rows: list[dict]) -> str:
    first, last = rows[0]['due_date'], rows[-1]['due_date']
    return f'{first:%m/%Y}' if first == last else f'{first:%m/%Y}–{last:%m/%Y}'


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
    Take the cash, issue ONE חשבונית מס/קבלה for all of it, and lay out the months (D1).

    The document is dated today and carries a cash payment line for the whole
    sum; the months are the schedule of lessons it covers. `monthly_document_type`
    is still accepted from an older screen and no longer used: no monthly
    document is issued.
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
    start = start_month or month_first(_today())
    rows = schedule(start, plan_months(total, monthly), monthly, total)
    period = _period(rows)

    document = create_combined({
        'document_type': 'combined',
        'client_type': 'existing',
        'child_id': str(child.id),
        'branch_id': str(branch.id) if branch else None,
        'invoice_details': {
            'document_date': str(israel_today()),
            'description': label,
            # Cash is a gross amount: VAT comes out of it, never on top.
            'prices_include_vat': True,
            'line_items': [{'description': f'{label} · {period}', 'quantity': 1, 'price': str(total)}],
            'payments': [{'method': 'cash', 'amount': str(total)}],
            'customer_notes': f'שולם במזומן מראש עבור {len(rows)} חודשים ({period})',
        },
    }, issued_by=actor)

    plan = CashPlan.objects.create(
        child=child,
        lesson=lesson,
        description=label,
        status='active',
        total_amount=total,
        monthly_amount=monthly,
        monthly_document_type='combined',
        mode=MODE_UPFRONT,
        receipt=document,
        branch=branch,
        created_by=actor if getattr(actor, 'is_authenticated', False) else None,
    )
    # Every month is covered by the one document from the start: none is left
    # pending for a monthly run to issue anything for — not this code's run,
    # and not the older code's either, were it ever put back.
    now = timezone.now()
    for row in rows:
        CashPlanMonth.objects.create(
            plan=plan, due_date=row['due_date'], amount=row['amount'],
            status='invoiced', document=document, invoiced_at=now,
        )

    # A plan whose last month has already begun is complete at once.
    issue_due_cash_documents(today=_today(), plan_id=plan.id)
    plan.refresh_from_db()
    # Cash never writes paid_until_date, so nothing else would tell the child's
    # status that the money is in: a registration left on בתהליך רישום.
    refresh_child_status(child, reason='נרשם תשלום במזומן', changed_by=actor)
    return plan


ISSUED = 'issued'
COVERED = 'covered'


@transaction.atomic
def _issue_month(month: CashPlanMonth, *, today: date) -> str | None:
    """
    One month whose 1st has come: ISSUED (an older plan's tax invoice),
    COVERED (an 'upfront' plan's month, covered by its IRM — no document), or
    None when another run has it or its plan is not active.
    """
    # Locked and re-checked: the hourly cron and beat can overlap, and a month
    # must never get two documents. Then the plan, as cancelling takes them.
    month = (
        CashPlanMonth.objects
        .select_for_update(of=('self',), skip_locked=True)
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch', 'plan__receipt')
        .filter(pk=month.pk, status='pending', due_date__lte=today)
        .first()
    )
    if month is None:
        return None
    if CashPlan.objects.select_for_update().filter(pk=month.plan_id, status='active').first() is None:
        return None
    plan = month.plan

    if plan.mode == MODE_UPFRONT:
        month.status = 'invoiced'
        month.document_id = plan.receipt_id
        month.invoiced_at = timezone.now()
        month.save(update_fields=['status', 'document', 'invoiced_at'])
        return COVERED

    # The original design (mode NULL): the cash is on the plan's receipt. The
    # month's VAT is invoiced by a tax invoice dated today and settled against
    # that receipt — never another חשבונית מס/קבלה, which counted the cash twice.
    course_name = ''
    if plan.lesson_id and plan.lesson and plan.lesson.course:
        course_name = plan.lesson.course.name
    line = f'{plan.description or "מנוי במזומן"} · {month.due_date:%m/%Y}'
    if course_name:
        line = f'{course_name} · {month.due_date:%m/%Y}'
    receipt = plan.receipt
    document = create_invoice({
        'document_type': 'tax_invoice',
        'client_type': 'existing',
        'child_id': str(plan.child_id),
        'branch_id': str(plan.branch_id) if plan.branch_id else None,
        'invoice_details': {
            'document_date': str(israel_today()),
            'description': line,
            # Cash is a gross amount: VAT comes out of it, never on top.
            'prices_include_vat': True,
            'line_items': [{'description': line, 'quantity': 1, 'price': str(month.amount)}],
            'customer_notes': (
                f'שולם במזומן מראש — קבלה {receipt.document_number}' if receipt is not None else 'שולם במזומן מראש'
            ),
        },
    }, 'tax_invoice')
    if receipt is not None and receipt.document_type == 'receipt':
        from apps.documents.settlement import SettlementError, record_settlements

        try:
            record_settlements(receipt, [{'invoice_id': document.pk, 'amount': document.total_amount}])
        except SettlementError as exc:
            logger.warning('Cash month %s: invoice %s issued but not settled against %s: %s',
                           month.pk, document.document_number, receipt.document_number, exc)

    month.status = 'invoiced'
    month.document = document
    month.invoiced_at = timezone.now()
    month.save(update_fields=['status', 'document', 'invoiced_at'])
    return ISSUED


def issue_due_cash_documents(*, today: date | None = None, plan_id=None, limit: int = 40) -> dict:
    """
    Every cash month whose 1st has come (never later than today in Israel):
    an older plan's month gets its tax invoice, an 'upfront' plan's month is
    marked covered. {'checked', 'issued', 'covered', 'errors'}.
    """
    today = min(today or _today(), israel_today())
    qs = (
        CashPlanMonth.objects
        .select_related('plan', 'plan__child', 'plan__lesson', 'plan__lesson__course', 'plan__branch')
        .filter(status='pending', due_date__lte=today, plan__status='active')
        .order_by('due_date', 'created_at')
    )
    if plan_id is not None:
        qs = qs.filter(plan_id=plan_id)
    rows = list(qs[: max(1, min(int(limit or 40), 200))])

    issued = covered = 0
    errors: list[str] = []
    for month in rows:
        try:
            outcome = _issue_month(month, today=today)
        except Exception as exc:
            logger.exception('Cash month %s: not processed', month.pk)
            errors.append(f'{month.id}: {exc}')
            continue
        if outcome == ISSUED:
            issued += 1
        elif outcome == COVERED:
            covered += 1

    affected = {month.plan_id for month in rows}
    if plan_id is not None:
        affected.add(plan_id)
    # An older plan completes when its last month has its document.
    for plan in CashPlan.objects.filter(id__in=affected, status='active', mode__isnull=True):
        if not plan.months.filter(status='pending').exists():
            plan.status = 'completed'
            plan.save(update_fields=['status', 'updated_at'])
    # An 'upfront' plan's months are all covered from the start: it completes
    # when its last month begins, and counts for that month until it ends
    # (child_status._plan_covers_this_month), as an older plan does.
    finished = (
        CashPlan.objects.filter(mode=MODE_UPFRONT, status='active')
        .annotate(last_month=Max('months__due_date')).filter(last_month__lte=today)
    )
    if plan_id is not None:
        finished = finished.filter(pk=plan_id)
    for plan in finished:
        plan.status = 'completed'
        plan.save(update_fields=['status', 'updated_at'])

    return {'checked': len(rows), 'issued': issued, 'covered': covered, 'errors': errors}


def unused_amount(plan: CashPlan, *, today: date | None = None) -> Decimal:
    """What the months not yet begun come to (their 1st is after today in Israel)."""
    today = today or israel_today()
    return sum((m.amount for m in plan.months.all() if m.due_date > today), Decimal('0')).quantize(Decimal('0.01'))


@transaction.atomic
def cancel_cash_plan(plan_id, *, user=None, reason: str = '', refund_amount=None) -> dict:
    """
    Stop a cash plan: the months not yet begun get nothing more.

    'upfront' (D1): the IRM is credited for what is given back — by default
    the months not yet begun; `refund_amount` when the office gave back
    another sum (0: nothing). The credit may not pass what is left of the IRM.
    An older plan (mode NULL): nothing was invoiced for the months not begun,
    so there is nothing to credit — the cash stays on its receipt, and the
    answer says so for the accountant. A cancelled plan is not cancelled again.

    Returns {'plan', 'credit_note', 'unused_amount', 'message', 'already_cancelled'}.
    """
    from apps.documents.check_plans import credit_plan_invoice
    from apps.documents.settlement import money, money_text

    # The months first, then the plan: the order the hourly run takes them.
    list(CashPlanMonth.objects.select_for_update().filter(plan_id=plan_id).order_by('pk'))
    plan = CashPlan.objects.select_for_update(of=('self',)).select_related('receipt').get(pk=plan_id)
    unused = unused_amount(plan)
    if plan.status == 'cancelled':
        return {'plan': plan, 'credit_note': None, 'unused_amount': unused, 'message': '',
                'already_cancelled': True}

    why = 'ביטול מנוי במזומן' + (f' — {reason.strip()}' if (reason or '').strip() else '')
    credit = None
    message = ''
    if plan.mode == MODE_UPFRONT:
        amount = unused if refund_amount in (None, '') else money(refund_amount)
        if amount < 0:
            raise CashPlanError('סכום ההחזר אינו יכול להיות שלילי')
        if amount > 0:
            if plan.receipt is None:
                raise CashPlanError('לתוכנית אין חשבונית מס/קבלה לזכות')
            credit = credit_plan_invoice(plan.receipt, amount, reason=why)
    else:
        if refund_amount not in (None, '') and money(refund_amount) != 0:
            raise CashPlanError(
                'בתוכנית שנרשמה לפני 30.9.2026 לא הופקה חשבונית לחודשים שלא התחילו, ואין מה לזכות. '
                f'מזומן שהוחזר נרשם מול הקבלה {plan.receipt.document_number if plan.receipt else ""} אצל רואה החשבון.'
            )
        if unused > 0:
            message = (
                f'לחודשים שלא התחילו ({money_text(unused)}) לא הופקה חשבונית, ולכן אין מה לזכות. '
                'אם הוחזר מזומן — יש לדווח לרואה החשבון מול הקבלה '
                f'{plan.receipt.document_number if plan.receipt else ""}.'
            )
    plan.status = 'cancelled'
    plan.cancelled_at = timezone.now()
    plan.cancelled_by = user if getattr(user, 'is_authenticated', False) else None
    plan.save(update_fields=['status', 'cancelled_at', 'cancelled_by', 'updated_at'])
    logger.info('Cash plan %s cancelled by %s; unused %s; credit %s', plan.pk, getattr(user, 'email', user),
                unused, credit.document_number if credit else '—')
    return {'plan': plan, 'credit_note': credit, 'unused_amount': unused, 'message': message,
            'already_cancelled': False}
