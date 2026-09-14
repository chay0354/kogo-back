"""Charging a tenant's standing order — every path by which rental money moves.

    charge_due(today=, limit=)        the monthly cron: due active orders, one per transaction
    retry_charge(charge, user=)       the office's "retry now" on a failed month
    mark_charged(charge, ...)         the office's decision on a month in review: it went through
    void_charge(charge, ...)          ... or it is not charged
    reserve_month / reclaim_failed / call_gateway / record_result
                                      the rails the tenant's card page (card.py) runs on too

docs/12-RECURRING-BILLING-CHAIN.md lists how the courses' chain can charge a card
twice. Each of those weaknesses is closed here, and each has a test:

A. The row that says a month is taken is written and committed *before* the
   gateway is called: TenantCharge 'reserved', under UNIQUE(tenancy, period) —
   one row per tenancy and month, whichever of the tenancy's orders made it,
   so a second order on the same tenancy can never charge a month again. The
   gateway's answer is written in a transaction of its own (record_result),
   and the receipt only after that, in its own try/except. The blocks are
   durable: they refuse to run inside an outer transaction that could still
   undo them.
B. Only an explicit decline in Tranzila's own JSON is 'failed'. Everything
   else — a timeout, an HTTP error page, a broken body, a dropped connection,
   an exception — is 'review': the card may have been charged. A month in
   review is never sent again by itself; the office checks Tranzila and
   decides. A reservation that never heard back goes to review too.
C. The cron takes one due order at a time under select_for_update(skip_locked=True),
   so overlapping runs never hold the same order; a tenancy is sent to the
   gateway at most once a day, and never while one of its months is undecided.
E. A decline stops the order ('failed') and opens a card link for the tenant;
   the office can retry the month, or the tenant's new card pays it.

No month is charged in arrears by itself. The next charge date is never set to
a month before the current one, and the cron charges only a billing date that
falls in the current month; a month that went by uncharged is listed for the
office, never charged automatically.

The month key is also sent to Tranzila as DCdisable (duplicate_guard_key). It
is off in production (docs/12), so nothing here relies on it.

Every Tranzila call goes through gateway(), which refuses while
RENTAL_BILLING_ENABLED is off, and charges on the rental terminal set
(RENTAL_TRANZILA_*, each falling back to the production value).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.core.models import Business
from apps.core.tranzila_service import (
    TranzilaService,
    extract_card_token,
    is_tranzila_approved,
    is_tranzila_rest_ok,
)
from apps.core.vat import add_vat
from apps.rental_billing.errors import DISABLED_MESSAGE, BillingDisabled, BillingError
from apps.rental_billing.links import cancel_live_links, ensure_card_link
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.schedule import (
    add_months,
    billing_date_after,
    first_billing_on_or_after,
    first_of_month,
    month_label,
)

logger = logging.getLogger(__name__)

Order = TenantStandingOrder
Charge = TenantCharge

# A reservation this old never heard back from the gateway: a function killed
# mid-call, a crash between the call and the write. Its outcome is unknown.
STALE_RESERVATION = timedelta(minutes=15)
STALE_MESSAGE = 'לא התקבלה תשובה מטרנזילה על החיוב — ייתכן שהכרטיס חויב. יש לבדוק בטרנזילה ולהכריע.'
CENT = Decimal('0.01')

OUTCOME_CHARGED = 'charged'
OUTCOME_FAILED = 'failed'
OUTCOME_REVIEW = 'review'
# The gateway answered after the month was already decided on (the stale sweep, the office).
OUTCOME_LATE = 'late'

# A month whose outcome only the office may decide on. While a tenancy has one,
# nothing else of it is sent to the gateway.
UNDECIDED_STATUSES = (Charge.STATUS_RESERVED, Charge.STATUS_REVIEW)


# --------------------------------------------------------------- the switch

def billing_enabled() -> bool:
    return bool(getattr(settings, 'RENTAL_BILLING_ENABLED', False))


def business_name() -> str:
    return (getattr(settings, 'RENTAL_BILLING_BUSINESS_NAME', '') or '').strip()


def rental_business():
    """The Business every rental charge is tagged to, found by name — never created here."""
    name = business_name()
    return Business.objects.filter(name=name).first() if name else None


def missing_business_message() -> str:
    return f'העסק "{business_name()}" לא נמצא במערכת, ולכן לא בוצע חיוב. יש להקים אותו בהגדרות העסקים.'


def require_business() -> Business:
    business = rental_business()
    if business is None:
        raise BillingError(missing_business_message())
    return business


# ----------------------------------------------------------------- terminal

# The terminal set tenant billing charges on, setting by setting. Each falls
# back to the production value the courses' standing orders, card links and
# widget charge on, so by default nothing differs — and the owner can point
# tenant billing alone at another terminal (the ₪1 test) without moving the
# courses' charges.
RENTAL_TERMINAL_SETTINGS = (
    ('terminal', 'RENTAL_TRANZILA_TERMINAL', 'TRANZILA_PROD_TERMINAL'),
    ('token_terminal', 'RENTAL_TRANZILA_TOKEN_TERMINAL', 'TRANZILA_PROD_TOKEN_TERMINAL'),
    ('supplier', 'RENTAL_TRANZILA_SUPPLIER', 'TRANZILA_PROD_SUPPLIER'),
    ('public_key', 'RENTAL_TRANZILA_PUBLIC_KEY', 'TRANZILA_PROD_PUBLIC_KEY'),
    ('secret_key', 'RENTAL_TRANZILA_SECRET_KEY', 'TRANZILA_PROD_SECRET_KEY'),
)


def rental_credentials() -> tuple[dict, list]:
    """(TranzilaService kwargs, the names of the RENTAL_TRANZILA_* settings that are set)."""
    values, overridden = {}, []
    for kwarg, own, fallback in RENTAL_TERMINAL_SETTINGS:
        mine = str(getattr(settings, own, '') or '').strip()
        if mine:
            overridden.append(own)
        values[kwarg] = mine or getattr(settings, fallback, '')
    return values, overridden


def terminal_report() -> dict:
    """Which terminal set tenant billing charges on. Names only: the keys never leave the server."""
    values, overridden = rental_credentials()
    if not overridden:
        kind = 'production'
    elif len(overridden) == len(RENTAL_TERMINAL_SETTINGS):
        kind = 'rental'
    else:
        kind = 'mixed'
    return {
        'terminal_set': kind,
        'terminal': values['terminal'],
        'token_terminal': values['token_terminal'],
        'overridden': overridden,
    }


class RentalTranzila(TranzilaService):
    """
    The REST client on the rental terminal set. It keeps the JSON Tranzila last
    answered with, so a charge's outcome is read from Tranzila's own words and
    not from how a failure happened to be worded on the way back.
    """

    last_response = None
    # How many requests this instance actually put on the wire. A call that
    # never got that far (no token, credentials missing) moved no money.
    requests_made = 0

    def _make_api_request(self, params, endpoint='/v1/transactions'):
        self.last_response = None
        self.requests_made += 1
        response = super()._make_api_request(params, endpoint)
        self.last_response = response
        return response


def gateway() -> TranzilaService:
    """The only way this app reaches Tranzila. Refuses while the switch is off."""
    if not billing_enabled():
        raise BillingDisabled()
    values, _overridden = rental_credentials()
    # charge_with_token bills the token terminal, charge_with_card and verify_card the card terminal.
    return RentalTranzila(**values)


def today_local() -> date:
    return timezone.localdate()


# ------------------------------------------------------------------ amounts

def split_amount(amount_before_vat) -> tuple[int, int, int]:
    """(before VAT, VAT, total) in agorot for a monthly amount quoted before VAT, at today's rate."""
    net = Decimal(str(amount_before_vat)).quantize(CENT)
    total = add_vat(net)
    net_agorot = int(net * 100)
    total_agorot = int(total * 100)
    return net_agorot, total_agorot - net_agorot, total_agorot


def shekels(agorot) -> Decimal:
    return (Decimal(int(agorot or 0)) / 100).quantize(CENT)


def guard_key(order_id, period: date) -> str:
    """The month's key at Tranzila (DCdisable). The same for every attempt at the same month."""
    return f'rental-{order_id}-{period:%Y-%m}'


def charge_description(order, period: date) -> str:
    return f'שכירות סטודיו {month_label(period)} - {order.tenant.full_name}'.strip()


def tranzila_items(order, period: date, total: Decimal) -> list:
    return [{
        'name': charge_description(order, period)[:80],
        'type': 'I',
        'unit_price': float(total),
        'units_number': 1,
        'unit_type': 1,
        # Gross: the item price is what the card is charged, VAT included.
        'price_type': 'G',
        'currency_code': 'ILS',
    }]


def card_token(result: dict) -> str:
    raw = result.get('raw_response') if isinstance(result.get('raw_response'), dict) else {}
    return (result.get('token') or '').strip() or extract_card_token(raw, result)


# ----------------------------------------------------------------- schedule

def tenancy_periods(tenancy_id, statuses=None) -> set:
    """The months the tenancy has a charge row for — any of its orders, any state unless `statuses` narrows it."""
    queryset = Charge.objects.filter(tenancy_id=tenancy_id)
    if statuses is not None:
        queryset = queryset.filter(status__in=statuses)
    return set(queryset.values_list('period', flat=True))


def next_open_billing_date(order, today: date, *, after_period: date | None = None) -> date:
    """
    The date this order charges next: the billing day of the first month the
    tenancy has no charge for (by any of its orders) — never in a month before
    the current one, never before the order's start, and after `after_period`
    when given. It may be earlier this month than today: the current month is
    still due, and the next run charges it.
    """
    taken = tenancy_periods(order.tenancy_id)
    base = max(first_of_month(today), order.start_date)
    if after_period is not None:
        base = max(base, add_months(after_period, 1))
    candidate = first_billing_on_or_after(base, order.billing_day)
    while first_of_month(candidate) in taken:
        candidate = billing_date_after(first_of_month(candidate), order.billing_day)
    return candidate


def advance(order, period: date, today: date | None = None) -> None:
    """
    Move the order's next charge past `period` — to the next open month, never
    to one before the current — and end it once that passes its end date.
    Changes the instance; the caller saves.
    """
    nxt = next_open_billing_date(order, today or today_local(), after_period=period)
    if order.next_charge_date is None or order.next_charge_date < nxt:
        order.next_charge_date = nxt
    if (
        order.end_date
        and order.next_charge_date
        and order.next_charge_date > order.end_date
        and order.status in Order.OPEN_STATUSES
    ):
        order.status = Order.STATUS_ENDED


def is_undecided(charge, now=None) -> bool:
    """In review, or a reservation that never heard back: only the office may decide on it."""
    if charge.status == Charge.STATUS_REVIEW:
        return True
    return charge.status == Charge.STATUS_RESERVED and charge.reserved_at < (now or timezone.now()) - STALE_RESERVATION


def sweep_stale_reservations(now=None) -> int:
    """
    Reservations that never heard back, to review — their outcome is unknown,
    the card may be charged. Run by the cron, when the office lists charges,
    and when the card page opens, so none sits as 'reserved' for long.
    """
    now = now or timezone.now()
    return Charge.objects.filter(
        status=Charge.STATUS_RESERVED, reserved_at__lt=now - STALE_RESERVATION,
    ).update(status=Charge.STATUS_REVIEW, error=STALE_MESSAGE, updated_at=now)


# -------------------------------------------------------------------- rails

def _insert_reservation(order, period: date, *, business, trigger: str):
    """The month's row as 'reserved', or None when the tenancy already has a row for it. Inside the caller's transaction."""
    net, vat, total = split_amount(order.amount_before_vat)
    category = order.business_category if order.business_category_id else None
    if category is not None and category.business_id != business.id:
        category = None
    if order.business_id is None:
        # Opened before the business existed: it carries the tag its charges carry from now on.
        Order.objects.filter(pk=order.pk, business__isnull=True).update(business=business)
    try:
        # A savepoint: a month that is taken must not break the caller's transaction.
        with transaction.atomic():
            return Charge.objects.create(
                standing_order=order,
                tenancy_id=order.tenancy_id,
                period=period,
                amount_before_vat=net,
                vat_amount=vat,
                total=total,
                business=business,
                business_category=category,
                status=Charge.STATUS_RESERVED,
                trigger=trigger,
                attempts=1,
                card_last4=order.card_last4,
                reserved_at=timezone.now(),
            )
    except IntegrityError:
        if not Charge.objects.filter(tenancy_id=order.tenancy_id, period=period).exists():
            raise  # not the month guard: a bug, never swallowed
        return None


def _recheck(order, period: date, allowed_statuses) -> None:
    """The order, locked, looked at again: still in a state that may be charged, and the month inside its agreement."""
    if allowed_statuses is not None and order.status not in allowed_statuses:
        raise BillingError('הוראת הקבע השתנתה בינתיים', status_code=409)
    if order.end_date and period > order.end_date:
        raise BillingError('החודש הזה אחרי סיום ההסכם', status_code=409)


def reserve_month(order, period: date, *, business, trigger: str, allowed_statuses=None):
    """
    Reserve the month and commit it, before anything calls the gateway. None
    when the tenancy has the month already. The order is locked and checked
    again first (BillingError when it changed meanwhile).
    """
    with transaction.atomic(durable=True):
        locked = Order.objects.select_for_update().get(pk=order.pk)
        _recheck(locked, period, allowed_statuses)
        return _insert_reservation(locked, period, business=business, trigger=trigger)


def sent_to_the_gateway_today(tenancy_id, today: date, *, apart_from=None) -> bool:
    """Whether a month of this tenancy was already sent to Tranzila today — the once-a-day guard."""
    queryset = Charge.objects.filter(tenancy_id=tenancy_id, reserved_at__date=today)
    if apart_from is not None:
        queryset = queryset.exclude(pk=apart_from)
    return queryset.exists()


def reclaim_failed(charge_id, *, trigger: str, order=None, allowed_statuses=None, today: date | None = None):
    """
    A failed month back to 'reserved' for one more attempt, committed before
    the gateway is called. The order is locked and checked again — the one
    given, or the charge's own — and with `order` it also pays the month: a
    month an earlier order on the tenancy failed to charge moves to the order
    that pays it now.

    The office's retry keeps the once-a-day guard the monthly run keeps: a
    tenancy already sent to Tranzila today is refused (409), so a retry cannot
    put a second charge on the same card the same day.
    """
    with transaction.atomic(durable=True):
        charge = Charge.objects.select_for_update().get(pk=charge_id)
        if charge.status != Charge.STATUS_FAILED:
            return None
        fields = ['status', 'trigger', 'attempts', 'reserved_at', 'error', 'response_code', 'updated_at']
        # Always under the order's lock, as the card page's path is: nothing may
        # end or pause the order between this check and the charge.
        locked = Order.objects.select_for_update().get(pk=(order.pk if order is not None else charge.standing_order_id))
        _recheck(locked, charge.period, allowed_statuses)
        if trigger == Charge.TRIGGER_RETRY and sent_to_the_gateway_today(
            charge.tenancy_id, today or today_local(), apart_from=charge.pk,
        ):
            raise BillingError('הוראת הקבע כבר נשלחה היום לטרנזילה. אפשר לנסות שוב מחר.', status_code=409)
        if order is not None:
            if locked.tenancy_id != charge.tenancy_id:
                raise BillingError('החודש שייך להסכם אחר', status_code=409)
            if charge.standing_order_id != locked.pk:
                charge.standing_order = locked
                fields.append('standing_order')
        charge.status = Charge.STATUS_RESERVED
        charge.trigger = trigger
        charge.attempts += 1
        charge.reserved_at = timezone.now()
        charge.error = ''
        charge.response_code = ''
        charge.save(update_fields=fields)
        return charge


def call_gateway(call, tranzila=None) -> dict:
    """
    The gateway's answer. An exception is the uncertain answer it is: the card
    may be charged. The JSON Tranzila answered with (RentalTranzila keeps it) is
    attached as 'tranzila_json', so outcome_of reads Tranzila's own words.
    """
    before = getattr(tranzila, 'requests_made', None) if tranzila is not None else None
    if tranzila is not None:
        tranzila.last_response = None
    try:
        result = call()
    except Exception as exc:
        logger.exception('Rental billing: the Tranzila call raised')
        result = {'success': False, 'error': str(exc) or exc.__class__.__name__, 'uncertain': True}
    if not isinstance(result, dict):
        result = {'success': False, 'error': 'Invalid gateway response', 'uncertain': True}
    raw = getattr(tranzila, 'last_response', None) if tranzila is not None else None
    if isinstance(raw, dict):
        result = {**result, 'tranzila_json': raw}
    after = getattr(tranzila, 'requests_made', None) if tranzila is not None else None
    if isinstance(before, int) and isinstance(after, int) and after == before and not result.get('success'):
        # The client refused before it reached Tranzila (no token, no credentials):
        # nothing was sent, so nothing was charged.
        result = {**result, 'never_sent': True}
    return result


def explicit_decline(raw) -> bool:
    """
    Tranzila's JSON says, in so many words, that the card was not charged: an
    application error_code other than 0, or a transaction_result whose
    processor response code is not 000. A dict with neither — an error built
    locally from an HTTP error page, a broken body or a dropped connection —
    is not a decline.
    """
    if not isinstance(raw, dict):
        return False
    txn = raw.get('transaction_result')
    if isinstance(txn, dict):
        code = txn.get('processor_response_code') or txn.get('Response')
        if code not in (None, '') and not is_tranzila_approved(code):
            return True
    error_code = raw.get('error_code')
    return error_code not in (None, '', False) and not is_tranzila_rest_ok(error_code)


# Codes that never stand for a decline: the ones a locally built failure
# carries, and '000' — the processor's approval, which the parser puts here when
# Tranzila sent no error_code of its own. (An error_code of 0 arrives as '0',
# and with a "Charge failed" message it is the processor saying no.)
_NOT_A_DECLINE_CODE = frozenset({'', '999', 'N/A', 'NONE', '000'})


def _parsed_as_decline(result: dict) -> bool:
    """
    A result with no JSON attached (a caller that did not come through
    RentalTranzila) is read by the one shape TranzilaService gives an explicit
    decline: its parser's 'Charge failed: …' with the code Tranzila sent. Its
    local failures carry an 'uncertain' key and code 999, and are not declines.
    """
    if 'uncertain' in result:
        return False
    code = str(result.get('response_code') or '').strip().upper()
    if code in _NOT_A_DECLINE_CODE:
        # Nothing certain in the code, and the JSON reading calls such an answer
        # review: the two readings must not disagree.
        return False
    return str(result.get('message') or '').startswith('Charge failed: ')


def outcome_of(result: dict, raw=None) -> str:
    """
    'charged' on a yes. 'failed' only on an explicit decline in Tranzila's JSON.
    Everything else is 'review': whatever happened, the card may be charged.
    """
    if result.get('success'):
        # A yes with nothing to identify the transaction by is not something the
        # office could ever check in Tranzila, nor a receipt could name: ambiguous.
        if str(result.get('transaction_id') or '').strip():
            return OUTCOME_CHARGED
        return OUTCOME_REVIEW
    if result.get('never_sent'):
        return OUTCOME_FAILED
    if raw is None:
        for key in ('tranzila_json', 'raw_response'):
            if isinstance(result.get(key), dict):
                raw = result[key]
                break
    if raw is not None:
        return OUTCOME_FAILED if explicit_decline(raw) else OUTCOME_REVIEW
    return OUTCOME_FAILED if _parsed_as_decline(result) else OUTCOME_REVIEW


def _error_text(result: dict) -> str:
    return str(result.get('error') or result.get('message') or 'החיוב נכשל')[:1000]


def record_result(charge_id, result: dict, *, on_charged=None, fail_order: bool = True, card_last4: str = '',
                  today: date | None = None) -> str:
    """
    Write the gateway's answer, in a transaction of its own. Returns the outcome.

    on_charged(order) runs in the same transaction when the answer is a yes:
    the card page stores the new card there, so the order turns active together
    with the charge. Without it, a failed order that is charged is active again.

    fail_order: a decline on the monthly run or an office retry stops the order
    and opens a card link; one on the card page does not — the tenant may try
    another card.
    """
    outcome = outcome_of(result)
    today = today or today_local()
    now = timezone.now()
    with transaction.atomic(durable=True):
        charge = Charge.objects.select_for_update().get(pk=charge_id)
        order = Order.objects.select_for_update().get(pk=charge.standing_order_id)
        if charge.status != Charge.STATUS_RESERVED:
            # Decided on while the gateway was answering. That decision stands;
            # what Tranzila said is kept for whoever looks at it.
            note = f'תשובת טרנזילה הגיעה אחרי ההכרעה ({outcome}): {result.get("transaction_id") or _error_text(result)}'
            charge.error = f'{charge.error}\n{note}'.strip()[:2000]
            if result.get('success') and not charge.transaction_id:
                charge.transaction_id = str(result.get('transaction_id') or '')[:100]
                charge.confirmation_code = str(result.get('confirmation_code') or '')[:50]
            charge.save(update_fields=['error', 'transaction_id', 'confirmation_code', 'updated_at'])
            logger.error('Rental charge %s: Tranzila answered %s after the charge was %s', charge.pk, outcome, charge.status)
            return OUTCOME_LATE

        charge.response_code = str(result.get('response_code') or '')[:20]
        if outcome == OUTCOME_CHARGED:
            charge.status = Charge.STATUS_CHARGED
            charge.transaction_id = str(result.get('transaction_id') or '')[:100]
            charge.confirmation_code = str(result.get('confirmation_code') or '')[:50]
            charge.charged_at = now
            charge.error = ''
            if card_last4:
                charge.card_last4 = card_last4
            advance(order, charge.period, today)
            if on_charged is not None:
                on_charged(order)
            elif order.status == Order.STATUS_FAILED:
                order.status = Order.STATUS_ACTIVE
                order.last_error = ''
                order.failed_at = None
        elif outcome == OUTCOME_REVIEW:
            charge.status = Charge.STATUS_REVIEW
            charge.error = _error_text(result)
        else:
            charge.status = Charge.STATUS_FAILED
            charge.error = _error_text(result)
            if fail_order and order.status in (Order.STATUS_ACTIVE, Order.STATUS_FAILED):
                order.status = Order.STATUS_FAILED
                order.last_error = charge.error
                order.failed_at = now
        charge.save()
        order.save()
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
        elif outcome == OUTCOME_FAILED and fail_order and order.status == Order.STATUS_FAILED:
            ensure_card_link(order)
    return outcome


def issue_receipt_safely(charge_id) -> bool:
    """
    The receipt for a charge that is on record. Never raises: a receipt that
    fails leaves the charge charged, with the reason written on it, and the
    office sees "charged, no receipt" and can issue it again.
    """
    from apps.rental_billing.receipts import issue_receipt

    try:
        issue_receipt(charge_id)
        return True
    except Exception as exc:
        logger.exception('Rental receipt not issued for charge %s (the charge is recorded)', charge_id)
        Charge.objects.filter(pk=charge_id).update(
            receipt_error=(str(exc) or exc.__class__.__name__)[:1000], updated_at=timezone.now(),
        )
        return False


def _charge_saved_card(tranzila, order, charge) -> dict:
    total = shekels(charge.total)
    return call_gateway(lambda: tranzila.charge_with_token(
        token=order.tranzila_token,
        amount=total,
        description=charge_description(order, charge.period),
        transaction_id=str(charge.pk),
        items=tranzila_items(order, charge.period, total),
        expire_month=order.card_expire_month,
        expire_year=order.card_expire_year,
        duplicate_guard_key=guard_key(order.pk, charge.period),
    ), tranzila)


# --------------------------------------------------------------------- cron

def _next_due_order(today: date, seen: list):
    """
    The next due active order nobody else holds, locked. Never a tenancy that
    was sent to the gateway today already, nor one with a month undecided.
    """
    sent_today = Charge.objects.filter(tenancy_id=OuterRef('tenancy_id'), reserved_at__date=today)
    undecided = Charge.objects.filter(tenancy_id=OuterRef('tenancy_id'), status__in=UNDECIDED_STATUSES)
    return (
        Order.objects.select_for_update(skip_locked=True, of=('self',))
        .select_related('tenant')
        .filter(status=Order.STATUS_ACTIVE, next_charge_date__lte=today)
        .exclude(pk__in=seen)
        .exclude(Exists(sent_today))
        .exclude(Exists(undecided))
        .order_by('next_charge_date', 'created_at')
        .first()
    )


def months_never_charged(order, today: date, charges=None) -> list:
    """
    The order's months, before the current one, that carry no charge at all —
    never billed and never decided on. Read by the office's screen long after
    the run that skipped them. `charges` may be the tenancy's rows, already loaded.
    """
    taken = {charge.period for charge in charges} if charges is not None else tenancy_periods(order.tenancy_id)
    month = first_of_month(order.start_date)
    last = first_of_month(today)
    if order.end_date:
        last = min(last, add_months(first_of_month(order.end_date), 1))
    out = []
    while month < last:
        if month not in taken:
            out.append(month)
        month = add_months(month, 1)
    return out


def blocked_orders(today: date) -> list:
    """
    Due orders the run cannot touch because a month of their tenancy is
    undecided. Reported by every run, so a tenancy never stops billing quietly.
    """
    undecided = Charge.objects.filter(tenancy_id=OuterRef('tenancy_id'), status__in=UNDECIDED_STATUSES)
    orders = (
        Order.objects.filter(status=Order.STATUS_ACTIVE, next_charge_date__lte=today)
        .filter(Exists(undecided))
        .order_by('next_charge_date', 'created_at')
    )
    out = []
    for order in orders:
        charge = (
            Charge.objects.filter(tenancy_id=order.tenancy_id, status__in=UNDECIDED_STATUSES)
            .order_by('period').first()
        )
        out.append({
            'standing_order': str(order.pk),
            'tenancy': str(order.tenancy_id),
            'due': order.next_charge_date.isoformat(),
            'period': charge.period.isoformat() if charge else '',
            'charge': str(charge.pk) if charge else '',
            'status': charge.status if charge else '',
        })
    return out


def _missed_months(order, due: date, today: date) -> list:
    """The months from `due` (never before the order's start) to the one before the current that the tenancy has no charge for."""
    taken = tenancy_periods(order.tenancy_id)
    month = max(first_of_month(due), first_of_month(order.start_date))
    current, out = first_of_month(today), []
    while month < current:
        if month not in taken:
            out.append(month)
        month = add_months(month, 1)
    return out


def _end(order, summary: dict) -> None:
    order.status = Order.STATUS_ENDED
    order.save(update_fields=['status', 'updated_at'])
    cancel_live_links(order)
    summary['ended'] += 1


def _reserve_due_month(order, business, summary: dict, today: date):
    """Inside the order's transaction: end it, skip it, or reserve its month."""
    if not order.has_card:
        summary['skipped'] += 1
        summary['errors'].append(f'{order.pk}: אין כרטיס שמור בהוראת הקבע')
        return None
    due = order.next_charge_date
    if due < first_of_month(today):
        # A month that went by uncharged is never charged by itself: it is
        # listed for the office, and the order moves on to the current month.
        for month in _missed_months(order, due, today):
            summary['missed'].append({'standing_order': str(order.pk), 'period': month.isoformat()})
        order.next_charge_date = next_open_billing_date(order, today)
        order.save(update_fields=['next_charge_date', 'updated_at'])
        due = order.next_charge_date
        if order.end_date and due > order.end_date:
            _end(order, summary)
            return None
        if due > today:
            summary['skipped'] += 1
            return None
    if order.end_date and due > order.end_date:
        _end(order, summary)
        return None
    period = first_of_month(due)
    charge = _insert_reservation(order, period, business=business, trigger=Charge.TRIGGER_CRON)
    if charge is not None:
        charge.standing_order = order
        return charge
    existing = Charge.objects.get(tenancy_id=order.tenancy_id, period=period)
    if existing.status in (Charge.STATUS_CHARGED, Charge.STATUS_VOIDED, Charge.STATUS_FAILED):
        # The month was settled elsewhere (the card page, the office, an earlier
        # order), or failed and waits for the office. Move on; charge nothing.
        advance(order, period, today)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
        if existing.status == Charge.STATUS_FAILED:
            summary['errors'].append(f'{order.pk}: {period:%Y-%m} נדחה — ממתין להכרעת המשרד')
    else:
        summary['errors'].append(f'{order.pk}: {period:%Y-%m} {existing.get_status_display()} — ממתין להכרעת המשרד')
    summary['skipped'] += 1
    return None


def charge_due(*, today: date | None = None, limit: int = 40) -> dict:
    """
    Charge every active standing order due in the current month, up to `limit`.

    Refuses as a whole — before any row is touched — while the switch is off,
    when the business to tag the charges to is missing, or when Tranzila is
    not configured. `limit` keeps one cron call under the function's time
    limit; what is left stays due for the next call. `missed` lists the months
    that went by uncharged: the office decides on them.
    """
    summary = {
        'ok': True, 'enabled': billing_enabled(), 'checked': 0, 'charged': 0, 'failed': 0, 'review': 0,
        'skipped': 0, 'ended': 0, 'receipts': 0, 'stale_to_review': 0, 'missed': [], 'blocked': [], 'errors': [],
    }
    if not summary['enabled']:
        summary.update(disabled=True, message=DISABLED_MESSAGE)
        return summary
    business = rental_business()
    if business is None:
        summary.update(ok=False, error=missing_business_message())
        return summary
    tranzila = gateway()
    credential_error = tranzila.credential_error()
    if credential_error:
        summary.update(ok=False, error=f'טרנזילה אינה מוגדרת: {credential_error}')
        return summary

    today = today or today_local()
    summary['stale_to_review'] = sweep_stale_reservations()
    # Read after the sweep, so a reservation that never heard back is named as
    # what it is: a month waiting for the office, holding up its tenancy.
    summary['blocked'] = blocked_orders(today)
    for row in summary['blocked']:
        summary['errors'].append(
            f"{row['standing_order']}: חסום — החיוב של {row['period']} ממתין להכרעת המשרד"
        )
    batch = max(1, min(int(limit or 40), 200))
    seen: list = []
    while summary['checked'] < batch:
        # One order per transaction: locked, its month reserved, committed.
        with transaction.atomic(durable=True):
            order = _next_due_order(today, seen)
            if order is None:
                break
            seen.append(order.pk)
            summary['checked'] += 1
            charge = _reserve_due_month(order, business, summary, today)
        if charge is None:
            continue

        result = _charge_saved_card(tranzila, order, charge)
        outcome = record_result(charge.pk, result, fail_order=True, today=today)
        if outcome == OUTCOME_CHARGED:
            summary['charged'] += 1
            if issue_receipt_safely(charge.pk):
                summary['receipts'] += 1
            else:
                summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} חויב, הקבלה לא הופקה')
        elif outcome == OUTCOME_FAILED:
            summary['failed'] += 1
            summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} נדחה — {_error_text(result)}')
        else:
            summary['review'] += 1
            summary['errors'].append(f'{order.pk}: {charge.period:%Y-%m} בבדיקה — {_error_text(result)}')
    return summary


# ------------------------------------------------------ the office's decisions

def retry_charge(charge, *, user=None) -> tuple[str, Charge]:
    """
    "Retry now" on a failed month: reserve-first, on the card the order holds,
    under the same month key. Refused while the switch is off.
    """
    tranzila = gateway()
    require_business()
    order = Order.objects.select_related('tenant').get(pk=charge.standing_order_id)
    if charge.status != Charge.STATUS_FAILED:
        raise BillingError('אפשר לנסות שוב רק חיוב שנדחה')
    if order.status == Order.STATUS_ENDED:
        raise BillingError('הוראת הקבע הסתיימה')
    if not order.has_card:
        raise BillingError('אין כרטיס שמור בהוראת הקבע. יש לשלוח לשוכר קישור להזנת כרטיס.')
    credential_error = tranzila.credential_error()
    if credential_error:
        raise BillingError(f'טרנזילה אינה מוגדרת: {credential_error}')

    claimed = reclaim_failed(
        charge.pk, trigger=Charge.TRIGGER_RETRY, order=order,
        allowed_statuses=(Order.STATUS_ACTIVE, Order.STATUS_PAUSED, Order.STATUS_FAILED),
    )
    if claimed is None:
        raise BillingError('החיוב כבר אינו במצב נדחה', status_code=409)
    logger.info('Rental charge %s: retried by %s', claimed.pk, getattr(user, 'pk', None))
    result = _charge_saved_card(tranzila, order, claimed)
    outcome = record_result(claimed.pk, result, fail_order=True)
    if outcome == OUTCOME_CHARGED:
        issue_receipt_safely(claimed.pk)
    return outcome, Charge.objects.get(pk=claimed.pk)


def _resolve(charge, user, note: str, now) -> None:
    charge.resolved_by = user if getattr(user, 'is_authenticated', False) else None
    charge.resolved_at = now
    charge.resolution_note = note


def mark_charged(charge, *, transaction_id: str, confirmation_code: str = '', note: str = '', user=None) -> Charge:
    """
    The office found the charge in Tranzila: the month in review is charged,
    with the transaction id they found. Then its receipt is issued.
    """
    transaction_id = str(transaction_id or '').strip()
    if not transaction_id:
        raise BillingError('יש להזין את מזהה העסקה שנמצא בטרנזילה')
    if len(transaction_id) > 100:
        raise BillingError('מזהה העסקה ארוך מדי')
    now = timezone.now()
    with transaction.atomic(durable=True):
        locked = Charge.objects.select_for_update().get(pk=charge.pk)
        if not is_undecided(locked, now):
            raise BillingError('אפשר לסמן כחויב רק חיוב שנמצא בבדיקה')
        order = Order.objects.select_for_update().get(pk=locked.standing_order_id)
        locked.status = Charge.STATUS_CHARGED
        locked.transaction_id = transaction_id
        locked.confirmation_code = str(confirmation_code or '').strip()[:50]
        # The money moved when the attempt was made, not when the office found it.
        locked.charged_at = locked.reserved_at
        _resolve(locked, user, str(note or '').strip()[:1000], now)
        locked.save()
        advance(order, locked.period)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
    issue_receipt_safely(locked.pk)
    return Charge.objects.get(pk=locked.pk)


def void_charge(charge, *, reason: str, user=None) -> Charge:
    """
    The month is not charged: a charge in review that the office found never
    went through, or a failed one they settle another way. Final: no order on
    the tenancy charges the month again, and the schedule moves past it.
    """
    reason = str(reason or '').strip()
    if not reason:
        raise BillingError('יש להזין סיבת ביטול')
    if len(reason) > 1000:
        raise BillingError('סיבת הביטול ארוכה מדי')
    now = timezone.now()
    with transaction.atomic(durable=True):
        locked = Charge.objects.select_for_update().get(pk=charge.pk)
        if not (is_undecided(locked, now) or locked.status == Charge.STATUS_FAILED):
            raise BillingError('אפשר לבטל רק חיוב שנמצא בבדיקה או שנדחה')
        order = Order.objects.select_for_update().get(pk=locked.standing_order_id)
        locked.status = Charge.STATUS_VOIDED
        _resolve(locked, user, reason, now)
        locked.save()
        advance(order, locked.period)
        order.save(update_fields=['next_charge_date', 'status', 'updated_at'])
        if order.status == Order.STATUS_ENDED:
            cancel_live_links(order)
    return Charge.objects.get(pk=locked.pk)
