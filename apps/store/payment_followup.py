"""
A store payment on Tranzila's hosted page that did not end cleanly — the CRM
half of stage 3 of the cogolive plan (the website store opens on cogolive).

The website's checkout, and the till's walk-in "secure page", pay on
Tranzila's page. Tranzila's notify completes the sale
(PaymentService.complete_store_purchase_from_webhook): the notify is public
and unsigned, so the sale happens only when the terminal's own report shows
the same transaction.

Two rules hold everything here (reviews of 29.9-30.9.2026):

  * A number not tied to the order with certainty never completes it. Only
    the report ties a number to an order: an approved charge, this sum, this
    approval number, made after the order, not paying for another. A charge
    found in the report by sum and time alone ("suspected") is tied by
    nothing — the terminal is shared with the other website — and completes
    an order only when a person brings the customer's own evidence (the
    approval number, or the card's last four digits) and the report agrees.
  * A number not decided with certainty never leaves the follow-up. Only the
    report's confirmation (a sale, or a second charge) or its definite no
    (declined, not a charge, another sum) settles one. A person's "release"
    lets the customer pay again, but the numbers it released are still asked
    about — by every notify, every returned number and every sweep — and a
    released charge the report later confirms is a sale, or a second charge
    for a refund. No reported number is ever lost: the first stays
    tranzila_transaction_id, every other one is kept in other_transactions
    with what is known about it.

An invoice that holds an undecided number Tranzila reported — or a suspected
charge — and is not settled, is IN REVIEW, whatever its status reads: it is
never failed on the word of a later notify, never handed a second payment
page, never dropped from the follow-up. A cart is sold once, whatever writes
over a status.

What can go wrong after the customer paid, and what answers it:

  1. The notify came, but the report could not be asked, or does not list
     the number yet, or disagrees. `recheck_pending_payment` asks the report
     again about every undecided number and settles what it allows through
     the same locked path as the notify (PaymentService.settle_reported_store_payment).
     Nothing here ever charges or refunds; the only call to Tranzila is the
     report read.
  2. The sale is complete, but the website never heard: `tell_website_paid`
     keeps the site's answer (website_paid_notified_at) and is repeated.
  3. A second number for the same order: kept, told to the office once per
     order as a possible double charge, recorded as a second charge once the
     report confirms it — also while the sweep's switch is off — and told as
     "a confirmed second charge, to refund". Every reported number is kept
     (up to MAX_KEPT_NUMBERS per order); more than MAX_UNDECIDED_NUMBERS
     unanswered ones are told once. What is limited is the work: one check
     reads the report at most MAX_REPORT_READS_PER_INVOICE times per order,
     the numbers taking turns (`next_to_ask`), so each is asked in the end.
  4. The notify never came at all. The site can report the number it got
     back from the page (widget/payment/returned/, `record_returned_number`),
     for an order whose page it opened in the last RETURNED_PAGE_WINDOW —
     believed exactly as much as a notify. The terminal's report is searched
     for a charge of the same sum after the order's FIRST page opened
     (`find_unreported_payment`), by a retry (a match is kept as suspected and
     blocks a second page; a report that has not caught up — the last page
     opened less than REPORT_SETTLE ago — blocks it too) and by the sweep
     (also for paid orders that opened more than one page). A match is kept
     on the invoice as suspected, whatever the sweep's switch reads, and
     stays in the brief until a person decides. A report that cannot be read
     — or was read only in part, or answered an error — is "unknown", never
     "nothing there": no second page is handed out on it, and the office is
     told a customer is waiting. An order's pages are looked for until one
     complete read made SEARCH_FINAL_AFTER after its last page
     (payment_search_done_at), at any age.
  5. Any of it stays that way: the office hears (apps/core/office_alerts.py),
     once per order and event — and again after a person's release, when
     something new happens to the order. A person settles an invoice with the
     managers' tool (`complete_reported_payment` / `release_reported_payment`
     / `close_reported_numbers`), with a reason; who and when are kept on the
     invoice.

Who drives the follow-up:
  * the website's status poll, GET /api/v1/store/widget/payment/status/
    (`website_order_status`), at most once per RECHECK_INTERVAL per invoice;
  * the morning brief's sweep, "תשלומים שנתקעו בחנות"
    (`sweep_stuck_store_payments`). With STORE_SWEEP_COMPLETES_PAYMENTS off
    (until the owner decides) it sells nothing: no sale, no status, no
    document, no email. What it keeps either way is what it learned — a
    charge found in the report (suspected), a second charge the report
    confirms on a paid order, when each number was last asked. On, it also
    completes what the report confirms, as the poll does;
  * the site asking to pay an order again (widget/payment/initiate/): every
    number the order holds — released ones too — is asked about before a
    second page leaves;
  * another notify, or a returned number, for the same invoice: the order's
    other undecided numbers are asked about too.
There is no cron of its own: a new Vercel cron is a Level-2 decision (plan
item 2.7), so between the site's polls and the morning an invoice waits.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.store.models import StoreInvoice

logger = logging.getLogger(__name__)

# The site polls every few seconds while the customer waits; Tranzila's report
# is asked about the same invoice at most this often, whoever asks.
RECHECK_INTERVAL = timedelta(seconds=15)
# A payment that is still not settled this long after Tranzila reported it —
# or a paid order the site still has not acknowledged — goes to the office.
STUCK_AFTER = timedelta(minutes=10)
# The report may lag the page by a little. A number it does not list yet is
# "not known yet" for this long after it was reported, and "not there" after;
# a retry this soon after the last page opened gets no new page on "not found".
REPORT_SETTLE = timedelta(minutes=10)
# The morning's one read of the report covers the orders whose first page
# opened this recently; an older order still not looked for gets a read of
# its own days (MAX_OLD_SEARCHES a morning). Also how far back invoices from
# before 29.9.2026 (no payment_reported_at) are swept.
SEARCH_WINDOW = timedelta(days=3)
LEGACY_WINDOW = timedelta(days=3)
MAX_OLD_SEARCHES = 3
# An order's pages are looked for in the report until one complete read made
# this long after its last page was handed out: nothing new can appear later.
SEARCH_FINAL_AFTER = timedelta(hours=24)
# Pages of the report (1000 rows each) one search may read; more than that is
# "read in part", which is "unknown".
DAY_REPORT_MAX_PAGES = 10
# The site may report a returned number only for an order whose page it
# opened this recently.
RETURNED_PAGE_WINDOW = timedelta(hours=2)
# More reported numbers than this that the report has not answered for (the
# order's own and the open ones; a charge found in the report or one a person
# released is not counted) is told to the office, once. All of them are kept.
MAX_UNDECIDED_NUMBERS = 3
# Numbers kept per order, in any state. Beyond it a new unconfirmed number is
# not kept (and the office is told); one the report confirms always is.
MAX_KEPT_NUMBERS = 50
# A transaction number is an integer on Tranzila's side; nothing longer is one.
MAX_NUMBER_DIGITS = 20
# A second page leaves only when every released number of the order was asked
# about this recently.
ASKED_FRESH = timedelta(minutes=10)
# The sweep runs inside the morning brief's request (the platform cuts a request
# at 300 seconds, and every report read may take up to 30). What it does not
# reach in time it lists as not checked, and the next morning carries on.
SWEEP_BUDGET_SECONDS = 60
# ...and no one invoice may take more than this of it.
SWEEP_INVOICE_SECONDS = 20
# Report reads one check of one invoice may make — the sweep, the poll, a
# notify, a retry, a person's decision. The numbers take turns.
MAX_REPORT_READS_PER_INVOICE = 4
# From the status poll the site is waiting on our answer, so a "paid" call to
# it made on the way gets a shorter leash than the notify's.
POLL_SITE_TIMEOUT_SECONDS = 8

# What the website reads (apps/store/widget_views.WidgetStorePaymentStatusView).
STATUS_PENDING = 'pending'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
STATUS_REFUNDED = 'refunded'

# Statuses in which the order's payment is settled: never sold again, never
# failed by a notify. (A refund comes later, by the office.)
PAID_STATUSES = ('completed', 'refunded', 'refund_failed')
# Settled and the money is with us: a refund that failed leaves it paid.
MONEY_KEPT_STATUSES = ('completed', 'refund_failed')
# Statuses in which a reported number keeps the invoice in review.
UNSETTLED_STATUSES = ('pending', 'failed')

# What the report says about one reported number.
ANSWER_VERIFIED = 'verified'   # an approved charge of this sum for this order
ANSWER_REJECTED = 'rejected'   # a definite no: declined, not a charge, or another sum
ANSWER_DISPUTED = 'disputed'   # the report disagrees, not definitely (approval, time, owner, not listed)
ANSWER_UNKNOWN = 'unknown'     # the report could not be asked, or does not list it yet
WHY_NO_ANSWER = 'הדוח של טרנזילה לא ענה'

# other_transactions[*].state
OTHER_OPEN = 'open'                    # undecided; the order is in review
OTHER_SUSPECTED = 'suspected'          # found in the report by sum and time; the order is in review
OTHER_RELEASED = 'released'            # a person found no charge; undecided, still asked, not blocking
OTHER_SECOND_CHARGE = 'second_charge'  # decided: a real second payment, for a refund
OTHER_REJECTED = 'rejected'            # decided: the report's definite no
OTHER_CLOSED = 'closed'                # decided by a person, on a paid order: not this order's
UNDECIDED_STATES = (OTHER_OPEN, OTHER_SUSPECTED, OTHER_RELEASED)
BLOCKING_STATES = (OTHER_OPEN, OTHER_SUSPECTED)

# What a recheck ended in.
RECHECK_COMPLETED = 'completed'
RECHECK_CONFIRMED = 'confirmed'      # the report confirms it; completing was not allowed here
RECHECK_PENDING = 'pending'          # asked; still in review
RECHECK_PAID = 'paid'                # a settled invoice: its further numbers were asked about
RECHECK_PACED = 'paced'              # asked less than RECHECK_INTERVAL ago
RECHECK_NOT_PENDING = 'not_pending'  # nothing undecided to ask about
RECHECK_NOT_ELIGIBLE = 'not_eligible'

# The report's dates and times are Israel's.
REPORT_TZ = ZoneInfo('Asia/Jerusalem')

WHERE_WEBSITE = 'חנות האתר'
WHERE_TILL = 'קופה — עמוד התשלום של טרנזילה'

_NUMBER = re.compile(r'[0-9]+')


# ---------------------------------------------------------------------------
# Transaction numbers
# ---------------------------------------------------------------------------

def is_transaction_number(value) -> bool:
    """ASCII digits only: str.isdigit() also passes '١٢٣', which no query of ours would ever match again."""
    return bool(_NUMBER.fullmatch(str(value or '').strip()))


def normal_number(value) -> str:
    """
    A transaction number as it is kept and asked about: ASCII digits, without
    leading zeros ('0123456' and '123456' are one transaction — the report is
    asked by its value), at most MAX_NUMBER_DIGITS. '' for anything else.
    """
    value = str(value or '').strip()
    if not _NUMBER.fullmatch(value):
        return ''
    value = value.lstrip('0')
    return value if value and len(value) <= MAX_NUMBER_DIGITS else ''


def shown_number(value) -> str:
    """A transaction number for an alert or the brief — never anything else that sat in its place."""
    value = str(value or '').strip()
    return value if is_transaction_number(value) else '(לא ידוע)'


def notify_index(tranzila_response: dict) -> str:
    """
    The transaction number a store notify reported: Tranzila's `index`, ASCII digits only.

    parse_webhook_response falls back to TranzilaTK (the card's token) when the
    POST has no index. On the store's path a token must never be kept as a
    transaction number, printed in an alert or shown in the brief, so the
    number is read from the POST itself, and from `transaction_id` only when
    there is no POST (a caller that already holds the number).
    """
    raw = tranzila_response.get('raw_payload')
    value = raw.get('index', '') if isinstance(raw, dict) else tranzila_response.get('transaction_id', '')
    return normal_number(value)


def report_card_last4(row: Optional[dict]) -> str:
    """The card's last four digits on a report row (the token ends with them), never anything more."""
    if not row:
        return ''
    for key in ('credit_card_last_4_digits', 'last_4_digits', 'card_last4', 'ccno', 'credit_card_token'):
        digits = re.sub(r'[^0-9]', '', str(row.get(key) or ''))
        if len(digits) >= 4:
            return digits[-4:]
    return ''


@dataclass(frozen=True)
class ReportedNumber:
    index: str
    code: str
    terminal: str
    reported_at: datetime
    primary: bool
    state: str = OTHER_OPEN   # for a further number: its other_transactions state
    asked_at: Optional[datetime] = None   # for a further number: when the report was last asked about it

    @property
    def suspected(self) -> bool:
        return not self.primary and self.state == OTHER_SUSPECTED

    @property
    def released(self) -> bool:
        return not self.primary and self.state == OTHER_RELEASED


def _is_till_charge(invoice: StoreInvoice) -> bool:
    """A till charge of a saved or typed card: no hosted page, and its own "uncertain" handling."""
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

    return bool(invoice.charged_with_token) or invoice.tranzila_confirmation_code == TILL_CHARGE_UNCERTAIN_MARK


def _parse_time(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    return parse_datetime(str(value or ''))


def _entry_number(invoice: StoreInvoice, entry: dict) -> ReportedNumber:
    return ReportedNumber(
        index=str(entry['index']),
        code=str(entry.get('code') or ''),
        terminal=str(entry.get('terminal') or ''),
        reported_at=_parse_time(entry.get('reported_at')) or invoice.created_at,
        primary=False,
        state=str(entry.get('state') or OTHER_OPEN),
        asked_at=_parse_time(entry.get('asked_at')) if entry.get('asked_at') else None,
    )


def _holds_own_number(invoice: StoreInvoice) -> bool:
    return (
        invoice.payment_status in UNSETTLED_STATUSES
        and not _is_till_charge(invoice)
        and is_transaction_number(invoice.tranzila_transaction_id)
    )


def open_numbers(invoice: StoreInvoice, *, include_suspected: bool = False,
                 include_released: bool = False) -> list[ReportedNumber]:
    """
    The undecided numbers of this invoice: its own (while unsettled) and the
    further open ones — the ones that keep it in review. `include_released`
    adds those a person released (still asked about, not blocking);
    `include_suspected` those found in the report (a person's decision only,
    on an unpaid order and on a paid one).
    """
    numbers = []
    if _holds_own_number(invoice):
        numbers.append(ReportedNumber(
            index=invoice.tranzila_transaction_id.strip(),
            code=(invoice.tranzila_confirmation_code or '').strip(),
            terminal=(invoice.tranzila_terminal or '').strip(),
            reported_at=invoice.payment_reported_at or invoice.created_at,
            primary=True,
        ))
    wanted = {OTHER_OPEN}
    if include_released:
        wanted.add(OTHER_RELEASED)
    if include_suspected:
        wanted.add(OTHER_SUSPECTED)
    for entry in invoice.other_transactions or []:
        if entry.get('state') in wanted and is_transaction_number(entry.get('index')):
            numbers.append(_entry_number(invoice, entry))
    return numbers


def suspected_numbers(invoice: StoreInvoice) -> list[ReportedNumber]:
    return [
        _entry_number(invoice, entry)
        for entry in invoice.other_transactions or []
        if entry.get('state') == OTHER_SUSPECTED and is_transaction_number(entry.get('index'))
    ]


def unanswered_count(invoice: StoreInvoice) -> int:
    """
    Reported numbers the report has not answered for: the order's own and the
    open ones. A charge found in the report, or a number a person released,
    is not a number somebody keeps sending.
    """
    own = 1 if _holds_own_number(invoice) else 0
    return own + sum(1 for e in invoice.other_transactions or [] if e.get('state') == OTHER_OPEN)


def has_room(invoice: StoreInvoice) -> bool:
    """Whether one more number may be kept on this order (MAX_KEPT_NUMBERS, in any state)."""
    own = 1 if is_transaction_number(invoice.tranzila_transaction_id) else 0
    return own + len(invoice.other_transactions or []) < MAX_KEPT_NUMBERS


def next_to_ask(numbers: list[ReportedNumber], limit: int) -> list[ReportedNumber]:
    """
    The numbers one check asks the report about: at most `limit`, the order's
    own first, then the ones asked longest ago (never asked before all). Each
    check stamps what it asked (`mark_asked`), so the numbers take turns and
    every one is asked in the end, however many an order holds.
    """
    never = datetime.min.replace(tzinfo=dt_timezone.utc)
    return sorted(numbers, key=lambda n: (not n.primary, n.asked_at or never))[:max(limit, 1)]


def mark_asked(invoice: StoreInvoice, indexes) -> bool:
    """Stamp the further numbers just asked about (the caller holds the row lock and saves). True when any was."""
    wanted = {str(i) for i in indexes}
    now = timezone.now().isoformat()
    entries = [dict(entry) for entry in (invoice.other_transactions or [])]
    stamped = False
    for entry in entries:
        if str(entry.get('index')) in wanted and entry.get('state') in UNDECIDED_STATES:
            entry['asked_at'] = now
            stamped = True
    if stamped:
        invoice.other_transactions = entries
    return stamped


def released_not_asked(invoice: StoreInvoice) -> list[ReportedNumber]:
    """Released numbers of this order the report was not asked about in the last ASKED_FRESH."""
    since = timezone.now() - ASKED_FRESH
    return [n for n in open_numbers(invoice, include_released=True)
            if n.released and (n.asked_at is None or n.asked_at < since)]


def holds_reported_payment(invoice: StoreInvoice) -> bool:
    """
    In review: an undecided number Tranzila reported — or a charge in the
    report that may be this order's — and the invoice's payment not settled,
    whatever its status reads. A number a person released does not hold it.
    """
    if invoice.payment_status not in UNSETTLED_STATUSES:
        return False
    return bool(open_numbers(invoice)) or bool(suspected_numbers(invoice))


def number_for(invoice: StoreInvoice, index: str, code: str, terminal: str, *,
               code_wins: bool = True) -> ReportedNumber:
    """
    A number a notify (or the site) reports now. One the invoice already
    holds keeps the time it was first reported and its state; a new one — or
    one that takes the place of a number ruled out — starts its own clock now.

    The approval number: a notify's is Tranzila's own and wins over whatever
    was kept (`code_wins`); the one the site returned only fills a blank — it
    never writes over a kept one. Neither is believed: the report decides.
    """
    def effective(kept) -> str:
        kept = str(kept or '').strip()
        return code if code and (code_wins or not kept) else kept

    if index == (invoice.tranzila_transaction_id or '').strip():
        return ReportedNumber(index, effective(invoice.tranzila_confirmation_code), terminal,
                              invoice.payment_reported_at or timezone.now(), True)
    for entry in invoice.other_transactions or []:
        if str(entry.get('index')) == index and entry.get('state') in UNDECIDED_STATES:
            return ReportedNumber(index, effective(entry.get('code')), terminal,
                                  _parse_time(entry.get('reported_at')) or timezone.now(),
                                  False, str(entry.get('state')))
    return ReportedNumber(index, code, terminal, timezone.now(), not is_transaction_number(invoice.tranzila_transaction_id))


def other_state(invoice: StoreInvoice, index: str) -> str:
    for entry in invoice.other_transactions or []:
        if str(entry.get('index')) == str(index):
            return str(entry.get('state') or '')
    return ''


def keep_other_transaction(invoice: StoreInvoice, number: ReportedNumber, state: str, *,
                           code_wins: bool = False, **extra) -> bool:
    """
    Keep a further number on the invoice (the caller holds the row lock and
    saves). A number already kept moves on only while undecided (open,
    suspected, released); a decided one stays as it is. Its approval number
    fills a blank; it replaces a kept one only with `code_wins` (a notify's
    own). True when new.
    """
    entries = [dict(entry) for entry in (invoice.other_transactions or [])]
    for entry in entries:
        if str(entry.get('index')) == number.index and str(entry.get('terminal') or '') == number.terminal:
            if entry.get('state') in UNDECIDED_STATES and state != entry.get('state'):
                entry['state'] = state
                entry['settled_at'] = timezone.now().isoformat()
                entry.update(extra)
            if number.code and state != OTHER_SUSPECTED and (code_wins or not entry.get('code')):
                entry['code'] = number.code
            invoice.other_transactions = entries
            return False
    entries.append({
        'index': number.index, 'code': number.code, 'terminal': number.terminal,
        'reported_at': number.reported_at.isoformat(), 'state': state, **extra,
    })
    invoice.other_transactions = entries
    return True


def drop_other_transaction(invoice: StoreInvoice, index: str) -> None:
    """The number became the invoice's own (it paid for it, or it is reported again): it leaves the further ones."""
    entries = [e for e in (invoice.other_transactions or []) if str(e.get('index')) != str(index)]
    invoice.other_transactions = entries or None


def with_evidence(number: ReportedNumber, *, confirmation_code: str = '', card_last4: str = '') -> ReportedNumber:
    """
    A person's "complete after verification": the number with the customer's
    own evidence. A typed approval number wins over any kept code. The card's
    last four digits are compared with the report's row; when they agree the
    row's approval number is the one checked, and when they do not — or
    nothing is given for a suspected charge — no approval number is, and the
    report cannot tie the number to the order.
    """
    from apps.core.tranzila_service import TranzilaService

    if confirmation_code:
        return replace(number, code=confirmation_code)
    if card_last4:
        service = TranzilaService.iframe(terminal=number.terminal or None)
        try:
            found = service.find_transaction(number.index)
        except Exception as exc:  # noqa: BLE001 — no answer is no evidence
            logger.error('Store: report lookup for %s failed: %s', number.index, exc)
            found = {}
        row = found.get('transaction') if found.get('success') else None
        if row and report_card_last4(row) == card_last4:
            return replace(number, code=str(row.get('authorization_number') or '').strip())
        return replace(number, code='')
    if number.suspected:
        return replace(number, code='')
    return number


# ---------------------------------------------------------------------------
# The report's word on one number
# ---------------------------------------------------------------------------

def report_answer(invoice: StoreInvoice, number: ReportedNumber) -> tuple[str, Optional[dict], str]:
    """
    (verified | rejected | disputed | unknown, the report's row, why) for one number.

    The judge is the same check the notify has always made
    (verify_transaction_with_tranzila): an approved charge (A/AK), this sum,
    this approval number, made after the order, not paying for another one.

      rejected  the report's definite no: the row is there and was declined,
                is not a charge, or is of another sum;
      disputed  the report disagrees but not definitely: another approval
                number, made before the order, held by another order, or
                still not listed REPORT_SETTLE after it was reported;
      unknown   the report could not be asked, does not list it yet, the
                number came with no approval number to compare, or it was
                made on another terminal than the page's current one.

    Only "verified" ties a number to an order; only "rejected" settles it as
    not paid.
    """
    from apps.core.tranzila_service import (
        CHARGE_TRANMODES,
        TranzilaService,
        is_tranzila_approved,
        report_transaction_amount,
    )
    from apps.payment_links import public_views
    from apps.payment_links.models import money

    current = (TranzilaService.iframe().terminal or '').strip()
    if (number.terminal or '').strip() != current:
        return ANSWER_UNKNOWN, None, (
            f'התשלום נעשה במסוף {number.terminal or "(לא ידוע)"}, ועמוד התשלום עבר מאז למסוף '
            f'{current or "(לא מוגדר)"}; אי אפשר לשאול עליו את הדוח'
        )
    verdict, row = public_views.verify_transaction_with_tranzila(
        SimpleNamespace(id=invoice.pk, amount=invoice.total_amount, created_at=invoice.created_at),
        number.index,
        confirmation_code=number.code,
    )
    if verdict == 'verified':
        return ANSWER_VERIFIED, row, ''
    if verdict == 'unavailable':
        return ANSWER_UNKNOWN, None, WHY_NO_ANSWER
    if row is not None:
        if not is_tranzila_approved(row.get('processor_response_code') or row.get('response_code')):
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה נדחתה'
        if str(row.get('tranmode') or '').strip().upper() not in CHARGE_TRANMODES:
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה אינה חיוב (בדיקת כרטיס או זיכוי)'
        if report_transaction_amount(row) != money(invoice.total_amount):
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה על סכום אחר'
        if not number.code:
            return ANSWER_UNKNOWN, row, 'אין מספר אישור של הלקוח להשוות אליו'
        return ANSWER_DISPUTED, row, ('העסקה בדוח של טרנזילה לא תואמת את ההזמנה (מספר אישור, מועד, '
                                      'או שהיא כבר שייכת להזמנה אחרת)')
    if timezone.now() - number.reported_at >= REPORT_SETTLE:
        return ANSWER_DISPUTED, None, 'העסקה לא נמצאה בדוח של טרנזילה'
    return ANSWER_UNKNOWN, None, 'העסקה עוד לא מופיעה בדוח של טרנזילה'


# ---------------------------------------------------------------------------
# Which invoices this is about
# ---------------------------------------------------------------------------

def _with_undecided_numbers():
    """
    Every invoice with a number nothing has decided yet: an unsettled
    hosted-page invoice (website or till) holding its own reported number,
    and any invoice with a further number open, suspected or released. A till
    charge of a saved or typed card never had a hosted page and has its own
    handling.
    """
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

    own = (
        Q(payment_status__in=UNSETTLED_STATUSES, charged_with_token=False, tranzila_transaction_id__regex=r'^[0-9]+$')
        & ~Q(tranzila_confirmation_code=TILL_CHARGE_UNCERTAIN_MARK)
    )
    further = Q()
    for state in UNDECIDED_STATES:
        further |= Q(other_transactions__contains=[{'state': state}])
    return StoreInvoice.objects.filter(own | further)


def _in_review():
    """Invoices in review — held by an undecided number of their own, or a further open or suspected one."""
    return _with_undecided_numbers().filter(payment_status__in=UNSETTLED_STATUSES)


def _needing_report_search():
    """
    Website orders whose pages must still be looked for in the report: a page
    was handed out, and no complete read of the report has covered the order
    SEARCH_FINAL_AFTER after its last page (payment_search_done_at) — at any
    age, so an order the report could not be read for is never dropped by
    the clock.

      * unpaid orders that nothing holds in review: the notify of a page may
        never have come. Numbers a person released do not stop the search —
        the customer got a new page since;
      * paid orders that handed out more than one page: an earlier page may
        have been paid too.
    """
    unpaid = Q(payment_status__in=UNSETTLED_STATUSES, tranzila_transaction_id='')
    for state in BLOCKING_STATES:
        unpaid &= ~Q(other_transactions__contains=[{'state': state}])
    several_pages = Q(payment_status__in=PAID_STATUSES, payment_page_first_opened_at__lt=F('payment_page_opened_at'))
    return (
        StoreInvoice.objects
        .filter(website_order_number__isnull=False, payment_page_opened_at__isnull=False)
        .exclude(website_order_number='')
        .filter(Q(payment_search_done_at__isnull=True)
                | Q(payment_search_done_at__lt=F('payment_page_opened_at') + SEARCH_FINAL_AFTER))
        .filter(unpaid | several_pages)
    )


def search_is_final(invoice: StoreInvoice) -> bool:
    """A complete read of the report covered this order's pages long enough after the last one: nothing new can show."""
    done, opened = invoice.payment_search_done_at, invoice.payment_page_opened_at
    return done is not None and opened is not None and done >= opened + SEARCH_FINAL_AFTER


def mark_searched(invoice_id) -> None:
    """A complete read of the report looked for this order's pages now."""
    StoreInvoice.objects.filter(pk=invoice_id).update(payment_search_done_at=timezone.now())


def _owed_a_paid_call():
    """
    Paid website orders the site has not acknowledged. Only orders paid through
    Tranzila's page after this follow-up existed (payment_reported_at): the
    retired widget/order/ endpoint wrote "completed" orders with no payment
    behind them, and the orders of the test-terminal weeks are settled by hand.
    """
    return (
        StoreInvoice.objects
        .filter(payment_status='completed', website_order_number__isnull=False,
                website_paid_notified_at__isnull=True, payment_reported_at__isnull=False,
                tranzila_transaction_id__regex=r'^[0-9]+$')
        .exclude(website_order_number='')
    )


def not_rechecked_because(invoice: StoreInvoice) -> str:
    """Why the report cannot settle this unsettled invoice by itself ('' when it can)."""
    from apps.core.payment_service import parse_store_cart_notes
    from apps.core.tranzila_service import TranzilaService

    if not open_numbers(invoice, include_released=True):
        if suspected_numbers(invoice):
            return 'בדוח של המסוף יש חיוב באותו סכום שלא דווח על ההזמנה — אדם צריך להחליט'
        return 'לא התקבל מספר עסקה מטרנזילה'
    if _is_till_charge(invoice):
        return 'חיוב בקופה שאינו מעמוד התשלום'
    # A transaction number means something only on its own terminal. The
    # report asked is the hosted page's current one; if the page has moved
    # since this payment, that report cannot speak for it.
    current = (TranzilaService.iframe().terminal or '').strip()
    if is_transaction_number(invoice.tranzila_transaction_id) and (invoice.tranzila_terminal or '').strip() != current:
        return (f'התשלום נעשה במסוף {invoice.tranzila_terminal or "(לא ידוע)"}, '
                f'ועמוד התשלום עבר מאז למסוף {current or "(לא מוגדר)"}')
    # Completing sells what the cart holds; without it a sale would be a
    # "completed" invoice with nothing sold and no stock taken.
    if parse_store_cart_notes(invoice.notes) is None:
        return 'בחשבונית לא נשמרה העגלה, ואין מה למכור'
    return ''


def _claim_followup(invoice_id, min_interval: timedelta) -> bool:
    """
    Take this invoice's follow-up turn, or learn that someone had it less than
    `min_interval` ago. One conditional UPDATE, so two server instances (or
    the poll and the sweep) cannot both take the same turn.
    """
    now = timezone.now()
    return bool(
        StoreInvoice.objects.filter(pk=invoice_id)
        .filter(Q(payment_followup_at__isnull=True) | Q(payment_followup_at__lte=now - min_interval))
        .update(payment_followup_at=now)
    )


# ---------------------------------------------------------------------------
# 1. Asking the report again
# ---------------------------------------------------------------------------

def recheck_pending_payment(invoice_id, *, min_interval: timedelta = RECHECK_INTERVAL, complete: bool = True,
                            write: bool = True, site_timeout: Optional[float] = None,
                            max_numbers: Optional[int] = None) -> str:
    """
    Ask Tranzila's report again about every undecided number of ONE invoice —
    released ones included — and settle what the report now allows
    (PaymentService.settle_reported_store_payment): a confirmed number
    completes the sale — when `complete` — through the locked path the notify
    uses; on a paid invoice, a confirmed further number is a second charge.
    With `write` off (the morning sweep while its switch is off) nothing is
    sold and no status changes: the office is told what the report says. What
    is kept either way: a second charge the report confirms on a paid order,
    and when each number was asked. At most MAX_REPORT_READS_PER_INVOICE
    numbers are asked at a time (`max_numbers`), in turns. Nothing is ever
    charged; the report is read.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return RECHECK_NOT_ELIGIBLE
    if not open_numbers(invoice, include_released=True):
        return RECHECK_NOT_PENDING
    if invoice.payment_status not in PAID_STATUSES:
        reason = not_rechecked_because(invoice)
        if reason:
            logger.warning('Store invoice %s not rechecked: %s', invoice.invoice_number, reason)
            alert_if_stuck(invoice, why=reason)
            return RECHECK_NOT_ELIGIBLE
    if write and not _claim_followup(invoice.pk, min_interval):
        return RECHECK_PACED

    result = PaymentService().settle_reported_store_payment(
        invoice.pk, complete=complete and write, write=write, site_timeout=site_timeout, max_numbers=max_numbers,
    )
    outcome = result.get('outcome')
    logger.info('Store invoice %s rechecked: %s', invoice.invoice_number, outcome)
    return {
        'completed': RECHECK_COMPLETED,
        'confirmed': RECHECK_CONFIRMED,
        'paid': RECHECK_PAID,
    }.get(outcome, RECHECK_PENDING)


# ---------------------------------------------------------------------------
# 2. Telling the website
# ---------------------------------------------------------------------------

def tell_website_paid(invoice: StoreInvoice, *, timeout: Optional[float] = None) -> bool:
    """
    Tell the site this order is paid and keep its answer on the invoice.

    Called right after a sale completes, and again (retry_website_paid) until
    the site answers 2xx. The site's side is idempotent on the order's status;
    each "paid" it accepts also sends the staff's order email, so a repeat is
    made only when the previous call was not acknowledged.
    """
    from apps.store.website_integration import ORDER_STATUS_TIMEOUT_SECONDS, post_website_order_status

    if not invoice.website_order_number:
        return True
    ok, why = post_website_order_status(
        website_order_number=invoice.website_order_number,
        invoice_number=invoice.invoice_number,
        invoice_id=str(invoice.pk),
        status='paid',
        provider_txn_id=invoice.tranzila_transaction_id or '',
        timeout=timeout or ORDER_STATUS_TIMEOUT_SECONDS,
    )
    if ok:
        now = timezone.now()
        StoreInvoice.objects.filter(pk=invoice.pk, website_paid_notified_at__isnull=True).update(
            website_paid_notified_at=now,
        )
        invoice.website_paid_notified_at = invoice.website_paid_notified_at or now
        return True
    logger.error('Website not told that order %s is paid: %s', invoice.website_order_number, why)
    if timezone.now() - (invoice.payment_reported_at or invoice.created_at) >= STUCK_AFTER:
        alert_website_not_told(invoice, why)
    return False


def retry_website_paid(invoice_id, *, min_interval: timedelta = RECHECK_INTERVAL,
                       timeout: Optional[float] = None) -> bool:
    """Repeat the "paid" call for a paid order the site has not acknowledged. True when it now has."""
    invoice = _owed_a_paid_call().filter(pk=invoice_id).first()
    if invoice is None:
        return StoreInvoice.objects.filter(pk=invoice_id, website_paid_notified_at__isnull=False).exists()
    if not _claim_followup(invoice.pk, min_interval):
        return False
    return tell_website_paid(invoice, timeout=timeout)


# ---------------------------------------------------------------------------
# What the website sees, and what it tells us
# ---------------------------------------------------------------------------

def site_status(invoice: StoreInvoice) -> dict:
    """
    The order as the website reads it (the contract, docs/CHANGE-IMPACT-2026-09-29-STAGE3-…):

      status            pending | completed | failed | refunded
      paid              true only for a Tranzila payment the report confirmed
                        and the money still with us (refund_failed included)
      payment_reported  true while the CRM holds a payment Tranzila reported,
                        or a charge in the report that may be this order's,
                        that it has neither confirmed nor ruled out — the
                        order is in review; status is then "pending"

    A refunded order is "refunded" and not paid, so the site neither marks it
    paid nor sends its staff email. An order the retired widget/order/
    endpoint marked "completed" had no payment behind it: pending, not paid.
    """
    reported = holds_reported_payment(invoice)
    if invoice.payment_status in MONEY_KEPT_STATUSES and is_transaction_number(invoice.tranzila_transaction_id):
        state, paid = STATUS_COMPLETED, True
    elif invoice.payment_status == 'refunded':
        state, paid = STATUS_REFUNDED, False
    elif reported:
        state, paid = STATUS_PENDING, False
    elif invoice.payment_status == 'failed':
        state, paid = STATUS_FAILED, False
    else:
        state, paid = STATUS_PENDING, False
    return {
        'status': state,
        'invoice_number': invoice.invoice_number,
        'paid': paid,
        'payment_reported': reported,
    }


def website_order_status(website_order_number: str) -> Optional[dict]:
    """
    The site's poll for one order. Every undecided number of an unpaid order
    — a payment in review, and a number a person released — is asked about
    again (paced) and the order completed when the report confirms one: the
    customer is waiting for exactly that. A paid order the site never
    acknowledged is told again. None when there is no such order.
    """
    invoice = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
    if invoice is None:
        return None
    try:
        if invoice.payment_status in UNSETTLED_STATUSES and open_numbers(invoice, include_released=True):
            recheck_pending_payment(invoice.pk, site_timeout=POLL_SITE_TIMEOUT_SECONDS)
            invoice.refresh_from_db()
        elif invoice.payment_status == 'completed' and invoice.website_paid_notified_at is None:
            retry_website_paid(invoice.pk, timeout=POLL_SITE_TIMEOUT_SECONDS)
    except Exception:
        # The poll always gets the stored answer; the sweep tries again.
        logger.exception('Store order %s: follow-up from the status poll failed', website_order_number)
        invoice.refresh_from_db()
    return site_status(invoice)


RETURNED_NO_ORDER = 'no_order'
RETURNED_NO_RECENT_PAGE = 'no_recent_page'


def record_returned_number(website_order_number: str, index: str, code: str = '') -> tuple[str, Optional[dict]]:
    """
    The number the site got back from Tranzila's page (its return address),
    for an order whose notify may never come: ('ok', status) | ('no_order',
    None) | ('no_recent_page', status).

    Taken only for an order whose page the CRM handed out within
    RETURNED_PAGE_WINDOW. Believed exactly as much as a notify — which is not
    at all: recorded as reported, judged by the report through the notify's
    own path. A number that is not this order's payment does not pass, and is
    kept for the office like any other. The approval number it brings only
    fills a blank: a notify's own is never written over by it.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
    if invoice is None:
        return RETURNED_NO_ORDER, None
    opened = invoice.payment_page_opened_at
    if opened is None or timezone.now() - opened > RETURNED_PAGE_WINDOW:
        logger.warning('Store order %s: returned number refused — no page handed out in the last %s',
                       website_order_number, RETURNED_PAGE_WINDOW)
        return RETURNED_NO_RECENT_PAGE, site_status(invoice)
    PaymentService().complete_store_purchase_from_webhook(
        str(invoice.pk),
        {'is_successful': True, 'transaction_id': index, 'confirmation_code': code},
        site_timeout=POLL_SITE_TIMEOUT_SECONDS,
        source='returned',
    )
    invoice.refresh_from_db()
    return 'ok', site_status(invoice)


# ---------------------------------------------------------------------------
# 3. A notify that never came
# ---------------------------------------------------------------------------

def _day_report(first_day, last_day) -> Optional[list[dict]]:
    """
    The page terminal's report rows for these days, or None when it cannot be
    asked, answered an error, or was read only in part (a page failed, or
    more pages than read): a partial list says nothing about what it lacks.
    """
    from apps.core.tranzila_service import TranzilaService

    try:
        response = TranzilaService.iframe().list_all_transactions(first_day, last_day, max_pages=DAY_REPORT_MAX_PAGES)
    except Exception as exc:  # network, keys — never a reason to open a second page
        logger.error('Store: day report failed: %s', exc)
        return None
    if not response.get('success') or response.get('complete') is False:
        return None
    return list(response.get('transactions') or [])


def _search_days(invoice: StoreInvoice):
    """The report days a payment of this order's pages can be on: from its first page to the day after its last."""
    first = invoice.payment_page_first_opened_at or invoice.payment_page_opened_at
    last = invoice.payment_page_opened_at or first
    today = timezone.localtime(timezone.now(), REPORT_TZ).date()
    return (timezone.localtime(first, REPORT_TZ).date(),
            min(today, timezone.localtime(last, REPORT_TZ).date() + timedelta(days=1)))


def _index_explained(index: str, terminal: str, invoice_pk) -> bool:
    """
    Whether this number already paid for something else of ours on this
    terminal: a paid store order (its own number, or a second charge kept on
    it), a completed payment link, a completed course signup. A number that
    is only suspected, released or open on another order explains nothing —
    it may be this order's payment as much as that one's.
    """
    from apps.customers.models import CourseCheckout
    from apps.payment_links.models import PaymentLinkPayment

    others = StoreInvoice.objects.exclude(pk=invoice_pk)
    return (
        others.filter(tranzila_transaction_id=index, tranzila_terminal=terminal, payment_status__in=PAID_STATUSES).exists()
        or others.filter(other_transactions__contains=[
            {'index': index, 'terminal': terminal, 'state': OTHER_SECOND_CHARGE}]).exists()
        or PaymentLinkPayment.objects.filter(
            Q(tranzila_terminal=terminal) | Q(tranzila_terminal=''), gateway_transaction_id=index,
            status=PaymentLinkPayment.STATUS_COMPLETED,
        ).exists()
        or CourseCheckout.objects.filter(
            page_terminal=terminal, page_index=index, status=CourseCheckout.STATUS_COMPLETED,
        ).exists()
    )


def find_unreported_payment(invoice: StoreInvoice, rows: Optional[list[dict]] = None) -> tuple[str, list[dict]]:
    """
    ('found', rows) | ('none', []) | ('unknown', []) — whether the terminal's
    report shows a payment for this order whose notify never came.

    Asked of the report of the days from the order's FIRST page to the day
    after its last, on the page's terminal (or of `rows`, the sweep's one
    read of it). A
    candidate row is an approved charge (A/AK) of exactly this sum, made after
    that first page, under a number this order does not hold already and
    nothing of ours was paid by. The report's rows carry no pdesc (seen on the
    NK page, 29.9.2026), so a row is tied to the order only by sum and time;
    one that does carry a pdesc must carry this order's. The terminal is shared
    with the other website, so a match is a reason to stop and ask a person,
    never a reason to complete the sale. A report read only in part is
    "unknown".
    """
    from apps.core.tranzila_service import (
        CHARGE_TRANMODES,
        TranzilaService,
        invoice_id_from_pdesc,
        is_tranzila_approved,
        report_transaction_amount,
        report_transaction_time,
    )
    from apps.payment_links.models import money
    from apps.payment_links.public_views import TRANSACTION_CLOCK_SKEW

    first = invoice.payment_page_first_opened_at or invoice.payment_page_opened_at
    if first is None:
        return 'none', []
    if rows is None:
        rows = _day_report(*_search_days(invoice))
        if rows is None:
            return 'unknown', []
    terminal = (TranzilaService.iframe().terminal or '').strip()
    # A number this order already holds — its own, or one kept beside it in
    # any state — is known, not a lost payment to be found again.
    own = {(invoice.tranzila_transaction_id or '').strip()} | {
        str(entry.get('index')) for entry in invoice.other_transactions or []
    }
    matches = []
    for row in rows:
        if not is_tranzila_approved(row.get('processor_response_code') or row.get('response_code')):
            continue
        if str(row.get('tranmode') or '').strip().upper() not in CHARGE_TRANMODES:
            continue
        if report_transaction_amount(row) != money(invoice.total_amount):
            continue
        made_at = report_transaction_time(row)
        if made_at is None or made_at < first - TRANSACTION_CLOCK_SKEW:
            continue
        index = str(row.get('index') or row.get('transaction_index') or '').strip()
        if not is_transaction_number(index) or index in own:
            continue
        pdesc = str(row.get('pdesc') or '').strip()
        if pdesc and invoice_id_from_pdesc(pdesc) != str(invoice.pk):
            continue
        if _index_explained(index, terminal, invoice.pk):
            continue
        matches.append(row)
    return ('found', matches) if matches else ('none', [])


def keep_suspected_charges(invoice_id, rows: list[dict]) -> list[str]:
    """
    Keep charges the report shows for this order's pages, whose notify never
    came, on the invoice as "suspected" — whoever found them, whatever the
    sweep's switch reads: keeping one is not a sale, a document or an email.
    On an unpaid order it is then in review (the site reads payment_reported)
    and gets no second page until a person decides; on a paid one it is a
    possible second charge, listed every morning until a person decides. The
    report's own approval number is NOT kept with them: it would let the
    charge verify itself. Returns the numbers kept now for the first time.
    """
    from apps.core.tranzila_service import TranzilaService, report_transaction_time

    terminal = (TranzilaService.iframe().terminal or '').strip()
    kept = []
    with transaction.atomic():
        invoice = StoreInvoice.objects.select_for_update().filter(pk=invoice_id).first()
        if invoice is None:
            return kept
        for row in rows:
            index = normal_number(row.get('index') or row.get('transaction_index'))
            if not index or other_state(invoice, index) or index == (invoice.tranzila_transaction_id or '').strip():
                continue
            if not has_room(invoice):
                logger.error('Store invoice %s: charge %s found in the report not kept — %s numbers already',
                             invoice.invoice_number, index, MAX_KEPT_NUMBERS)
                break
            keep_other_transaction(invoice, ReportedNumber(
                index=index, code='', terminal=terminal,
                reported_at=report_transaction_time(row) or timezone.now(), primary=False, state=OTHER_SUSPECTED,
            ), OTHER_SUSPECTED)
            kept.append(index)
        if kept:
            invoice.save(update_fields=['other_transactions'])
    return kept


# ---------------------------------------------------------------------------
# 4. A person decides
# ---------------------------------------------------------------------------

def _log_review(invoice: StoreInvoice, *, action: str, by: str, reason: str, numbers: list[str], outcome: str,
                evidence: str = '') -> None:
    log = list(invoice.payment_review_log or [])
    log.append({
        'action': action, 'by': by, 'at': timezone.now().isoformat(), 'reason': reason,
        'numbers': numbers, 'outcome': outcome, 'evidence': evidence,
    })
    invoice.payment_review_log = log


EVIDENCE_NEEDED = 'evidence_needed'


def complete_reported_payment(invoice_id, *, by: str, reason: str, confirmation_code: str = '',
                              card_last4: str = '') -> dict:
    """
    A person, having looked, asks to complete an order: one in review, or a
    failed one that holds numbers a person released. Only through the report:
    the numbers it holds are asked about with the customer's own evidence —
    the approval number the customer read, or the card's last four digits —
    which wins over any kept code, and the sale happens on the locked path
    only if the report confirms one. A suspected charge (found in the report
    by sum and time) needs that evidence: without it nothing ties it to this
    order.

    On a PAID order the same evidence decides a further number — a charge
    found in the report, or one reported beside the payment: when the report
    confirms it, it is a second charge, recorded for a refund. Nothing is
    sold again.

    One call reads the report at most MAX_REPORT_READS_PER_INVOICE times (two
    reads per number with the card's digits), the numbers taking turns:
    'not_asked' says how many still wait for the next call. Who, when, why
    and what evidence are kept on the invoice either way.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return {'outcome': 'not_found'}
    paid = invoice.payment_status in PAID_STATUSES
    numbers = open_numbers(invoice, include_suspected=True, include_released=True)
    if not numbers:
        return {'outcome': 'not_in_review', 'status': invoice.payment_status}
    confirmation_code = re.sub(r'[^0-9]', '', confirmation_code or '')[:20]
    card_last4 = re.sub(r'[^0-9]', '', card_last4 or '')[-4:]
    card_last4 = card_last4 if len(card_last4) == 4 else ''
    evidence = ('approval' if confirmation_code else '') or ('card_last4' if card_last4 else '')
    if not evidence and all(n.suspected or not n.code for n in numbers):
        result = {'outcome': EVIDENCE_NEEDED, 'status': invoice.payment_status}
    else:
        reads_per_number = 2 if evidence == 'card_last4' else 1
        result = PaymentService().settle_reported_store_payment(
            invoice.pk, complete=True, include_suspected=True,
            evidence={'confirmation_code': confirmation_code, 'card_last4': card_last4},
            max_numbers=max(1, MAX_REPORT_READS_PER_INVOICE // reads_per_number),
        )
        if paid:
            result = {**result, 'outcome': 'second_charge' if result.get('second_charges') else 'not_confirmed'}
    with transaction.atomic():
        locked = StoreInvoice.objects.select_for_update().get(pk=invoice.pk)
        _log_review(locked, action='complete', by=by, reason=reason, numbers=[n.index for n in numbers],
                    outcome=result.get('outcome', ''), evidence=evidence)
        locked.save(update_fields=['payment_review_log'])
    logger.warning('Store invoice %s: %s asked to complete (%s, evidence %s): %s', invoice.invoice_number, by,
                   reason, evidence or 'none', result.get('outcome'))
    return result


def release_reported_payment(invoice_id, *, by: str, reason: str) -> dict:
    """
    A person, having checked Tranzila, found no charge for this order: the
    customer may pay again. The numbers it holds are RELEASED, not ruled out
    — a person's look is not the report's no: they stop holding the order in
    review, but the report is still asked about them — by every notify and
    returned number, by the site's poll, by the sweep, and before a second
    page leaves — and one it later confirms is a sale (the order not paid
    yet) or a second charge (paid meanwhile). The order is failed and the site is
    told. Nothing is charged, refunded or deleted; who, when and why are kept.
    """
    from apps.core.payment_service import PaymentService

    with transaction.atomic():
        invoice = StoreInvoice.objects.select_for_update().filter(pk=invoice_id).first()
        if invoice is None:
            return {'outcome': 'not_found'}
        if not holds_reported_payment(invoice):
            return {'outcome': 'not_in_review', 'status': invoice.payment_status}
        numbers = open_numbers(invoice, include_suspected=True)
        marks = {'release_reason': reason, 'released_by': by, 'released_at': timezone.now().isoformat()}
        for number in numbers:
            keep_other_transaction(invoice, number, OTHER_RELEASED, **marks)
        invoice.tranzila_transaction_id = ''
        invoice.tranzila_confirmation_code = ''
        invoice.payment_status = 'failed'
        _log_review(invoice, action='release', by=by, reason=reason, numbers=[n.index for n in numbers],
                    outcome='released')
        invoice.save(update_fields=[
            'tranzila_transaction_id', 'tranzila_confirmation_code', 'payment_status', 'other_transactions',
            'payment_review_log',
        ])
        transaction.on_commit(lambda: PaymentService._tell_website_failed(invoice))
    logger.warning('Store invoice %s released by %s: %s', invoice.invoice_number, by, reason)
    return {'outcome': 'released', 'status': 'failed'}


def close_reported_numbers(invoice_id, *, by: str, reason: str) -> dict:
    """
    "סגור — לא שלנו": a person decides that the further numbers a PAID order
    still holds undecided are not this order's — a number reported beside its
    payment that the report never listed, a charge found in the report that
    is another customer's. Without it such a number would be asked about, and
    listed in the brief, for ever.

    The report is asked first (in turns, MAX_REPORT_READS_PER_INVOICE a
    call): a number it confirms is a second charge and is recorded as one,
    never closed; one the report could not be asked about stays as it is.
    The rest are CLOSED: they leave the follow-up and stay on the invoice,
    with who, when and why. Nothing is charged, refunded or deleted.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return {'outcome': 'not_found'}
    if invoice.payment_status not in PAID_STATUSES:
        return {'outcome': 'not_paid', 'status': invoice.payment_status}
    numbers = next_to_ask(open_numbers(invoice, include_suspected=True, include_released=True),
                          MAX_REPORT_READS_PER_INVOICE)
    if not numbers:
        return {'outcome': 'not_in_review', 'status': invoice.payment_status}
    asked = {n.index: report_answer(invoice, with_evidence(n)) for n in numbers}

    closed, seconds = [], []
    marks = {'close_reason': reason, 'closed_by': by, 'closed_at': timezone.now().isoformat()}
    with transaction.atomic():
        locked = StoreInvoice.objects.select_for_update().get(pk=invoice.pk)
        for number in numbers:
            answer, row, why = asked[number.index]
            if other_state(locked, number.index) not in UNDECIDED_STATES:
                continue  # decided meanwhile
            if answer == ANSWER_VERIFIED:
                PaymentService()._record_second_store_charge(locked, number, answer, row)
                seconds.append(number.index)
            elif answer == ANSWER_UNKNOWN and why == WHY_NO_ANSWER:
                continue  # the report could not be asked: not closed blind
            else:
                keep_other_transaction(locked, number, OTHER_CLOSED, **marks)
                closed.append(number.index)
        outcome = 'closed' if closed else ('second_charge' if seconds else 'not_closed')
        left = len(open_numbers(locked, include_suspected=True, include_released=True))
        _log_review(locked, action='close', by=by, reason=reason, numbers=closed + seconds, outcome=outcome)
        locked.save(update_fields=['other_transactions', 'payment_review_log'])
    logger.warning('Store invoice %s: %s closed numbers %s (%s); second charges %s', invoice.invoice_number, by,
                   closed, reason, seconds)
    return {'outcome': outcome, 'status': invoice.payment_status, 'closed': closed, 'second_charges': seconds,
            'not_asked': left}


# ---------------------------------------------------------------------------
# 5. The morning sweep
# ---------------------------------------------------------------------------

def sweep_stuck_store_payments(*, budget_seconds: float = SWEEP_BUDGET_SECONDS,
                               complete: Optional[bool] = None) -> dict:
    """
    Every invoice with an undecided number (at any age once this follow-up
    recorded when its payment was reported; released and suspected numbers
    included), every website order whose pages still have to be looked for in
    the report (`_needing_report_search`), and every paid website order the
    site has not acknowledged.

    With STORE_SWEEP_COMPLETES_PAYMENTS on (`complete`), it settles what the
    report allows, as the site's poll does. Off, it sells nothing: no sale, no
    status, no document, no email. Either way it keeps what it learned — a
    second charge the report confirms on a paid order (recorded and told as
    confirmed), a charge found in the report for an order's pages (kept as
    suspected, never completed: a person decides), when each number was asked
    — and repeats the "paid" call for an order already paid. Each invoice gets
    at most MAX_REPORT_READS_PER_INVOICE report reads, its numbers taking
    turns from one morning to the next. Never charges; never raises for one
    invoice.

    Returns lists of invoices: 'settled' (completed now), 'confirmed' (the
    report confirms it, not completed here), 'still_pending' (with the reason),
    'second_confirmed' (a second charge of a paid order the report confirmed
    now), 'second_open' (a further number of a paid order still undecided),
    'released_open' (an unpaid order with only numbers a person released,
    still undecided), 'unexplained' (a charge in the report may be its payment,
    or a second one — until a person decides), 'site_told', 'site_not_told',
    'not_reached' (out of time), 'not_searched' (the report could not be read
    in full for its pages).
    """
    if complete is None:
        complete = bool(getattr(settings, 'STORE_SWEEP_COMPLETES_PAYMENTS', False))
    write = complete
    started = time.monotonic()
    now = timezone.now()
    result = {'settled': [], 'confirmed': [], 'still_pending': [], 'second_confirmed': [], 'second_open': [],
              'released_open': [], 'unexplained': [], 'site_told': [], 'site_not_told': [], 'not_reached': [],
              'not_searched': []}

    def out_of_time() -> bool:
        return time.monotonic() - started > budget_seconds

    def second_charges(invoice) -> set:
        return {str(e.get('index')) for e in invoice.other_transactions or [] if e.get('state') == OTHER_SECOND_CHARGE}

    undecided = (
        _with_undecided_numbers()
        .filter(Q(payment_reported_at__isnull=False) | Q(created_at__gte=now - LEGACY_WINDOW)
                | Q(payment_status__in=PAID_STATUSES)
                | Q(other_transactions__contains=[{'state': OTHER_SUSPECTED}])
                | Q(other_transactions__contains=[{'state': OTHER_RELEASED}]))
        .order_by(F('payment_reported_at').desc(nulls_last=True), '-created_at')
    )
    for invoice in undecided:
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        was_paid = invoice.payment_status in PAID_STATUSES
        seconds_before = second_charges(invoice)
        outcome = None
        if open_numbers(invoice, include_released=True):
            invoice_started = time.monotonic()
            try:
                outcome = recheck_pending_payment(invoice.pk, complete=complete, write=write,
                                                  max_numbers=MAX_REPORT_READS_PER_INVOICE)
            except Exception as exc:  # noqa: BLE001 — one invoice never stops the rest
                logger.exception('Store sweep: recheck of %s failed', invoice.invoice_number)
                outcome = f'error: {exc}'
            if time.monotonic() - invoice_started > SWEEP_INVOICE_SECONDS:
                logger.warning('Store sweep: %s took %.0fs', invoice.invoice_number, time.monotonic() - invoice_started)
            invoice.refresh_from_db()
        if was_paid:
            if second_charges(invoice) - seconds_before:
                result['second_confirmed'].append(invoice)
            if open_numbers(invoice, include_released=True):
                result['second_open'].append(invoice)
            if suspected_numbers(invoice):
                # A charge found in the report beside the payment: listed until a person decides.
                result['unexplained'].append(invoice)
            continue
        if invoice.payment_status == 'completed':
            result['settled'].append(invoice)
            continue
        if outcome == RECHECK_CONFIRMED:
            result['confirmed'].append(invoice)
            continue
        if suspected_numbers(invoice) and not open_numbers(invoice):
            # Held by a charge found in the report, never reported for it: a person decides.
            alert_payment_unreported(invoice, [], suspected=[n.index for n in suspected_numbers(invoice)])
            result['unexplained'].append(invoice)
            continue
        if not holds_reported_payment(invoice):
            if open_numbers(invoice, include_released=True):
                result['released_open'].append(invoice)
            continue
        reason = not_rechecked_because(invoice) or {
            RECHECK_PACED: 'נבדק ממש עכשיו מול טרנזילה, ועדיין לא אושר',
            RECHECK_PENDING: 'הדוח של טרנזילה עדיין לא מאשר את העסקה',
        }.get(outcome, str(outcome))
        alert_if_stuck(invoice, why=reason)
        result['still_pending'].append((invoice, reason))

    # Orders whose pages still have to be looked for in the report: a notify
    # that may never have come, or an earlier page of a paid order. What is
    # found is kept on the invoice (suspected) and told; never completed.
    def look_for(invoice, rows) -> None:
        found, matches = find_unreported_payment(invoice, rows)
        if found == 'found':
            keep_suspected_charges(invoice.pk, matches)
            if invoice.payment_status in PAID_STATUSES:
                alert_second_charge_found(invoice, [normal_number(m.get('index') or m.get('transaction_index'))
                                                    for m in matches])
            else:
                alert_payment_unreported(invoice, matches)
            if invoice not in result['unexplained']:
                result['unexplained'].append(invoice)
        mark_searched(invoice.pk)

    def first_page(invoice):
        return invoice.payment_page_first_opened_at or invoice.payment_page_opened_at

    to_search = list(_needing_report_search().order_by('payment_page_opened_at'))
    recent = [i for i in to_search if first_page(i) >= now - SEARCH_WINDOW]
    old = [i for i in to_search if first_page(i) < now - SEARCH_WINDOW]
    if recent and out_of_time():
        result['not_reached'].extend(recent)
    elif recent:
        # One read of the report for all of them.
        rows = _day_report(min(timezone.localtime(first_page(i), REPORT_TZ).date() for i in recent),
                           timezone.localtime(now, REPORT_TZ).date())
        if rows is None:
            result['not_searched'].extend(recent)  # unknown is not "nothing there": the next morning looks again
        else:
            for invoice in recent:
                look_for(invoice, rows)
    # An order nobody could look for in time (the report was down, or read in
    # part, for days) is not dropped by the clock: a read of its own days.
    for position, invoice in enumerate(old):
        if position >= MAX_OLD_SEARCHES or out_of_time():
            result['not_reached'].append(invoice)
            continue
        rows = _day_report(*_search_days(invoice))
        if rows is None:
            result['not_searched'].append(invoice)
            continue
        look_for(invoice, rows)

    for invoice in _owed_a_paid_call().order_by('created_at'):
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        try:
            told = retry_website_paid(invoice.pk)
        except Exception:  # noqa: BLE001
            logger.exception('Store sweep: telling the site about %s failed', invoice.invoice_number)
            told = False
        (result['site_told'] if told else result['site_not_told']).append(invoice)
    return result


# ---------------------------------------------------------------------------
# The office
# ---------------------------------------------------------------------------

def _where(invoice: StoreInvoice, step: str) -> str:
    return f'{WHERE_WEBSITE if invoice.website_order_number else WHERE_TILL} — {step}'


def _order_ref(invoice: StoreInvoice) -> str:
    return invoice.website_order_number or invoice.invoice_number


def describe_store_customer(invoice: StoreInvoice) -> str:
    """One line: who bought, how to reach them, the sum, the order and the invoice."""
    from apps.core.office_alerts import describe_family

    parts = []
    child = invoice.child if invoice.child_id else None
    if child is not None:
        parts.append(describe_family(child.family, children=[child]))
    else:
        parts.append(f'לקוח: {invoice.customer_name or "לקוח מזדמן"}'
                     + (f' {invoice.customer_phone}' if invoice.customer_phone else ''))
    if invoice.customer_email:
        parts.append(f'מייל: {invoice.customer_email}')
    parts.append(f'סכום: ₪{invoice.total_amount}')
    if invoice.website_order_number:
        parts.append(f'הזמנה באתר: {invoice.website_order_number}')
    parts.append(f'חשבונית: {invoice.invoice_number}')
    return ' · '.join(part for part in parts if part)


def _link(invoice: StoreInvoice) -> str:
    from apps.core.office_alerts import crm_child_link

    if invoice.child_id:
        return crm_child_link(invoice.child_id)
    base = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    return f'{base}/invoices' if base else ''


def _episode(invoice: StoreInvoice) -> int:
    """How many times a person released this order's payment: what happens after a release is a new event."""
    return sum(1 for entry in invoice.payment_review_log or []
               if entry.get('action') == 'release' and entry.get('outcome') == 'released')


def _alert(invoice: StoreInvoice, *, kind: str, key: str, title: str, step: str, what: str,
           why: str = '', action: str = '', extra: Optional[dict] = None, per_episode: bool = True) -> None:
    """
    One alert per key. A key of an order counts its releases (`per_episode`):
    "once per order" would otherwise be once for ever, and a payment stuck
    again after a person released the first would reach nobody.
    """
    from apps.core.office_alerts import raise_office_alert

    episode = _episode(invoice) if per_episode else 0
    if episode:
        key = f'{key}:{episode}'

    try:
        customer = describe_store_customer(invoice)
    except Exception:  # noqa: BLE001 — an alert goes out even without its customer line
        logger.exception('Store alert %s: customer not described', key)
        customer = f'חשבונית {invoice.invoice_number}'
    details = {'invoice_id': str(invoice.pk), 'invoice_number': invoice.invoice_number,
               'website_order_number': invoice.website_order_number or '',
               'transaction': shown_number(invoice.tranzila_transaction_id),
               'terminal': invoice.tranzila_terminal or ''}
    details.update(extra or {})
    raise_office_alert(
        kind=kind, dedup_key=key, title=title, where=_where(invoice, step), what=what, why=why,
        customer=customer, action=action, link=_link(invoice), details=details,
    )


def _check_in_tranzila(invoice: StoreInvoice) -> str:
    # The template's "action" line holds 300 characters; this stays well inside.
    return (
        f'לבדוק בטרנזילה (מסוף {invoice.tranzila_terminal or "עמוד התשלום"}) את עסקה '
        f'{shown_number(invoice.tranzila_transaction_id)} על ₪{invoice.total_amount}'
        + (f', אישור {invoice.tranzila_confirmation_code}' if invoice.tranzila_confirmation_code else '')
        + '. אושרה — הלקוח שילם: לא לבקש תשלום שוב; המערכת תשלים לבד כשהדוח יאשר, ואם לא — '
          'להעביר לבדיקה טכנית. לא אושרה — לחזור ללקוח.'
    )


def alert_payment_unverified(invoice: StoreInvoice, report_row: Optional[dict] = None, why: str = '') -> None:
    """The report says no to a reported number: nothing was sold. Once per invoice."""
    from apps.core.tranzila_service import report_transaction_amount

    if report_row:
        try:
            seen = f'בדוח העסקה מופיעה על סך ₪{report_transaction_amount(report_row)}, tranmode {report_row.get("tranmode") or "?"}.'
        except Exception:  # noqa: BLE001
            seen = 'העסקה נמצאה בדוח אבל לא תאמה.'
    else:
        seen = 'העסקה לא נמצאה בדוח של המסוף, גם אחרי כמה דקות.'
    _alert(
        invoice, kind='store_payment_unverified', key=f'store_payment_review:{invoice.pk}',
        title='תשלום בחנות שלא תאם לטרנזילה — ההזמנה בבדיקה',
        step='אישור התשלום מול הדוח של טרנזילה',
        what=(f'הגיעה הודעה שהתשלום אושר (עסקה {shown_number(invoice.tranzila_transaction_id)}), '
              f'אבל הדוח של טרנזילה לא מאשר אותה. {seen} '
              'לא נמכר דבר, המלאי לא ירד ולא הופק מסמך. ההזמנה בבדיקה, והאתר לא יציע ללקוח לשלם שוב.'),
        why=why or ('הסכום, מספר האישור, סוג העסקה או מועד העסקה בדוח שונים ממה שנשלח, '
                    'או שהמספר כבר שייך להזמנה אחרת.'),
        action=_check_in_tranzila(invoice),
    )


def alert_if_stuck(invoice: StoreInvoice, *, why: str = '') -> None:
    """In review for longer than STUCK_AFTER since Tranzila reported the payment. Once per invoice."""
    if not holds_reported_payment(invoice):
        return
    reported_at = invoice.payment_reported_at or invoice.created_at
    if timezone.now() - reported_at < STUCK_AFTER:
        return
    minutes = int((timezone.now() - reported_at).total_seconds() // 60)
    _alert(
        invoice, kind='store_payment_stuck', key=f'store_payment_review:{invoice.pk}',
        title='תשלום בחנות תקוע — לא ידוע אם הלקוח שילם',
        step='אישור התשלום מול הדוח של טרנזילה',
        what=(f'טרנזילה דיווחה לפני {minutes} דקות על עסקה {shown_number(invoice.tranzila_transaction_id)}, '
              'אבל התשלום עדיין לא אומת מול הדוח ולכן ההזמנה לא הושלמה: לא נמכר דבר, המלאי לא ירד '
              'ולא הופק מסמך. ההזמנה בבדיקה, והאתר לא יציע ללקוח לשלם שוב.'),
        why=why or 'הדוח של טרנזילה לא ענה או עדיין לא מאשר את העסקה.',
        action=_check_in_tranzila(invoice),
    )


def alert_decline_conflict(invoice: StoreInvoice, why: str) -> None:
    """A "declined" notify for an order a payment was already reported for, and the report could not settle it."""
    numbers = ', '.join(n.index for n in open_numbers(invoice)) or shown_number(invoice.tranzila_transaction_id)
    _alert(
        invoice, kind='store_payment_conflict', key=f'store_payment_conflict:{invoice.pk}',
        title='הודעת "נדחה" על הזמנה שכבר דווח עליה תשלום',
        step='הודעת טרנזילה על דחייה',
        what=(f'על ההזמנה כבר דווח תשלום (עסקה {numbers}), ואחר כך הגיעה הודעה שתשלום נדחה — כנראה ניסיון '
              'בלשונית אחרת. הדוח של טרנזילה לא אישר ולא שלל את התשלום, ולכן ההזמנה לא סומנה כנכשלה '
              'ונשארת בבדיקה. לא נמכר דבר ולא הופק מסמך.'),
        why=why,
        action=_check_in_tranzila(invoice),
    )


def alert_possible_double_charge(invoice: StoreInvoice, index: str, answer: str = ANSWER_UNKNOWN,
                                 *, others: Optional[list[str]] = None) -> None:
    """
    A further number reported for the same order, which the report has not
    confirmed. At once, and once per order: the order's page in the CRM lists
    every number (other_transactions); a stream of numbers is one event. One
    the report confirms has its own alert (`alert_second_charge_confirmed`).
    """
    said = {
        ANSWER_REJECTED: 'הדוח של טרנזילה לא מאשר אותה כחיוב של ההזמנה',
    }.get(answer, 'הדוח של טרנזילה עוד לא הכריע לגביה')
    further = ', '.join(dict.fromkeys(shown_number(i) for i in ([index] + list(others or [])) if i))
    _alert(
        invoice, kind='store_possible_double_charge', key=f'store_double:{invoice.pk}',
        title='ייתכן חיוב כפול בחנות',
        step='הודעות טרנזילה על תשלום',
        what=(f'על הזמנה {_order_ref(invoice)} יש יותר מתשלום אחד: עסקה '
              f'{shown_number(invoice.tranzila_transaction_id)} ועסקה {further} ({said}). '
              'ייתכן שהלקוח שילם פעמיים — למשל בשתי לשוניות. המכירה נרשמת פעם אחת בלבד.'),
        why='אותה הזמנה קיבלה, או שבדוח נמצא לה, יותר ממספר עסקה אחד.',
        action=(f'לבדוק בטרנזילה את העסקאות ({shown_number(invoice.tranzila_transaction_id)}, {further}). '
                'אם שתיים אושרו — לזכות אחת. לא לחייב שוב. כל המספרים מופיעים בחשבונית.'),
        extra={'second_transaction': shown_number(index)},
    )


def alert_second_charge_confirmed(invoice: StoreInvoice, index: str) -> None:
    """
    The report confirms a second charge on a paid order: recorded, to refund.
    Once per number — it comes from the report itself, so it cannot be
    flooded — and whatever the sweep's switch reads.
    """
    _alert(
        invoice, kind='store_second_charge_confirmed', key=f'store_second:{invoice.pk}:{index}',
        title='חיוב שני מאושר בחנות — לזכות',
        step='אישור התשלום מול הדוח של טרנזילה',
        what=(f'הזמנה {_order_ref(invoice)} שולמה בעסקה {shown_number(invoice.tranzila_transaction_id)}, והדוח של '
              f'טרנזילה מאשר שגם עסקה {shown_number(index)} על ₪{invoice.total_amount} היא חיוב אמיתי של אותה '
              'הזמנה: הלקוח שילם פעמיים. המכירה נרשמה פעם אחת; על החיוב השני לא הופק מסמך ולא נשלח מייל.'),
        why='אותה הזמנה שולמה פעמיים — למשל בשתי לשוניות, או בעמוד תשלום שנפתח פעמיים.',
        action=(f'לזכות בטרנזילה את עסקה {shown_number(index)} (₪{invoice.total_amount}) ולעדכן את הלקוח. '
                f'לא לזכות את {shown_number(invoice.tranzila_transaction_id)}. '
                'החיוב מופיע גם בתדריך הבוקר תחת "חיובים כפולים".'),
        extra={'second_transaction': shown_number(index)},
        per_episode=False,
    )


def alert_second_charge_found(invoice: StoreInvoice, indexes: list[str]) -> None:
    """
    The report shows a charge of the same sum made on an earlier page of an
    order that is already paid: kept on the invoice (suspected) until a
    person decides. Told per charge found — the report is its source — also
    when the order was already told about another number.
    """
    indexes = [i for i in dict.fromkeys(indexes) if i]
    if not indexes:
        return
    numbers = ', '.join(shown_number(i) for i in indexes[:5])
    _alert(
        invoice, kind='store_possible_double_charge', key=f'store_double_found:{invoice.pk}:{indexes[0]}',
        title='ייתכן חיוב שני בחנות — נמצא בדוח של טרנזילה',
        step='בדיקת הבוקר של תשלומים בחנות',
        what=(f'הזמנה {_order_ref(invoice)} שולמה בעסקה {shown_number(invoice.tranzila_transaction_id)}, ונפתח לה '
              f'יותר מעמוד תשלום אחד. בדוח של טרנזילה יש עוד חיוב מאושר באותו סכום מאז העמוד הראשון: {numbers}. '
              'ייתכן שהלקוח שילם פעמיים. העסקה נשמרה בחשבונית ותופיע בתדריך עד שתוכרע.'),
        why='לא הגיעה עליה הודעה. המסוף משותף עם האתר השני, כך שזה עשוי גם להיות תשלום של לקוח אחר.',
        action=('לבדוק בטרנזילה של מי העסקה. של הלקוח — לזכות אותה, ובמסך החשבונית "השלם אחרי אימות" עם '
                'מספר האישור או 4 ספרות הכרטיס (נרשם כחיוב שני). לא שלו — "סגור — לא שלנו".'),
        extra={'second_transaction': shown_number(indexes[0])},
        per_episode=False,
    )


def alert_too_many_numbers(invoice: StoreInvoice, index: str, *, kept: bool = True) -> None:
    """
    More reported numbers the report has not answered for than
    MAX_UNDECIDED_NUMBERS on one order. Once per order. They are all kept
    (up to MAX_KEPT_NUMBERS) and asked about in turns.
    """
    _alert(
        invoice, kind='store_too_many_numbers', key=f'store_too_many_numbers:{invoice.pk}',
        title='יותר מדי מספרי עסקה על הזמנה אחת בחנות',
        step='הודעות טרנזילה / דיווח מהאתר',
        what=(f'על הזמנה {_order_ref(invoice)} דווחו יותר מ-{MAX_UNDECIDED_NUMBERS} מספרי עסקה שהדוח של טרנזילה '
              f'לא אישר (האחרון: {shown_number(index)}). '
              + ('כולם נשמרו בחשבונית ונבדקים מול הדוח בתורות, כמה בכל בדיקה. '
                 if kept else f'כבר נשמרו {MAX_KEPT_NUMBERS} מספרים, ולכן האחרון לא נשמר. ')
              + 'מספר שהדוח מאשר נשמר תמיד.'),
        why='כנראה מספרים שגויים או מזויפים שנשלחו על ההזמנה.',
        action='לבדוק בטרנזילה את התשלומים של ההזמנה, ולבדוק מי שלח את המספרים (האתר / הודעות טרנזילה).',
    )


def alert_payment_confirmed(invoice: StoreInvoice, index: str) -> None:
    """The morning sweep found the report confirms a payment it was not allowed to complete. Once per invoice."""
    if invoice.website_order_number:
        action = ('לא לבקש מהלקוח לשלם שוב. ההזמנה תושלם לבד כשהאתר ישאל עליה או כשהלקוח ינסה לשלם שוב. '
                  'אפשר להשלים אותה כבר עכשיו במסך החשבונית: "השלם אחרי אימות".')
    else:
        action = ('לא לבקש מהלקוח לשלם שוב. להשלים את ההזמנה במסך החשבונית: "השלם אחרי אימות" — '
                  'בקופה אין מי שישאל עליה לבד.')
    _alert(
        invoice, kind='store_payment_confirmed', key=f'store_payment_confirmed:{invoice.pk}',
        title='הדוח של טרנזילה מאשר תשלום בחנות — ההזמנה לא הושלמה',
        step='בדיקת הבוקר של תשלומים תקועים',
        what=(f'עסקה {shown_number(index)} על ₪{invoice.total_amount} מאושרת בדוח של טרנזילה: הלקוח שילם. '
              'בדיקת הבוקר לא משלימה מכירות (STORE_SWEEP_COMPLETES_PAYMENTS כבוי), ולכן עדיין לא נמכר דבר, '
              'המלאי לא ירד ולא הופק מסמך.'),
        why='ההודעה של טרנזילה לא אומתה בזמנה, והדוח מאשר רק עכשיו.',
        action=action,
    )


def alert_payment_unreported(invoice: StoreInvoice, rows: list[dict], *, suspected: Optional[list[str]] = None) -> None:
    """
    A charge in the terminal's report may be this order's payment, whose notify
    never came. Once per invoice (and again after a release): the order is
    held in review until a person decides.
    """
    from apps.core.tranzila_service import report_transaction_amount

    seen = '; '.join(
        [f"עסקה {shown_number(row.get('index') or row.get('transaction_index'))} על ₪{report_transaction_amount(row)}"
         for row in rows[:3]]
        + [f'עסקה {shown_number(index)}' for index in (suspected or [])[:3]]
    )
    _alert(
        invoice, kind='store_payment_unreported', key=f'store_payment_unreported:{invoice.pk}',
        title='ייתכן שהלקוח כבר שילם — ההזמנה בבדיקה',
        step='תשלום שלא הגיעה עליו הודעה',
        what=(f'בדוח של טרנזילה יש חיוב מאושר באותו סכום של הזמנה {_order_ref(invoice)}, אחרי שנפתח לה עמוד '
              f'התשלום, בלי שהגיעה עליו הודעה: {seen}. ההזמנה בבדיקה: לא נפתח לה עמוד נוסף ולא נמכר דבר.'),
        why=('ייתכן שההודעה של טרנזילה על התשלום לא הגיעה. המסוף משותף עם האתר השני, כך שזה עשוי גם '
             'להיות תשלום של לקוח אחר.'),
        action=('לבדוק בטרנזילה אם העסקה שייכת להזמנה הזאת, ולהחליט במסך החשבונית: "השלם אחרי אימות" '
                'או "אין תשלום — שחרר". עד אז הלקוח לא יתבקש לשלם שוב.'),
    )


def alert_report_unavailable(invoice: StoreInvoice) -> None:
    """
    A customer asked to pay an order again and the report could not say
    whether an earlier page was paid — it did not answer, answered an error,
    or was read only in part. No second page without that check, however long
    it lasts; the office hears once per order.
    """
    _alert(
        invoice, kind='store_report_unavailable', key=f'store_report_unavailable:{invoice.pk}',
        title='הדוח של טרנזילה לא זמין — לקוח מחכה',
        step='בקשה לשלם שוב על הזמנה שכבר נפתח לה עמוד תשלום',
        what=(f'הלקוח ביקש לשלם שוב על הזמנה {_order_ref(invoice)}, אחרי שכבר נפתח לה עמוד תשלום. הדוח של '
              'טרנזילה לא ענה או נקרא רק בחלקו, ולכן אי אפשר לדעת אם העמוד הקודם נגבה. לא נפתח עמוד נוסף, '
              'והלקוח מחכה.'),
        why='בלי הדוח אי אפשר לשלול שהלקוח כבר שילם, ועמוד נוסף עלול לחייב אותו פעמיים.',
        action=('לבדוק בטרנזילה אם יש חיוב על הסכום הזה מאז שנפתח העמוד. יש — לא לבקש תשלום שוב ולהעביר '
                'לבדיקה טכנית. אין — לומר ללקוח לנסות שוב בעוד כמה דקות; עמוד ייפתח כשהדוח יחזור לענות.'),
    )


def alert_website_not_told(invoice: StoreInvoice, why: str) -> None:
    """A paid order the site has not acknowledged after STUCK_AFTER. Once per invoice."""
    _alert(
        invoice, kind='store_website_not_told', key=f'store_website_not_told:{invoice.pk}',
        title='האתר לא יודע שהזמנה שולמה',
        step='עדכון האתר שההזמנה שולמה',
        what=(f'הזמנה {invoice.website_order_number} שולמה ונרשמה ב-CRM (חשבונית {invoice.invoice_number}, '
              f'₪{invoice.total_amount}), אבל האתר לא אישר שקיבל את העדכון. באתר היא עדיין "ממתינה לתשלום", '
              'וייתכן שמייל ההזמנה לצוות לא יצא.'),
        why=why,
        action=('לטפל בהזמנה מתוך ה-CRM (המכירה, המלאי והמסמך כבר נרשמו). לא לבקש מהלקוח לשלם שוב. '
                'המערכת תנסה לעדכן את האתר שוב בבדיקת הבוקר.'),
    )


def alert_oversold(invoice: StoreInvoice, lines: list[dict]) -> None:
    """A paid sale that took more units than the shelf had. The sale is kept."""
    described = '; '.join(
        f"{line['name']}{(' מידה ' + line['size']) if line.get('size') else ''} — הוזמנו {line['quantity']}, "
        f"היו במלאי {line['available']}"
        for line in lines
    )
    _alert(
        invoice, kind='store_oversold', key=f'store_oversold:{invoice.pk}',
        title='נמכר מעבר למלאי בחנות',
        step='רישום המכירה אחרי התשלום',
        what=f'ההזמנה שולמה והמכירה נרשמה, אבל במלאי לא היו מספיק יחידות: {described}.',
        why='המלאי השתנה בין פתיחת עמוד התשלום לבין התשלום (קנייה אחרת, או עדכון מלאי ידני).',
        action=('לבדוק את המלאי בפועל. אם המוצר חסר — לחזור ללקוח ולהציע החלפה או זיכוי. '
                'המכירה נשמרה כמו שהיא; לא לחייב שוב.'),
    )


def alert_payment_page_failed(invoice: StoreInvoice, error: str, terminal: str) -> None:
    """Tranzila's page could not be opened for a website order. Once a day."""
    _alert(
        invoice, kind='store_page_failed', key=f'store_page_failed:{timezone.localdate().isoformat()}',
        title='עמוד התשלום של החנות באתר לא נפתח',
        step='פתיחת עמוד התשלום (handshake מול טרנזילה)',
        what=('לקוח הגיע לתשלום באתר ועמוד טרנזילה לא נפתח. הלקוח קיבל הודעה שהתשלום אינו זמין כרגע; '
              'לא ירד כסף וההזמנה סומנה כנכשלה, כך שאפשר לנסות שוב. הלקוח המופיע כאן הוא הראשון היום.'),
        why=str(error)[:300],
        action=(f'לבדוק את מסוף {terminal or "(לא מוגדר)"} ואת המפתחות שלו. אם זה חוזר — לפנות לטרנזילה. '
                'ההתראה הזאת נשלחת פעם אחת ביום.'),
        per_episode=False,
    )
