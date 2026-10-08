"""
What is wrong with a customer — worked out from the records each time it is
asked, never stored.

The owner (6.10.2026): a small red light on the customers list next to a
customer something is wrong with, and at the top of the customer's page what
the problem is and what to do to put it right. His examples: a status that is
not true (a child who pays and is not "פעיל"), the same class paid for twice in
one month, a card that never collected anything.

Rules of this module:

  * It reads. Nothing here writes, charges, sends or calls another company —
    and it never goes through `customers/recurring-payments/`, whose reads
    write (apply_due_pending_recurring_amounts).
  * It answers for a whole page in a fixed handful of queries, however many
    children are on it (`_load`). The customers list calls it for twenty
    children; the "only with problems" filter calls it for everyone.
  * It invents no rule the system does not already have. The status rule is
    `resolve_child_status` with the exception the morning routine makes; an
    unpaid month is `months_outstanding`; a child with nothing to charge is
    the reading `children_without_standing_order` takes.
  * A child means the person: every card of the same name on the family —
    the rule that hides the weaker card from the list
    (child_identity.exclude_weaker_duplicate_children). Two registrations of
    one child are where the double charges came from.

Each problem says what happened, with sums and dates, and what to do.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.core.tranzila_service import recorded_decline_code
from apps.customers.child_status import (
    LIVE_ENROLLMENT_STATUSES,
    MANUAL_REASON_PREFIX,
    STATUS_ACTIVE,
    STATUS_GHOST,
    STATUS_INACTIVE,
    STATUS_PAYMENT_PROBLEM,
    STATUS_PENDING,
    STATUS_TRIAL_COMPLETED,
    STATUS_TRIAL_SIGNED,
    LoadedFacts,
    canonical_status,
    resolve_child_status,
    status_label,
)

logger = logging.getLogger(__name__)

# The codes, in the order the card lists them: money taken twice first.
DOUBLE_STANDING_ORDER = 'double_standing_order'
DOUBLE_CHARGE = 'double_charge'
DECLINED_RECORDED_PAID = 'declined_recorded_paid'
STUCK_CHARGE = 'stuck_charge'
SIGNUP_DECLINED = 'signup_declined'
STANDING_ORDER_FAILED = 'standing_order_failed'
STANDING_ORDER_OVERDUE = 'standing_order_overdue'
STANDING_ORDER_NO_CARD = 'standing_order_no_card'
NO_STANDING_ORDER = 'no_standing_order'
STATUS_MISMATCH = 'status_mismatch'
DUPLICATE_CARD = 'duplicate_card'

ORDER = (
    DOUBLE_STANDING_ORDER, DOUBLE_CHARGE, DECLINED_RECORDED_PAID, STUCK_CHARGE, SIGNUP_DECLINED,
    STANDING_ORDER_FAILED, STANDING_ORDER_OVERDUE, STANDING_ORDER_NO_CARD, NO_STANDING_ORDER,
    STATUS_MISMATCH, DUPLICATE_CARD,
)

# A student, as counts of students read it (enrollment_counts.ACTIVE_STUDENT_CHILD_STATUSES).
STUDENT_STATUSES = (STATUS_ACTIVE, STATUS_PAYMENT_PROBLEM)

# The billing cron works in batches, so a charge can honestly wait a day or
# two — the same grace the morning brief gives (daily_brief.OVERDUE_GRACE_DAYS).
OVERDUE_GRACE_DAYS = 2

# A charge still waiting after this long is not on its way any more.
STUCK_AFTER = timedelta(hours=24)

# A double charge older than this is history, not something to put right now.
DOUBLE_CHARGE_LOOKBACK_MONTHS = 12

# A sign-up whose charge failed is somebody to phone for this long. After it
# the parent has moved on, and a light that never goes out is not read.
SIGNUP_DECLINED_LOOKBACK_DAYS = 60

# Above this many children the rows are read whole instead of by a list of ids.
READ_WHOLE_ABOVE = 800

HEBREW_MONTHS = (
    'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני',
    'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר',
)

OTHER_CARD = 'בכרטיס הנוסף של הילד'
# How the child's card labels a row that comes from that other card.
OTHER_CARD_LABEL = 'מכרטיס נוסף של הילד'

# A catch-up charge from a card replacement names the month it is for
# (card_replacement._charge_one_month): "מנוי חודשי 09/2026 - …".
_NAMED_MONTH = re.compile(r'מנוי חודשי (\d{2})/(\d{4})')


@dataclass
class Problem:
    """
    One thing wrong with a child, in words the office reads.

    The card shows the title alone; `what` and `action` open under it (owner,
    6.10.2026: short lines, the point of each in bold). The two texts carry two
    marks and nothing else: **…** is bold, and a line starting with "• " is an
    item of a list. The title carries neither — the list's tooltip shows it.
    """
    code: str
    title: str
    what: str
    action: str
    # The branches of the lessons the problem is about; empty when it is about
    # the child as a whole. A partner is shown only what touches their branches.
    branch_ids: set = field(default_factory=set)

    def as_dict(self) -> dict:
        return {'code': self.code, 'title': self.title, 'what': self.what, 'action': self.action}


_Payment = namedtuple('_Payment', (
    'id', 'child_id', 'status', 'payment_type', 'lesson_id', 'bundle_id', 'final_amount',
    'registration_fee', 'trial_credit_amount', 'trial_lesson_date', 'payment_date',
    'created_at', 'updated_at', 'description', 'response_code',
))
# A charge that did not go through: a sign-up or a paid trial the parent tried.
_FailedCharge = namedtuple('_FailedCharge', (
    'id', 'child_id', 'lesson_id', 'final_amount', 'trial_lesson_date', 'created_at',
    'failure_code', 'failure_reason',
))
_Enrollment = namedtuple('_Enrollment', (
    'child_id', 'lesson_id', 'bundle_id', 'status', 'trial_lesson_date', 'trial_held_on',
    'trial_outcome', 'end_date',
))
_Lesson = namedtuple('_Lesson', ('course_id', 'course_name', 'branch_id'))


# --- small helpers -----------------------------------------------------------

def _today() -> date:
    return timezone.localtime(timezone.now()).date()


def _local(moment):
    """A stored moment on the Israeli clock: a charge at 00:30 on the 1st belongs to the new month."""
    if moment is None:
        return None
    return timezone.localtime(moment) if timezone.is_aware(moment) else moment


def _month(day) -> date:
    return date(day.year, day.month, 1)


def _previous_month(month: date) -> date:
    return date(month.year - 1, 12, 1) if month.month == 1 else date(month.year, month.month - 1, 1)


def _months_back(month: date, count: int) -> date:
    for _ in range(count):
        month = _previous_month(month)
    return month


def _month_label(month: date) -> str:
    return f'{HEBREW_MONTHS[month.month - 1]} {month.year}'


def _day_label(day) -> str:
    return f'{day.day}.{day.month}.{day.year}'


def _money(value) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    if amount == amount.to_integral_value():
        return f'₪{int(amount):,}'
    return f'₪{amount:,.2f}'


def _b(text) -> str:
    """Bold, in the mark the card draws (Problem)."""
    return f'**{text}**'


def _lines(*lines) -> str:
    """The lines that say something, one under the other."""
    return '\n'.join(line for line in lines if line)


def _items(*items) -> str:
    """A list: one item to a line."""
    return '\n'.join(f'• {item}' for item in items if item)


def identity_key(child) -> tuple:
    """
    The person a card belongs to: the family and the name, case ignored — the
    same match exclude_weaker_duplicate_children hides the weaker card by.
    """
    return (child.family_id, (child.first_name or '').upper(), (child.last_name or '').upper())


def _is_monthly_charge(payment: _Payment) -> bool:
    """A completed charge that bought a month of a lesson — not a fee alone, not a trial."""
    if payment.status != 'completed' or payment.payment_type != 'recurring_subscription':
        return False
    if payment.lesson_id is None or payment.trial_lesson_date is not None:
        return False
    if payment.final_amount <= 0:
        return False
    if recorded_decline_code(payment.response_code):
        # Written down as paid and refused by the card company: no money, so not a charge.
        return False
    # payment_service.payment_is_fee_only: the trial credit lowered the charge, not the month.
    return payment.final_amount - payment.registration_fee + payment.trial_credit_amount > 0


def _paid_on(payment: _Payment):
    return _local(payment.payment_date or payment.created_at)


def _billing_month(payment: _Payment) -> date:
    """The month a monthly charge is for: the one its description names, else the one it was taken in."""
    named = _NAMED_MONTH.search(payment.description or '')
    if named:
        month, year = int(named.group(1)), int(named.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1)
    return _month(_paid_on(payment))


# --- reading everything once ---------------------------------------------------

class _Records:
    """Everything the rules read, for a set of people, loaded once."""

    def __init__(self):
        self.cards: dict = {}                       # child id -> Child
        self.people: dict = defaultdict(list)       # identity key -> [Child]
        self.standing_orders: dict = defaultdict(list)
        self.payments: dict = defaultdict(list)
        self.failed_charges: dict = defaultdict(list)   # child id -> charges that did not go through, lately
        self.enrollments: dict = defaultdict(list)
        self.cash_plans: dict = defaultdict(list)
        self.check_plans: dict = defaultdict(list)
        self.lessons: dict = {}
        self.bundle_lessons: dict = defaultdict(set)
        self.overrides: dict = defaultdict(dict)    # standing order id -> {month: override}
        self.held: dict = {}                        # standing order id (str) -> when its charge stopped
        self.documents: dict = defaultdict(int)     # child id -> issued documents
        self.set_by_hand: dict = {}                 # child id -> the reason written

    def person(self, child) -> list:
        return self.people.get(identity_key(child)) or [child]

    def lesson(self, lesson_id) -> _Lesson:
        return self.lessons.get(lesson_id) or _Lesson(None, '', None)

    def covered_lessons(self, standing_order) -> set:
        """The lessons a standing order bills: its own, and its bundle's."""
        initial = standing_order.initial_payment
        if initial is None:
            return set()
        lessons = set(self.bundle_lessons.get(initial.bundle_id, ())) if initial.bundle_id else set()
        if initial.lesson_id:
            lessons.add(initial.lesson_id)
        return lessons

    def facts(self, child) -> LoadedFacts:
        """What resolve_child_status asks about this card, from the rows already read."""
        return LoadedFacts(
            child,
            cash_plans=self.cash_plans.get(child.id, ()),
            check_plans=self.check_plans.get(child.id, ()),
            payments=[
                (p.lesson_id, p.trial_lesson_date, p.final_amount, p.registration_fee, p.trial_credit_amount)
                for p in self.payments.get(child.id, ()) if p.status == 'completed'
            ],
            standing_orders=[(r.status, r.tranzila_token) for r in self.standing_orders.get(child.id, ())],
            enrollments=[
                (e.status, e.trial_lesson_date, e.trial_held_on, e.trial_outcome, e.end_date)
                for e in self.enrollments.get(child.id, ())
            ],
        )


_CARD_FIELDS = ('id', 'family_id', 'first_name', 'last_name', 'status', 'paid_until_date', 'created_at')


def _load(children) -> _Records:
    """
    Read what the rules need about these children and their other cards.

    A dozen queries, whatever the number of children. Past READ_WHOLE_ABOVE
    children the tables are read whole rather than by a list of ids.
    """
    from apps.courses.models import Lesson, LessonBundle
    from apps.customers.card_replacement import REPLACEABLE_STATUSES
    from apps.customers.models import Child, Payment, RecurringChargeOverride, RecurringPayment, TranzilaTransaction
    from apps.documents.models import CashPlan, CashPlanMonth, CheckItem, CheckPlan
    from apps.enrollments.models import LessonEnrollment

    records = _Records()
    asked = [child for child in children if child.status != STATUS_GHOST]
    if not asked:
        return records

    # Every card on the families asked about: that is where the other card of
    # the same child is. A walk-in is nobody's duplicate.
    family_ids = {child.family_id for child in asked}
    whole = len(family_ids) > READ_WHOLE_ABOVE
    cards = Child.objects.exclude(status=STATUS_GHOST).only(*_CARD_FIELDS)
    if not whole:
        cards = cards.filter(family_id__in=family_ids)
    wanted = {identity_key(child) for child in asked}
    for card in cards:
        key = identity_key(card)
        if key in wanted:
            records.people[key].append(card)
            records.cards[card.id] = card
    for group in records.people.values():
        group.sort(key=lambda card: (card.created_at, str(card.id)))

    ids = list(records.cards)

    def scoped(queryset, lookup='child_id'):
        return queryset if whole else queryset.filter(**{f'{lookup}__in': ids})

    # The lesson rides along: months_outstanding reads it off the initial payment.
    for order in scoped(
        RecurringPayment.objects.select_related('initial_payment', 'initial_payment__lesson')
    ).order_by('created_at'):
        if order.child_id in records.cards:
            records.standing_orders[order.child_id].append(order)

    payments = scoped(Payment.objects.filter(status__in=('completed', 'refunded', 'pending', 'processing')))
    for row in payments.order_by('created_at').values_list(
        'id', 'child_id', 'status', 'payment_type', 'lesson_id', 'bundle_id', 'final_amount',
        'registration_fee', 'trial_credit_amount', 'trial_lesson_date', 'payment_date',
        'created_at', 'updated_at', 'description', 'tranzila_transaction__response_code',
    ):
        payment = _Payment(*row)
        if payment.child_id in records.cards:
            records.payments[payment.child_id].append(payment)

    # Charges that did not go through, kept apart: no other rule reads them.
    since = timezone.now() - timedelta(days=SIGNUP_DECLINED_LOOKBACK_DAYS)
    for row in scoped(Payment.objects.filter(status='failed', created_at__gte=since)).order_by('created_at').values_list(
        'id', 'child_id', 'lesson_id', 'final_amount', 'trial_lesson_date', 'created_at',
        'failure_code', 'failure_reason',
    ):
        failed = _FailedCharge(*row)
        if failed.child_id in records.cards:
            records.failed_charges[failed.child_id].append(failed)

    for row in scoped(LessonEnrollment.objects.all()).values_list(
        'child_id', 'lesson_id', 'bundle_id', 'status', 'trial_lesson_date', 'trial_held_on',
        'trial_outcome', 'end_date',
    ):
        enrollment = _Enrollment(*row)
        if enrollment.child_id in records.cards:
            records.enrollments[enrollment.child_id].append(enrollment)

    def plans(plan_model, date_model, target):
        by_plan = {
            plan_id: (child_id, status, [])
            for plan_id, child_id, status in scoped(plan_model.objects.all()).values_list('id', 'child_id', 'status')
            if child_id in records.cards
        }
        if by_plan:
            for plan_id, due in date_model.objects.filter(plan_id__in=list(by_plan)).values_list('plan_id', 'due_date'):
                by_plan[plan_id][2].append(due)
        for child_id, status, dates in by_plan.values():
            target[child_id].append((status, dates))

    plans(CashPlan, CashPlanMonth, records.cash_plans)
    plans(CheckPlan, CheckItem, records.check_plans)

    orders = [order for group in records.standing_orders.values() for order in group]
    lesson_ids = {p.lesson_id for group in records.payments.values() for p in group if p.lesson_id}
    lesson_ids |= {e.lesson_id for group in records.enrollments.values() for e in group if e.lesson_id}
    lesson_ids |= {f.lesson_id for group in records.failed_charges.values() for f in group if f.lesson_id}
    bundle_ids = set()
    for order in orders:
        initial = order.initial_payment
        if initial is not None:
            if initial.lesson_id:
                lesson_ids.add(initial.lesson_id)
            if initial.bundle_id:
                bundle_ids.add(initial.bundle_id)
    if bundle_ids:
        through = LessonBundle.lessons.through
        for bundle_id, lesson_id in through.objects.filter(lessonbundle_id__in=bundle_ids).values_list(
            'lessonbundle_id', 'lesson_id',
        ):
            records.bundle_lessons[bundle_id].add(lesson_id)
            lesson_ids.add(lesson_id)
    if lesson_ids:
        lessons = Lesson.objects.all() if whole else Lesson.objects.filter(id__in=lesson_ids)
        for lesson_id, course_id, name, branch_id in lessons.values_list(
            'id', 'course_id', 'course__name', 'course__branch_id',
        ):
            records.lessons[lesson_id] = _Lesson(course_id, name or '', branch_id)

    billable = [order.id for order in orders if order.status in REPLACEABLE_STATUSES]
    if billable:
        for override in RecurringChargeOverride.objects.filter(
            recurring_payment_id__in=billable, applied_at__isnull=True,
        ):
            records.overrides[override.recurring_payment_id][override.billing_month] = override
        # A monthly charge the gateway never answered: the billing run holds the
        # standing order until a person has looked (daily_brief.check_unresolved_charges).
        for key, when in (
            TranzilaTransaction.objects
            .filter(is_successful=False, idempotency_key__startswith='recurring_')
            .order_by('request_timestamp')
            .values_list('idempotency_key', 'request_timestamp')
        ):
            parts = key.split('_')
            if len(parts) >= 3:
                records.held.setdefault(parts[1], when)

    # Documents are asked only of people with more than one card — the other
    # card's papers are what the flag is about.
    doubled = [card.id for group in records.people.values() if len(group) > 1 for card in group]
    if doubled:
        _count_documents(records, doubled)
    return records


def _count_documents(records: _Records, child_ids: list) -> None:
    from apps.customers.financial_models import Invoice
    from apps.documents.models import FormalDocument
    from apps.store.models import StoreInvoice

    seen = set()
    receipts = (
        Invoice.objects
        .filter(Q(children__child_id__in=child_ids) | Q(payment__child_id__in=child_ids))
        .values_list('id', 'children__child_id', 'payment__child_id')
    )
    for invoice_id, line_child, payment_child in receipts:
        for child_id in (line_child, payment_child):
            if child_id in records.cards and (invoice_id, child_id) not in seen:
                seen.add((invoice_id, child_id))
                records.documents[child_id] += 1
    for child_id in StoreInvoice.objects.filter(child_id__in=child_ids).values_list('child_id', flat=True):
        records.documents[child_id] += 1
    for child_id in (
        FormalDocument.objects.filter(child_id__in=child_ids).exclude(document_type='draft')
        .values_list('child_id', flat=True)
    ):
        records.documents[child_id] += 1


def _load_hand_set(records: _Records, child_ids) -> None:
    """Which of these cards carry a status the office set by hand, and the reason it wrote."""
    from apps.customers.status_history_models import ChildStatusHistory

    if not child_ids:
        return
    latest: dict = {}
    for child_id, new_status, reason in (
        ChildStatusHistory.objects.filter(child_id__in=list(child_ids))
        .order_by('child_id', '-changed_at')
        .values_list('child_id', 'new_status', 'reason')
    ):
        latest.setdefault(child_id, (new_status, reason or ''))
    for child_id, (new_status, reason) in latest.items():
        card = records.cards.get(child_id)
        if card is not None and new_status == card.status and reason.startswith(MANUAL_REASON_PREFIX):
            records.set_by_hand[child_id] = reason


# --- the rules ------------------------------------------------------------------
#
# Each takes the cards of one person and the card being looked at (`viewer`),
# so a line about the other card says so.

def _where(child_id, viewer) -> str:
    return '' if child_id == viewer.id else f' ({OTHER_CARD})'


def _double_standing_orders(person, viewer, records) -> list[Problem]:
    """Two live standing orders billing the same lesson — on one card, or on the two cards of one child."""
    active = [
        order for card in person for order in records.standing_orders.get(card.id, ())
        if order.status == 'active'
    ]
    by_lesson = defaultdict(list)
    for order in active:
        for lesson_id in records.covered_lessons(order):
            by_lesson[lesson_id].append(order)

    problems, reported = [], set()
    for lesson_id, orders in by_lesson.items():
        if len(orders) < 2:
            continue
        cluster = frozenset(order.id for order in orders)
        if cluster in reported:
            continue
        reported.add(cluster)
        lesson = records.lesson(lesson_id)
        problems.append(Problem(
            code=DOUBLE_STANDING_ORDER,
            title='שתי הוראות קבע על אותו חוג',
            what=_lines(
                f'על {lesson.course_name or "אותו חוג"} יש {_b(f"{len(orders)} הוראות קבע פעילות")}, '
                'וכל אחת מחויבת בכל חודש:',
                _items(*(
                    f'{_b(_money(order.amount))} לחודש, מ־{_day_label(order.start_date)}'
                    f'{_where(order.child_id, viewer)}'
                    for order in orders
                )),
            ),
            action=_items(
                f'{_b("לבטל את הוראת הקבע המיותרת")} — לשונית "תשלומים", תחת "הוראות קבע".',
                f'{_b("לזכות את החיובים הכפולים")} שכבר ירדו — לשונית "תשלומים", כפתור "זיכוי".',
            ),
            branch_ids={lesson.branch_id} if lesson.branch_id else set(),
        ))
    return problems


def _next_month(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def _first_billable_month(order) -> date:
    """
    The first month a standing order could have charged.

    A sign-up that paid for its month starts there. One that paid the
    registration fee alone starts the month after — or on the date monthly
    billing was put off to (SUBSCRIPTION_FIRST_CHARGE_DATE: the August 2026
    sign-ups were first billed on 1.9), when it signed before it.
    """
    from django.conf import settings

    start = _month(order.start_date)
    initial = order.initial_payment
    if initial is None or initial.final_amount - initial.registration_fee + initial.trial_credit_amount > 0:
        return start
    first = _next_month(start)
    try:
        put_off_to = date.fromisoformat(str(getattr(settings, 'SUBSCRIPTION_FIRST_CHARGE_DATE', '') or '').strip())
    except ValueError:
        return first
    if order.start_date < put_off_to:
        first = max(first, _month(put_off_to))
    return first


def _owed_before(month: date, charged_months: set, floor: date) -> int:
    """
    How many months just before `month` went unpaid — the room a catch-up has.

    A card replaced in October pays September too, and a billing run that was
    held does the same: two completed charges in one calendar month, rightly.
    Only an unbroken run of unpaid months counts, and never further back than
    the standing order itself (`floor`).
    """
    owed = 0
    cursor = _previous_month(month)
    while cursor >= floor and cursor not in charged_months and owed < 12:
        owed += 1
        cursor = _previous_month(cursor)
    return owed


def _double_charges(person, viewer, records, today: date) -> list[Problem]:
    """The same lesson paid for twice in one month — by one card, or by the two cards of one child."""
    since = _months_back(_month(today), DOUBLE_CHARGE_LOOKBACK_MONTHS)
    by_lesson = defaultdict(list)
    for card in person:
        for payment in records.payments.get(card.id, ()):
            if _is_monthly_charge(payment):
                by_lesson[payment.lesson_id].append(payment)

    problems = []
    for lesson_id, charges in by_lesson.items():
        by_month = defaultdict(list)
        for charge in charges:
            by_month[_billing_month(charge)].append(charge)
        for month in sorted(by_month):
            rows = by_month[month]
            if len(rows) < 2 or month < since:
                continue
            cards_charged = {row.child_id for row in rows}
            if len(cards_charged) < 2:
                # One card, charged more than once: a catch-up of months that
                # went unpaid is not a double charge.
                card_id = rows[0].child_id
                charged = {m for m, group in by_month.items() if any(r.child_id == card_id for r in group)}
                starts = [
                    _first_billable_month(order) for order in records.standing_orders.get(card_id, ())
                    if lesson_id in records.covered_lessons(order) and _month(order.start_date) <= month
                ]
                floor = max(starts) if starts else min(charged)
                if len(rows) - 1 <= _owed_before(month, charged, floor):
                    continue
            lesson = records.lesson(lesson_id)
            problems.append(Problem(
                code=DOUBLE_CHARGE,
                title='חיוב כפול באותו חודש',
                what=_lines(
                    f'ב{_month_label(month)} ירדו {_b(f"{len(rows)} חיובים חודשיים")} על '
                    f'{lesson.course_name or "אותו חוג"}:',
                    _items(*(
                        f'{_day_label(_paid_on(row))} — {_b(_money(row.final_amount))}'
                        f'{_where(row.child_id, viewer)}'
                        for row in sorted(rows, key=_paid_on)
                    )),
                ),
                action=_items(
                    f'{_b("לזכות את החיוב המיותר")} — לשונית "תשלומים", כפתור "זיכוי".',
                    f'אם יש שתי הוראות קבע על החוג — {_b("לבטל אחת")}, כדי שזה לא יחזור בחודש הבא.',
                ),
                branch_ids={lesson.branch_id} if lesson.branch_id else set(),
            ))
    return problems


def _declined_recorded_paid(person, viewer, records) -> list[Problem]:
    """A payment marked completed whose stored Tranzila answer is a refusal: the money never came."""
    problems = []
    for card in person:
        for payment in records.payments.get(card.id, ()):
            if payment.status != 'completed' or payment.final_amount <= 0:
                continue
            code = recorded_decline_code(payment.response_code)
            if not code:
                continue
            lesson = records.lesson(payment.lesson_id)
            subject = f' על {lesson.course_name}' if lesson.course_name else ''
            problems.append(Problem(
                code=DECLINED_RECORDED_PAID,
                title='חיוב שנדחה ורשום כשולם',
                what=_lines(
                    f'החיוב מ־{_day_label(_paid_on(payment))} בסך {_b(_money(payment.final_amount))}{subject}'
                    f'{_where(payment.child_id, viewer)} רשום "הושלם", אבל {_b("טרנזילה דחתה אותו")} '
                    f'(קוד {code}).',
                    _b('הכסף לא נגבה.'),
                ),
                action=_items(
                    f'{_b("לגבות את הסכום מההורה")}, אחרי בדיקה בטרנזילה שהחיוב באמת לא עבר.',
                    f'{_b("לא לזכות")} את החיוב הזה: אין מה להחזיר, והמערכת תסרב.',
                    'הסימון נשאר עד שרישום התשלום יתוקן.',
                ),
                branch_ids={lesson.branch_id} if lesson.branch_id else set(),
            ))
    return problems


def _stuck_charges(person, viewer, records, now) -> list[Problem]:
    """
    A charge that has been waiting more than a day.

    'processing' is a charge the gateway never answered. 'pending' is flagged
    only when it is a standing order's monthly charge — a registration nobody
    paid for is also 'pending', for good, and that is not a problem.
    """
    orders = [order for card in person for order in records.standing_orders.get(card.id, ())]
    problems = []
    for card in person:
        for payment in records.payments.get(card.id, ()):
            if payment.status == 'processing':
                since = payment.updated_at or payment.created_at
            elif payment.status == 'pending' and _is_standing_order_charge(payment, orders, records):
                since = payment.created_at
            else:
                continue
            if since is None or now - since < STUCK_AFTER:
                continue
            lesson = records.lesson(payment.lesson_id)
            subject = f' על {lesson.course_name}' if lesson.course_name else ''
            state = 'בבדיקה' if payment.status == 'processing' else 'ממתין'
            state_text = _b(f'"{state}"')
            problems.append(Problem(
                code=STUCK_CHARGE,
                title='חיוב תקוע',
                what=_lines(
                    f'חיוב של {_b(_money(payment.final_amount))}{subject}{_where(payment.child_id, viewer)} '
                    f'נמצא במצב {state_text} מאז {_b(_day_label(_local(since)))}.',
                    'ייתכן שהכסף ירד בטרנזילה והמערכת לא רשמה זאת.',
                ),
                action=_items(
                    f'{_b("לבדוק בטרנזילה")} אם החיוב עבר.',
                    f'עבר — {_b("לא לחייב שוב")} את אותו חודש.',
                    f'לא עבר — {_b("לגבות מחדש")}.',
                    'עד הבדיקה: לא לזכות ולא לחייב שוב.',
                ),
                branch_ids={lesson.branch_id} if lesson.branch_id else set(),
            ))
    return problems


def _why_it_failed(failed: _FailedCharge) -> str:
    """The reason the charge did not go through, as it was recorded — in the office's words."""
    reason = (failed.failure_reason or '').strip()
    if not reason or reason == 'Success':
        # An answer whose refusal code was not kept (before 27.9.2026).
        return 'טרנזילה דחתה את החיוב (הקוד לא נשמר).'
    if 'validation schema' in reason:
        return 'תקלה בבקשה ששלחנו לטרנזילה — לא בעיה בכרטיס.'
    if 'fetching failed' in reason:
        return 'טרנזילה לא הצליחה לקרוא את פרטי הכרטיס.'
    # Ours are written as "<what happened> (קוד NNN). <what was done>": the first sentence says it.
    first = reason.split('. ')[0].strip().rstrip('.')
    return f'{first[:140]}.'


def _signup_declined(person, viewer, records, now) -> list[Problem]:
    """
    A sign-up — to a course, or to a paid trial lesson — whose charge did not
    go through, with nothing after it (owner, 8.10.2026).

    The parent reached the card and it stopped there. The child keeps the
    status they had (a trial child stays a trial child, a new one is בתהליך
    רישום), and until now nothing on the screen said they had tried: the office
    saw it only in the card company's own system. This is somebody to phone.

    It is over — and the light goes out — once the person paid for a course
    sign-up after the failure, holds a student's place in that course, or, for
    a trial, has a trial booked or paid there. A customer who already paid for
    this very course is not a sign-up: their failed month is the standing
    order's rules' to say. And after SIGNUP_DECLINED_LOOKBACK_DAYS it is history.
    """
    failures = [failed for card in person for failed in records.failed_charges.get(card.id, ())]
    if not failures:
        return []
    cutoff = now - timedelta(days=SIGNUP_DECLINED_LOOKBACK_DAYS)
    payments = [payment for card in person for payment in records.payments.get(card.id, ())]
    enrollments = [enrollment for card in person for enrollment in records.enrollments.get(card.id, ())]
    orders = [order for card in person for order in records.standing_orders.get(card.id, ())]

    def took_money(payment: _Payment) -> bool:
        return (
            payment.status == 'completed' and payment.final_amount > 0 and payment.lesson_id is not None
            and not recorded_decline_code(payment.response_code)
        )

    def course_of(lesson_id):
        return records.lesson(lesson_id).course_id or lesson_id

    course_paid = [p for p in payments if took_money(p) and p.trial_lesson_date is None]
    trial_paid = [p for p in payments if took_money(p) and p.trial_lesson_date is not None]
    student_in = {
        course_of(e.lesson_id) for e in enrollments
        if e.status in LIVE_ENROLLMENT_STATUSES and e.trial_lesson_date is None
    }
    trial_in = {course_of(e.lesson_id) for e in enrollments if e.trial_lesson_date is not None and e.status != 'cancelled'}

    # One light for each thing they tried: the last failure of each course, sign-up and trial apart.
    last: dict = {}
    tries: dict = defaultdict(int)
    for failed in failures:
        if failed.lesson_id is None or failed.created_at is None or failed.created_at < cutoff:
            continue
        key = (course_of(failed.lesson_id), failed.trial_lesson_date is not None)
        tries[key] += 1
        if key not in last or failed.created_at > last[key].created_at:
            last[key] = failed

    problems = []
    for (course, is_trial), failed in sorted(last.items(), key=lambda item: item[1].created_at):
        paid_this_course = [p for p in course_paid if course_of(p.lesson_id) == course]
        if any(p.created_at <= failed.created_at for p in paid_this_course):
            continue    # already a customer of this course: a failed month, not a failed sign-up
        if paid_this_course or course in student_in:
            continue    # they got in after all
        if is_trial:
            if course in trial_in or any(course_of(p.lesson_id) == course for p in trial_paid):
                continue    # the trial was booked in the end
        if any(
            p.created_at > failed.created_at and not _is_standing_order_charge(p, orders, records)
            for p in course_paid
        ):
            continue    # they signed up to another course instead
        lesson = records.lesson(failed.lesson_id)
        # "לחוג <name>": a name may start with a number ("4.5-6 מחול"), and a
        # letter glued to it does not read.
        course_name = lesson.course_name or 'שלא נשמר שמו'
        when = _day_label(_local(failed.created_at))
        count = tries[(course, is_trial)]
        again = f'זה קרה {_b(f"{count} פעמים")}; האחרונה ב־{when}.' if count > 1 else ''
        if is_trial:
            title = 'ניסה להזמין שיעור ניסיון — החיוב נכשל'
            tried = f'ב־{when} ניסו להזמין לו {_b("שיעור ניסיון")} בחוג {course_name}{_where(failed.child_id, viewer)}.'
            after = f'מאז {_b("לא הוזמן שיעור ניסיון")} בחוג הזה.'
            help_with = 'להזמין את שיעור הניסיון'
        else:
            title = 'ניסה להירשם — החיוב נכשל'
            tried = f'ב־{when} ניסו {_b("לרשום אותו")} לחוג {course_name}{_where(failed.child_id, viewer)}.'
            after = f'מאז {_b("לא נרשם תשלום")} על החוג הזה.'
            help_with = 'להשלים את ההרשמה'
        problems.append(Problem(
            code=SIGNUP_DECLINED,
            title=title,
            what=_lines(
                tried,
                f'החיוב של {_b(_money(failed.final_amount))} {_b("לא עבר")}: {_why_it_failed(failed)}',
                again,
                after,
            ),
            action=_items(
                f'{_b("להתקשר להורה")} ולעזור לו {help_with}.',
                'אפשר לשלוח לו קישור להזנת כרטיס, מלשונית התשלומים בכרטיס הילד.',
                f'הסימון יורד כשההרשמה מושלמת, או אחרי {SIGNUP_DECLINED_LOOKBACK_DAYS} יום.',
            ),
            branch_ids={lesson.branch_id} if lesson.branch_id else set(),
        ))
    return problems


def _is_standing_order_charge(payment: _Payment, orders, records) -> bool:
    """A monthly charge made for a standing order that was already there — not a sign-up waiting to be paid."""
    from apps.customers.card_replacement import REPLACEABLE_STATUSES

    if payment.payment_type != 'recurring_subscription' or payment.lesson_id is None:
        return False
    if payment.registration_fee > 0 or payment.trial_lesson_date is not None:
        return False
    return any(
        order.status in REPLACEABLE_STATUSES
        and order.initial_payment_id != payment.id
        and order.created_at <= payment.created_at
        and payment.lesson_id in records.covered_lessons(order)
        for order in orders
    )


def _unpaid(person, viewer, records, today: date) -> list[Problem]:
    """
    A student on a regular place whose month nobody is collecting.

    The standing order failed, is being held, has no card behind it — or there
    is none at all and nothing else paid for the month.
    """
    from apps.customers.card_replacement import REPLACEABLE_STATUSES, months_outstanding

    if not any(card.status in STUDENT_STATUSES for card in person):
        return []
    places = [
        enrollment for card in person for enrollment in records.enrollments.get(card.id, ())
        if enrollment.status in LIVE_ENROLLMENT_STATUSES and enrollment.trial_lesson_date is None
    ]
    if not places:
        return []
    # Cash or cheques the office registered: the card is not what pays for this child.
    facts = [records.facts(card) for card in person]
    if any(f.plan_running() or f.plan_covers_this_month(today) for f in facts):
        return []

    # Which months each lesson was paid for, on any card of the child.
    paid = defaultdict(set)
    for card in person:
        for payment in records.payments.get(card.id, ()):
            if (
                payment.status == 'completed' and payment.payment_type == 'recurring_subscription'
                and payment.lesson_id and payment.payment_date
            ):
                when = _local(payment.payment_date)
                paid[payment.lesson_id].add((when.year, when.month))

    def bills(order, place) -> bool:
        """The standing order bills this place: its lesson, its bundle or its course (change_course.recurring_payments_for_unit)."""
        initial = order.initial_payment
        if initial is None:
            return False
        if initial.bundle_id and initial.bundle_id == place.bundle_id:
            return True
        covered = records.covered_lessons(order)
        if place.lesson_id in covered:
            return True
        course_id = records.lesson(place.lesson_id).course_id
        return any(records.lesson(lesson_id).course_id == course_id for lesson_id in covered)

    live_orders = [
        order for card in person for order in records.standing_orders.get(card.id, ())
        if order.status in REPLACEABLE_STATUSES
    ]
    # A child moved to another class keeps the standing order of the class they
    # signed up to — the order still names the old lesson. So when some place
    # has no order naming it, the orders that name no place are taken to be
    # paying for it. When every place has its own, an order naming none is
    # left over from a class the child is no longer in, and is not chased.
    every_place_has_one = all(any(bills(order, place) for order in live_orders) for place in places)

    problems = []
    usable = False
    for order in live_orders:
        managed_by_tranzila = bool((order.tranzila_recurring_index or '').strip())
        has_card = bool((order.tranzila_token or '').strip())
        if has_card or managed_by_tranzila:
            usable = True
        if managed_by_tranzila or order.status == 'paused':
            continue
        if every_place_has_one and not any(bills(order, place) for place in places):
            continue
        initial = order.initial_payment
        lesson_id = initial.lesson_id if initial is not None else None
        lesson = records.lesson(lesson_id)
        subject = lesson.course_name or 'החוג'
        where = _where(order.child_id, viewer)
        branch_ids = {lesson.branch_id} if lesson.branch_id else set()

        if order.status == 'active' and not has_card:
            problems.append(Problem(
                code=STANDING_ORDER_NO_CARD,
                title='הוראת קבע בלי כרטיס',
                what=_lines(
                    f'הוראת הקבע על {subject}{where} ({_b(_money(order.amount))} לחודש) פעילה, '
                    f'אבל {_b("אין מאחוריה כרטיס שמור")}.',
                    'היא לא תחויב.',
                ),
                action=_items(
                    f'{_b("לשלוח להורה קישור להזנת כרטיס")} — לשונית "תשלומים", "קישור להזנת כרטיס".',
                    f'או {_b("לרשום תשלום במזומן")}.',
                ),
                branch_ids=branch_ids,
            ))
            continue

        due = months_outstanding(
            order, today=today, paid_months=paid.get(lesson_id, set()),
            overrides=records.overrides.get(order.id, {}),
        ) if lesson_id else []
        open_text = _open_months(due)

        if order.status == 'failed':
            problems.append(Problem(
                code=STANDING_ORDER_FAILED,
                title='הכרטיס נדחה — החודש לא שולם',
                what=_lines(
                    f'{_b(f"החיוב החודשי על {subject} נדחה")}{where}, והוראת הקבע '
                    f'({_b(_money(order.amount))} לחודש) עצרה.',
                    open_text or 'היא לא תחויב שוב עד שיוחלף הכרטיס.',
                ),
                action=_items(
                    f'{_b("להחליף את הכרטיס")} — לשונית "תשלומים", "החלפת כרטיס אשראי". '
                    'ההחלפה גובה גם את החודשים הפתוחים.',
                    f'אפשר גם {_b("לשלוח להורה קישור")} לעדכון הכרטיס.',
                ),
                branch_ids=branch_ids,
            ))
            continue

        held_since = records.held.get(str(order.id))
        late = bool(order.next_billing_date) and order.next_billing_date < today - timedelta(days=OVERDUE_GRACE_DAYS)
        if held_since is not None:
            problems.append(Problem(
                code=STANDING_ORDER_OVERDUE,
                title='החיוב החודשי נעצר',
                what=_lines(
                    f'ניסיון החיוב על {subject}{where} מ־{_b(_day_label(_local(held_since)))} '
                    f'{_b("לא קיבל תשובה מטרנזילה")}.',
                    'המערכת לא תחייב שוב עד שמישהו יבדוק.',
                    open_text,
                ),
                action=(
                    f'{_b("לבדוק בטרנזילה")} אם הכסף ירד באותו יום. '
                    'עד שזה מוסדר, הוראת הקבע הזאת לא תחויב.'
                ),
                branch_ids=branch_ids,
            ))
        elif late and due:
            problems.append(Problem(
                code=STANDING_ORDER_OVERDUE,
                title='הוראת הקבע לא חויבה',
                what=_lines(
                    f'הוראת הקבע על {subject}{where} הייתה אמורה לרדת ב־'
                    f'{_b(_day_label(order.next_billing_date))} {_b("ולא חויבה")}.',
                    open_text,
                ),
                action=_items(
                    f'{_b("לבדוק בבריף הבוקר")} למה החיוב לא יצא (מפתחות המסוף, תוקף הכרטיס).',
                    f'{_b("לבדוק בטרנזילה")} שהכסף לא ירד.',
                    f'אם הכרטיס לא תקין — {_b("להחליף אותו")} מכרטיס הילד.',
                ),
                branch_ids=branch_ids,
            ))

    if problems or usable:
        return problems

    # Nothing is set up to charge this child. Is the month paid all the same?
    if any(card.paid_until_date and card.paid_until_date >= today for card in person):
        return []
    this_month = (today.year, today.month)
    course_money = [
        payment for card in person for payment in records.payments.get(card.id, ())
        if payment.status == 'completed' and payment.lesson_id and payment.trial_lesson_date is None
        and payment.final_amount - payment.registration_fee + payment.trial_credit_amount > 0
    ]
    if any((_paid_on(p).year, _paid_on(p).month) == this_month for p in course_money):
        return []
    courses = sorted({records.lesson(place.lesson_id).course_name for place in places} - {''})
    last = max(course_money, key=_paid_on) if course_money else None
    history = (
        f'התשלום האחרון על חוג: {_day_label(_paid_on(last))}, {_money(last.final_amount)}.'
        if last else 'עד היום לא נגבה אף תשלום על חוג.'
    )
    branch_ids = {records.lesson(place.lesson_id).branch_id for place in places} - {None}
    return [Problem(
        code=NO_STANDING_ORDER,
        title='אין הוראת קבע — החודש לא שולם',
        what=_lines(
            f'יש רישום ל{" ול".join(courses) if courses else "חוג"}, אבל {_b("אין הוראת קבע")} — '
            'לא בכרטיס, לא במזומן ולא בצ׳קים.',
            _b(f'לא נרשם תשלום על {_month_label(_month(today))}.'),
            history,
        ),
        action=_items(
            f'{_b("לשלוח להורה קישור להזנת כרטיס")} — לשונית "תשלומים", "קישור להזנת כרטיס".',
            f'או {_b("לרשום תשלום במזומן")}.',
            f'אם הילד כבר לא בחוג — {_b("להסיר אותו מהחוג")}.',
        ),
        branch_ids=branch_ids,
    )]


def _open_months(due) -> str:
    if not due:
        return ''
    total = sum((row.amount for row in due), Decimal('0.00'))
    months = ', '.join(f'{_month_label(row.month)} {_money(row.amount)}' for row in due)
    if len(due) == 1:
        return f'פתוח לתשלום: {_b(months)}.'
    return f'פתוח לתשלום: {months} — {_b(f"סך הכול {_money(total)}")}.'


_STATUS_WHY = {
    STATUS_INACTIVE: 'אין תשלום בתוקף ואין לו מקום בחוג.',
    STATUS_TRIAL_SIGNED: 'יש לו שיעור ניסיון שעוד לא התקיים.',
    STATUS_TRIAL_COMPLETED: 'שיעור הניסיון שלו כבר התקיים, ואין תשלום על חוג.',
    STATUS_PENDING: 'אין תשלום על חוג ואין שיעור ניסיון.',
}


def _status_mismatch(card, records, today: date) -> tuple | None:
    """
    (recorded, by the records, why) when the card's status contradicts its own
    records — the morning routine's reading (morning_fixes.status_fix_candidates),
    with its one exception: a charge not made yet is not a card problem.
    """
    facts = records.facts(card)
    current = canonical_status(card.status)
    target = resolve_child_status(card, facts)
    if not target or target == current:
        return None
    if target == STATUS_PAYMENT_PROBLEM and facts.still_charged():
        return None
    if target == STATUS_ACTIVE:
        if card.paid_until_date and card.paid_until_date >= today:
            why = f'שולם עליו עד {_day_label(card.paid_until_date)}.'
        elif facts.plan_running() or facts.plan_covers_this_month(today):
            why = 'יש לו תשלום במזומן או בצ׳קים שעדיין בתוקף.'
        else:
            why = 'נרשם עליו תשלום על חוג.'
    elif target == STATUS_PAYMENT_PROBLEM:
        if card.paid_until_date and card.paid_until_date < today:
            why = f'התקופה ששולמה נגמרה ב־{_day_label(card.paid_until_date)}, והוא עדיין רשום לחוג.'
        else:
            why = 'הוראת הקבע שלו נכשלה, והוא עדיין רשום לחוג.'
    else:
        why = _STATUS_WHY.get(target, '')
    return current, target, why


def _status_problem(card, records, today: date) -> list[Problem]:
    found = _status_mismatch(card, records, today)
    if found is None:
        return []
    current, target, why = found
    recorded = status_label(current) if current else f'{card.status} (סטטוס ישן)'
    by_hand = records.set_by_hand.get(card.id)
    hand_note = f'הסטטוס נקבע ביד במשרד ("{by_hand[:120]}").' if by_hand else ''
    should_be = status_label(target)
    recorded_text = _b(f'"{recorded}"')
    should_be_text = _b(f'"{should_be}"')
    change_text = _b(f'לשנות את הסטטוס ל"{should_be}"')
    return [Problem(
        code=STATUS_MISMATCH,
        title='הסטטוס לא תואם את הרישומים',
        what=_lines(
            f'הסטטוס הרשום: {recorded_text}.',
            f'לפי התשלומים וההרשמות הוא אמור להיות: {should_be_text}.',
            why,
            hand_note,
        ),
        action=_items(
            f'אם הרישומים נכונים — {change_text}: '
            'לחיצה על הסטטוס ברשימת הלקוחות, עם סיבה.',
            f'אם הסטטוס הוא הנכון — {_b("לתקן את מה שחסר ברישומים")}.',
        ),
    )]


def _card_summary(card, records) -> dict:
    """What another card of the same child holds."""
    payments = [p for p in records.payments.get(card.id, ()) if p.status in ('completed', 'refunded')]
    completed = [p for p in payments if p.status == 'completed']
    orders = [o for o in records.standing_orders.get(card.id, ()) if o.status in ('active', 'failed', 'paused')]
    return {
        'id': str(card.id),
        'full_name': f'{card.first_name} {card.last_name}'.strip(),
        'status': card.status,
        'status_label': status_label(card.status),
        'created_at': card.created_at.isoformat() if card.created_at else None,
        'payments_count': len(payments),
        'completed_total': str(sum((p.final_amount for p in completed), Decimal('0.00'))),
        'standing_orders_count': len(orders),
        'documents_count': records.documents.get(card.id, 0),
    }


def _holds_money(summary: dict) -> bool:
    return bool(summary['payments_count'] or summary['documents_count'] or summary['standing_orders_count'])


def _duplicate_cards(person, viewer, records) -> list[Problem]:
    """Another card of the same child that carries payments, documents or a standing order."""
    problems = []
    for card in person:
        if card.id == viewer.id:
            continue
        summary = _card_summary(card, records)
        if not _holds_money(summary):
            continue
        holds = []
        if summary['payments_count']:
            count = summary['payments_count']
            holds.append(
                _b('חיוב אחד' if count == 1 else f'{count} חיובים')
                + f' (שהושלמו: {_money(summary["completed_total"])})'
            )
        if summary['standing_orders_count']:
            count = summary['standing_orders_count']
            holds.append(_b('הוראת קבע' if count == 1 else f'{count} הוראות קבע'))
        if summary['documents_count']:
            count = summary['documents_count']
            holds.append(_b('מסמך אחד' if count == 1 else f'{count} מסמכים'))
        opened = f', נפתח ב־{_day_label(_local(card.created_at))}' if card.created_at else ''
        branch_ids = set()
        for payment in records.payments.get(card.id, ()):
            branch_ids.add(records.lesson(payment.lesson_id).branch_id)
        for enrollment in records.enrollments.get(card.id, ()):
            branch_ids.add(records.lesson(enrollment.lesson_id).branch_id)
        branch_ids.discard(None)
        problems.append(Problem(
            code=DUPLICATE_CARD,
            title='לילד יש כרטיס נוסף במערכת',
            what=_lines(
                f'יש במערכת {_b("כרטיס נוסף על אותו שם")} (סטטוס "{summary["status_label"]}"{opened}), '
                'והוא לא מופיע ברשימת הלקוחות.',
                f'רשומים עליו: {", ".join(holds)}.',
            ),
            action=_items(
                f'{_b("לבדוק שאין חיוב כפול ושאין הוראת קבע מיותרת")}: החיובים, המסמכים והוראות הקבע '
                f'שלו מוצגים כאן, בלשונית "תשלומים", מסומנים "{OTHER_CARD_LABEL}".',
                'הסימון נשאר עד שהכרטיסים יאוחדו. כשלשניהם יש כסף, האיחוד נעשה ידנית '
                'על ידי מי שמתחזק את המערכת.',
            ),
            branch_ids=branch_ids,
        ))
    return problems


# --- what the views call ----------------------------------------------------------

def _problems_of(card, records, today: date, now) -> list[Problem]:
    if card.status == STATUS_GHOST or card.id not in records.cards:
        return []
    person = records.person(card)
    problems = []
    problems += _double_standing_orders(person, card, records)
    problems += _double_charges(person, card, records, today)
    problems += _declined_recorded_paid(person, card, records)
    problems += _stuck_charges(person, card, records, now)
    problems += _signup_declined(person, card, records, now)
    problems += _unpaid(person, card, records, today)
    problems += _status_problem(card, records, today)
    problems += _duplicate_cards(person, card, records)
    problems.sort(key=lambda problem: ORDER.index(problem.code))
    return problems


def _visible_to(problems: list[Problem], branch_ids) -> list[Problem]:
    """A partner sees a problem about the child as a whole, or about a lesson in their branches."""
    if branch_ids is None:
        return problems
    allowed = set(branch_ids)
    return [problem for problem in problems if not problem.branch_ids or problem.branch_ids & allowed]


def problems_for_children(children, *, branch_ids=None, today: date | None = None, now=None) -> dict:
    """
    {child id: [Problem, …]} for these children — an entry for every one of
    them, empty when nothing is wrong.

    `branch_ids` is a partner's branches (None for a manager).
    """
    children = list(children)
    today = today or _today()
    now = now or timezone.now()
    records = _load(children)
    cards = [records.cards[child.id] for child in children if child.id in records.cards]

    # Only a card whose status disagrees needs its history read.
    disagreeing = [card.id for card in cards if _status_mismatch(card, records, today) is not None]
    _load_hand_set(records, disagreeing)

    found = {child.id: [] for child in children}
    for card in cards:
        found[card.id] = _visible_to(_problems_of(card, records, today, now), branch_ids)
    return found


def child_ids_with_problems(children, *, branch_ids=None, code: str | None = None) -> list:
    """
    The ids, out of these children, that have at least one problem — for the
    list's filter. With `code`, only a problem of that kind counts (the list of
    people to phone: SIGNUP_DECLINED).
    """
    return [
        child_id for child_id, problems in problems_for_children(children, branch_ids=branch_ids).items()
        if (any(problem.code == code for problem in problems) if code else problems)
    ]


def child_problem_detail(child, *, branch_ids=None) -> dict:
    """
    What the child's card shows: every problem in full, and the child's other
    cards — whose charges and documents the card lists beside its own.
    """
    today, now = _today(), timezone.now()
    records = _load([child])
    card = records.cards.get(child.id)
    if card is None:
        return {'problems': [], 'duplicate_cards': []}
    if _status_mismatch(card, records, today) is not None:
        _load_hand_set(records, [card.id])
    problems = _visible_to(_problems_of(card, records, today, now), branch_ids)
    others = [_card_summary(other, records) for other in records.person(card) if other.id != card.id]
    return {
        'problems': [problem.as_dict() for problem in problems],
        'duplicate_cards': others,
    }


def list_fields(problems: list[Problem]) -> dict:
    """The light pair a list row carries: how many, and their short titles (each once)."""
    titles = list(dict.fromkeys(problem.title for problem in problems))
    return {'problems_count': len(problems), 'problem_titles': titles}
