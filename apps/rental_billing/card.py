"""The tenant's card page — GET shows what they are signing up to, POST stores the card.

    pending_card, the agreement already started   charge the current month now, keep the card
    pending_card, the agreement starts later      verify the card only; the first charge is the
                                                  first billing day on or after the start
    failed                                        the new card replaces the old one and pays the
                                                  failed month, if it is still unpaid

A charge here runs on billing.py's rails: the month is reserved and committed
before the gateway is called, the answer is recorded in its own transaction,
the receipt comes after. On top of that the link itself is claimed under a row
lock ('processing') before anything is sent, as card links do, so a double
click or a second tab gets "in progress" and never reaches Tranzila.

Refused, before any gateway call: while the switch is off, when the link is
unknown, expired, used or cancelled, and — for an order the signing page opened
— while the tenancy's current contract is not signed.

Card numbers pass straight to Tranzila; they are never stored or logged. What
is kept is the token Tranzila returns, its expiry and the last four digits.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.rental_billing import billing
from apps.rental_billing.errors import DISABLED_MESSAGE
from apps.rental_billing.links import PROCESSING_STALE_AFTER, expires_at, is_expired
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.schedule import billing_date_after, first_of_month

logger = logging.getLogger(__name__)

Order = TenantStandingOrder
Charge = TenantCharge
Link = TenantCardLink

MAX_ATTEMPTS = 6
MODE_CHARGE = 'charge'
MODE_VERIFY = 'verify'

UNAVAILABLE_MESSAGE = 'לא ניתן לחייב כרגע. פנו למשרד.'
IN_PROGRESS_MESSAGE = 'חיוב על החודש הזה כבר בעיבוד או בבדיקה במשרד. אל תנסו שוב — המשרד יחזור אליכם.'
UNCERTAIN_MESSAGE = 'החיוב לא אושר בוודאות. המשרד יבדוק לפני ניסיון נוסף — אל תנסו שוב.'


class CardEntryError(ValueError):
    """Why the card page refuses. The message is shown to the tenant as is."""

    def __init__(self, message: str, *, status_code: int = 400, already_done: bool = False,
                 processing: bool = False, disabled: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.already_done = already_done
        self.processing = processing
        self.disabled = disabled


def tenant_facing_error(raw: str) -> str:
    """The gateway's text is for the log; the tenant gets Hebrew unless it already is."""
    text = (raw or '').strip()
    if any('\u0590' <= ch <= '\u05ff' for ch in text):
        return text
    return 'התשלום לא אושר. נסו כרטיס אחר או פנו למשרד.'


# ------------------------------------------------------------------- the link

def resolve_link(token: str) -> Link:
    raw = (token or '').strip()
    if not raw or len(raw) > 40:
        raise CardEntryError('קישור לא תקין', status_code=404)
    link = (
        Link.objects.select_related(
            'standing_order', 'standing_order__tenant', 'standing_order__branch', 'standing_order__tenancy',
        )
        .filter(token=raw)
        .first()
    )
    if link is None:
        raise CardEntryError('קישור לא תקין', status_code=404)
    if link.status == Link.STATUS_USED:
        raise CardEntryError('הקישור כבר מומש.', already_done=True)
    if link.status == Link.STATUS_CANCELLED:
        raise CardEntryError('הקישור כבר לא בתוקף. בקשו מהמשרד קישור חדש.')
    if link.status == Link.STATUS_REVIEW:
        raise CardEntryError('הניסיון הקודם נמצא בבדיקה במשרד. אל תנסו שוב.', status_code=409, processing=True)
    if is_expired(link):
        raise CardEntryError('פג תוקף הקישור. בקשו מהמשרד קישור חדש.')
    return link


def _claim(link_id) -> Link:
    """Mark the link 'processing' under a row lock, or say why not."""
    stale = False
    with transaction.atomic(durable=True):
        link = Link.objects.select_for_update().get(pk=link_id)
        if link.status == Link.STATUS_USED:
            raise CardEntryError('הקישור כבר מומש.', already_done=True)
        if link.status == Link.STATUS_CANCELLED:
            raise CardEntryError('הקישור כבר לא בתוקף. בקשו מהמשרד קישור חדש.')
        if link.status == Link.STATUS_REVIEW:
            raise CardEntryError('הניסיון הקודם נמצא בבדיקה במשרד. אל תנסו שוב.', status_code=409, processing=True)
        if link.status == Link.STATUS_PROCESSING:
            started = link.charge_started_at or timezone.now()
            if timezone.now() - started < PROCESSING_STALE_AFTER:
                raise CardEntryError('הכרטיס בעיבוד, המתינו רגע.', status_code=409, processing=True)
            # Never came back: money may have moved. Written after this block,
            # since raising inside it would roll the write back.
            stale = True
        if not stale:
            if link.attempts >= MAX_ATTEMPTS:
                raise CardEntryError('יותר מדי ניסיונות בקישור הזה. בקשו מהמשרד קישור חדש.')
            link.status = Link.STATUS_PROCESSING
            link.charge_started_at = timezone.now()
            link.attempts += 1
            link.save(update_fields=['status', 'charge_started_at', 'attempts', 'updated_at'])
    if stale:
        _to_review(link.pk, 'stale_processing')
        raise CardEntryError('הניסיון הקודם לא הסתיים. המשרד יבדוק לפני ניסיון נוסף.', status_code=409, processing=True)
    return link


def _release(link_id, error: str) -> None:
    """Back to waiting — only while no money can have moved."""
    Link.objects.filter(pk=link_id, status=Link.STATUS_PROCESSING).update(
        status=Link.STATUS_PENDING, last_error=(error or '')[:1000], updated_at=timezone.now(),
    )


def _to_review(link_id, reason: str, error: str = '') -> None:
    """Money may have moved: the link is frozen until a person looks."""
    Link.objects.filter(pk=link_id, status=Link.STATUS_PROCESSING).update(
        status=Link.STATUS_REVIEW, review_reason=reason[:200], last_error=(error or '')[:1000], updated_at=timezone.now(),
    )


def _mark_used(link_id) -> None:
    now = timezone.now()
    Link.objects.filter(pk=link_id).update(status=Link.STATUS_USED, used_at=now, updated_at=now)


# ------------------------------------------------------------------- the plan

def _refuse_unsigned(order) -> None:
    """An order the signing page opened takes a card only once the contract is signed."""
    if order.source != Order.SOURCE_SIGNING:
        return
    from apps.rentals.contracts import current_contract
    from apps.rentals.models import RentalContract

    contract = current_contract(order.tenancy)
    if contract is None or contract.status != RentalContract.STATUS_SIGNED:
        raise CardEntryError('החוזה עדיין לא נחתם. יש לחתום על החוזה לפני הזנת הכרטיס.')


def plan_for(order, today: date) -> dict:
    """
    What a card entered today does: {'mode', 'period', 'charge', 'next'}.

    mode 'charge' charges `period` (a new month, or the failed `charge`);
    'verify' only checks the card. `next` is the next charge date for a verify.
    Raises CardEntryError when the order takes no card, or its month is in flight.
    """
    charges = {charge.period: charge for charge in Charge.objects.filter(standing_order_id=order.pk)}
    if order.end_date and order.end_date < today:
        raise CardEntryError('תקופת החיוב בהסכם הסתיימה. פנו למשרד.')

    if order.status == Order.STATUS_PENDING_CARD:
        if order.start_date <= today:
            period = first_of_month(today)
            existing = charges.get(period)
            if existing is None:
                return {'mode': MODE_CHARGE, 'period': period, 'charge': None, 'next': None}
            if existing.status == Charge.STATUS_FAILED:
                return {'mode': MODE_CHARGE, 'period': period, 'charge': existing, 'next': None}
            if existing.status in (Charge.STATUS_CHARGED, Charge.STATUS_VOIDED):
                # The month is settled already: keep the card, charge nothing now.
                after = billing_date_after(period, order.billing_day)
                return {'mode': MODE_VERIFY, 'period': None, 'charge': None,
                        'next': billing.next_open_billing_date(order, after)}
            raise CardEntryError(IN_PROGRESS_MESSAGE, status_code=409, processing=True)
        return {'mode': MODE_VERIFY, 'period': None, 'charge': None,
                'next': billing.next_open_billing_date(order, order.start_date)}

    if order.status == Order.STATUS_FAILED:
        if any(c.status in (Charge.STATUS_RESERVED, Charge.STATUS_REVIEW) for c in charges.values()):
            raise CardEntryError(IN_PROGRESS_MESSAGE, status_code=409, processing=True)
        failed = [c for c in charges.values() if c.status == Charge.STATUS_FAILED]
        if failed:
            target = max(failed, key=lambda c: c.period)
            return {'mode': MODE_CHARGE, 'period': target.period, 'charge': target, 'next': None}
        return {'mode': MODE_VERIFY, 'period': None, 'charge': None,
                'next': billing.next_open_billing_date(order, order.next_charge_date or today)}

    raise CardEntryError('הוראת הקבע אינה ממתינה לכרטיס. פנו למשרד.')


def preview_payload(link: Link, *, today: date | None = None) -> dict:
    """What the page shows before the tenant types anything."""
    today = today or billing.today_local()
    order = link.standing_order
    net, vat, total = billing.split_amount(order.amount_before_vat)
    out = {
        'ok': True,
        'enabled': billing.billing_enabled(),
        'state': order.status,
        'tenant_name': order.tenant.full_name,
        'branch_name': order.branch.name if order.branch_id else '',
        'amount_before_vat': str(billing.shekels(net)),
        'vat_amount': str(billing.shekels(vat)),
        'monthly_total': str(billing.shekels(total)),
        'billing_day': order.billing_day,
        'start_date': order.start_date.isoformat(),
        'end_date': order.end_date.isoformat() if order.end_date else None,
        'expires_at': expires_at(link).isoformat(),
        'charge_now': False,
        'charge_amount': '0.00',
        'charge_period': None,
        'next_charge_date': None,
    }
    if not out['enabled']:
        out['message'] = DISABLED_MESSAGE
    try:
        _refuse_unsigned(order)
        plan = plan_for(order, today)
    except CardEntryError as exc:
        out['error'] = exc.message
        return out
    if plan['mode'] == MODE_CHARGE:
        amount = plan['charge'].total if plan['charge'] is not None else total
        out.update(
            charge_now=True,
            charge_amount=str(billing.shekels(amount)),
            charge_period=plan['period'].isoformat(),
            next_charge_date=billing_date_after(plan['period'], order.billing_day).isoformat(),
        )
    else:
        out['next_charge_date'] = plan['next'].isoformat()
    return out


# -------------------------------------------------------------------- submit

class _Attempt:
    """Whether this submit reached the gateway with a charge: after that, a crash is a review, never a release."""

    def __init__(self):
        self.reached = False


def _store_card(order, token: str, card: dict) -> None:
    order.tranzila_token = token
    order.card_expire_month = int(card['expiry_month'])
    order.card_expire_year = int(card['expiry_year'])
    order.card_last4 = str(card['card_number'])[-4:]
    order.status = Order.STATUS_ACTIVE
    order.last_error = ''
    order.failed_at = None


def apply_card(token: str, card: dict, *, today: date | None = None) -> dict:
    """Store the tenant's card, charging the month it owes when it owes one. Returns {state, charged, amount, next_charge_date}."""
    link = resolve_link(token)
    if not billing.billing_enabled():
        raise CardEntryError(DISABLED_MESSAGE, status_code=503, disabled=True)
    business = billing.rental_business()
    if business is None:
        logger.error('Rental card link %s refused: %s', link.pk, billing.missing_business_message())
        raise CardEntryError(UNAVAILABLE_MESSAGE, status_code=503)
    order = Order.objects.select_related('tenancy', 'tenant', 'branch', 'business_category').get(pk=link.standing_order_id)
    _refuse_unsigned(order)
    today = today or billing.today_local()
    plan = plan_for(order, today)
    tranzila = billing.gateway()
    credential_error = tranzila.credential_error()
    if credential_error:
        logger.error('Rental card link %s refused: Tranzila not configured (%s)', link.pk, credential_error)
        raise CardEntryError(UNAVAILABLE_MESSAGE, status_code=503)

    link = _claim(link.pk)
    attempt = _Attempt()
    try:
        if plan['mode'] == MODE_CHARGE:
            return _charge(link, order, card, plan, business, tranzila, attempt)
        return _verify(link, order, card, plan, tranzila)
    except CardEntryError as exc:
        _settle(link.pk, attempt, exc.message)
        raise
    except Exception as exc:
        # Never the card: only the link and the exception.
        logger.exception('Rental card link %s failed', link.pk)
        _settle(link.pk, attempt, str(exc))
        if attempt.reached:
            raise CardEntryError(
                'החיוב עבר ככל הנראה, אך הרישום לא הושלם. המשרד יטפל — אל תנסו שוב.', status_code=409, processing=True,
            ) from exc
        raise CardEntryError('לא הצלחנו לשמור את הכרטיס. נסו שוב או פנו למשרד.') from exc


def _settle(link_id, attempt: _Attempt, error: str) -> None:
    """A link still 'processing' when the submit ends: waiting again if no money can have moved, review if it may have."""
    if attempt.reached:
        _to_review(link_id, 'record_failed', error)
    else:
        _release(link_id, error)


def _charge(link, order, card: dict, plan: dict, business, tranzila, attempt: _Attempt) -> dict:
    if plan['charge'] is None:
        charge = billing.reserve_month(order, plan['period'], business=business, trigger=Charge.TRIGGER_CARD)
    else:
        charge = billing.reclaim_failed(plan['charge'].pk, trigger=Charge.TRIGGER_CARD)
    if charge is None:
        # Someone took the month between the plan and now (the cron, a second tab).
        raise CardEntryError(IN_PROGRESS_MESSAGE, status_code=409, processing=True)

    total = billing.shekels(charge.total)
    attempt.reached = True
    result = billing.call_gateway(lambda: tranzila.charge_with_card(
        card_number=card['card_number'],
        expiry_month=card['expiry_month'],
        expiry_year=card['expiry_year'],
        cvv=card['cvv'],
        card_holder_id=card.get('card_holder_id') or '',
        amount=total,
        description=billing.charge_description(order, charge.period),
        items=billing.tranzila_items(order, charge.period, total),
        duplicate_guard_key=billing.guard_key(order.pk, charge.period),
    ))
    token = billing.card_token(result) if result.get('success') else ''

    def on_charged(locked_order):
        if locked_order.status not in (Order.STATUS_PENDING_CARD, Order.STATUS_FAILED):
            return  # ended or changed meanwhile: the charge stands, the order is left as it is
        if token:
            _store_card(locked_order, token, card)
        else:
            locked_order.last_error = 'החיוב עבר, אך טרנזילה לא החזירה טוקן לכרטיס. יש לשלוח לשוכר קישור חדש.'

    outcome = billing.record_result(
        charge.pk, result, on_charged=on_charged, fail_order=False, card_last4=str(card['card_number'])[-4:],
    )
    if outcome == billing.OUTCOME_CHARGED:
        _mark_used(link.pk)
        if not token:
            logger.error('Rental card link %s: charged, but Tranzila returned no token', link.pk)
        billing.issue_receipt_safely(charge.pk)
        fresh = Order.objects.get(pk=order.pk)
        return {
            'success': True,
            'state': fresh.status,
            'charged': True,
            'amount': str(total),
            'next_charge_date': fresh.next_charge_date.isoformat() if fresh.next_charge_date else None,
        }
    if outcome == billing.OUTCOME_FAILED:
        # A clear decline: nothing moved. The tenant may try another card.
        _release(link.pk, billing._error_text(result))
        raise CardEntryError(tenant_facing_error(result.get('error') or ''))
    _to_review(link.pk, 'gateway_uncertain', billing._error_text(result))
    raise CardEntryError(UNCERTAIN_MESSAGE, status_code=409, processing=True)


def _verify(link, order, card: dict, plan: dict, tranzila) -> dict:
    """Check the card with the issuer and keep its token. No money moves (J2)."""
    _net, _vat, total = billing.split_amount(order.amount_before_vat)
    amount = max(billing.shekels(total), Decimal('1.00'))
    result = billing.call_gateway(lambda: tranzila.verify_card(
        card_number=card['card_number'],
        expiry_month=card['expiry_month'],
        expiry_year=card['expiry_year'],
        cvv=card['cvv'],
        card_holder_id=card.get('card_holder_id') or '',
        amount=amount,
        description='אימות כרטיס לשכירות',
        duplicate_guard_key=f'rental-verify-{link.pk}-{link.attempts}',
    ))
    if not result.get('success'):
        _release(link.pk, billing._error_text(result))
        if billing.outcome_of(result) == billing.OUTCOME_REVIEW:
            raise CardEntryError('לא הצלחנו לאמת את הכרטיס כרגע. נסו שוב בעוד רגע.')
        raise CardEntryError(tenant_facing_error(result.get('error') or ''))
    token = billing.card_token(result)
    if not token:
        logger.error('Rental card link %s: the card was verified, but Tranzila returned no token', link.pk)
        _release(link.pk, 'verified, no token')
        raise CardEntryError('הכרטיס אומת אך לא נשמר. פנו למשרד.')

    with transaction.atomic(durable=True):
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status not in (Order.STATUS_PENDING_CARD, Order.STATUS_FAILED):
            raise CardEntryError('הוראת הקבע השתנתה בינתיים. פנו למשרד.')
        _store_card(locked, token, card)
        locked.next_charge_date = plan['next']
        locked.save()
        _mark_used(link.pk)
    return {
        'success': True,
        'state': locked.status,
        'charged': False,
        'amount': '0.00',
        'next_charge_date': locked.next_charge_date.isoformat(),
    }
