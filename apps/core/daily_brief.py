"""
The morning brief: what the office would otherwise find out the hard way.

Every check here answers one question the owner has had to ask after the fact —
a parent whose monthly charge never went out, a child left on the wrong status
after paying, a charge with no receipt, a screen that refuses to work because
something was never configured. They run against the CRM's own records, and the
takings are checked against Tranzila itself so "the system says it was charged"
and "the money arrived" are two separate statements.

Rules the checks follow:
  * read-only — nothing here charges, sends, fixes or writes a row;
  * one failing check never hides the rest: it comes back as its own red item;
  * a quiet morning must read as quiet, so a check that finds nothing says so
    instead of filling the screen.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

RED = 'red'
YELLOW = 'yellow'
GREEN = 'green'

# Rows shown per item; `count` always carries the true total.
MAX_ROWS = 25

# A charge is only late once the billing cron has had the day to run.
OVERDUE_GRACE_DAYS = 1


@dataclass
class BriefItem:
    key: str
    title: str
    severity: str
    count: int = 0
    summary: str = ''
    action: str = ''
    rows: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _row(label: str, detail: str = '', href: str = '') -> dict:
    return {'label': label, 'detail': detail, 'href': href}


def _child_href(child_id) -> str:
    return f'/customers?child={child_id}'


def _money(value) -> str:
    try:
        return f'{Decimal(value):,.0f} ₪'
    except Exception:  # noqa: BLE001 — a label must never break the brief
        return f'{value} ₪'


def _israel_today() -> date:
    return timezone.localtime(timezone.now()).date()


# --- the checks ------------------------------------------------------------


def check_overdue_recurring(today: date) -> BriefItem:
    """Standing orders whose day came and went without a charge."""
    from apps.customers.models import RecurringPayment

    cutoff = today - timedelta(days=OVERDUE_GRACE_DAYS)
    rows = (
        RecurringPayment.objects
        .filter(status='active', tranzila_recurring_index='', next_billing_date__lt=cutoff)
        .exclude(tranzila_token='')
        .select_related('child')
        .order_by('next_billing_date')
    )
    total = rows.count()
    item = BriefItem(
        key='overdue_recurring',
        title='הוראות קבע שלא ירדו',
        severity=RED if total else GREEN,
        count=total,
        action='לבדוק בכרטיס הלקוח למה החיוב לא יצא, ולחייב ידנית אם צריך.',
    )
    if not total:
        item.summary = 'כל החיובים החודשיים יצאו בזמן.'
        return item
    item.summary = f'{total} הוראות קבע שתאריך החיוב שלהן עבר ועדיין לא חויבו.'
    for recurring in rows[:MAX_ROWS]:
        days_late = (today - recurring.next_billing_date).days if recurring.next_billing_date else 0
        item.rows.append(_row(
            recurring.child.full_name if recurring.child else str(recurring.id),
            f'{_money(recurring.amount)} · היה אמור לרדת ב-{recurring.next_billing_date:%d/%m} · באיחור {days_late} ימים',
            _child_href(recurring.child_id),
        ))
    return item


def check_unresolved_charges(today: date) -> BriefItem:
    """
    Standing orders whose monthly billing has stopped and is waiting for a person.

    The same definition the billing cron uses: one unsuccessful call to the
    gateway under a standing order's key and it charges nothing more for that
    child, because the card may already carry that month. It says so in its own
    log and nowhere else, which is how a parent stops being billed quietly.
    """
    from apps.customers.models import RecurringPayment, TranzilaTransaction

    blocked = (
        TranzilaTransaction.objects
        .filter(is_successful=False, idempotency_key__startswith='recurring_')
        .order_by('request_timestamp')
    )
    by_recurring: dict[str, TranzilaTransaction] = {}
    for row in blocked:
        parts = row.idempotency_key.split('_')
        if len(parts) < 3:
            continue
        by_recurring.setdefault(parts[1], row)

    if by_recurring:
        # Only the ones still expecting to be charged matter this morning.
        live = set(
            RecurringPayment.objects
            .filter(id__in=list(by_recurring), status='active')
            .values_list('id', flat=True)
        )
        by_recurring = {key: row for key, row in by_recurring.items() if str(key) in {str(x) for x in live}}

    item = BriefItem(
        key='unresolved_charges',
        title='הוראות קבע שהחיוב שלהן נעצר',
        severity=RED if by_recurring else GREEN,
        count=len(by_recurring),
        action='לבדוק בטרנזילה אם הכסף ירד באותו חודש. עד שזה מוסדר, המערכת לא תחייב את הלקוח הזה שוב.',
    )
    if not by_recurring:
        item.summary = 'אין הוראת קבע שתקועה בין המערכת לסליקה.'
        return item
    item.summary = (
        f'{len(by_recurring)} הוראות קבע פעילות שהחיוב עליהן נעצר אחרי ניסיון שלא הצליח. '
        'ייתכן שהכסף בכל זאת ירד, ולכן המערכת לא מנסה שוב לבד.'
    )
    children = {
        str(r.id): r
        for r in RecurringPayment.objects.filter(id__in=list(by_recurring)).select_related('child')
    }
    for recurring_id, row in list(by_recurring.items())[:MAX_ROWS]:
        recurring = children.get(str(recurring_id))
        child = recurring.child if recurring else None
        item.rows.append(_row(
            child.full_name if child else row.idempotency_key,
            f'ניסיון אחרון {timezone.localtime(row.request_timestamp):%d/%m %H:%M}'
            + (f' · {row.response_message[:60]}' if row.response_message else ''),
            _child_href(child.id) if child else '',
        ))
    return item


def check_failed_payments(today: date) -> BriefItem:
    """Charges the gateway refused in the last week."""
    from apps.customers.models import Payment

    since = timezone.now() - timedelta(days=7)
    rows = (
        Payment.objects
        .filter(status='failed', created_at__gte=since)
        .select_related('child')
        .order_by('-created_at')
    )
    total = rows.count()
    item = BriefItem(
        key='failed_payments',
        title='תשלומים שנכשלו בשבוע האחרון',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לשלוח להורה קישור לעדכון כרטיס, או לחייב שוב.',
    )
    if not total:
        item.summary = 'לא נכשל אף תשלום בשבוע האחרון.'
        return item
    item.summary = f'{total} תשלומים נכשלו בשבעת הימים האחרונים.'
    for payment in rows[:MAX_ROWS]:
        item.rows.append(_row(
            payment.child.full_name if payment.child else 'ללא ילד משויך',
            f'{_money(payment.final_amount)} · {timezone.localtime(payment.created_at):%d/%m}',
            _child_href(payment.child_id) if payment.child_id else '',
        ))
    return item


def check_expiring_cards(today: date) -> BriefItem:
    """Cards behind an active standing order that expired, or expire this month."""
    from apps.customers.models import RecurringPayment

    rows = (
        RecurringPayment.objects
        .filter(status='active', card_expire_year__isnull=False, card_expire_month__isnull=False)
        .select_related('child')
    )
    expiring = []
    for recurring in rows:
        year, month = recurring.card_expire_year, recurring.card_expire_month
        if year < today.year or (year == today.year and month <= today.month):
            expiring.append(recurring)
    item = BriefItem(
        key='expiring_cards',
        title='כרטיסים שפג תוקפם',
        severity=YELLOW if expiring else GREEN,
        count=len(expiring),
        action='לשלוח קישור לעדכון כרטיס לפני החיוב הבא.',
    )
    if not expiring:
        item.summary = 'לכל הוראות הקבע הפעילות יש כרטיס בתוקף.'
        return item
    item.summary = f'{len(expiring)} הוראות קבע פעילות עם כרטיס שפג או שפג החודש.'
    for recurring in expiring[:MAX_ROWS]:
        item.rows.append(_row(
            recurring.child.full_name if recurring.child else str(recurring.id),
            f'תוקף {recurring.card_expire_month:02d}/{recurring.card_expire_year}',
            _child_href(recurring.child_id),
        ))
    return item


def check_status_mismatch(today: date) -> BriefItem:
    """
    Children whose status does not match what the records support.

    The case the office keeps meeting: a child who did a trial, joined a
    standing order, and stayed 'ניסיון' on every screen.
    """
    from apps.customers.child_status import canonical_status, resolve_child_status, status_label
    from apps.customers.models import Child

    children = (
        Child.objects
        .exclude(status='ghost')
        .select_related('family')
        .prefetch_related('lesson_enrollments', 'payments')
    )
    mismatched = []
    for child in children.iterator(chunk_size=500):
        should_be = resolve_child_status(child)
        if should_be and canonical_status(child.status) != should_be:
            mismatched.append((child, should_be))
    item = BriefItem(
        key='status_mismatch',
        title='ילדים בסטטוס לא נכון',
        severity=YELLOW if mismatched else GREEN,
        count=len(mismatched),
        action='לפתוח את כרטיס הילד ולתקן את הסטטוס, או לבדוק למה הרישום לא עודכן.',
    )
    if not mismatched:
        item.summary = 'הסטטוס של כל הילדים תואם את הרישומים.'
        return item
    item.summary = f'{len(mismatched)} ילדים שהסטטוס שלהם לא תואם את מה שרשום עליהם.'
    for child, should_be in mismatched[:MAX_ROWS]:
        item.rows.append(_row(
            child.full_name,
            f'רשום {status_label(child.status)} · אמור להיות {status_label(should_be)}',
            _child_href(child.id),
        ))
    return item


def check_missing_receipts(today: date) -> BriefItem:
    """Money that came in without its receipt."""
    from apps.documents.missing_receipts import payments_without_invoice

    rows = payments_without_invoice(since=timezone.now() - timedelta(days=90))
    item = BriefItem(
        key='missing_receipts',
        title='תשלומים ללא חשבונית',
        severity=YELLOW if rows else GREEN,
        count=len(rows),
        action='להפיק את המסמכים החסרים במסך הקבלות החסרות.',
    )
    if not rows:
        item.summary = 'לכל תשלום ב-90 הימים האחרונים יש מסמך.'
        return item
    item.summary = f'{len(rows)} תשלומים ב-90 הימים האחרונים בלי חשבונית או קבלה.'
    for payment in rows[:MAX_ROWS]:
        item.rows.append(_row(
            payment.child.full_name if getattr(payment, 'child', None) else 'ללא ילד משויך',
            f'{_money(payment.final_amount)} · {timezone.localtime(payment.created_at):%d/%m}',
            '/invoices',
        ))
    return item


def check_recurring_without_lesson(today: date) -> BriefItem:
    """
    Active standing orders the billing cron cannot charge.

    Without a lesson on the first payment the cron skips the row every hour and
    says so only in its own log, so the parent quietly stops being billed.
    """
    from apps.customers.models import RecurringPayment

    rows = (
        RecurringPayment.objects
        .filter(status='active', tranzila_recurring_index='')
        .filter(Q(initial_payment__isnull=True) | Q(initial_payment__lesson__isnull=True))
        .select_related('child')
    )
    total = rows.count()
    item = BriefItem(
        key='recurring_without_lesson',
        title='הוראות קבע שאי אפשר לחייב',
        severity=RED if total else GREEN,
        count=total,
        action='לשייך שיעור להוראת הקבע, אחרת היא לא תיגבה אף פעם.',
    )
    if not total:
        item.summary = 'לכל הוראת קבע פעילה יש שיעור לחייב לפיו.'
        return item
    item.summary = f'{total} הוראות קבע פעילות בלי שיעור משויך — החיוב החודשי מדלג עליהן בשקט.'
    for recurring in rows[:MAX_ROWS]:
        item.rows.append(_row(
            recurring.child.full_name if recurring.child else str(recurring.id),
            f'{_money(recurring.amount)} · ללא שיעור',
            _child_href(recurring.child_id),
        ))
    return item


def check_registration_only_payments(today: date) -> BriefItem:
    """
    A sign-up that collected the registration fee and not the course.

    The owner's case: 260 for the course plus 120 registration, and only the
    120 was taken. The charge looks successful everywhere, and the month is
    simply missing.
    """
    from apps.customers.models import Payment

    since = timezone.now() - timedelta(days=45)
    rows = (
        Payment.objects
        .filter(status='completed', created_at__gte=since, registration_fee__gt=0, lesson__isnull=False)
        .filter(final_amount__lte=models.F('registration_fee'))
        .select_related('child')
        .order_by('-created_at')
    )
    total = rows.count()
    item = BriefItem(
        key='registration_only_payments',
        title='נגבו דמי רישום בלבד',
        severity=RED if total else GREEN,
        count=total,
        action='לבדוק מול ההורה ולגבות את החוג, או לתקן את ההרשמה.',
    )
    if not total:
        item.summary = 'כל הרשמה ב-45 הימים האחרונים נגבתה במלואה.'
        return item
    item.summary = f'{total} הרשמות שבהן נגבו דמי הרישום אבל לא התשלום על החוג.'
    for payment in rows[:MAX_ROWS]:
        item.rows.append(_row(
            payment.child.full_name if payment.child else 'ללא ילד משויך',
            f'שולם {_money(payment.final_amount)} · דמי רישום {_money(payment.registration_fee)} · '
            f'{timezone.localtime(payment.created_at):%d/%m}',
            _child_href(payment.child_id) if payment.child_id else '',
        ))
    return item


def check_duplicate_charges(today: date) -> BriefItem:
    """The same child charged the same amount twice on one day."""
    from django.db.models import Count

    from apps.customers.models import Payment

    since = timezone.now() - timedelta(days=14)
    groups = (
        Payment.objects
        .filter(status='completed', created_at__gte=since, child__isnull=False)
        .values('child_id', 'final_amount', 'created_at__date')
        .annotate(times=Count('id'))
        .filter(times__gt=1)
        .order_by('-created_at__date')
    )
    rows = list(groups)
    item = BriefItem(
        key='duplicate_charges',
        title='חיובים כפולים',
        severity=RED if rows else GREEN,
        count=len(rows),
        action='לבדוק אם ההורה חויב פעמיים, ולהחזיר לו את ההפרש.',
    )
    if not rows:
        item.summary = 'לא נמצא חיוב כפול בשבועיים האחרונים.'
        return item
    item.summary = f'{len(rows)} מקרים של אותו ילד שחויב באותו סכום פעמיים באותו יום.'
    from apps.customers.models import Child

    names = {
        str(child.id): child.full_name
        for child in Child.objects.filter(id__in=[r['child_id'] for r in rows[:MAX_ROWS]])
    }
    for row in rows[:MAX_ROWS]:
        item.rows.append(_row(
            names.get(str(row['child_id']), 'ילד'),
            f"{_money(row['final_amount'])} · {row['times']} פעמים · {row['created_at__date']:%d/%m}",
            _child_href(row['child_id']),
        ))
    return item


def check_revenue_drop(today: date) -> BriefItem:
    """
    Yesterday's takings against the same weekday over the last month.

    A billing day that quietly did not run shows up here first: the day is not
    empty by accident, it is empty because nothing charged.
    """
    from django.db.models import Sum

    from apps.customers.models import Payment

    yesterday = today - timedelta(days=1)

    def takings(day: date) -> Decimal:
        total = (
            Payment.objects
            .filter(status='completed', created_at__date=day)
            .aggregate(total=Sum('final_amount'))['total']
        )
        return Decimal(total or 0)

    actual = takings(yesterday)
    history = [takings(yesterday - timedelta(days=7 * week)) for week in range(1, 5)]
    known = [value for value in history if value > 0]
    item = BriefItem(
        key='revenue_drop',
        title=f'הכנסות {yesterday:%d/%m}',
        severity=GREEN,
        action='לוודא שהחיוב החודשי רץ, ושאין תקלה בסליקה.',
    )
    if not known:
        item.summary = f'נכנסו {_money(actual)}. אין מספיק היסטוריה להשוואה.'
        return item
    typical = sum(known) / len(known)
    item.summary = f'נכנסו {_money(actual)}, מול {_money(typical)} בממוצע באותו יום בשבוע.'
    if actual == 0 and typical > 0:
        item.severity = RED
        item.count = 1
        item.rows.append(_row('לא נכנס כסף כלל', f'ממוצע רגיל {_money(typical)}', '/credit-charge'))
    elif typical > 0 and actual < typical / 2:
        item.severity = YELLOW
        item.count = 1
        item.rows.append(_row('פחות ממחצית מהרגיל', f'{_money(actual)} מול {_money(typical)}', '/credit-charge'))
    return item


def check_refunds(today: date) -> BriefItem:
    """Every refund of the last week, in one place, so none passes unseen."""
    from django.db.models import Sum

    from apps.customers.models import Payment

    since = timezone.now() - timedelta(days=7)
    rows = (
        Payment.objects
        .filter(status='refunded', updated_at__gte=since)
        .select_related('child')
        .order_by('-updated_at')
    )
    total = rows.count()
    amount = rows.aggregate(total=Sum('final_amount'))['total'] or 0
    item = BriefItem(
        key='refunds',
        title='זיכויים בשבוע האחרון',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לוודא שלכל זיכוי יש חשבונית זיכוי, ושהכסף אכן הוחזר בטרנזילה.',
    )
    if not total:
        item.summary = 'לא בוצעו זיכויים בשבוע האחרון.'
        return item
    item.summary = f'{total} זיכויים בסך {_money(amount)}.'
    for payment in rows[:MAX_ROWS]:
        item.rows.append(_row(
            payment.child.full_name if payment.child else 'ללא ילד משויך',
            f'{_money(payment.final_amount)} · {timezone.localtime(payment.updated_at):%d/%m}',
            _child_href(payment.child_id) if payment.child_id else '',
        ))
    return item


def check_active_without_standing_order(today: date) -> BriefItem:
    """A child on the books as active with nothing set up to charge."""
    from apps.customers.models import Child, RecurringPayment

    paying = set(
        RecurringPayment.objects.filter(status='active').values_list('child_id', flat=True)
    )
    children = (
        Child.objects
        .filter(status='active')
        .exclude(id__in=paying)
        .select_related('family')
    )
    total = children.count()
    item = BriefItem(
        key='active_without_standing_order',
        title='ילדים פעילים בלי הוראת קבע',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לבדוק אם הם משלמים בדרך אחרת (מזומן, צ׳קים, העברה) או שפשוט לא נגבה מהם.',
    )
    if not total:
        item.summary = 'לכל ילד פעיל יש הוראת קבע.'
        return item
    item.summary = f'{total} ילדים בסטטוס פעיל שאין להם הוראת קבע פעילה.'
    for child in children[:MAX_ROWS]:
        item.rows.append(_row(child.full_name, 'פעיל · אין הוראת קבע', _child_href(child.id)))
    return item


def check_ended_standing_orders(today: date) -> BriefItem:
    """Standing orders whose end date passed and are still charging."""
    from apps.customers.models import RecurringPayment

    rows = (
        RecurringPayment.objects
        .filter(status='active', end_date__lt=today)
        .select_related('child')
        .order_by('end_date')
    )
    total = rows.count()
    item = BriefItem(
        key='ended_standing_orders',
        title='הוראות קבע שתאריך הסיום שלהן עבר',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לסגור אותן, אחרת ההורה ימשיך להיות מחויב אחרי שסיים.',
    )
    if not total:
        item.summary = 'אין הוראת קבע פעילה שתאריך הסיום שלה עבר.'
        return item
    item.summary = f'{total} הוראות קבע פעילות שתאריך הסיום שלהן כבר עבר.'
    for recurring in rows[:MAX_ROWS]:
        item.rows.append(_row(
            recurring.child.full_name if recurring.child else str(recurring.id),
            f'{_money(recurring.amount)} · הסתיימה ב-{recurring.end_date:%d/%m/%Y}',
            _child_href(recurring.child_id),
        ))
    return item


def check_overdue_instalments(today: date) -> BriefItem:
    """Cash and cheque instalments whose date passed with no document issued."""
    from apps.documents.models import CashPlanMonth, CheckItem

    cash = CashPlanMonth.objects.filter(status='pending', due_date__lt=today).select_related('plan')
    checks = CheckItem.objects.filter(status='pending', due_date__lt=today).select_related('plan')
    total = cash.count() + checks.count()
    item = BriefItem(
        key='overdue_instalments',
        title='מזומן וצ׳קים שעבר מועדם',
        severity=YELLOW if total else GREEN,
        count=total,
        action='להפיק את המסמך ולוודא שהכסף התקבל.',
    )
    if not total:
        item.summary = 'אין תשלום במזומן או בצ׳ק שעבר מועדו בלי מסמך.'
        return item
    item.summary = f'{total} תשלומים במזומן או בצ׳קים שהמועד שלהם עבר ולא הופק עליהם מסמך.'
    for month in cash[:MAX_ROWS]:
        item.rows.append(_row('מזומן', f'{_money(month.amount)} · לתאריך {month.due_date:%d/%m/%Y}', '/invoices'))
    for check in checks[:max(0, MAX_ROWS - cash.count())]:
        item.rows.append(_row(
            f"צ׳ק {check.check_number}".strip(),
            f'{_money(check.amount)} · לתאריך {check.due_date:%d/%m/%Y}',
            '/invoices',
        ))
    return item


def check_business_categories(today: date) -> BriefItem:
    """A business with no active category blocks the invoice screen."""
    from apps.core.models import Business

    businesses = Business.objects.filter(is_active=True).prefetch_related('categories')
    empty = [b for b in businesses if not any(c.is_active for c in b.categories.all())]
    item = BriefItem(
        key='business_categories',
        title='עסקים בלי קטגוריה',
        severity=RED if empty else GREEN,
        count=len(empty),
        action='להוסיף קטגוריה בהגדרות ← כספים. בלי קטגוריה פעילה אי אפשר להפיק חשבונית לעסק הזה.',
    )
    if not empty:
        item.summary = 'לכל עסק פעיל יש קטגוריה להפיק לפיה.'
        return item
    item.summary = f'{len(empty)} עסקים פעילים בלי אף קטגוריה פעילה — הפקת חשבונית עבורם תיתקע.'
    for business in empty[:MAX_ROWS]:
        item.rows.append(_row(business.name, 'אין קטגוריה פעילה', '/settings/finance'))
    return item


def check_document_numbering(today: date) -> BriefItem:
    """
    That the next document number can actually be produced.

    Numbering breaks quietly — a series with no opening, a counter that never
    got set — and the office only learns when a receipt refuses to be issued
    with money already taken.
    """
    from apps.documents.missing_receipts import next_receipt_number

    item = BriefItem(
        key='document_numbering',
        title='מספור מסמכים',
        severity=GREEN,
        action='לבדוק את סדרות המספור בהגדרות ← מספור מסמכים.',
    )
    try:
        number = next_receipt_number()
    except Exception as exc:  # noqa: BLE001 — this is exactly the failure being looked for
        item.severity = RED
        item.count = 1
        item.summary = 'לא ניתן להפיק את המספר הבא של קבלה — הפקת מסמכים תיכשל.'
        item.rows.append(_row('מספור', str(exc), '/settings/numbering'))
        return item
    item.summary = f'הקבלה הבאה תקבל מספר {number}.'
    return item


def check_tranzila_health(today: date) -> BriefItem:
    """The gateway's own readiness — the reason a charge screen suddenly errors."""
    from apps.core.tranzila_service import TranzilaService

    report = TranzilaService.production().live_readiness()
    checks = report.get('checks') if isinstance(report, dict) else []
    failed = [c for c in (checks or []) if not c.get('ok')]
    blocking = [c for c in failed if c.get('blocking')]
    item = BriefItem(
        key='tranzila_health',
        title='תקינות הסליקה',
        severity=RED if blocking else (YELLOW if failed else GREEN),
        count=len(failed),
        action='לתקן בהגדרות ← סליקה, או במסוף של טרנזילה.',
    )
    if not failed:
        item.summary = 'החיבור לטרנזילה תקין.'
        return item
    item.summary = f'{len(failed)} בדיקות סליקה נכשלו. חיוב בכרטיס עלול להיכשל.'
    for check in failed[:MAX_ROWS]:
        item.rows.append(_row(str(check.get('name')), str(check.get('detail') or ''), '/settings/billing'))
    return item


def check_manychat_health(today: date) -> BriefItem:
    """WhatsApp sending, before the office finds out mid-broadcast."""
    from apps.core.manychat_service import ManyChatError, ManyChatService

    service = ManyChatService()
    item = BriefItem(
        key='manychat_health',
        title='תקינות WhatsApp',
        severity=GREEN,
        action='לבדוק את החיבור ל-ManyChat בהגדרות ← הודעות.',
    )
    if not service.is_configured:
        item.severity = RED
        item.count = 1
        item.summary = 'ManyChat אינו מוגדר בשרת — שום הודעה לא תצא.'
        item.rows.append(_row('MANYCHAT_KEY', 'חסר', '/settings/whatsapp'))
        return item
    try:
        flows = service.get_flows()
    except ManyChatError as exc:
        item.severity = RED
        item.count = 1
        item.summary = 'ManyChat לא עונה — שליחת הודעות תיכשל.'
        item.rows.append(_row('ManyChat', str(exc), '/settings/whatsapp'))
        return item
    item.summary = f'ManyChat מחובר · {len(flows or [])} אוטומציות זמינות.'
    return item


def check_tranzila_reconciliation(today: date) -> BriefItem:
    """
    Yesterday's takings as the CRM recorded them, against Tranzila itself.

    Two different statements — "the system charged" and "the money arrived" —
    and only the terminal can settle the second.
    """
    from apps.core.tranzila_service import TranzilaService
    from apps.customers.models import TranzilaTransaction

    day = today - timedelta(days=1)
    service = TranzilaService.production()
    item = BriefItem(
        key='tranzila_reconciliation',
        title=f'התאמה מול טרנזילה ({day:%d/%m})',
        severity=GREEN,
        action='להשוות בטרנזילה מול דף התשלומים במערכת.',
    )
    if service.credential_error():
        item.severity = YELLOW
        item.count = 1
        item.summary = 'לא ניתן לבדוק מול טרנזילה — חסרים מפתחות.'
        item.rows.append(_row('טרנזילה', service.credential_error() or '', '/settings/billing'))
        return item

    response = service.list_all_transactions(day, day)
    if not response.get('success'):
        item.severity = YELLOW
        item.count = 1
        item.summary = 'טרנזילה לא החזירה את רשימת העסקאות.'
        item.rows.append(_row('טרנזילה', str(response.get('error') or ''), '/settings/billing'))
        return item

    gateway_rows = response.get('transactions') or []
    ours = TranzilaTransaction.objects.filter(
        is_successful=True,
        request_timestamp__date=day,
    ).count()
    theirs = len(gateway_rows)
    item.count = abs(theirs - ours)
    item.summary = f'במערכת {ours} חיובים מוצלחים, בטרנזילה {theirs}.'
    if theirs != ours:
        item.severity = RED if abs(theirs - ours) > 1 else YELLOW
        item.rows.append(_row(
            'פער בין המערכת לטרנזילה',
            f'{ours} במערכת מול {theirs} בטרנזילה — לבדוק מי חויב ולא נרשם, או נרשם ולא חויב.',
            '/credit-charge',
        ))
    return item


CHECKS = (
    check_overdue_recurring,
    check_unresolved_charges,
    check_recurring_without_lesson,
    check_failed_payments,
    check_registration_only_payments,
    check_expiring_cards,
    check_duplicate_charges,
    check_revenue_drop,
    check_refunds,
    check_active_without_standing_order,
    check_ended_standing_orders,
    check_overdue_instalments,
    check_status_mismatch,
    check_missing_receipts,
    check_business_categories,
    check_document_numbering,
    check_tranzila_health,
    check_manychat_health,
    check_tranzila_reconciliation,
)

# Checks that call an outside service, so a quick brief can leave them out.
EXTERNAL_CHECKS = {'tranzila_health', 'manychat_health', 'tranzila_reconciliation'}


def build_daily_brief(*, today: date | None = None, include_external: bool = True) -> dict:
    """Run every check and return the brief. Never raises."""
    day = today or _israel_today()
    started = timezone.now()
    items: list[dict] = []
    for check in CHECKS:
        name = check.__name__.replace('check_', '')
        if not include_external and name in EXTERNAL_CHECKS:
            continue
        try:
            items.append(check(day).as_dict())
        except Exception as exc:  # noqa: BLE001 — a broken check is a finding, not a crash
            logger.exception('daily brief check failed: %s', name)
            items.append(BriefItem(
                key=name,
                title=f'הבדיקה "{name}" נכשלה',
                severity=RED,
                count=1,
                summary=f'הבדיקה עצמה נכשלה ולכן אין עליה תשובה: {exc}',
                action='לדווח למפתח — זו תקלה בבדיקה, לא בהכרח במערכת.',
            ).as_dict())

    red = [i for i in items if i['severity'] == RED]
    yellow = [i for i in items if i['severity'] == YELLOW]
    return {
        'generated_at': timezone.now().isoformat(),
        'for_date': day.isoformat(),
        'duration_ms': int((timezone.now() - started).total_seconds() * 1000),
        'red_count': len(red),
        'yellow_count': len(yellow),
        'headline': 'אין בעיות דחופות' if not red else f'{len(red)} נושאים דורשים טיפול היום',
        'items': items,
    }
