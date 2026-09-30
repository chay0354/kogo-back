"""
A store payment on Tranzila's hosted page that did not end cleanly — the CRM
half of stage 3 of the cogolive plan (the website store opens on cogolive).

The website's checkout, and the till's walk-in "secure page", pay on
Tranzila's page. Tranzila's notify completes the sale
(PaymentService.complete_store_purchase_from_webhook): the notify is public
and unsigned, so the sale happens only when the terminal's own report shows
the same transaction.

The rule everything here keeps (reviews of 29-30.9.2026): an invoice that
holds a transaction number Tranzila reported — or a charge found in the
terminal's report that may be its payment — and is not settled, is IN
REVIEW, whatever its status reads. It is never failed on the word of a later
notify (only on the report's definite "no", or a person's), never handed a
second payment page, never dropped from the follow-up, and no number reported
for it is ever lost: the first one stays tranzila_transaction_id, every other
one is kept in other_transactions with what the report said about it. A cart
is sold once, whatever writes over a status.

What can go wrong after the customer paid, and what answers it:

  1. The notify came, but the report could not be asked, or does not list
     the number yet, or disagrees. The invoice stays in review with the
     number. `recheck_pending_payment` asks the report again about every
     number the invoice holds and, when the report confirms one, completes
     the sale through the same locked path as the notify
     (PaymentService.settle_reported_store_payment). Nothing here ever
     charges or refunds; the only call to Tranzila is the report read.
  2. The sale is complete, but the website never heard: `tell_website_paid`
     keeps the site's answer on the invoice (website_paid_notified_at), and
     the call is repeated until it lands.
  3. A second number for the same order (two tabs, a page paid twice): kept,
     told to the office at once as a possible double charge, and — once the
     report confirms it — recorded as a second charge for a refund.
  4. The notify never came at all. The site can report the number it got
     back from Tranzila's page (widget/payment/returned/, `record_returned_number`)
     — believed exactly as much as a notify. Without it, the terminal's report
     is searched for an approved charge of the same sum after the order's page
     opened (`find_unreported_payment`): by a retry of the order (a match is
     kept on the invoice as "suspected" and blocks a second page until a
     person decides) and by the morning sweep (it lists and tells). A
     suspected charge is never completed by itself: the terminal is shared
     with the other website, so a person looks.
  5. Any of it stays that way: the office hears (apps/core/office_alerts.py),
     once per event, with who the customer is, the sum, the transaction
     number and what to check in Tranzila. A person settles an invoice in
     review with the managers' tool (`complete_reported_payment` — only
     through the report — or `release_reported_payment`, after checking
     Tranzila), with a reason, and who and when are kept on the invoice.

Who drives the follow-up:
  * the website's status poll, GET /api/v1/store/widget/payment/status/
    (`website_order_status`), at most once per RECHECK_INTERVAL per invoice;
  * the morning brief's sweep, "תשלומים שנתקעו בחנות"
    (`sweep_stuck_store_payments`). With STORE_SWEEP_COMPLETES_PAYMENTS off
    (until the owner decides) it only READS and TELLS: it writes nothing to
    any invoice — no sale, no document, no email, no status. On, it settles
    what the report confirms, as the site's poll does;
  * the site asking to pay an order again (widget/payment/initiate/);
  * another notify for the same invoice (the ordinary path).
There is no cron of its own: a new Vercel cron is a Level-2 decision (plan
item 2.7), so between the site's polls and the morning an invoice waits.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
# The report may lag the notify by a little. A number it does not list yet is
# "not known yet" for this long after it was reported, and "not there" after.
REPORT_SETTLE = timedelta(minutes=10)
# A report that cannot be asked stops a retry this soon after a page was
# handed out; after that the retry gets its page.
UNREPORTED_WINDOW = timedelta(minutes=30)
# How far back a payment whose notify never came is looked for, and how far
# back invoices from before 29.9.2026 (no payment_reported_at) are swept.
SEARCH_WINDOW = timedelta(days=3)
LEGACY_WINDOW = timedelta(days=3)
# The sweep runs inside the morning brief's request (the platform cuts a request
# at 300 seconds, and every report read may take up to 30). What it does not
# reach in time it lists as not checked, and the next morning carries on.
SWEEP_BUDGET_SECONDS = 60
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

# other_transactions[*].state
OTHER_OPEN = 'open'
OTHER_SECOND_CHARGE = 'second_charge'
OTHER_REJECTED = 'rejected'
OTHER_SUSPECTED = 'suspected'

# What a recheck ended in.
RECHECK_COMPLETED = 'completed'
RECHECK_CONFIRMED = 'confirmed'      # the report confirms it; completing was not allowed here
RECHECK_PENDING = 'pending'          # asked; still in review
RECHECK_PAID = 'paid'                # a settled invoice: its further numbers were asked about
RECHECK_PACED = 'paced'              # asked less than RECHECK_INTERVAL ago
RECHECK_NOT_PENDING = 'not_pending'  # nothing reported and unsettled
RECHECK_NOT_ELIGIBLE = 'not_eligible'

# The report's dates and times are Israel's.
REPORT_TZ = ZoneInfo('Asia/Jerusalem')

WHERE_WEBSITE = 'חנות האתר'
WHERE_TILL = 'קופה — עמוד התשלום של טרנזילה'


# ---------------------------------------------------------------------------
# Transaction numbers
# ---------------------------------------------------------------------------

def is_transaction_number(value) -> bool:
    return str(value or '').strip().isdigit()


def shown_number(value) -> str:
    """A transaction number for an alert or the brief — never anything else that sat in its place."""
    value = str(value or '').strip()
    return value if value.isdigit() else '(לא ידוע)'


def notify_index(tranzila_response: dict) -> str:
    """
    The transaction number a store notify reported: Tranzila's `index`, digits only.

    parse_webhook_response falls back to TranzilaTK (the card's token) when the
    POST has no index. On the store's path a token must never be kept as a
    transaction number, printed in an alert or shown in the brief, so the
    number is read from the POST itself, and from `transaction_id` only when
    there is no POST (a caller that already holds the number).
    """
    raw = tranzila_response.get('raw_payload')
    value = raw.get('index', '') if isinstance(raw, dict) else tranzila_response.get('transaction_id', '')
    value = str(value or '').strip()
    return value if value.isdigit() else ''


@dataclass(frozen=True)
class ReportedNumber:
    index: str
    code: str
    terminal: str
    reported_at: datetime
    primary: bool
    suspected: bool = False


def _is_till_charge(invoice: StoreInvoice) -> bool:
    """A till charge of a saved or typed card: no hosted page, and its own "uncertain" handling."""
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

    return bool(invoice.charged_with_token) or invoice.tranzila_confirmation_code == TILL_CHARGE_UNCERTAIN_MARK


def _parse_time(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    return parse_datetime(str(value or ''))


def _entry_number(invoice: StoreInvoice, entry: dict, *, suspected: bool = False) -> ReportedNumber:
    return ReportedNumber(
        index=str(entry['index']),
        code=str(entry.get('code') or ''),
        terminal=str(entry.get('terminal') or ''),
        reported_at=_parse_time(entry.get('reported_at')) or invoice.created_at,
        primary=False,
        suspected=suspected,
    )


def open_numbers(invoice: StoreInvoice, *, include_suspected: bool = False) -> list[ReportedNumber]:
    """
    Every number reported for this invoice that is not settled: the invoice's
    own and the further ones. A suspected charge (found in the report, never
    reported for this order) only for a person's decision.
    """
    numbers = []
    if (
        invoice.payment_status in UNSETTLED_STATUSES
        and not _is_till_charge(invoice)
        and is_transaction_number(invoice.tranzila_transaction_id)
    ):
        numbers.append(ReportedNumber(
            index=invoice.tranzila_transaction_id.strip(),
            code=(invoice.tranzila_confirmation_code or '').strip(),
            terminal=(invoice.tranzila_terminal or '').strip(),
            reported_at=invoice.payment_reported_at or invoice.created_at,
            primary=True,
        ))
    for entry in invoice.other_transactions or []:
        if not is_transaction_number(entry.get('index')):
            continue
        if entry.get('state') == OTHER_OPEN:
            numbers.append(_entry_number(invoice, entry))
        elif include_suspected and entry.get('state') == OTHER_SUSPECTED and invoice.payment_status in UNSETTLED_STATUSES:
            numbers.append(_entry_number(invoice, entry, suspected=True))
    return numbers


def suspected_numbers(invoice: StoreInvoice) -> list[ReportedNumber]:
    return [
        _entry_number(invoice, entry, suspected=True)
        for entry in invoice.other_transactions or []
        if entry.get('state') == OTHER_SUSPECTED and is_transaction_number(entry.get('index'))
    ]


def holds_reported_payment(invoice: StoreInvoice) -> bool:
    """
    In review: a number Tranzila reported — or a charge in the report that may
    be this order's — and the invoice's payment not settled, whatever its
    status reads.
    """
    if invoice.payment_status not in UNSETTLED_STATUSES:
        return False
    return bool(open_numbers(invoice)) or bool(suspected_numbers(invoice))


def number_for(invoice: StoreInvoice, index: str, code: str, terminal: str) -> ReportedNumber:
    """
    A number a notify reports now. One the invoice already holds keeps the time
    it was first reported; a new one — or one that takes the place of a number
    ruled out — starts its own clock now.
    """
    if index == (invoice.tranzila_transaction_id or '').strip():
        return ReportedNumber(index, code, terminal, invoice.payment_reported_at or timezone.now(), True)
    for entry in invoice.other_transactions or []:
        if str(entry.get('index')) == index and entry.get('state') == OTHER_OPEN:
            return ReportedNumber(index, code, terminal, _parse_time(entry.get('reported_at')) or timezone.now(), False)
    return ReportedNumber(index, code, terminal, timezone.now(), not is_transaction_number(invoice.tranzila_transaction_id))


def other_state(invoice: StoreInvoice, index: str) -> str:
    for entry in invoice.other_transactions or []:
        if str(entry.get('index')) == str(index):
            return str(entry.get('state') or '')
    return ''


def keep_other_transaction(invoice: StoreInvoice, number: ReportedNumber, state: str, **extra) -> bool:
    """
    Keep a further number on the invoice (the caller holds the row lock and
    saves). A number already kept only moves on from open or suspected. True
    when new.
    """
    entries = [dict(entry) for entry in (invoice.other_transactions or [])]
    for entry in entries:
        if str(entry.get('index')) == number.index and str(entry.get('terminal') or '') == number.terminal:
            if entry.get('state') in (OTHER_OPEN, OTHER_SUSPECTED) and state != entry.get('state'):
                entry['state'] = state
                entry['settled_at'] = timezone.now().isoformat()
                entry.update(extra)
            invoice.other_transactions = entries
            return False
    entries.append({
        'index': number.index, 'code': number.code, 'terminal': number.terminal,
        'reported_at': number.reported_at.isoformat(), 'state': state, **extra,
    })
    invoice.other_transactions = entries
    return True


def drop_other_transaction(invoice: StoreInvoice, index: str) -> None:
    """The number became the invoice's own (it paid for it): it leaves the further ones."""
    entries = [e for e in (invoice.other_transactions or []) if str(e.get('index')) != str(index)]
    invoice.other_transactions = entries or None


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

    Only "rejected" may fail an order on a "declined" notify.
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
        return ANSWER_UNKNOWN, None, 'הדוח של טרנזילה לא ענה'
    if row is not None:
        if not is_tranzila_approved(row.get('processor_response_code') or row.get('response_code')):
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה נדחתה'
        if str(row.get('tranmode') or '').strip().upper() not in CHARGE_TRANMODES:
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה אינה חיוב (בדיקת כרטיס או זיכוי)'
        if report_transaction_amount(row) != money(invoice.total_amount):
            return ANSWER_REJECTED, row, 'בדוח של טרנזילה העסקה על סכום אחר'
        if not number.code:
            return ANSWER_UNKNOWN, row, 'לא התקבל מספר אישור להשוות אליו'
        return ANSWER_DISPUTED, row, ('העסקה בדוח של טרנזילה לא תואמת את ההזמנה (מספר אישור, מועד, '
                                      'או שהיא כבר שייכת להזמנה אחרת)')
    if timezone.now() - number.reported_at >= REPORT_SETTLE:
        return ANSWER_DISPUTED, None, 'העסקה לא נמצאה בדוח של טרנזילה'
    return ANSWER_UNKNOWN, None, 'העסקה עוד לא מופיעה בדוח של טרנזילה'


# ---------------------------------------------------------------------------
# Which invoices this is about
# ---------------------------------------------------------------------------

def _in_review():
    """
    Invoices a notify reported a number for (or a suspected charge was kept
    for) that are not settled: hosted-page payments (website or till),
    whatever their status reads. A till charge of a saved or typed card never
    had a hosted page and has its own handling.
    """
    from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

    return (
        StoreInvoice.objects
        .filter(payment_status__in=UNSETTLED_STATUSES, charged_with_token=False)
        .filter(
            Q(tranzila_transaction_id__regex=r'^[0-9]+$')
            | Q(other_transactions__contains=[{'state': OTHER_OPEN}])
            | Q(other_transactions__contains=[{'state': OTHER_SUSPECTED}])
        )
        .exclude(tranzila_confirmation_code=TILL_CHARGE_UNCERTAIN_MARK)
    )


def _settled_with_open_numbers():
    """Paid invoices a further number was reported for that the report has not settled yet."""
    return StoreInvoice.objects.filter(
        payment_status__in=PAID_STATUSES, other_transactions__contains=[{'state': OTHER_OPEN}],
    )


def _pages_with_nothing_reported(since):
    """Website orders a page was handed out for since `since`, with no number reported or kept."""
    return (
        StoreInvoice.objects
        .filter(payment_status__in=UNSETTLED_STATUSES, website_order_number__isnull=False,
                payment_page_opened_at__gte=since, tranzila_transaction_id='')
        .exclude(website_order_number='')
        .exclude(other_transactions__contains=[{'state': OTHER_OPEN}])
        .exclude(other_transactions__contains=[{'state': OTHER_SUSPECTED}])
    )


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
    """Why the report cannot settle this invoice's own number by itself ('' when it can)."""
    from apps.core.payment_service import parse_store_cart_notes
    from apps.core.tranzila_service import TranzilaService

    if not open_numbers(invoice):
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
                            write: bool = True, site_timeout: Optional[float] = None) -> str:
    """
    Ask Tranzila's report again about every number reported for ONE invoice
    that is not settled, and settle what the report now allows
    (PaymentService.settle_reported_store_payment): a confirmed number
    completes the sale — when `complete` — through the locked path the notify
    uses; on a paid invoice, a confirmed further number is a second charge.
    With `write` off (the morning sweep while its switch is off) it only reads
    the report and tells the office: nothing on the invoice changes, not even
    the pace-keeper. Nothing is ever charged; the report is read.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return RECHECK_NOT_ELIGIBLE
    if not open_numbers(invoice):
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
        invoice.pk, complete=complete and write, write=write, site_timeout=site_timeout,
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
    The site's poll for one order. A reported payment in review is asked about
    again (paced) and completed when the report confirms it — the customer is
    waiting for exactly that; a paid order the site never acknowledged is told
    again. None when there is no such order.
    """
    invoice = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
    if invoice is None:
        return None
    try:
        if holds_reported_payment(invoice):
            recheck_pending_payment(invoice.pk, site_timeout=POLL_SITE_TIMEOUT_SECONDS)
            invoice.refresh_from_db()
        elif invoice.payment_status == 'completed' and invoice.website_paid_notified_at is None:
            retry_website_paid(invoice.pk, timeout=POLL_SITE_TIMEOUT_SECONDS)
    except Exception:
        # The poll always gets the stored answer; the sweep tries again.
        logger.exception('Store order %s: follow-up from the status poll failed', website_order_number)
        invoice.refresh_from_db()
    return site_status(invoice)


def record_returned_number(website_order_number: str, index: str, code: str = '') -> Optional[dict]:
    """
    The number the site got back from Tranzila's page (its return address),
    for an order whose notify may never come. Believed exactly as much as a
    notify — which is not at all: it is recorded as reported and judged by
    the report through the notify's own path. A number that is not this
    order's payment does not pass, and is kept for the office like any other.
    None when there is no such order.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
    if invoice is None:
        return None
    PaymentService().complete_store_purchase_from_webhook(
        str(invoice.pk),
        {'is_successful': True, 'transaction_id': index, 'confirmation_code': code},
        site_timeout=POLL_SITE_TIMEOUT_SECONDS,
    )
    invoice.refresh_from_db()
    return site_status(invoice)


# ---------------------------------------------------------------------------
# 3. A notify that never came
# ---------------------------------------------------------------------------

def _day_report(first_day, last_day) -> Optional[list[dict]]:
    """The page terminal's report rows for these days, or None when it cannot be asked."""
    from apps.core.tranzila_service import TranzilaService

    try:
        response = TranzilaService.iframe().list_all_transactions(first_day, last_day, max_pages=3)
    except Exception as exc:  # network, keys — never a reason to open a second page
        logger.error('Store: day report failed: %s', exc)
        return None
    if not response.get('success'):
        return None
    return list(response.get('transactions') or [])


def _index_explained(index: str, terminal: str, invoice_pk) -> bool:
    """Whether this number already belongs to another order, link, signup or record of ours on this terminal."""
    from apps.customers.models import CourseCheckout, TranzilaTransaction
    from apps.payment_links.models import PaymentLinkPayment

    others = StoreInvoice.objects.exclude(pk=invoice_pk)
    return (
        others.filter(tranzila_transaction_id=index, tranzila_terminal=terminal).exists()
        or others.filter(other_transactions__contains=[{'index': index, 'terminal': terminal}]).exists()
        or PaymentLinkPayment.objects.filter(
            Q(tranzila_terminal=terminal) | Q(tranzila_terminal=''), gateway_transaction_id=index,
        ).exists()
        or CourseCheckout.objects.filter(page_terminal=terminal, page_index=index).exists()
        or TranzilaTransaction.objects.filter(transaction_id=index, tranzila_terminal=terminal).exists()
    )


def find_unreported_payment(invoice: StoreInvoice, rows: Optional[list[dict]] = None) -> tuple[str, list[dict]]:
    """
    ('found', rows) | ('none', []) | ('unknown', []) — whether the terminal's
    report shows a payment for this order whose notify never came.

    Asked of the report of the days since the order's last page was handed
    out, on the page's terminal (or of `rows`, the sweep's one read of it). A
    candidate row is an approved charge (A/AK) of exactly this sum, made after
    the page was handed out, under a number no order, link, signup or record
    of ours holds. The report's rows carry no pdesc (seen on the NK page,
    29.9.2026), so a row is tied to the order only by sum and time; one that
    does carry a pdesc must carry this order's. The terminal is shared with
    the other website, so a match is a reason to stop and ask a person, never
    a reason to complete the sale.
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

    opened = invoice.payment_page_opened_at
    if opened is None:
        return 'none', []
    if rows is None:
        rows = _day_report(timezone.localtime(opened, REPORT_TZ).date(), timezone.localtime(timezone.now(), REPORT_TZ).date())
        if rows is None:
            return 'unknown', []
    terminal = (TranzilaService.iframe().terminal or '').strip()
    matches = []
    for row in rows:
        if not is_tranzila_approved(row.get('processor_response_code') or row.get('response_code')):
            continue
        if str(row.get('tranmode') or '').strip().upper() not in CHARGE_TRANMODES:
            continue
        if report_transaction_amount(row) != money(invoice.total_amount):
            continue
        made_at = report_transaction_time(row)
        if made_at is None or made_at < opened - TRANSACTION_CLOCK_SKEW:
            continue
        index = str(row.get('index') or row.get('transaction_index') or '').strip()
        if not index.isdigit():
            continue
        pdesc = str(row.get('pdesc') or '').strip()
        if pdesc and invoice_id_from_pdesc(pdesc) != str(invoice.pk):
            continue
        if _index_explained(index, terminal, invoice.pk):
            continue
        matches.append(row)
    return ('found', matches) if matches else ('none', [])


def keep_suspected_charges(invoice_id, rows: list[dict]) -> bool:
    """
    Keep charges the report shows for this order's page, whose notify never
    came, on the invoice as "suspected": the order is then in review (the
    site reads payment_reported) and gets no second page until a person
    decides. Only on an invoice still unsettled with nothing reported.
    True when kept.
    """
    from apps.core.tranzila_service import TranzilaService, report_transaction_time

    terminal = (TranzilaService.iframe().terminal or '').strip()
    with transaction.atomic():
        invoice = StoreInvoice.objects.select_for_update().filter(pk=invoice_id).first()
        if invoice is None or invoice.payment_status not in UNSETTLED_STATUSES:
            return False
        for row in rows:
            index = str(row.get('index') or row.get('transaction_index') or '').strip()
            keep_other_transaction(invoice, ReportedNumber(
                index=index, code=str(row.get('authorization_number') or '').strip()[:100], terminal=terminal,
                reported_at=report_transaction_time(row) or timezone.now(), primary=False, suspected=True,
            ), OTHER_SUSPECTED)
        invoice.save(update_fields=['other_transactions'])
    return True


# ---------------------------------------------------------------------------
# 4. A person decides
# ---------------------------------------------------------------------------

def _log_review(invoice: StoreInvoice, *, action: str, by: str, reason: str, numbers: list[str], outcome: str) -> None:
    log = list(invoice.payment_review_log or [])
    log.append({
        'action': action, 'by': by, 'at': timezone.now().isoformat(), 'reason': reason,
        'numbers': numbers, 'outcome': outcome,
    })
    invoice.payment_review_log = log


def complete_reported_payment(invoice_id, *, by: str, reason: str, confirmation_code: str = '') -> dict:
    """
    A person, having looked, asks to complete an order in review. Only through
    the report: the numbers it holds — a suspected charge included — are asked
    about, and the sale happens on the locked path only if the report confirms
    one. `confirmation_code` fills an approval number the order never got (a
    number reported without one), for the report to compare with. Who, when
    and why are kept on the invoice either way.
    """
    from apps.core.payment_service import PaymentService

    invoice = StoreInvoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return {'outcome': 'not_found'}
    numbers = [n.index for n in open_numbers(invoice, include_suspected=True)]
    if not holds_reported_payment(invoice):
        return {'outcome': 'not_in_review', 'status': invoice.payment_status}
    result = PaymentService().settle_reported_store_payment(
        invoice.pk, complete=True, include_suspected=True, code_for_missing=confirmation_code.strip()[:100],
    )
    with transaction.atomic():
        locked = StoreInvoice.objects.select_for_update().get(pk=invoice.pk)
        _log_review(locked, action='complete', by=by, reason=reason, numbers=numbers,
                    outcome=result.get('outcome', ''))
        locked.save(update_fields=['payment_review_log'])
    logger.warning('Store invoice %s: %s asked to complete (%s): %s', invoice.invoice_number, by, reason,
                   result.get('outcome'))
    return result


def release_reported_payment(invoice_id, *, by: str, reason: str) -> dict:
    """
    A person, having checked Tranzila, says no payment came for this order:
    every number it holds in review is marked rejected with the reason, the
    order is failed (the customer may pay again) and the site is told.
    Nothing is charged, refunded or deleted; who, when and why are kept.
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
            keep_other_transaction(invoice, number, OTHER_REJECTED, **marks)
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


# ---------------------------------------------------------------------------
# 5. The morning sweep
# ---------------------------------------------------------------------------

def sweep_stuck_store_payments(*, budget_seconds: float = SWEEP_BUDGET_SECONDS,
                               complete: Optional[bool] = None) -> dict:
    """
    Every invoice in review (at any age once this follow-up recorded when its
    payment was reported), every paid invoice with a further number not
    settled yet, every website order whose page was opened in SEARCH_WINDOW
    with nothing reported (searched for in the report), and every paid
    website order the site has not acknowledged.

    With STORE_SWEEP_COMPLETES_PAYMENTS on (`complete`), it settles what the
    report allows, as the site's poll does. Off, it only reads and tells: no
    sale, no document, no email, no status, nothing written to an invoice in
    review — the one thing it still does is repeat the "paid" call for an
    order already paid, to our own website, and keep the site's answer. A
    charge found for an order whose notify never came is listed and told,
    never completed. Never charges; never raises for one invoice.

    Returns lists of invoices: 'settled' (completed now), 'confirmed' (the
    report confirms it, not completed here), 'still_pending' (with the reason),
    'second_open' (a further number still unsettled), 'unexplained' (a charge
    in the report may be its payment), 'site_told', 'site_not_told',
    'not_reached' (the budget ran out before them).
    """
    if complete is None:
        complete = bool(getattr(settings, 'STORE_SWEEP_COMPLETES_PAYMENTS', False))
    write = complete
    started = time.monotonic()
    now = timezone.now()
    result = {'settled': [], 'confirmed': [], 'still_pending': [], 'second_open': [], 'unexplained': [],
              'site_told': [], 'site_not_told': [], 'not_reached': []}

    def out_of_time() -> bool:
        return time.monotonic() - started > budget_seconds

    in_review = (
        _in_review()
        .filter(Q(payment_reported_at__isnull=False) | Q(created_at__gte=now - LEGACY_WINDOW)
                | Q(other_transactions__contains=[{'state': OTHER_SUSPECTED}]))
        .order_by(F('payment_reported_at').desc(nulls_last=True), '-created_at')
    )
    for invoice in in_review:
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        if not open_numbers(invoice) and suspected_numbers(invoice):
            # Found in the report, never reported for it: a person decides.
            alert_payment_unreported(invoice, [], suspected=[n.index for n in suspected_numbers(invoice)])
            result['unexplained'].append(invoice)
            continue
        try:
            outcome = recheck_pending_payment(invoice.pk, complete=complete, write=write)
        except Exception as exc:  # noqa: BLE001 — one invoice never stops the rest
            logger.exception('Store sweep: recheck of %s failed', invoice.invoice_number)
            outcome = f'error: {exc}'
        invoice.refresh_from_db()
        if invoice.payment_status == 'completed':
            result['settled'].append(invoice)
            continue
        if outcome == RECHECK_CONFIRMED:
            result['confirmed'].append(invoice)
            continue
        if not holds_reported_payment(invoice):
            continue
        reason = not_rechecked_because(invoice) or {
            RECHECK_PACED: 'נבדק ממש עכשיו מול טרנזילה, ועדיין לא אושר',
            RECHECK_PENDING: 'הדוח של טרנזילה עדיין לא מאשר את העסקה',
        }.get(outcome, str(outcome))
        alert_if_stuck(invoice, why=reason)
        result['still_pending'].append((invoice, reason))

    for invoice in _settled_with_open_numbers().order_by('-created_at'):
        if out_of_time():
            result['not_reached'].append(invoice)
            continue
        try:
            recheck_pending_payment(invoice.pk, complete=complete, write=write)
        except Exception:  # noqa: BLE001
            logger.exception('Store sweep: further numbers of %s not asked', invoice.invoice_number)
        invoice.refresh_from_db()
        if open_numbers(invoice):
            result['second_open'].append(invoice)

    # Orders whose page was opened and whose notify may never have come: one
    # read of the report for all of them.
    lost = list(_pages_with_nothing_reported(now - SEARCH_WINDOW).order_by('payment_page_opened_at'))
    if lost and not out_of_time():
        first = min(timezone.localtime(i.payment_page_opened_at, REPORT_TZ).date() for i in lost)
        rows = _day_report(first, timezone.localtime(now, REPORT_TZ).date())
        for invoice in lost:
            if rows is None:
                break  # the report cannot be asked: the next morning looks again
            found, matches = find_unreported_payment(invoice, rows)
            if found == 'found':
                # Kept on the order (switch on) so the site reads it as a
                # payment in review and a retry gets no second page; switch
                # off, only listed and told — a retry keeps it itself.
                if write:
                    keep_suspected_charges(invoice.pk, matches)
                alert_payment_unreported(invoice, matches)
                result['unexplained'].append(invoice)
    elif lost:
        result['not_reached'].extend(lost)

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


def _alert(invoice: StoreInvoice, *, kind: str, key: str, title: str, step: str, what: str,
           why: str = '', action: str = '', extra: Optional[dict] = None) -> None:
    from apps.core.office_alerts import raise_office_alert

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


def alert_possible_double_charge(invoice: StoreInvoice, index: str, answer: str = ANSWER_UNKNOWN) -> None:
    """A further number reported for the same order. At once, once per number."""
    said = {
        ANSWER_VERIFIED: 'הדוח של טרנזילה מאשר שגם היא חיוב אמיתי',
        ANSWER_REJECTED: 'הדוח של טרנזילה לא מאשר אותה כחיוב של ההזמנה',
    }.get(answer, 'הדוח של טרנזילה עוד לא ענה עליה')
    _alert(
        invoice, kind='store_possible_double_charge', key=f'store_double:{invoice.pk}:{index}',
        title='ייתכן חיוב כפול בחנות',
        step='הודעות טרנזילה על תשלום',
        what=(f'על הזמנה {_order_ref(invoice)} דווחו שני תשלומים: עסקה '
              f'{shown_number(invoice.tranzila_transaction_id)} ועסקה {shown_number(index)} ({said}). '
              'ייתכן שהלקוח שילם פעמיים — למשל בשתי לשוניות. המכירה נרשמת פעם אחת בלבד.'),
        why='אותה הזמנה קיבלה יותר ממספר עסקה אחד מטרנזילה.',
        action=(f'לבדוק בטרנזילה את שתי העסקאות ({shown_number(invoice.tranzila_transaction_id)}, '
                f'{shown_number(index)}). אם שתיהן אושרו — לזכות אחת מהן. לא לחייב שוב.'),
        extra={'second_transaction': shown_number(index)},
    )


def alert_payment_confirmed(invoice: StoreInvoice, index: str) -> None:
    """The morning sweep found the report confirms a payment it was not allowed to complete. Once per invoice."""
    _alert(
        invoice, kind='store_payment_confirmed', key=f'store_payment_confirmed:{invoice.pk}',
        title='הדוח של טרנזילה מאשר תשלום בחנות — ההזמנה לא הושלמה',
        step='בדיקת הבוקר של תשלומים תקועים',
        what=(f'עסקה {shown_number(index)} על ₪{invoice.total_amount} מאושרת בדוח של טרנזילה: הלקוח שילם. '
              'בדיקת הבוקר לא משלימה מכירות (STORE_SWEEP_COMPLETES_PAYMENTS כבוי), ולכן עדיין לא נמכר דבר, '
              'המלאי לא ירד ולא הופק מסמך.'),
        why='ההודעה של טרנזילה לא אומתה בזמנה, והדוח מאשר רק עכשיו.',
        action=('לא לבקש מהלקוח לשלם שוב. ההזמנה תושלם לבד כשהאתר ישאל עליה; אחרת — להשלים אותה ידנית, '
                'או להדליק את המתג אחרי אישור בעל המערכת.'),
    )


def alert_payment_unreported(invoice: StoreInvoice, rows: list[dict], *, suspected: Optional[list[str]] = None) -> None:
    """
    A charge in the terminal's report may be this order's payment, whose notify
    never came (or the report could not be asked when a retry came). Once per
    invoice: the order is held in review until a person decides.
    """
    from apps.core.tranzila_service import report_transaction_amount

    if rows or suspected:
        seen = '; '.join(
            [f"עסקה {shown_number(row.get('index') or row.get('transaction_index'))} על ₪{report_transaction_amount(row)}"
             for row in rows[:3]]
            + [f'עסקה {shown_number(index)}' for index in (suspected or [])[:3]]
        )
        what = (f'בדוח של טרנזילה יש חיוב מאושר באותו סכום של הזמנה {_order_ref(invoice)}, אחרי שנפתח לה עמוד '
                f'התשלום, בלי שהגיעה עליו הודעה: {seen}. ההזמנה בבדיקה: לא נפתח לה עמוד נוסף ולא נמכר דבר.')
        why = ('ייתכן שההודעה של טרנזילה על התשלום לא הגיעה. המסוף משותף עם האתר השני, כך שזה עשוי גם '
               'להיות תשלום של לקוח אחר.')
    else:
        what = (f'הלקוח ביקש לשלם שוב על הזמנה {_order_ref(invoice)}, זמן קצר אחרי שנפתח לו עמוד תשלום. '
                'הדוח של טרנזילה לא ענה, ולכן לא נפתח עמוד נוסף (לחצי שעה מפתיחת העמוד).')
        why = 'אי אפשר לדעת אם העמוד הקודם כבר נגבה.'
    _alert(
        invoice, kind='store_payment_unreported', key=f'store_payment_unreported:{invoice.pk}',
        title='ייתכן שהלקוח כבר שילם — ההזמנה בבדיקה',
        step='תשלום שלא הגיעה עליו הודעה',
        what=what, why=why,
        action=('לבדוק בטרנזילה אם העסקה שייכת להזמנה הזאת, ולהחליט במסך החשבונית: "השלם אחרי אימות" '
                'או "אין תשלום — שחרר". עד אז הלקוח לא יתבקש לשלם שוב.'),
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
    )
