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

# The billing cron charges in batches of 40, eight times a day, so on a busy
# first-of-month a charge can honestly wait a day. Two days is late.
OVERDUE_GRACE_DAYS = 2

# The whole brief has to answer inside one request on the hosting platform.
# Checks run cheapest first, so if the budget runs out it is the calls to
# Tranzila and ManyChat that are dropped — and the brief says so rather than
# letting the screen hang on a request nobody will answer.
TIME_BUDGET_SECONDS = 40

# Working out a child's true status means reading their enrollments and their
# payments. The newest records are where a wrong status actually shows up, so
# beyond this many children the check says how far it got instead of running on.
MAX_CHILDREN_SCANNED = 3000


@dataclass
class BriefItem:
    key: str
    title: str
    severity: str
    count: int = 0
    summary: str = ''
    action: str = ''
    rows: list = field(default_factory=list)
    duration_ms: int = 0

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

    from apps.customers.models import TranzilaTransaction

    cutoff = today - timedelta(days=OVERDUE_GRACE_DAYS)
    rows = (
        RecurringPayment.objects
        .filter(status='active', tranzila_recurring_index='', next_billing_date__lt=cutoff)
        .exclude(tranzila_token='')
        .select_related('child')
        .order_by('next_billing_date')
    )
    # The ones the billing cron is deliberately holding have their own item; a
    # standing order should appear in one place, with one thing to do about it.
    stuck = {
        key.split('_')[1]
        for key in TranzilaTransaction.objects
        .filter(is_successful=False, idempotency_key__startswith='recurring_')
        .values_list('idempotency_key', flat=True)
        if len(key.split('_')) >= 3
    }
    rows = [row for row in rows if str(row.id) not in stuck]
    total = len(rows)
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
    item.summary = (
        f'{total} הוראות קבע שתאריך החיוב שלהן עבר ביותר מיומיים ועדיין לא חויבו. '
        'החיוב החודשי רץ בקבוצות, אז יום אחד של פיגור הוא נורמלי.'
    )
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
    failures = list(
        Payment.objects
        .filter(status='failed', created_at__gte=since)
        .select_related('child')
        .order_by('-created_at')
    )
    # A failure the office already put right — the parent updated the card and
    # was charged — is history, not something to act on this morning.
    recovered = set(
        Payment.objects
        .filter(child_id__in={row.child_id for row in failures}, status='completed', created_at__gte=since)
        .values_list('child_id', flat=True)
    )
    rows = [row for row in failures if row.child_id not in recovered]
    total = len(rows)
    item = BriefItem(
        key='failed_payments',
        title='תשלומים שנכשלו בשבוע האחרון',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לשלוח להורה קישור לעדכון כרטיס, או לחייב שוב.',
    )
    if not total:
        item.summary = 'לא נכשל אף תשלום בשבוע האחרון, או שכל מה שנכשל כבר נגבה.'
        return item
    item.summary = f'{total} תשלומים נכשלו בשבעת הימים האחרונים ועדיין לא נגבו.'
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
    Children being charged, or sitting in a lesson, who are still marked as a trial.

    Statuses drift for all sorts of reasons and a list of every disagreement is
    too long to act on — `manage.py audit_child_statuses` exists for that. This
    is the half the office can do something about this morning: money is coming
    in, or a place is taken, and the child still reads as "ניסיון" on every
    screen.
    """
    from apps.customers.child_status import canonical_status, resolve_child_status, status_label
    from apps.customers.models import Child, RecurringPayment

    paying = set(
        RecurringPayment.objects.filter(status='active').values_list('child_id', flat=True)
    )
    base = Child.objects.exclude(status='ghost')
    total_children = base.count()
    children = (
        base
        .filter(status__in=('trial_signed', 'trial_completed', 'pending'))
        .select_related('family')
        .prefetch_related('lesson_enrollments', 'payments')
        .order_by('-updated_at')[:MAX_CHILDREN_SCANNED]
    )
    mismatched = []
    for child in children.iterator(chunk_size=500):
        should_be = resolve_child_status(child)
        if not should_be or canonical_status(child.status) == should_be:
            continue
        if should_be == 'active' or child.id in paying:
            mismatched.append((child, should_be))

    item = BriefItem(
        key='status_mismatch',
        title='ילדים שרשומים כניסיון אבל כבר לומדים',
        severity=YELLOW if mismatched else GREEN,
        count=len(mismatched),
        action='לפתוח את כרטיס הילד ולעדכן את הסטטוס לפעיל.',
    )
    if not mismatched:
        item.summary = f'אין ילד שנשאר בסטטוס ניסיון אחרי שנרשם או שילם (מתוך {total_children} ילדים).'
        return item
    item.summary = (
        f'{len(mismatched)} ילדים שהסטטוס שלהם עדיין ניסיון או ממתין, למרות שיש להם הוראת קבע פעילה '
        'או רישום לחוג.'
    )
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

    # 30 days, not 90: the screen for the whole year already exists, and this
    # one has to answer inside a single request.
    rows = payments_without_invoice(since=timezone.now() - timedelta(days=30))
    item = BriefItem(
        key='missing_receipts',
        title='תשלומים ללא חשבונית',
        severity=YELLOW if rows else GREEN,
        count=len(rows),
        action='להפיק את המסמכים החסרים במסך הקבלות החסרות.',
    )
    if not rows:
        item.summary = 'לכל תשלום ב-30 הימים האחרונים יש מסמך.'
        return item
    item.summary = f'{len(rows)} תשלומים ב-30 הימים האחרונים בלי חשבונית או קבלה.'
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
    A sign-up that paid the registration fee and never paid for the course.

    The owner's case: 260 for the course plus 120 registration, and only the
    120 was taken. But registration and course are two separate charges here —
    the fee on the day of signing, the course on the monthly run — so a fee on
    its own is the normal shape of a sign-up and says nothing. What says
    something is a fee with no course charge behind it at all, and no standing
    order waiting to make one.
    """
    from django.db.models import F

    from apps.customers.models import Payment, RecurringPayment

    since = timezone.now() - timedelta(days=45)
    fees = list(
        Payment.objects
        .filter(status='completed', created_at__gte=since, registration_fee__gt=0)
        .filter(final_amount__lte=F('registration_fee'))
        .exclude(child__isnull=True)
        .select_related('child')
        .order_by('-created_at')
    )
    child_ids = {row.child_id for row in fees}

    # Anyone who also paid for a course: a charge of their own beyond the fee.
    paid_for_course = set(
        Payment.objects
        .filter(child_id__in=child_ids, status='completed')
        .exclude(id__in=[row.id for row in fees])
        .filter(Q(payment_type='recurring_subscription') | Q(final_amount__gt=F('registration_fee')))
        .values_list('child_id', flat=True)
    )
    # Or is set up to be charged for one.
    will_be_charged = set(
        RecurringPayment.objects
        .filter(child_id__in=child_ids, status='active')
        .values_list('child_id', flat=True)
    )
    settled = paid_for_course | will_be_charged

    open_rows = [row for row in fees if row.child_id not in settled]
    item = BriefItem(
        key='registration_only_payments',
        title='שולמו דמי רישום בלי תשלום על החוג',
        severity=RED if open_rows else GREEN,
        count=len(open_rows),
        action='לבדוק מול ההורה: אם הילד לומד, לגבות את החוג או לפתוח הוראת קבע.',
    )
    if not open_rows:
        item.summary = 'לכל מי ששילם דמי רישום ב-45 הימים האחרונים יש גם חיוב על החוג או הוראת קבע.'
        return item
    item.summary = (
        f'{len(open_rows)} ילדים ששילמו דמי רישום, ואין להם שום חיוב על החוג ולא הוראת קבע פעילה.'
    )
    for payment in open_rows[:MAX_ROWS]:
        item.rows.append(_row(
            payment.child.full_name if payment.child else 'ללא ילד משויך',
            f'שילם {_money(payment.final_amount)} דמי רישום ב-{timezone.localtime(payment.created_at):%d/%m} · '
            'אין חיוב על החוג',
            _child_href(payment.child_id) if payment.child_id else '',
        ))
    return item


def check_next_billing_run(today: date) -> BriefItem:
    """
    What the next monthly run will do, before it does it.

    The owner's rule: the charges have to go through. This walks the standing
    orders that are due next and applies the billing cron's own conditions, so
    an order that would be skipped is seen days before the 1st rather than
    found missing afterwards.
    """
    from apps.customers.models import RecurringPayment, TranzilaTransaction

    # The next 1st — the day the monthly run does its work.
    run_day = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    due = list(
        RecurringPayment.objects
        .filter(status='active', next_billing_date__lte=run_day)
        .select_related('child', 'initial_payment', 'initial_payment__lesson')
    )
    stuck = {
        key.split('_')[1]
        for key in TranzilaTransaction.objects
        .filter(is_successful=False, idempotency_key__startswith='recurring_')
        .values_list('idempotency_key', flat=True)
        if len(key.split('_')) >= 3
    }

    blocked: list[tuple] = []
    gateway_owned = 0
    expected = Decimal('0')
    for recurring in due:
        if recurring.tranzila_recurring_index:
            # Tranzila holds this one's schedule; our cron does not touch it.
            gateway_owned += 1
            continue
        expected += Decimal(recurring.amount or 0)
        if not recurring.tranzila_token:
            blocked.append((recurring, 'אין כרטיס שמור — לא ניתן לחייב'))
        elif str(recurring.id) in stuck:
            blocked.append((recurring, 'ניסיון קודם נתקע — הגבייה עוצרת עד שמסדרים'))
        elif not recurring.initial_payment or not recurring.initial_payment.lesson_id:
            blocked.append((recurring, 'אין שיעור משויך — החיוב מדלג עליה'))
        elif (
            recurring.card_expire_year
            and recurring.card_expire_month
            and (
                recurring.card_expire_year < run_day.year
                or (recurring.card_expire_year == run_day.year and recurring.card_expire_month < run_day.month)
            )
        ):
            blocked.append((recurring, f'הכרטיס פג בתוקף {recurring.card_expire_month:02d}/{recurring.card_expire_year}'))

    item = BriefItem(
        key='next_billing_run',
        title=f'הגבייה הבאה ({run_day:%d/%m})',
        severity=RED if blocked else GREEN,
        count=len(blocked),
        action='לטפל לפני ה-1: לשלוח קישור לעדכון כרטיס, לשייך שיעור, או להסדיר חיוב תקוע.',
    )
    charging = len(due) - gateway_owned
    base = f'{charging} הוראות קבע אמורות להיגבות ב-{run_day:%d/%m} בסך {_money(expected)}'
    if gateway_owned:
        base += f' · {gateway_owned} מנוהלות ישירות בטרנזילה'
    if not blocked:
        item.summary = f'{base}. כולן מוכנות לחיוב.'
        return item
    item.summary = f'{base}. {len(blocked)} מהן ייכשלו מראש אם לא יטופלו.'
    for recurring, reason in blocked[:MAX_ROWS]:
        item.rows.append(_row(
            recurring.child.full_name if recurring.child else str(recurring.id),
            f'{_money(recurring.amount)} · {reason}',
            _child_href(recurring.child_id),
        ))
    return item


def check_children_to_deactivate(today: date) -> BriefItem:
    """
    Children the records say have left, still carried as paying customers.

    A refund that closed the account, or a standing order that was cancelled
    and whose paid period has run out: the child stops being charged and stays
    "פעיל" on every screen and in every count. The rule used is the system's
    own — `resolve_child_status` — so this agrees with the customers page
    rather than inventing a second opinion.
    """
    from apps.customers.child_status import resolve_child_status, status_label
    from apps.customers.models import Child, RecurringPayment

    still_charged = set(
        RecurringPayment.objects
        .filter(status='active')
        .exclude(tranzila_token='')
        .values_list('child_id', flat=True)
    )
    children = (
        Child.objects
        .filter(status__in=('active', 'payment_problem'))
        .select_related('family')
        .prefetch_related('lesson_enrollments', 'payments')
        .order_by('-updated_at')[:MAX_CHILDREN_SCANNED]
    )
    leaving = []
    for child in children.iterator(chunk_size=500):
        if child.id in still_charged:
            # A live standing order with a card behind it: nobody has left.
            continue
        if resolve_child_status(child) == 'inactive':
            leaving.append(child)

    item = BriefItem(
        key='children_to_deactivate',
        title='ילדים שסיימו ועדיין רשומים כפעילים',
        severity=YELLOW if leaving else GREEN,
        count=len(leaving),
        action='להעביר ללא פעיל. אין להם הוראת קבע עם כרטיס, ולכן לא ייגבה מהם שוב.',
    )
    if not leaving:
        item.summary = 'אין ילד פעיל שהרישומים שלו אומרים שהוא כבר לא.'
        return item
    item.summary = (
        f'{len(leaving)} ילדים שזוכו או שהוראת הקבע שלהם בוטלה והתקופה ששולמה נגמרה, '
        'ועדיין רשומים כפעילים.'
    )
    for child in leaving[:MAX_ROWS]:
        paid_until = f'שולם עד {child.paid_until_date:%d/%m/%Y}' if child.paid_until_date else 'ללא תקופה משולמת'
        item.rows.append(_row(
            child.full_name,
            f'רשום {status_label(child.status)} · {paid_until} · אין הוראת קבע פעילה',
            _child_href(child.id),
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
        .values('child_id', 'final_amount', 'created_at__date', 'lesson_id')
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
    item.summary = f'{len(rows)} מקרים של אותו ילד שחויב פעמיים באותו יום, על אותו שיעור ובאותו סכום.'
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
    # Only a day with nothing at all, where that same weekday always had money,
    # is treated as a finding: the big charges land on one day of the month, so
    # "less than usual" is the normal shape of most days here and would cry wolf.
    if actual == 0 and len(known) == len(history):
        item.severity = RED
        item.count = 1
        item.rows.append(_row('לא נכנס כסף כלל', f'ממוצע רגיל {_money(typical)}', '/credit-charge'))
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

    from apps.customers.models import Payment
    from apps.documents.models import CashPlan, CheckPlan

    paying = set(RecurringPayment.objects.filter(status='active').values_list('child_id', flat=True))
    # Cash, cheques and a charge that already came in are all "being paid for".
    paying |= set(CashPlan.objects.filter(status='active').values_list('child_id', flat=True))
    paying |= set(CheckPlan.objects.values_list('child_id', flat=True))
    paying |= set(
        Payment.objects
        .filter(status='completed', created_at__gte=timezone.now() - timedelta(days=60))
        .values_list('child_id', flat=True)
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
        action='לבדוק למה לא נגבה מהם — מי שמשלם במזומן, בצ׳קים או שחויב לאחרונה כבר לא מופיע כאן.',
    )
    if not total:
        item.summary = 'לכל ילד פעיל יש הוראת קבע, תשלום אחר, או חיוב שנכנס לאחרונה.'
        return item
    item.summary = (
        f'{total} ילדים בסטטוס פעיל בלי הוראת קבע, בלי מזומן או צ׳קים, ובלי חיוב בחודשיים האחרונים.'
    )
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
    """
    Cash and cheque instalments whose document never got issued.

    The billing cron issues these itself, for active plans, in batches — so a
    document that is a day late is the batch, not a problem, and an instalment
    on a cancelled plan is not waiting for anything. What is left is a plan the
    office believes is running whose paperwork stopped.
    """
    from apps.documents.models import CashPlanMonth, CheckItem

    cutoff = today - timedelta(days=OVERDUE_GRACE_DAYS)
    cash = list(
        CashPlanMonth.objects
        .filter(status='pending', due_date__lt=cutoff, plan__status='active')
        .select_related('plan', 'plan__child')
        .order_by('due_date')[:MAX_ROWS]
    )
    cheques = list(
        CheckItem.objects
        .filter(status='pending', due_date__lt=cutoff, plan__status='active')
        .select_related('plan', 'plan__child')
        .order_by('due_date')[:MAX_ROWS]
    )
    total = (
        CashPlanMonth.objects.filter(status='pending', due_date__lt=cutoff, plan__status='active').count()
        + CheckItem.objects.filter(status='pending', due_date__lt=cutoff, plan__status='active').count()
    )
    item = BriefItem(
        key='overdue_instalments',
        title='מזומן וצ׳קים בלי מסמך',
        severity=YELLOW if total else GREEN,
        count=total,
        action='לבדוק למה המסמך לא הופק, ושהכסף אכן התקבל.',
    )
    if not total:
        item.summary = 'לכל תשלום במזומן או בצ׳ק שהגיע מועדו הופק מסמך.'
        return item
    item.summary = (
        f'{total} תשלומים בתוכניות מזומן או צ׳קים פעילות שהמועד שלהם עבר ולא הופק עליהם מסמך.'
    )

    def child_name(plan) -> str:
        child = getattr(plan, 'child', None)
        return child.full_name if child else 'ללא ילד משויך'

    for month in cash:
        item.rows.append(_row(
            f'מזומן · {child_name(month.plan)}',
            f'{_money(month.amount)} · לתאריך {month.due_date:%d/%m/%Y}',
            '/invoices',
        ))
    for cheque in cheques[:max(0, MAX_ROWS - len(cash))]:
        item.rows.append(_row(
            f"צ׳ק {cheque.check_number} · {child_name(cheque.plan)}".strip(),
            f'{_money(cheque.amount)} · לתאריך {cheque.due_date:%d/%m/%Y}',
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


# What each readiness check means for the office, in its own words. Two of them
# read as alarms and are not: the document terminal falls back to the payment
# terminal, and the environment label is not used by anything that charges.
READINESS_WORDING = {
    'credentials': 'מפתחות הסליקה',
    'token_terminal': 'מסוף החיוב בטוקן — בלעדיו אין גבייה חודשית',
    'webhook_secret': 'אימות ההודעות מטרנזילה',
    'notify_url': 'כתובת הדיווח על תשלום — אם אינה מוגדרת גם אצל טרנזילה, תשלומים לא יאושרו',
    'handshake': 'לחיצת יד מול המסוף',
    'environment': 'תווית סביבה בלבד — לא משפיעה על חיובים',
    'billing_terminal': 'מסוף להפקת מסמכים בטרנזילה — כשהוא ריק, המסמכים מופקים במערכת עצמה',
}


def check_tranzila_health(today: date) -> BriefItem:
    """
    The gateway's own readiness — the reason a charge screen suddenly errors.

    Only a blocking failure is a red here. The two non-blocking ones are
    labels, and calling them failures sent the office looking for a fault that
    was not there.
    """
    from apps.core.tranzila_service import TranzilaService

    report = TranzilaService.production().live_readiness()
    checks = report.get('checks') if isinstance(report, dict) else []
    failed = [c for c in (checks or []) if not c.get('ok')]
    blocking = [c for c in failed if c.get('blocking')]
    notes = [c for c in failed if not c.get('blocking')]

    item = BriefItem(
        key='tranzila_health',
        title='תקינות הסליקה',
        severity=RED if blocking else GREEN,
        count=len(blocking),
        action='לתקן בהגדרות ← סליקה, או במסוף של טרנזילה.',
    )
    if not blocking:
        item.summary = 'החיבור לטרנזילה תקין — חיוב בכרטיס יעבוד.'
    else:
        item.summary = f'{len(blocking)} בדיקות חוסמות נכשלו. חיוב בכרטיס עלול להיכשל.'
    for check in blocking[:MAX_ROWS]:
        item.rows.append(_row(
            READINESS_WORDING.get(str(check.get('name')), str(check.get('name'))),
            str(check.get('detail') or ''),
            '/settings/billing',
        ))
    # Said, but not as an alarm: these do not stop a charge.
    for check in notes[:MAX_ROWS]:
        item.rows.append(_row(
            f"לידיעה · {READINESS_WORDING.get(str(check.get('name')), str(check.get('name')))}",
            str(check.get('detail') or ''),
            '/settings/billing',
        ))
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

    # Two pages is 2,000 transactions — far more than a day here — and it
    # keeps one slow gateway from eating the whole brief's time.
    response = service.list_all_transactions(day, day, max_pages=2)
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
    item.summary = f'במערכת {ours} חיובים מוצלחים, בטרנזילה {theirs} עסקאות.'
    item.rows.append(_row(
        'להשוואה',
        'רשימת טרנזילה כוללת גם עסקאות שנדחו וגם מה שרץ במסוף ממקורות אחרים, '
        'ולכן הפרש אינו בהכרח תקלה.',
        '/credit-charge',
    ))
    # One direction does mean something: charges we recorded and the terminal
    # does not have cannot be explained by declines.
    if ours > theirs:
        item.severity = RED
        item.count = ours - theirs
        item.rows.insert(0, _row(
            'יש במערכת יותר חיובים מאשר בטרנזילה',
            f'{ours} מול {theirs} — לבדוק אם נרשם חיוב שלא באמת ירד.',
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
    check_next_billing_run,
    check_children_to_deactivate,
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


def check_key(check) -> str:
    return check.__name__.replace('check_', '')


# Asked for one at a time by the screen: a single request per check is the only
# shape that survives a hosting platform's limit on how long one request may
# take, and it lets a slow check be seen instead of hiding behind a spinner.
CHECK_REGISTRY = {check_key(check): check for check in CHECKS}


def check_catalogue() -> list[dict]:
    """The checks in the order they should run: cheap first, outside services last."""
    titles = {
        'overdue_recurring': 'הוראות קבע שלא ירדו',
        'unresolved_charges': 'הוראות קבע שהחיוב שלהן נעצר',
        'recurring_without_lesson': 'הוראות קבע שאי אפשר לחייב',
        'failed_payments': 'תשלומים שנכשלו',
        'registration_only_payments': 'דמי רישום בלי תשלום על החוג',
        'expiring_cards': 'כרטיסים שפג תוקפם',
        'next_billing_run': 'הגבייה הבאה',
        'children_to_deactivate': 'ילדים שסיימו ועדיין פעילים',
        'duplicate_charges': 'חיובים כפולים',
        'revenue_drop': 'הכנסות אתמול',
        'refunds': 'זיכויים',
        'active_without_standing_order': 'ילדים פעילים בלי הוראת קבע',
        'ended_standing_orders': 'הוראות קבע שהסתיימו',
        'overdue_instalments': 'מזומן וצ׳קים בלי מסמך',
        'status_mismatch': 'ילדים שרשומים כניסיון אבל כבר לומדים',
        'missing_receipts': 'תשלומים ללא חשבונית',
        'business_categories': 'עסקים בלי קטגוריה',
        'document_numbering': 'מספור מסמכים',
        'tranzila_health': 'תקינות הסליקה',
        'manychat_health': 'תקינות WhatsApp',
        'tranzila_reconciliation': 'התאמה מול טרנזילה',
    }
    return [
        {'key': key, 'title': titles.get(key, key), 'external': key in EXTERNAL_CHECKS}
        for key in CHECK_REGISTRY
    ]


def run_check(key: str, *, today: date | None = None) -> dict:
    """One check, by name. Never raises: a broken check comes back as a red item."""
    check = CHECK_REGISTRY.get(key)
    if check is None:
        raise KeyError(key)
    day = today or _israel_today()
    started = timezone.now()
    try:
        item = check(day)
    except Exception as exc:  # noqa: BLE001 — a broken check is a finding, not a crash
        logger.exception('daily brief check failed: %s', key)
        item = BriefItem(
            key=key,
            title=f'הבדיקה "{key}" נכשלה',
            severity=RED,
            count=1,
            summary=f'הבדיקה עצמה נכשלה ולכן אין עליה תשובה: {exc}',
            action='לדווח למפתח — זו תקלה בבדיקה, לא בהכרח במערכת.',
        )
    item.duration_ms = int((timezone.now() - started).total_seconds() * 1000)
    return item.as_dict()


def summarise(items: list[dict], *, day: date, duration_ms: int = 0) -> dict:
    """Wrap items as a brief — used both by the nightly run and by the screen."""
    red = [i for i in items if i['severity'] == RED]
    yellow = [i for i in items if i['severity'] == YELLOW]
    return {
        'generated_at': timezone.now().isoformat(),
        'for_date': day.isoformat(),
        'duration_ms': duration_ms,
        'red_count': len(red),
        'yellow_count': len(yellow),
        'headline': 'אין בעיות דחופות' if not red else f'{len(red)} נושאים דורשים טיפול היום',
        'items': items,
    }


def build_daily_brief(*, today: date | None = None, include_external: bool = True) -> dict:
    """
    Run every check and return the brief. Never raises, and always answers.

    Cheap checks first: if the time budget runs out, what is dropped is the
    part that talks to another company's server, and the brief names it as
    unchecked instead of pretending it passed.
    """
    day = today or _israel_today()
    started = timezone.now()
    items: list[dict] = []
    skipped: list[str] = []
    for check in CHECKS:
        name = check.__name__.replace('check_', '')
        if not include_external and name in EXTERNAL_CHECKS:
            continue
        elapsed = (timezone.now() - started).total_seconds()
        if elapsed > TIME_BUDGET_SECONDS:
            skipped.append(name)
            continue
        check_started = timezone.now()
        try:
            item = check(day)
        except Exception as exc:  # noqa: BLE001 — a broken check is a finding, not a crash
            logger.exception('daily brief check failed: %s', name)
            item = BriefItem(
                key=name,
                title=f'הבדיקה "{name}" נכשלה',
                severity=RED,
                count=1,
                summary=f'הבדיקה עצמה נכשלה ולכן אין עליה תשובה: {exc}',
                action='לדווח למפתח — זו תקלה בבדיקה, לא בהכרח במערכת.',
            )
        item.duration_ms = int((timezone.now() - check_started).total_seconds() * 1000)
        items.append(item.as_dict())

    if skipped:
        items.append(BriefItem(
            key='skipped_checks',
            title='בדיקות שלא הספיקו לרוץ',
            severity=YELLOW,
            count=len(skipped),
            summary='הבדיקה נעצרה בזמן שהוקצב לה, ולכן החלק הזה לא נבדק הבוקר.',
            action='הבריף הלילי בודק הכל. אפשר גם ללחוץ "בדוק עכשיו" שוב.',
            rows=[_row(name, 'לא נבדק') for name in skipped],
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
