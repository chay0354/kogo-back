"""Failed standing-order recovery: signed link → new card → fix token + STO."""
from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import transaction
from django.utils import timezone

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_service import ManyChatService
from apps.core.password_reset_email import crm_frontend_url
from apps.core.payment_service import JERUSALEM_TZ, PaymentService, subscription_tranzila_items
from apps.core.tranzila_service import TranzilaService, extract_card_token
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction

logger = logging.getLogger(__name__)

SIGN_SALT = 'kogo-card-update'
CARD_UPDATE_TOKEN_MAX_AGE = 14 * 24 * 3600


class CardUpdateError(ValueError):
    def __init__(self, message: str, *, already_done: bool = False):
        super().__init__(message)
        self.already_done = already_done


def _recurring_qs():
    return RecurringPayment.objects.select_related(
        'child',
        'child__family',
        'initial_payment',
        'initial_payment__lesson',
        'initial_payment__lesson__course',
        'initial_payment__lesson__course__branch',
        'initial_payment__bundle',
        'initial_payment__branch',
    )


def _stamp(recurring: RecurringPayment) -> str:
    updated = recurring.updated_at
    if timezone.is_aware(updated):
        updated = timezone.localtime(updated)
    return updated.isoformat(timespec='microseconds')


def build_card_update_token(recurring: RecurringPayment) -> str:
    # Colons break Next.js / WhatsApp URL-button path segments.
    return dumps({'id': str(recurring.id), 'v': _stamp(recurring)}, salt=SIGN_SALT).replace(':', '~')


def card_update_public_url(recurring: RecurringPayment) -> str:
    return f'{crm_frontend_url()}/update-card/{build_card_update_token(recurring)}'


def format_sto_amount(amount) -> str:
    value = Decimal(str(amount)).quantize(Decimal('0.01'))
    if value == value.to_integral():
        return str(int(value))
    return f'{value:.2f}'


def _lesson_for(recurring: RecurringPayment):
    initial = recurring.initial_payment
    return initial.lesson if initial else None


def resolve_card_update_token(token: str) -> tuple[RecurringPayment, bool]:
    raw = (token or '').strip().replace('~', ':')
    if not raw:
        raise CardUpdateError('קישור לא תקין')
    try:
        payload = loads(raw, salt=SIGN_SALT, max_age=CARD_UPDATE_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise CardUpdateError('פג תוקף הקישור. בקשו מהמשרד קישור חדש.') from exc
    except BadSignature as exc:
        raise CardUpdateError('קישור לא תקין') from exc

    rec_id = str((payload or {}).get('id') or '').strip()
    stamp = str((payload or {}).get('v') or '').strip()
    if not rec_id:
        raise CardUpdateError('קישור לא תקין')

    recurring = _recurring_qs().filter(id=rec_id).first()
    if not recurring:
        raise CardUpdateError('קישור לא תקין')
    if recurring.status == 'cancelled':
        raise CardUpdateError('הוראת הקבע בוטלה. פנו למשרד.')
    if _stamp(recurring) != stamp:
        if recurring.status == 'active':
            return recurring, True
        raise CardUpdateError('הקישור כבר לא בתוקף. בקשו מהמשרד קישור חדש.')
    return recurring, False


def preview_payload(recurring: RecurringPayment, *, already_done: bool = False) -> dict:
    lesson = _lesson_for(recurring)
    course_name = ''
    branch_name = ''
    if lesson and lesson.course_id:
        course_name = lesson.course.name
        if lesson.course.branch_id:
            branch_name = lesson.course.branch.name
    child = recurring.child
    return {
        'ok': True,
        'already_done': already_done,
        'child_name': child.full_name if child else '',
        'course_name': course_name,
        'branch_name': branch_name,
        'amount': str(recurring.amount),
        'amount_label': format_sto_amount(recurring.amount),
        'next_billing_date': recurring.next_billing_date.isoformat() if recurring.next_billing_date else None,
        'will_charge': (not already_done) and _needs_catchup_charge(recurring),
    }


def send_card_update_whatsapp(recurring: RecurringPayment) -> dict:
    lesson = _lesson_for(recurring)
    if not lesson or not recurring.child_id:
        return {'sent': False, 'reason': 'missing_lesson_or_child'}

    ctx = build_enrollment_whatsapp_context(child=recurring.child, lesson=lesson)
    if not ctx:
        return {'sent': False, 'reason': 'no_parent_phone'}

    token = build_card_update_token(recurring)
    lookup_names = ctx.pop('lookup_names', None)
    extra_fields = {
        'kogo_card_update_url': f'{crm_frontend_url()}/update-card/{token}',
        'kogo_card_update_token': token,
        'kogo_amount': format_sto_amount(recurring.amount),
    }
    return ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_CARD_UPDATE,
        lookup_names=lookup_names,
        extra_fields=extra_fields,
        **ctx,
    )


def send_card_update_for_failed(
    *,
    ids: Iterable[str] | None = None,
    limit: int = 80,
) -> dict:
    qs = (
        _recurring_qs()
        .filter(status='failed')
        .order_by('next_billing_date', 'created_at')
    )
    id_list = [str(value).strip() for value in (ids or []) if str(value).strip()]
    if id_list:
        qs = qs.filter(id__in=id_list)
    rows = list(qs[: max(1, min(int(limit or 80), 200))])

    sent = 0
    failed = 0
    results: list[dict] = []
    for recurring in rows:
        result = send_card_update_whatsapp(recurring)
        row = {
            'id': str(recurring.id),
            'child_name': recurring.child.full_name if recurring.child_id else '',
            'sent': bool(result.get('sent')),
            'reason': result.get('reason'),
            'error': result.get('error'),
        }
        results.append(row)
        if row['sent']:
            sent += 1
        else:
            failed += 1
    return {
        'checked': len(rows),
        'sent': sent,
        'failed': failed,
        'results': results,
    }


def _next_month_first(from_day: date) -> date:
    if from_day.month == 12:
        return date(from_day.year + 1, 1, 1)
    return date(from_day.year, from_day.month + 1, 1)


def _paid_until(end_of_charge_month: date) -> date:
    last_day = calendar.monthrange(end_of_charge_month.year, end_of_charge_month.month)[1]
    return date(end_of_charge_month.year, end_of_charge_month.month, last_day)


def _needs_catchup_charge(recurring: RecurringPayment) -> bool:
    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    due = recurring.next_billing_date
    if due and due > today:
        return False
    charge_month = due or today
    if recurring.last_charge_date and recurring.last_charge_date.year == charge_month.year and recurring.last_charge_date.month == charge_month.month:
        return False
    lesson = _lesson_for(recurring)
    paid = Payment.objects.filter(
        child=recurring.child,
        payment_type='recurring_subscription',
        status='completed',
        payment_date__year=charge_month.year,
        payment_date__month=charge_month.month,
    )
    if lesson:
        paid = paid.filter(lesson=lesson)
    return not paid.exists()


def apply_new_card(recurring: RecurringPayment, card: dict[str, Any]) -> dict:
    lesson = _lesson_for(recurring)
    if not lesson:
        raise CardUpdateError('לא נמצא חוג להוראת הקבע. פנו למשרד.')
    if recurring.status == 'cancelled':
        raise CardUpdateError('הוראת הקבע בוטלה. פנו למשרד.')

    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    child = recurring.child
    family = child.family
    amount = Decimal(str(recurring.amount)).quantize(Decimal('0.01'))
    charge_month = recurring.next_billing_date or today
    will_charge = _needs_catchup_charge(recurring) and amount >= Decimal('1.00')

    tranzila = TranzilaService.production()
    label = f'{lesson.course.name} - {child.full_name}'
    payment = None

    if will_charge:
        items = subscription_tranzila_items(
            label=label,
            prorated_lesson=amount,
            registration_fee=Decimal('0'),
            prorated=False,
        )
        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first() if family else None,
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=recurring.initial_payment.bundle if recurring.initial_payment else None,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=recurring.base_amount or amount,
            discount_amount=recurring.discount_amount or Decimal('0.00'),
            final_amount=amount,
            registration_fee=Decimal('0.00'),
            description=f'מנוי חודשי - {lesson.course.name} - {child.full_name}',
        )
        result = tranzila.charge_with_card(
            card_number=card['card_number'],
            expiry_month=card['expiry_month'],
            expiry_year=card['expiry_year'],
            cvv=card['cvv'],
            card_holder_id=card.get('card_holder_id') or '',
            amount=amount,
            description=payment.description,
            items=items,
            duplicate_guard_key=f'card-update-{recurring.id}-{charge_month:%Y-%m}',
        )
    else:
        result = tranzila.verify_card(
            card_number=card['card_number'],
            expiry_month=card['expiry_month'],
            expiry_year=card['expiry_year'],
            cvv=card['cvv'],
            card_holder_id=card.get('card_holder_id') or '',
            amount=amount if amount >= Decimal('1.00') else Decimal('1.00'),
            description=f'עדכון כרטיס - {child.full_name}',
            duplicate_guard_key=f'card-update-verify-{recurring.id}',
        )

    if not result.get('success'):
        if payment is not None:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'התשלום נכשל')
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        raise CardUpdateError(result.get('error') or 'התשלום נכשל')

    token = (result.get('token') or '').strip() or extract_card_token(
        result.get('raw_response') if isinstance(result.get('raw_response'), dict) else {},
        result,
    )
    if not token:
        if payment is not None:
            payment.status = 'failed'
            payment.failure_reason = 'לא התקבל טוקן כרטיס חדש'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        raise CardUpdateError('הכרטיס חויב אבל לא התקבל טוקן. פנו למשרד לפני ניסיון נוסף.')

    service = PaymentService()
    with transaction.atomic():
        locked = (
            RecurringPayment.objects
            .select_for_update(of=('self',))
            .select_related('child', 'initial_payment', 'initial_payment__lesson')
            .get(id=recurring.id)
        )
        locked.tranzila_token = token
        locked.card_expire_month = card['expiry_month']
        locked.card_expire_year = card['expiry_year']
        locked.status = 'active'
        locked.cancellation_reason = ''
        update_fields = [
            'tranzila_token',
            'card_expire_month',
            'card_expire_year',
            'status',
            'cancellation_reason',
            'updated_at',
        ]

        if will_charge and payment is not None:
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save(update_fields=['status', 'payment_date', 'updated_at'])
            tranzila_txn = TranzilaTransaction.objects.create(
                transaction_id=result.get('transaction_id', ''),
                confirmation_code=result.get('confirmation_code', ''),
                transaction_type='recurring_charge',
                response_code=result.get('response_code', '000'),
                response_message='',
                request_data={},
                response_data=result.get('raw_response', {}) or {},
                idempotency_key=f'card_update_{locked.id}_{today.isoformat()}',
                is_successful=True,
                response_timestamp=timezone.now(),
            )
            payment.tranzila_transaction = tranzila_txn
            payment.save(update_fields=['tranzila_transaction'])
            service._create_invoice_from_payment(payment, tranzila_txn)
            locked.last_charge_date = today
            locked.next_billing_date = _next_month_first(charge_month)
            update_fields.extend(['last_charge_date', 'next_billing_date'])
            child.status = 'active'
            child.paid_until_date = _paid_until(charge_month)
            child.save(update_fields=['status', 'paid_until_date', 'updated_at'])
        else:
            if child.status == 'payment_problem':
                child.status = 'active'
                child.save(update_fields=['status', 'updated_at'])

        locked.save(update_fields=update_fields)

    return {
        'success': True,
        'charged': will_charge,
        'amount': str(amount) if will_charge else '0',
        'next_billing_date': (
            locked.next_billing_date.isoformat() if locked.next_billing_date else None
        ),
    }
