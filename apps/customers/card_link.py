"""
Card link for an existing customer: the office sends a signed link, the
parent enters a card, and the CRM either opens a standing order for a lesson
(first charge priced exactly like a widget signup) or takes a one-time
charge tagged by business/category.

Money rails, in order: claim the link under a row lock and commit
'processing' *before* the gateway is called; charge; re-lock and record.
An uncertain gateway answer leaves the link 'processing' for 90 seconds so
a second submit cannot charge twice. A decline puts the link back to
'pending' so the parent may try another card.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import transaction
from django.utils import timezone

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_service import ManyChatService
from apps.core.password_reset_email import crm_frontend_url
from apps.core.payment_service import (
    JERUSALEM_TZ,
    PaymentService,
    _compute_prorate,
    child_has_standing_order_for_lessons,
    deferred_first_charge_date,
    enroll_child_in_paid_lessons,
    registration_fee_amount,
    resolve_billing_price,
    resolve_include_registration_fee,
    standing_order_next_billing_date,
    subscription_payment_description,
    subscription_tranzila_items,
)
from apps.core.tranzila_service import TranzilaService, extract_card_token, is_tranzila_uncertain_gateway_error
from apps.customers.models import Payment, PaymentDiscountSnapshot, RecurringPayment, TranzilaTransaction
from apps.customers.widget_views import _ensure_recurring_payment_for_widget_charge
from apps.payment_links.models import CardLink, money

logger = logging.getLogger(__name__)

SIGN_SALT = 'kogo-card-link'
CARD_LINK_TOKEN_MAX_AGE = 14 * 24 * 3600
PROCESSING_STALE_AFTER = timedelta(seconds=90)
MAX_ATTEMPTS = 6


def parent_facing_error(raw: str) -> str:
    """The gateway's text is for the log; the parent gets Hebrew unless it already is."""
    text = (raw or '').strip()
    if any('\u0590' <= ch <= '\u05ff' for ch in text):
        return text
    return 'התשלום לא אושר. נסו כרטיס אחר או פנו למשרד.'


class CardLinkError(ValueError):
    def __init__(self, message: str, *, already_done: bool = False, processing: bool = False):
        super().__init__(message)
        self.already_done = already_done
        self.processing = processing


def _qs():
    return CardLink.objects.select_related(
        'child', 'child__family', 'lesson', 'lesson__course', 'lesson__course__branch',
        'branch', 'business', 'business_category', 'payment', 'recurring_payment',
    )


def build_card_link_token(link: CardLink) -> str:
    return dumps({'id': str(link.id), 'v': link.token_version}, salt=SIGN_SALT).replace(':', '~')


def card_link_public_url(link: CardLink) -> str:
    return f'{crm_frontend_url()}/card-link/{build_card_link_token(link)}'


def resolve_card_link_token(token: str) -> tuple[CardLink, bool]:
    """The link for a token, and whether it is already done."""
    raw = (token or '').strip().replace('~', ':')
    if not raw:
        raise CardLinkError('קישור לא תקין')
    try:
        payload = loads(raw, salt=SIGN_SALT, max_age=CARD_LINK_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise CardLinkError('פג תוקף הקישור. בקשו מהמשרד קישור חדש.') from exc
    except BadSignature as exc:
        raise CardLinkError('קישור לא תקין') from exc
    link_id = str((payload or {}).get('id') or '').strip()
    version = (payload or {}).get('v')
    link = _qs().filter(id=link_id).first() if link_id else None
    if link is None:
        raise CardLinkError('קישור לא תקין')
    if link.status == CardLink.STATUS_COMPLETED:
        return link, True
    if link.status == CardLink.STATUS_CANCELLED or version != link.token_version:
        raise CardLinkError('הקישור כבר לא בתוקף. בקשו מהמשרד קישור חדש.')
    return link, False


# ---------------------------------------------------------------------------
# Quote — what the parent will be charged, computed like a widget signup
# ---------------------------------------------------------------------------

def quote_standing_order(link: CardLink, today: date | None = None) -> dict:
    """
    Pure: mirrors the pricing block of PaymentService.charge_subscription_with_card
    (resolve_billing_price → discounts → prorate → registration fee → deferred
    first charge) without writing anything.
    """
    if today is None:
        today = timezone.now().astimezone(JERUSALEM_TZ).date()
    child = link.child
    lesson = link.lesson
    if lesson is None:
        raise CardLinkError('להוראת קבע נדרש שיעור')
    base_price, used_lesson_tier, _course_index, bundle, price_option = resolve_billing_price(child, lesson, None, None)
    if not base_price:
        raise CardLinkError('לשיעור אין מחיר מוגדר')
    discount = PaymentService().discount_service.evaluate_discounts_for_payment(
        family_id=str(child.family_id),
        child_id=str(child.id),
        payment_date=today,
        base_price=base_price,
        lesson_id=None if used_lesson_tier else str(lesson.id),
    )
    monthly = money(discount.final_price)
    factor, _, _, _ = _compute_prorate(today, lesson.day_of_week)
    charge_fee = resolve_include_registration_fee(child, lesson, link.include_registration_fee)
    fee = registration_fee_amount(lesson.course) if charge_fee else Decimal('0.00')
    deferred = deferred_first_charge_date(today)
    if deferred or monthly <= 0:
        prorated = Decimal('0.00')
    else:
        prorated = max(Decimal('1.00'), money(monthly * factor))
    return {
        'monthly_amount': monthly,
        'base_amount': money(discount.base_price),
        'discount_amount': money(discount.total_discount_amount),
        'registration_fee': money(fee),
        'prorated_lesson': prorated,
        'first_charge': money(prorated + fee),
        'next_billing_date': standing_order_next_billing_date(today=today, lesson=lesson),
        'deferred_first_charge_date': deferred,
        'description': subscription_payment_description(
            child=child, lesson=lesson, bundle=bundle, price_option=price_option, fee_only=bool(deferred),
        ),
        'discounts': discount.applicable_discounts,
        'bundle': bundle,
        'price_option': price_option,
    }


def preview_payload(link: CardLink, *, already_done: bool = False) -> dict:
    child = link.child
    out = {
        'ok': True,
        'kind': link.kind,
        'already_done': already_done,
        'status': link.status,
        'child_name': child.full_name if child else '',
    }
    if link.kind == CardLink.KIND_STANDING_ORDER and link.lesson_id:
        lesson = link.lesson
        out.update({
            'course_name': lesson.course.name,
            'branch_name': lesson.course.branch.name if lesson.course.branch_id else '',
            'day_name': lesson.get_day_of_week_display(),
            'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
            'end_time': lesson.end_time.strftime('%H:%M') if lesson.end_time else '',
        })
        if not already_done:
            try:
                q = quote_standing_order(link)
                out.update({
                    'first_charge': str(q['first_charge']),
                    'monthly_amount': str(q['monthly_amount']),
                    'registration_fee': str(q['registration_fee']),
                    'next_billing_date': q['next_billing_date'].isoformat(),
                })
            except CardLinkError as exc:
                out['quote_error'] = str(exc)
    else:
        out.update({
            'description': link.description,
            'amount': str(money(link.amount or 0)),
            'branch_name': link.branch.name if link.branch_id else '',
        })
    return out


# ---------------------------------------------------------------------------
# Validate → claim → charge → record
# ---------------------------------------------------------------------------

def _claim(link_id) -> CardLink:
    """Mark the link 'processing' under a row lock, or explain why not."""
    stale = False
    with transaction.atomic():
        link = CardLink.objects.select_for_update(of=('self',)).get(id=link_id)
        if link.status == CardLink.STATUS_COMPLETED:
            raise CardLinkError('הקישור כבר מומש', already_done=True)
        if link.status == CardLink.STATUS_CANCELLED:
            raise CardLinkError('הקישור בוטל. פנו למשרד.')
        if link.status == CardLink.STATUS_REVIEW:
            raise CardLinkError('החיוב נמצא בבדיקה במשרד. אל תנסו שוב.', processing=True)
        if link.status == CardLink.STATUS_PROCESSING:
            started = link.charge_started_at or timezone.now()
            if timezone.now() - started < PROCESSING_STALE_AFTER:
                raise CardLinkError('החיוב בעיבוד, המתינו רגע.', processing=True)
            # A charge that never reported back (function killed mid-gateway) may
            # have taken the money. Never re-open it by itself — a person checks.
            # (Written after the atomic block: raising inside it would roll it back.)
            stale = True
        if not stale and link.attempts >= MAX_ATTEMPTS:
            raise CardLinkError('יותר מדי ניסיונות בקישור הזה. בקשו מהמשרד קישור חדש.')
        if not stale:
            link.status = CardLink.STATUS_PROCESSING
            link.charge_started_at = timezone.now()
            link.attempts += 1
            link.save(update_fields=['status', 'charge_started_at', 'attempts', 'updated_at'])
    if stale:
        _to_review(link, 'stale_processing')
        raise CardLinkError('החיוב הקודם לא הסתיים. המשרד יבדוק לפני ניסיון נוסף.', processing=True)
    return _qs().get(id=link_id)


def _release(link: CardLink, error: str) -> None:
    """Back to pending — only while no money can have moved."""
    CardLink.objects.filter(id=link.id, status=CardLink.STATUS_PROCESSING).update(
        status=CardLink.STATUS_PENDING, last_error=error[:1000], updated_at=timezone.now(),
    )


def _to_review(link: CardLink, reason: str, error: str = '') -> None:
    """Money may have moved (or did): freeze the link until a person looks."""
    CardLink.objects.filter(id=link.id).exclude(status=CardLink.STATUS_COMPLETED).update(
        status=CardLink.STATUS_REVIEW, review_reason=reason[:200], last_error=error[:1000], updated_at=timezone.now(),
    )


def _prevalidate(link: CardLink, today: date) -> dict:
    """Everything that can refuse without a gateway call happens before the claim."""
    if link.kind == CardLink.KIND_STANDING_ORDER:
        if link.lesson is None:
            raise CardLinkError('להוראת קבע נדרש שיעור. פנו למשרד.')
        if child_has_standing_order_for_lessons(link.child, [link.lesson]):
            raise CardLinkError('לילד כבר יש הוראת קבע לשיעור הזה. פנו למשרד.')
        return quote_standing_order(link, today)
    amount = money(link.amount or 0)
    if amount < Decimal('1.00'):
        raise CardLinkError('סכום לא תקין. פנו למשרד.')
    return {'amount': amount}


def apply_card_link(link: CardLink, card: dict[str, Any]) -> dict:
    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    current = CardLink.objects.filter(id=link.id).values_list('status', flat=True).first()
    if current == CardLink.STATUS_COMPLETED:
        raise CardLinkError('הקישור כבר מומש', already_done=True)
    if current == CardLink.STATUS_CANCELLED:
        raise CardLinkError('הקישור בוטל. פנו למשרד.')
    plan = _prevalidate(link, today)
    link = _claim(link.id)
    gateway_reached = False
    try:
        if link.kind == CardLink.KIND_STANDING_ORDER:
            return _apply_standing_order(link, card, plan, today)
        return _apply_one_time(link, card, plan)
    except CardLinkError:
        raise
    except Exception as exc:
        logger.exception('card link %s failed', link.id)
        if _reached.get(link.id):
            # The gateway was called: the card may be charged. Freeze, do not
            # let the parent try again — that is how a card gets charged twice.
            _to_review(link, 'record_failed', str(exc))
            raise CardLinkError('החיוב עבר ככל הנראה, אך הרישום לא הושלם. המשרד יטפל — אל תנסו שוב.', processing=True) from exc
        _release(link, str(exc))
        raise CardLinkError('החיוב נכשל. נסו שוב או פנו למשרד.') from exc
    finally:
        _reached.pop(link.id, None)


# Which links have had their gateway call issued in this process (in-flight only).
_reached: dict = {}


def _charge_or_freeze(link: CardLink, call, *, payment: Payment) -> dict:
    """
    Issue the gateway call. Any answer that is not a clear yes or a clear no
    (timeout, connection error, an exception) freezes the link for review —
    the card may already be charged.
    """
    _reached[link.id] = True
    try:
        result = call()
    except Exception as exc:
        logger.exception('card link %s: gateway raised', link.id)
        _to_review(link, 'gateway_uncertain', str(exc))
        raise CardLinkError('החיוב לא אושר בוודאות. המשרד יבדוק לפני ניסיון נוסף.', processing=True) from exc
    if not result.get('success') and is_tranzila_uncertain_gateway_error(result):
        _to_review(link, 'gateway_uncertain', str(result.get('error') or ''))
        raise CardLinkError('החיוב לא אושר בוודאות. המשרד יבדוק לפני ניסיון נוסף.', processing=True)
    if not result.get('success'):
        # A clear decline: nothing moved. The parent may try another card.
        error = result.get('error') or 'התשלום נכשל'
        payment.status = 'failed'
        payment.failure_reason = error
        payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        _reached.pop(link.id, None)
        _release(link, error)
        raise CardLinkError(parent_facing_error(error))
    return result


def _record_gateway_fact(link: CardLink, payment: Payment, result: dict, *, transaction_type: str) -> TranzilaTransaction:
    """The first thing written after a yes from the gateway: the money moved."""
    with transaction.atomic():
        payment.status = 'completed'
        payment.payment_date = timezone.now()
        payment.save(update_fields=['status', 'payment_date', 'updated_at'])
        txn, _ = TranzilaTransaction.objects.get_or_create(
            idempotency_key=f'card_link_{link.id}_{link.token_version}_{link.attempts}',
            defaults={
                'transaction_id': result.get('transaction_id', '') or '',
                'confirmation_code': result.get('confirmation_code', '') or '',
                'transaction_type': transaction_type,
                'response_code': result.get('response_code', '000') or '000',
                'response_message': '',
                'request_data': {},
                'response_data': result.get('raw_response', {}) or {},
                'is_successful': True,
                'response_timestamp': timezone.now(),
            },
        )
        payment.tranzila_transaction = txn
        payment.save(update_fields=['tranzila_transaction'])
    return txn


def _apply_standing_order(link: CardLink, card: dict[str, Any], quote: dict, today: date, _unused=None) -> dict:
    child = link.child
    lesson = link.lesson
    family = child.family

    # The payment row exists — and is committed — before the gateway is called.
    with transaction.atomic():
        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first() if family else None,
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=quote['bundle'],
            price_option=quote['price_option'],
            payment_type='recurring_subscription',
            status='pending',
            base_amount=quote['base_amount'],
            discount_amount=quote['discount_amount'],
            final_amount=quote['first_charge'],
            registration_fee=quote['registration_fee'],
            description=quote['description'],
        )
        for applied in quote['discounts']:
            kwargs = {
                'payment': payment,
                'discount_name': applied.name,
                'discount_type': applied.discount_type,
                'discount_value': applied.value,
                'amount_deducted': applied.value,
                'reason': applied.reason,
            }
            if applied.discount_id:
                kwargs['discount_id'] = applied.discount_id
            PaymentDiscountSnapshot.objects.create(**kwargs)
        CardLink.objects.filter(id=link.id).update(payment=payment)

    tranzila = TranzilaService.production()
    label = f'{lesson.course.name} - {child.full_name}'
    guard = f'card-link-{link.id}-{link.token_version}-{link.attempts}'
    if quote['first_charge'] > 0:
        items = subscription_tranzila_items(
            label=label,
            prorated_lesson=quote['prorated_lesson'],
            registration_fee=quote['registration_fee'],
            prorated=not quote['deferred_first_charge_date'],
        )
        call = lambda: tranzila.charge_with_card(  # noqa: E731
            card_number=card['card_number'], expiry_month=card['expiry_month'], expiry_year=card['expiry_year'],
            cvv=card['cvv'], card_holder_id=card.get('card_holder_id') or '', amount=quote['first_charge'],
            description=payment.description, items=items, duplicate_guard_key=guard,
        )
    else:
        call = lambda: tranzila.verify_card(  # noqa: E731
            card_number=card['card_number'], expiry_month=card['expiry_month'], expiry_year=card['expiry_year'],
            cvv=card['cvv'], card_holder_id=card.get('card_holder_id') or '',
            amount=quote['monthly_amount'] if quote['monthly_amount'] >= Decimal('1.00') else Decimal('1.00'),
            description=payment.description, duplicate_guard_key=f'verify-{guard}',
        )
    result = _charge_or_freeze(link, call, payment=payment)

    # 1. The money moved — write that down before anything that can fail.
    txn = _record_gateway_fact(link, payment, result, transaction_type='recurring_setup')

    token = (result.get('token') or '').strip() or extract_card_token(
        result.get('raw_response') if isinstance(result.get('raw_response'), dict) else {}, result,
    )
    if not token:
        # Money moved (or the card was verified) but nothing came back to bill
        # next month with. Keep the facts, do not enroll, and flag it — the
        # honest version of the widget's silent leak.
        CardLink.objects.filter(id=link.id).update(
            status=CardLink.STATUS_REVIEW, review_reason='no_token', completed_at=timezone.now(), updated_at=timezone.now(),
        )
        logger.error('card link %s: charge succeeded but Tranzila returned no token', link.id)
        _reached.pop(link.id, None)
        return {'success': True, 'review': True, 'charged': str(quote['first_charge']),
                'message': 'החיוב עבר, אך הכרטיס לא נשמר להוראת הקבע. המשרד יטפל.'}

    # 2. Standing order + enrollment. A failure here is a review, never a release.
    with transaction.atomic():
        locked = CardLink.objects.select_for_update(of=('self',)).get(id=link.id)
        created = _ensure_recurring_payment_for_widget_charge(
            payment=payment, child=child, lesson=lesson, token=token,
            expiry_month=card['expiry_month'], expiry_year=card['expiry_year'],
            enrollment_date=today, next_billing_date=quote['next_billing_date'],
        )
        recurring = RecurringPayment.objects.filter(initial_payment=payment).first()
        if not created and recurring is None and not RecurringPayment.objects.filter(child=child, status='active').exists():
            locked.status = CardLink.STATUS_REVIEW
            locked.review_reason = 'no_standing_order'
            locked.completed_at = timezone.now()
            locked.save(update_fields=['status', 'review_reason', 'completed_at', 'updated_at'])
            logger.error('card link %s: charged but no standing order was created', link.id)
            _reached.pop(link.id, None)
            return {'success': True, 'review': True, 'charged': str(quote['first_charge']),
                    'message': 'החיוב עבר, אך הוראת הקבע לא נפתחה. המשרד יטפל.'}

        child.status = 'active'
        deferred = quote['deferred_first_charge_date']
        if deferred:
            child.subscription_start_date = deferred
            child.paid_until_date = None
        else:
            child.subscription_start_date = today
            _, _, _, next_bill = _compute_prorate(today, lesson.day_of_week)
            child.paid_until_date = next_bill - timedelta(days=1)
        child.save(update_fields=['status', 'subscription_start_date', 'paid_until_date', 'updated_at'])
        enroll_child_in_paid_lessons(child=child, lesson=lesson, bundle=quote['bundle'])

        locked.status = CardLink.STATUS_COMPLETED
        locked.recurring_payment = recurring
        locked.completed_at = timezone.now()
        locked.save(update_fields=['status', 'recurring_payment', 'completed_at', 'updated_at'])

    _reached.pop(link.id, None)
    _after_success(payment, txn, invoice=quote['first_charge'] > 0)
    return {
        'success': True,
        'charged': str(quote['first_charge']),
        'monthly_amount': str(quote['monthly_amount']),
        'next_billing_date': quote['next_billing_date'].isoformat(),
    }


def _apply_one_time(link: CardLink, card: dict[str, Any], plan: dict, _unused=None) -> dict:
    child = link.child
    family = child.family
    amount = plan['amount']
    description = link.description or f'חיוב חד-פעמי - {child.full_name}'

    with transaction.atomic():
        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first() if family else None,
            branch=link.branch,
            lesson=None,
            payment_type='one_time',
            status='pending',
            base_amount=amount,
            discount_amount=Decimal('0.00'),
            final_amount=amount,
            registration_fee=Decimal('0.00'),
            description=description,
        )
        CardLink.objects.filter(id=link.id).update(payment=payment)

    guard = f'card-link-{link.id}-{link.token_version}-{link.attempts}'
    call = lambda: TranzilaService.production().charge_with_card(  # noqa: E731
        card_number=card['card_number'], expiry_month=card['expiry_month'], expiry_year=card['expiry_year'],
        cvv=card['cvv'], card_holder_id=card.get('card_holder_id') or '', amount=amount, description=description,
        items=[{
            'name': description[:60], 'type': 'I', 'unit_price': float(amount), 'units_number': 1,
            'unit_type': 1, 'price_type': 'G', 'currency_code': 'ILS',
        }],
        duplicate_guard_key=guard,
    )
    result = _charge_or_freeze(link, call, payment=payment)
    txn = _record_gateway_fact(link, payment, result, transaction_type='charge')
    CardLink.objects.filter(id=link.id).update(
        status=CardLink.STATUS_COMPLETED, completed_at=timezone.now(), updated_at=timezone.now(),
    )
    _reached.pop(link.id, None)
    _after_success(payment, txn, invoice=True)
    return {'success': True, 'charged': str(amount)}


def _after_success(payment: Payment, txn: TranzilaTransaction, *, invoice: bool) -> None:
    """Receipt and WhatsApp after the money is safely recorded; neither may undo it."""
    service = PaymentService()
    if invoice:
        try:
            service._create_invoice_from_payment(payment, txn)
        except Exception:
            logger.exception('card link: invoice for payment %s failed', payment.id)
    if payment.payment_type == 'recurring_subscription':
        try:
            service._send_registration_whatsapp(payment)
        except Exception:
            logger.exception('card link: registration WhatsApp for payment %s failed', payment.id)


# ---------------------------------------------------------------------------
# Sending the link
# ---------------------------------------------------------------------------

def send_card_link_whatsapp(link: CardLink) -> dict:
    """Same field names as the card-update flow, so the owner can point one ManyChat flow at both."""
    child = link.child
    ctx = build_enrollment_whatsapp_context(child=child, lesson=link.lesson)
    if not ctx:
        return {'sent': False, 'reason': 'no_parent_phone'}
    token = build_card_link_token(link)
    lookup_names = ctx.pop('lookup_names', None)
    if link.kind == CardLink.KIND_STANDING_ORDER:
        try:
            amount_label = str(quote_standing_order(link)['first_charge'])
        except CardLinkError:
            amount_label = ''
    else:
        amount_label = str(money(link.amount or 0))
    extra_fields = {
        'kogo_card_update_url': f'{crm_frontend_url()}/card-link/{token}',
        'kogo_card_update_token': token,
        'kogo_amount': amount_label,
        'kogo_support_phone': '050-9424755',
    }
    result = ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_CARD_LINK,
        lookup_names=lookup_names,
        extra_fields=extra_fields,
        **ctx,
    )
    CardLink.objects.filter(id=link.id).update(
        sent_at=timezone.now() if result.get('sent') else link.sent_at,
        sent_result={k: (v if isinstance(v, (str, int, bool)) or v is None else str(v)) for k, v in result.items()},
        updated_at=timezone.now(),
    )
    return result
