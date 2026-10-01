"""
Money about to come in: what the card company transfers to the bank.

The rule (owner, 1.10.2026): on CARD_PAYOUT_DAY (the 6th) of every month the
card company transfers everything that was charged by card from the 1st to the
last day of the calendar month before it. On 6.10 arrives what was charged on
1–30.9. The 6th itself still belongs to "this month's transfer".

Two figures, shown side by side and never merged:

  * ours — the card money the CRM recorded for the month, gross less refunds,
    by source and by branch (our_card_money). This is the figure a branch
    filter narrows.
  * Tranzila's — each terminal's month as its own report has it, read on
    demand and kept as a snapshot (refresh_terminal_month). It is what the
    card company will really transfer, before clearing fees, and it has no
    branch in it.

The month is the Israeli calendar month on both sides. Sums are gross: clearing
fees are not known here, and the screen says "לפני עמלות".

Nothing here charges, refunds or writes to Tranzila: the only outside call is
the report (/v1/transactions), which only reads.

What each source can and cannot tell is written on its function below — where
"card only" or the day of payment could not be isolated, it says so rather than
guess.
"""
from __future__ import annotations

import calendar
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import BooleanField, Case, Count, Q, Sum, Value, When
from django.utils import timezone

from apps.core.tranzila_service import (
    CHARGE_TRANMODES,
    TranzilaService,
    is_mock_credential,
    is_tranzila_approved,
    report_transaction_amount,
)

logger = logging.getLogger(__name__)

IL_TZ = ZoneInfo('Asia/Jerusalem')
ZERO = Decimal('0.00')
CENT = Decimal('0.01')

NOTE = 'לפני עמלות סליקה. חברת האשראי מעבירה ב־{day} לכל חודש את מה שנגבה באשראי בחודש הקודם.'


class PayoutError(ValueError):
    """A request that cannot be answered: an unknown terminal, a month that has not started."""


# ---------------------------------------------------------------- the rule

def payout_day() -> int:
    """The day of the month the money arrives. Kept inside 1–28 so it exists in every month."""
    try:
        day = int(getattr(settings, 'CARD_PAYOUT_DAY', 6))
    except (TypeError, ValueError):
        day = 6
    return min(max(day, 1), 28)


def add_months(month: date, count: int) -> date:
    """The first day of the month `count` months from `month`'s."""
    index = month.year * 12 + (month.month - 1) + count
    return date(index // 12, index % 12 + 1, 1)


def month_label(month: date) -> str:
    """'ספטמבר 2026'."""
    from apps.documents.period_report import HEBREW_MONTHS

    return f'{HEBREW_MONTHS[month.month - 1]} {month.year}'


@dataclass(frozen=True)
class Payout:
    """One transfer: the month that was charged, and the day its money arrives."""

    month: date          # the first day of the month the charges were made in
    period_start: date
    period_end: date
    payout_date: date

    @property
    def label(self) -> str:
        return month_label(self.month)

    def is_closed(self, today: date) -> bool:
        """The month is over: nothing more is charged into it, the sum is final."""
        return today > self.period_end


def payout_for_month(month: date) -> Payout:
    """The transfer that carries what was charged in `month`: the payout day of the month after it."""
    start = month.replace(day=1)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return Payout(
        month=start,
        period_start=start,
        period_end=end,
        payout_date=add_months(start, 1).replace(day=payout_day()),
    )


def upcoming_payout(today: date) -> Payout:
    """
    The next transfer as of `today`. Up to and including the payout day it is
    this month's transfer, for last month (closed, final). After it, it is next
    month's transfer, for the month we are in (open, "so far").
    """
    this_month = today.replace(day=1)
    if today.day <= payout_day():
        return payout_for_month(add_months(this_month, -1))
    return payout_for_month(this_month)


def parse_month(raw) -> Optional[date]:
    """'2026-09' → date(2026, 9, 1); None when it is not a month."""
    try:
        return datetime.strptime(str(raw or '').strip(), '%Y-%m').date()
    except ValueError:
        return None


def israel_today() -> date:
    return timezone.now().astimezone(IL_TZ).date()


def _month_bounds(month: date) -> tuple[datetime, datetime]:
    """The month as aware moments in Israel: [first day 00:00, first day of the next month 00:00)."""
    start = datetime(month.year, month.month, 1, tzinfo=IL_TZ)
    nxt = add_months(month, 1)
    return start, datetime(nxt.year, nxt.month, 1, tzinfo=IL_TZ)


# ---------------------------------------------------------------- Tranzila's report

# The four terminals the business charges on, each with what runs on it. The
# rentals' own key set (RENTAL_TRANZILA_*), when it points at a terminal that
# is none of these, is not known to TranzilaService.for_terminal and so cannot
# be read here: its money is then in "ours" and missing from Tranzila's side.
TERMINAL_SETTINGS = (
    ('TRANZILA_PROD_TERMINAL', 'כרטיס מוקלד — הרשמות וחיובים מהמשרד'),
    ('TRANZILA_PROD_TOKEN_TERMINAL', 'הוראות קבע וכרטיסים שמורים'),
    ('TRANZILA_TERMINAL', 'עמוד התשלום — חנות, קישורים והאתר של מיכל קגן'),
    ('TRANZILA_TOKEN_TERMINAL', 'כרטיסים שמורים מעמוד התשלום'),
)

# A range "longer than about a month" came back empty from /v1/transactions
# (TranzilaService.find_transaction, 23.9.2026), with nothing to tell it from a
# month with no transactions. So a month is never asked for in one piece: it
# is read in windows of this many days, and their rows added up.
REPORT_WINDOW_DAYS = 10
# Pages of 1,000 rows per window: 20,000 rows in ten days, far above the
# busiest terminal's month. Reaching it marks the read incomplete.
REPORT_MAX_PAGES = 20


def _is_placeholder(name: str) -> bool:
    return is_mock_credential(name) or name.lower().startswith('mock')


def payout_terminals() -> list[dict]:
    """
    The configured terminals, each once, with a Hebrew label. Two settings that
    name the same terminal give one row (the first's label); an empty or
    placeholder value gives none.
    """
    out, seen = [], set()
    for setting, label in TERMINAL_SETTINGS:
        name = str(getattr(settings, setting, '') or '').strip()
        if not name or _is_placeholder(name) or name in seen:
            continue
        seen.add(name)
        out.append({'terminal': name, 'label': label})
    return out


def classify_report_row(row) -> Optional[str]:
    """
    What a /v1/transactions row did to the money: 'charge', 'refund' or None.

    A charge is tranmode A or AK (a charge, a charge that also made a token)
    that the card company approved. A refund is a tranmode that starts with C
    (C5 on the real rows of 1.10.2026), approved. Everything else moved no
    money: D (a charge cancelled the same day, code 800), N (a card check),
    V, K, and anything declined.
    """
    if not isinstance(row, dict):
        return None
    if not is_tranzila_approved(row.get('processor_response_code')):
        return None
    mode = str(row.get('tranmode') or '').strip().upper()
    if mode in CHARGE_TRANMODES:
        return 'charge'
    if mode.startswith('C'):
        return 'refund'
    return None


def _report_row_day(row: dict) -> Optional[date]:
    try:
        return datetime.strptime(str(row.get('transaction_date') or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _agorot(raw) -> Optional[Decimal]:
    try:
        return (Decimal(str(raw).strip()) / 100).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def _payments_number(row: dict) -> int:
    try:
        return int(str(row.get('number_of_payments') or '0').strip() or 0)
    except ValueError:
        return 0


def summarise_report_rows(rows: Iterable[dict], month: date) -> dict:
    """
    A month's report rows as the snapshot's sums. A row counts once (by its
    `index`), and only when its transaction_date — Israel time — is inside the
    month; a row with no readable date is taken as the window's.

    A charge with number_of_payments above 1 does not arrive whole in one
    transfer, so those are also kept apart: how many, their full sum, and the
    sum of their first payments. first_payment_amount is read in agorot like
    `amount`; no real instalment row was seen yet (1.10.2026), so that unit is
    an assumption to check against the first one.
    """
    start, end = payout_for_month(month).period_start, payout_for_month(month).period_end
    sums = {
        'charges_total': ZERO, 'charges_count': 0,
        'refunds_total': ZERO, 'refunds_count': 0,
        'installments_count': 0, 'installments_total': ZERO, 'installments_first_total': ZERO,
    }
    seen = set()
    for row in rows:
        kind = classify_report_row(row)
        if kind is None:
            continue
        index = str(row.get('index') or row.get('transaction_index') or '').strip()
        if index:
            if index in seen:
                continue
            seen.add(index)
        day = _report_row_day(row)
        if day is not None and not (start <= day <= end):
            continue
        amount = report_transaction_amount(row)
        if amount is None:
            continue
        if kind == 'refund':
            sums['refunds_total'] += abs(amount)
            sums['refunds_count'] += 1
            continue
        sums['charges_total'] += amount
        sums['charges_count'] += 1
        if _payments_number(row) > 1:
            sums['installments_count'] += 1
            sums['installments_total'] += amount
            first = _agorot(row.get('first_payment_amount'))
            if first is not None:
                sums['installments_first_total'] += first
    return sums


def report_windows(month: date, today: date) -> list[tuple[date, date]]:
    """The month in windows of REPORT_WINDOW_DAYS, up to `today` for a month still open."""
    payout = payout_for_month(month)
    last = min(payout.period_end, today)
    windows, start = [], payout.period_start
    while start <= last:
        end = min(start + timedelta(days=REPORT_WINDOW_DAYS - 1), last)
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def our_charges_on_terminal(terminal: str, month: date) -> int:
    """
    How many successful charges our own records hold on `terminal` in `month`.

    A row with no terminal is one of the production (michal) pair — every card
    saved before 25.9.2026 — and cannot say which of the two, so it counts for
    both. Used only to tell an empty report from a report closed to our key.
    """
    from apps.customers.models import TranzilaTransaction

    start, end = _month_bounds(month)
    production = {
        str(getattr(settings, 'TRANZILA_PROD_TERMINAL', '') or '').strip(),
        str(getattr(settings, 'TRANZILA_PROD_TOKEN_TERMINAL', '') or '').strip(),
    }
    names = [terminal]
    if terminal in production:
        names.append('')
    return (
        TranzilaTransaction.objects
        .filter(is_successful=True, tranzila_terminal__in=names,
                response_timestamp__gte=start, response_timestamp__lt=end)
        .exclude(transaction_type='refund')
        .exclude(transaction_id='')
        .count()
    )


def refresh_terminal_month(terminal: str, month: date, *, today: Optional[date] = None):
    """
    Read one terminal's month from Tranzila's report and keep its sums.

    Returns the CardPayoutTerminalMonth row. A window that failed, or was not
    read to its end, leaves `complete` False with the reason in `error`: the
    sums are then only what was read. Raises PayoutError for a terminal that is
    not one of the configured four, and for a month that has not started.
    """
    from apps.core.models import CardPayoutTerminalMonth

    terminal = str(terminal or '').strip()
    if terminal not in {entry['terminal'] for entry in payout_terminals()}:
        raise PayoutError(f'המסוף "{terminal}" אינו אחד מהמסופים המוגדרים במערכת')
    month = month.replace(day=1)
    today = today or israel_today()
    if month > today:
        raise PayoutError('החודש הזה עוד לא התחיל')

    service = TranzilaService.for_terminal(terminal)
    rows: list[dict] = []
    errors: list[str] = []
    complete = service is not None
    if service is None:
        errors.append('אין במערכת מפתחות למסוף הזה')
    for start, end in (report_windows(month, today) if service is not None else []):
        label = f'{start:%d/%m}–{end:%d/%m}'
        try:
            response = service.list_all_transactions(start, end, max_pages=REPORT_MAX_PAGES)
        except Exception as exc:  # noqa: BLE001 — a failed read is recorded, never raised into the screen
            logger.exception('Card payout: report of %s %s failed', terminal, label)
            response = {'success': False, 'error': str(exc)}
        if not isinstance(response, dict) or not response.get('success'):
            complete = False
            reason = response.get('error') if isinstance(response, dict) else ''
            errors.append(f'{label}: {reason or "טרנזילה לא החזירה את הדוח"}')
            continue
        rows.extend(response.get('transactions') or [])
        if response.get('complete') is not True:
            complete = False
            errors.append(f'{label}: הדוח לא נקרא עד סופו')

    summary = summarise_report_rows(rows, month)
    if complete and not rows:
        # An empty report is believed only when we hold no charge of our own
        # on this terminal in the month. On 1.10.2026 Tranzila answered the
        # michal terminals with an empty list and no error while hundreds of
        # charges had been made on them that month: a report that is closed to
        # our key looks exactly like a month with no transactions.
        ours = our_charges_on_terminal(terminal, month)
        if ours:
            complete = False
            errors.append(
                f'טרנזילה החזירה דוח ריק, אבל אצלנו רשומים {ours} חיובים מוצלחים במסוף הזה בחודש הזה — '
                'כנראה שדוח העסקאות לא פתוח למפתח שבידינו. לבקש מטרנזילה לפתוח אותו.'
            )

    snapshot, _ = CardPayoutTerminalMonth.objects.update_or_create(
        terminal=terminal,
        month=month,
        defaults={
            **summary,
            'complete': complete,
            'error': ' · '.join(errors)[:2000],
            'fetched_at': timezone.now(),
        },
    )
    logger.info('Card payout: %s %s read — %s rows, complete=%s', terminal, f'{month:%Y-%m}', len(rows), complete)
    return snapshot


def snapshot_payload(entry: dict, snapshot) -> dict:
    """One terminal's row for the screen; `snapshot` None is a month never read."""
    if snapshot is None:
        return {
            **entry, 'charges': None, 'refunds': None, 'net': None, 'count': None,
            'refunds_count': None, 'installments_total': None, 'installments_count': None,
            'installments_first_total': None, 'complete': False, 'error': '', 'fetched_at': None,
        }
    return {
        **entry,
        'charges': float(snapshot.charges_total),
        'refunds': float(snapshot.refunds_total),
        'net': float(snapshot.charges_total - snapshot.refunds_total),
        'count': snapshot.charges_count,
        'refunds_count': snapshot.refunds_count,
        'installments_total': float(snapshot.installments_total),
        'installments_count': snapshot.installments_count,
        'installments_first_total': float(snapshot.installments_first_total),
        'complete': snapshot.complete,
        'error': snapshot.error,
        'fetched_at': snapshot.fetched_at.isoformat(),
    }


def tranzila_summary(month: date, ours_total: Decimal) -> dict:
    """
    Tranzila's side of the month, from the snapshots alone (no call out).

    `total` adds up whatever was read. `complete` is true only when every
    terminal was read to its end, and only then is there a `gap` (Tranzila less
    ours): a gap against a partial read would be a number that means nothing.
    """
    from apps.core.models import CardPayoutTerminalMonth

    entries = payout_terminals()
    snapshots = {
        row.terminal: row
        for row in CardPayoutTerminalMonth.objects.filter(
            month=month.replace(day=1), terminal__in=[entry['terminal'] for entry in entries],
        )
    }
    terminals = [snapshot_payload(entry, snapshots.get(entry['terminal'])) for entry in entries]
    read = [snapshots[entry['terminal']] for entry in entries if entry['terminal'] in snapshots]
    total = sum((row.charges_total - row.refunds_total for row in read), ZERO)
    complete = bool(entries) and len(read) == len(entries) and all(row.complete for row in read)
    return {
        'terminals': terminals,
        'total': float(total),
        'installments_total': float(sum((row.installments_total for row in read), ZERO)),
        'complete': complete,
        'gap': float(total - ours_total) if complete else None,
    }


# ---------------------------------------------------------------- ours (the DB)

SOURCE_COURSES = 'courses'
SOURCE_STORE = 'store'
SOURCE_LINKS = 'links'
SOURCE_RENTALS = 'rentals'
SOURCE_MANUAL = 'manual_documents'
SOURCE_MICHAL = 'michal'

SOURCES = (
    (SOURCE_COURSES, 'חוגים'),
    (SOURCE_STORE, 'חנות'),
    (SOURCE_LINKS, 'קישורי תשלום וגבייה עסקית'),
    (SOURCE_RENTALS, 'השכרות'),
    (SOURCE_MANUAL, 'מסמכים ידניים באשראי'),
    (SOURCE_MICHAL, 'האתר של מיכל קגן'),
)

# What the owner should know about a source's figure: where the CRM cannot
# tell the whole story, the row says so instead of looking certain.
SOURCE_NOTES = {
    SOURCE_LINKS: 'זיכוי של תשלום בקישור נעשה בטרנזילה ואינו רשום במערכת.',
    SOURCE_RENTALS: 'זיכוי של שכירות נעשה בטרנזילה ואינו רשום במערכת.',
    SOURCE_MANUAL: (
        'קבלות וחשבוניות מס/קבלה שהופקו ביד עם שורת אשראי. המערכת לא יודעת באיזה מסוף נגבה הכסף, '
        'וייתכן כפל אם המסמך הופק על חיוב שכבר רשום כאן.'
    ),
    SOURCE_MICHAL: 'לפי תאריך המסמך שהופק לתשלום, לא לפי רגע החיוב באתר שלה.',
}

NO_BRANCH_LABEL = 'ללא סניף'

# Statuses of a charge whose money moved: paid, and paid then refunded (the
# refund is counted on its own day, so the charge stays in its month).
PAYMENT_CHARGED_STATUSES = ('completed', 'refunded')
STORE_PAID_STATUSES = ('completed', 'refunded', 'refund_failed')

PAYMENT_REFUND_KEYS = ('refund_claim_payment_', 'refund_payment_')
STORE_REFUND_KEYS = ('refund_claim_store_', 'refund_store_')


class _Ledger:
    """Charges and refunds by (source, branch). A branch of None is "no branch"."""

    def __init__(self, allowed: Optional[set]):
        self.allowed = allowed
        self.cells: dict = defaultdict(lambda: {'charges': ZERO, 'refunds': ZERO, 'count': 0})

    def keeps(self, branch_id) -> bool:
        if self.allowed is None:
            return True
        return branch_id is not None and str(branch_id) in self.allowed

    def add(self, source: str, branch_id, *, charges=ZERO, refunds=ZERO, count: int = 0) -> None:
        if not self.keeps(branch_id):
            return
        cell = self.cells[(source, str(branch_id) if branch_id else None)]
        cell['charges'] += Decimal(charges or 0)
        cell['refunds'] += Decimal(refunds or 0)
        cell['count'] += count


def _money(raw) -> Optional[Decimal]:
    try:
        return Decimal(str(raw).strip()).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def _keyed_id(key: str, prefixes: tuple) -> Optional[str]:
    """The UUID a refund row's key carries: 'refund_claim_payment_<uuid>' or the older 'refund_payment_<uuid>_<txn>'."""
    for prefix in prefixes:
        if key.startswith(prefix):
            raw = key[len(prefix):len(prefix) + 36]
            try:
                return str(uuid.UUID(raw))
            except ValueError:
                return None
    return None


def _refund_rows(prefixes: tuple, start: datetime, end: datetime) -> list[tuple[str, Optional[Decimal]]]:
    """
    The refunds Tranzila confirmed in the month, as (id of what was refunded,
    the sum sent). The row is the refund's own record (payment_service
    _claim_refund / _settle_refund_claim, and the row older code wrote), dated
    by Tranzila's answer. A claim still unanswered is not a refund yet.
    """
    from apps.customers.models import TranzilaTransaction

    by_key = Q()
    for prefix in prefixes:
        by_key |= Q(idempotency_key__startswith=prefix)
    rows = (
        TranzilaTransaction.objects
        .filter(by_key, transaction_type='refund', is_successful=True)
        .filter(
            Q(response_timestamp__gte=start, response_timestamp__lt=end)
            | Q(response_timestamp__isnull=True, created_at__gte=start, created_at__lt=end)
        )
        .values_list('idempotency_key', 'request_data')
    )
    out = []
    for key, request_data in rows:
        target = _keyed_id(key, prefixes)
        if target:
            out.append((target, _money((request_data or {}).get('amount'))))
    return out


def _add_payments(ledger: _Ledger, start: datetime, end: datetime) -> None:
    """
    Courses, and one-time charges taken through a card link.

    Card only: a Payment exists only for a card charge through Tranzila — the
    model has no payment method, and course money in cash or checks lives in
    CashPlan / CheckPlan documents, never here. A signup that only checked the
    card (no first charge) is a Payment of ₪0 and adds nothing. A store
    purchase put on the standing order is inside the month's Payment, which is
    why monthly_billing store invoices are not counted in the store.

    The month: payment_date, written the moment Tranzila said yes.

    Refunds: the refund's own row, on the day Tranzila confirmed it, for the
    sum that was sent — a partial refund marks the whole Payment 'refunded',
    so the status alone would overstate it. Not certain: a Payment marked
    'refunded' with no refund row behind it (changed by hand) is not deducted.
    """
    from apps.customers.models import Payment

    is_link = Case(
        When(lesson__isnull=True, card_link__isnull=False, then=Value(True)),
        default=Value(False), output_field=BooleanField(),
    )
    charged = Payment.objects.filter(
        status__in=PAYMENT_CHARGED_STATUSES, payment_date__gte=start, payment_date__lt=end, final_amount__gt=0,
    )
    if ledger.allowed is not None:
        charged = charged.filter(branch_id__in=ledger.allowed)
    for row in charged.annotate(is_link=is_link).values('branch_id', 'is_link').annotate(
        amount=Sum('final_amount'), rows=Count('id'),
    ):
        ledger.add(
            SOURCE_LINKS if row['is_link'] else SOURCE_COURSES, row['branch_id'],
            charges=row['amount'] or ZERO, count=row['rows'],
        )

    refunds = _refund_rows(PAYMENT_REFUND_KEYS, start, end)
    if not refunds:
        return
    payments = {
        str(row['id']): row
        for row in Payment.objects.filter(id__in=[target for target, _ in refunds])
        .annotate(is_link=is_link).values('id', 'branch_id', 'final_amount', 'is_link')
    }
    for target, amount in refunds:
        payment = payments.get(target)
        if payment is None:
            continue
        ledger.add(
            SOURCE_LINKS if payment['is_link'] else SOURCE_COURSES, payment['branch_id'],
            refunds=amount if amount is not None else payment['final_amount'],
        )


def _split(amount: Decimal, weights: dict) -> dict:
    """`amount` shared out by `weights`, to the agora, with the rounding left on the largest share."""
    total = sum(weights.values(), ZERO)
    if total <= 0:
        return {}
    shares = {
        key: (amount * weight / total).quantize(CENT, rounding=ROUND_HALF_UP)
        for key, weight in weights.items()
    }
    largest = max(shares, key=lambda key: shares[key])
    shares[largest] += amount - sum(shares.values(), ZERO)
    return shares


def _store_weights(invoice) -> dict:
    """
    Where a store invoice's money belongs: each sale line to its own branch (a
    website delivery line has none), and whatever the invoice charged beyond
    its lines — the delivery fee — to the invoice's branch.
    """
    weights: dict = defaultdict(lambda: ZERO)
    for sale in invoice.line_items.all():
        weights[sale.branch_id] += sale.total_price
    rest = invoice.total_amount - sum(weights.values(), ZERO)
    if rest > 0:
        weights[invoice.branch_id] += rest
    return dict(weights)


def _store_paid_at(invoice) -> datetime:
    """
    When a store invoice was paid, as near as the CRM knows it: when the
    payment was first reported (hosted page, from 29.9.2026), else when its
    sale lines were written — they are written with the payment — else when
    the invoice was made. Not Tranzila's own timestamp: a payment made in the
    last minutes of a month and recorded after midnight lands in the next one.
    """
    if invoice.payment_reported_at:
        return invoice.payment_reported_at
    sold = [sale.sale_date for sale in invoice.line_items.all() if sale.sale_date]
    return min(sold) if sold else invoice.issue_date


def _add_store(ledger: _Ledger, start: datetime, end: datetime) -> None:
    """
    Store sales paid by card: the till (a typed card, the child's saved card)
    and the hosted page.

    Card only: payment_method 'credit_card'. Cash is not card money, and
    'monthly_billing' is charged inside the standing order's Payment (see
    _add_payments). Only invoices that were paid: a website order still
    'pending' took no money here.

    Refunds: the refund's own row (refund_store_invoice), on the day Tranzila
    confirmed it, shared between branches as the invoice's money was.
    Not counted: a second charge kept beside an order (other_transactions) —
    money that moved at Tranzila and is not on the invoice.
    """
    from apps.store.models import StoreInvoice

    in_month = lambda field: Q(**{f'{field}__gte': start, f'{field}__lt': end})  # noqa: E731
    invoices = (
        StoreInvoice.objects
        .filter(payment_method='credit_card', payment_status__in=STORE_PAID_STATUSES)
        .filter(in_month('issue_date') | in_month('payment_reported_at') | in_month('line_items__sale_date'))
        .distinct()
        .prefetch_related('line_items')
    )
    for invoice in invoices:
        if not (start <= _store_paid_at(invoice) < end):
            continue
        weights = _store_weights(invoice) or {invoice.branch_id: invoice.total_amount}
        counted = False
        for branch_id, amount in _split(invoice.total_amount, weights).items():
            if ledger.keeps(branch_id):
                ledger.add(SOURCE_STORE, branch_id, charges=amount, count=0 if counted else 1)
                counted = True

    refunds = _refund_rows(STORE_REFUND_KEYS, start, end)
    if not refunds:
        return
    refunded = {
        str(invoice.id): invoice
        for invoice in StoreInvoice.objects.filter(id__in=[target for target, _ in refunds])
        .prefetch_related('line_items')
    }
    for target, amount in refunds:
        invoice = refunded.get(target)
        if invoice is None:
            continue
        if amount is None:
            amount = invoice.refunded_amount or invoice.total_amount
        weights = _store_weights(invoice) or {invoice.branch_id: invoice.total_amount}
        for branch_id, share in _split(amount, weights).items():
            ledger.add(SOURCE_STORE, branch_id, refunds=share)


def _add_payment_links(ledger: _Ledger, start: datetime, end: datetime) -> None:
    """
    Payment links and one-time business charges (PaymentLinkPayment).

    Card only: every such payment is taken on Tranzila's hosted page. Counted
    here whether or not a document was issued for it — and that document is
    then left out of the manual documents (_add_documents), so the money is
    counted once. The month: paid_at. The branch: the link's.

    Refunds: none are recorded — a link payment has no refund status, the
    refund is made at Tranzila. Such a refund is part of the gap. So is a
    payment left in 'review' (the report disagreed with the page): it is not
    counted until the office settles it.
    """
    from apps.payment_links.models import PaymentLinkPayment

    paid = PaymentLinkPayment.objects.filter(
        status=PaymentLinkPayment.STATUS_COMPLETED, paid_at__gte=start, paid_at__lt=end,
    )
    if ledger.allowed is not None:
        paid = paid.filter(link__branch_id__in=ledger.allowed)
    for row in paid.values('link__branch_id').annotate(total=Sum('amount'), rows=Count('id')):
        ledger.add(SOURCE_LINKS, row['link__branch_id'], charges=row['total'] or ZERO, count=row['rows'])


def _add_rentals(ledger: _Ledger, start: datetime, end: datetime) -> None:
    """
    Tenants' monthly charges (TenantCharge, sums in agorot).

    Card only: a month paid at the office in cash, by check or by transfer is
    'charged' too — what tells it apart is its receipt's payment line
    (rental_billing.offline), so a month whose receipt carries such a line is
    left out. The RT receipt of a card month is left out of the manual
    documents, so the money is counted once.

    The month: charged_at. The branch: the standing order's.

    Refunds: none are recorded — a rental charge is refunded at Tranzila,
    never from the CRM. Not counted either: a month Tranzila charged after it
    was voided or paid at the office (billing.charged_after_void). Tenant
    billing may run on its own terminal (RENTAL_TRANZILA_*), which Tranzila's
    side here does not read.
    """
    from apps.rental_billing.models import TenantCharge
    from apps.rental_billing.receipts import OFFLINE_METHODS

    charged = (
        TenantCharge.objects
        .filter(status=TenantCharge.STATUS_CHARGED, charged_at__gte=start, charged_at__lt=end)
        .exclude(receipt__payments__payment_method__in=OFFLINE_METHODS)
    )
    if ledger.allowed is not None:
        charged = charged.filter(standing_order__branch_id__in=ledger.allowed)
    for row in charged.values('standing_order__branch_id').annotate(total=Sum('total'), rows=Count('id')):
        ledger.add(
            SOURCE_RENTALS, row['standing_order__branch_id'],
            charges=(Decimal(row['total'] or 0) / 100).quantize(CENT), count=row['rows'],
        )


def _add_documents(ledger: _Ledger, month: date) -> None:
    """
    Card lines on documents: Michal Kagan's site, and documents issued by hand.

    Card only: DocumentPayment rows whose method is 'credit_card', on a receipt
    or a חשבונית מס/קבלה (never a draft). Left out, because their money is
    counted at its source: a rental's RT receipt, a document issued for a
    payment-link payment, and a store sale's document.

    Michal Kagan's site (documents marked 'michal-payment:') is its own source,
    with no branch: her payments are taken on the shared hosted-page terminal,
    so Tranzila's report includes them. Her refunds are the credit notes her
    site asked for ('michal-refund:'), on the credit note's date.

    The month: the payment line's paid_on when it has one, else the document's
    date. Not certain, and not guessed at:
      * her documents keep no payment day — one issued late (after midnight, or
        days later) falls in the month it was issued in;
      * a document typed by hand says "card" and nothing about the terminal:
        the charge may be on a terminal read here, on another device, or one
        the CRM already recorded (a receipt typed for a course charge) — then
        it is counted twice. Nothing on the document can tell;
      * a credit note typed by hand names no means of payment, so it is never
        taken as a card refund.
    """
    from apps.documents.michal.service import PAYMENT_MARK, REFUND_MARK
    from apps.documents.models import DocumentPayment, FormalDocument

    payout = payout_for_month(month)
    first, last = payout.period_start, payout.period_end
    is_michal = Case(
        When(document__internal_notes__startswith=PAYMENT_MARK, then=Value(True)),
        default=Value(False), output_field=BooleanField(),
    )
    lines = (
        DocumentPayment.objects
        .filter(payment_method='credit_card', document__document_type__in=('receipt', 'combined'))
        .filter(
            Q(paid_on__gte=first, paid_on__lte=last)
            | Q(paid_on__isnull=True, document__document_date__gte=first, document__document_date__lte=last)
        )
        .exclude(document__tenant_charge__isnull=False)
        .exclude(document__payment_link_payments__isnull=False)
        .exclude(document__store_invoices__isnull=False)
    )
    if ledger.allowed is not None:
        lines = lines.filter(document__branch_id__in=ledger.allowed)
    for row in lines.annotate(is_michal=is_michal).values('document__branch_id', 'is_michal').annotate(
        total=Sum('amount'), rows=Count('id'),
    ):
        ledger.add(
            SOURCE_MICHAL if row['is_michal'] else SOURCE_MANUAL, row['document__branch_id'],
            charges=row['total'] or ZERO, count=row['rows'],
        )

    credits = FormalDocument.objects.filter(
        document_type='credit_invoice', internal_notes__startswith=REFUND_MARK,
        document_date__gte=first, document_date__lte=last,
    )
    if ledger.allowed is not None:
        credits = credits.filter(branch_id__in=ledger.allowed)
    for row in credits.values('branch_id').annotate(total=Sum('total_amount')):
        ledger.add(SOURCE_MICHAL, row['branch_id'], refunds=row['total'] or ZERO)


def our_card_money(month: date, branch_ids=None) -> dict:
    """
    The card money the CRM recorded for `month`: gross charges less refunds,
    by source and by branch, each shekel once.

    `branch_ids` None is the whole company; money that belongs to no branch is
    then its own line (`no_branch`). With a list, only those branches are
    counted and money with no branch is left out — a partner never sees it.
    An empty list is nothing.

    Charges whose answer from Tranzila is unknown (processing, uncertain,
    review) are not counted: they may be in Tranzila's report, and are part of
    the gap.
    """
    from apps.core.models import Branch

    month = month.replace(day=1)
    allowed = None if branch_ids is None else {str(branch_id) for branch_id in branch_ids}
    ledger = _Ledger(allowed)
    if allowed is None or allowed:
        start, end = _month_bounds(month)
        _add_payments(ledger, start, end)
        _add_store(ledger, start, end)
        _add_payment_links(ledger, start, end)
        _add_rentals(ledger, start, end)
        _add_documents(ledger, month)

    def line(cells) -> dict:
        charges = sum((cell['charges'] for cell in cells), ZERO)
        refunds = sum((cell['refunds'] for cell in cells), ZERO)
        return {'charges': charges, 'refunds': refunds, 'net': charges - refunds}

    by_source = []
    for key, label in SOURCES:
        cells = [cell for (source, _), cell in ledger.cells.items() if source == key]
        by_source.append({
            'key': key, 'label': label, **line(cells),
            'count': sum(cell['count'] for cell in cells),
            'note': SOURCE_NOTES.get(key, ''),
        })

    branch_keys = {branch for (_, branch) in ledger.cells if branch}
    names = {str(branch.id): branch.name for branch in Branch.objects.filter(id__in=branch_keys)}
    by_branch = [
        {
            'branch_id': branch, 'branch_name': names.get(branch, NO_BRANCH_LABEL),
            **line([cell for (_, key), cell in ledger.cells.items() if key == branch]),
        }
        for branch in branch_keys
    ]
    by_branch.sort(key=lambda row: (-row['net'], row['branch_name']))

    no_branch = None
    if allowed is None:
        no_branch = line([cell for (_, key), cell in ledger.cells.items() if key is None])

    return {**line(list(ledger.cells.values())), 'by_source': by_source, 'by_branch': by_branch, 'no_branch': no_branch}


# ---------------------------------------------------------------- the screen's answer

def _floats(row: dict) -> dict:
    return {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}


def _period(payout: Payout) -> dict:
    return {'start': payout.period_start.isoformat(), 'end': payout.period_end.isoformat(), 'label': payout.label}


def incoming_money(month: Optional[date], *, today: date, branch_ids=None, with_tranzila: bool = False) -> dict:
    """
    What GET core/dashboard/incoming/ answers. `month` None is the upcoming
    transfer's month. `with_tranzila` adds Tranzila's side — for a manager
    looking at the whole company only; the snapshots have no branch in them.
    """
    payout = payout_for_month(month) if month else upcoming_payout(today)
    ours = our_card_money(payout.month, branch_ids)
    answer = {
        'month': f'{payout.month:%Y-%m}',
        'payout_date': payout.payout_date.isoformat(),
        'period': _period(payout),
        'is_closed': payout.is_closed(today),
        'is_upcoming': payout.month == upcoming_payout(today).month,
        'ours': {
            'total': float(ours['net']),
            'charges': float(ours['charges']),
            'refunds': float(ours['refunds']),
            'by_source': [_floats(row) for row in ours['by_source']],
            'by_branch': [_floats(row) for row in ours['by_branch']],
            'no_branch': _floats(ours['no_branch']) if ours['no_branch'] is not None else None,
        },
        'note': NOTE.format(day=payout_day()),
        'next': None,
    }
    # The transfer after this one, in one line — once its month has begun.
    following = payout_for_month(add_months(payout.month, 1))
    if following.period_start <= today:
        answer['next'] = {
            'month': f'{following.month:%Y-%m}',
            'payout_date': following.payout_date.isoformat(),
            'period': _period(following),
            'total': float(our_card_money(following.month, branch_ids)['net']),
            'is_closed': following.is_closed(today),
        }
    if with_tranzila:
        answer['tranzila'] = tranzila_summary(payout.month, ours['net'])
    return answer
