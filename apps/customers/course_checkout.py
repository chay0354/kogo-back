"""
A course signup paid on Tranzila's hosted page (stage 4 of the cogolive plan).

The widget registers as before: /widget/register/ writes one pending Payment
per child and selection, with the amounts worked out and frozen on the server.
Then, instead of a card form of ours:

  1. start_checkout — one CourseCheckout for the whole cart and Tranzila's page
     on TRANZILA_TERMINAL in tranmode NK: the card is checked (J2) and saved as
     a token. Nothing is charged there, and no wallet is offered (a wallet
     leaves no card for the monthly charge).
  2. handle_notify — Tranzila's notify is public and unsigned, so it proves
     nothing by itself. The page's row in the terminal's own report does: an
     approved NK (or VK) row, for this sum, with the approval number the notify
     quoted, made after the checkout, under a number no other checkout holds.
     The token and its expiry are read from that row, never from the POST.
  3. settle_checkout — one charge for the cart from that token on
     COURSE_TOKEN_TERMINAL, claimed first (course_checkout_<id>) so nothing can
     charge it twice, with the room and the trial credit checked again right
     before. On a yes every payment is activated exactly as a typed-card
     charge activates it (widget_views.activate_paid_widget_payment), the
     standing orders are opened on the same card and terminal, one receipt is
     issued and one WhatsApp goes to each child's family.

Nothing here runs unless COURSE_HOSTED_PAGE_ENABLED (and the hosted page) is on.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.tranzila_service import (
    TOKEN_CHARGED,
    TOKEN_DECLINED,
    TOKEN_SETUP_PROBLEM,
    TranzilaService,
    invoice_id_from_pdesc,
    is_tranzila_approved,
    report_transaction_amount,
    report_transaction_time,
    same_authorization_number,
    token_charge_outcome,
)
from apps.customers.models import CourseCheckout, Payment, TranzilaTransaction

logger = logging.getLogger(__name__)

PAGE_TRANMODE = 'NK'
# Report tranmodes that saved the card without taking the sum.
TOKEN_ROW_TRANMODES = frozenset({'NK', 'VK'})
# The payer changed the page address and paid there: the money moved on the page.
CHARGED_AT_PAGE_TRANMODES = frozenset({'A', 'AK'})
# A report row may predate the checkout by this much (clock skew), never more.
CLOCK_SKEW = timedelta(minutes=10)
# Pending payments older than this have let their discount and fee holds go
# (in-flight rows count for two hours): the parent registers again.
CHECKOUT_MAX_AGE = timedelta(hours=2)
# A page that got a transaction number but no verdict is asked about again
# after this long, from the widget's poll.
RETRY_VERIFY_AFTER = timedelta(seconds=15)
# The J2 check needs a sum; a cart with nothing to charge today still checks the card.
MIN_PAGE_SUM = Decimal('1.00')

MESSAGES = {
    CourseCheckout.STATUS_PAGE_OPEN: 'ממתינים לאישור מטרנזילה.',
    CourseCheckout.STATUS_VERIFIED: 'הכרטיס אושר, משלימים את התשלום.',
    CourseCheckout.STATUS_CHARGING: 'משלימים את התשלום. אל תסגרו את החלון.',
    CourseCheckout.STATUS_COMPLETED: 'התשלום התקבל וההרשמה הושלמה.',
    CourseCheckout.STATUS_DECLINED: 'הכרטיס לא אושר. אפשר לנסות שוב, בכרטיס אחר או באותו כרטיס.',
    CourseCheckout.STATUS_UNCERTAIN: 'לא התקבלה תשובה מחברת האשראי. אל תשלמו שוב — המשרד יבדוק ויחזור אליכם.',
    CourseCheckout.STATUS_REVIEW: 'התשלום בבדיקה במשרד. אל תשלמו שוב — נחזור אליכם.',
    CourseCheckout.STATUS_REPLACED: 'נפתח עמוד תשלום חדש.',
    CourseCheckout.STATUS_FAILED: 'לא ניתן להשלים את ההרשמה. אנא התחילו מחדש או פנו למשרד.',
}


class CheckoutError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def hosted_checkout_enabled(payment_ids=None) -> bool:
    """
    Whether this cart pays on the hosted page: for everyone once
    COURSE_HOSTED_PAGE_ENABLED is on, or — while it is off — when every
    payment is for a course listed in COURSE_HOSTED_PAGE_COURSE_IDS (the
    hidden test course of the real 1 ₪ signup).
    """
    if not getattr(settings, 'TRANZILA_HOSTED_PAGE_ENABLED', False):
        return False
    if getattr(settings, 'COURSE_HOSTED_PAGE_ENABLED', False):
        return True
    test_courses = {str(c) for c in getattr(settings, 'COURSE_HOSTED_PAGE_COURSE_IDS', []) or []}
    if not test_courses or not payment_ids:
        return False
    ids = [pid for pid in (_uuid_or_none(p) for p in payment_ids) if pid]
    course_ids = set()
    for payment in Payment.objects.filter(id__in=ids).select_related('lesson', 'bundle'):
        course_id = payment.lesson.course_id if payment.lesson_id else (
            payment.bundle.course_id if payment.bundle_id else None
        )
        course_ids.add(str(course_id) if course_id else '')
    return bool(course_ids) and course_ids <= test_courses


def course_token_terminal() -> str:
    return (
        (getattr(settings, 'COURSE_TOKEN_TERMINAL', '') or '').strip()
        or (getattr(settings, 'TRANZILA_TOKEN_TERMINAL', '') or '').strip()
    )


def _public_bases() -> tuple[str, str]:
    api = (getattr(settings, 'CRM_API_BASE_URL', '') or '').strip().rstrip('/')
    front = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    return api, front


# ---------------------------------------------------------------------------
# 1. The page
# ---------------------------------------------------------------------------

def start_checkout(payment_ids: list[str]) -> tuple[CourseCheckout, str]:
    """
    One checkout for these pending payments, and the Tranzila page (NK) to pay it.

    Raises CheckoutError when the cart can't be paid this way (the caller
    answers the widget with it). An earlier open checkout of the same
    payments is replaced: its page can no longer lead to a charge.
    """
    from apps.customers.widget_views import precheck_widget_capacity

    ids = list(dict.fromkeys(str(pid).strip() for pid in payment_ids if str(pid).strip()))
    if not ids:
        raise CheckoutError('לא נמצאו תשלומים להשלמה')
    api_base, front_base = _public_bases()
    if not api_base or not front_base:
        logger.error('Course checkout refused: CRM_API_BASE_URL / CRM_FRONTEND_URL missing')
        raise CheckoutError('הסליקה אינה זמינה כרגע. אנא פנו למשרד.', status_code=503)
    token_terminal = course_token_terminal()
    if TranzilaService.for_terminal(token_terminal) is None:
        logger.error('Course checkout refused: token terminal %r has no keys', token_terminal)
        raise CheckoutError('הסליקה אינה זמינה כרגע. אנא פנו למשרד.', status_code=503)

    capacity_error = precheck_widget_capacity(ids, single_lessons=True)
    if capacity_error:
        raise CheckoutError(capacity_error)

    with transaction.atomic():
        payments = list(
            Payment.objects.select_for_update(of=('self',))
            .select_related('child', 'family')
            .filter(id__in=ids)
            .order_by('created_at')
        )
        if len(payments) != len(ids):
            raise CheckoutError('אחד התשלומים אינו זמין. אנא התחילו את ההרשמה מחדש.')
        if any(p.status not in ('pending', 'failed') for p in payments):
            raise CheckoutError('ההרשמה הזאת כבר בתשלום או שולמה. אנא רעננו את הדף.')
        families = {p.family_id for p in payments}
        if len(families) != 1:
            raise CheckoutError('התשלומים שייכים למשפחות שונות. אנא התחילו את ההרשמה מחדש.')
        oldest = min(p.created_at for p in payments)
        if timezone.now() - oldest > CHECKOUT_MAX_AGE:
            raise CheckoutError('עבר יותר מדי זמן מאז ההרשמה. אנא התחילו אותה מחדש כדי שהמחיר יחושב שוב.')

        (
            CourseCheckout.objects
            .filter(payments__in=payments, status=CourseCheckout.STATUS_PAGE_OPEN)
            .update(status=CourseCheckout.STATUS_REPLACED, updated_at=timezone.now())
        )
        amount = sum((p.final_amount or Decimal('0')) for p in payments)
        checkout = CourseCheckout.objects.create(
            family_id=payments[0].family_id,
            amount=amount,
            page_sum=max(amount, MIN_PAGE_SUM),
            page_terminal=TranzilaService.iframe().terminal,
            token_terminal=token_terminal,
        )
        checkout.payments.set(payments)

    family = payments[0].family
    parent = family.parents.filter(is_primary=True).first() if family else None
    try:
        url = TranzilaService.iframe().create_payment_request(
            amount=checkout.page_sum,
            currency='ILS',
            description=f'הרשמה לחוגים — {family.name if family else ""}'[:80],
            customer_name=(parent.full_name if parent else (family.name if family else '')),
            customer_email=(family.email if family else ''),
            customer_phone=(family.phone if family else ''),
            success_url=f'{front_base}/widget/checkout-result?c={checkout.id}&r=ok',
            error_url=f'{front_base}/widget/checkout-result?c={checkout.id}&r=fail',
            callback_url=f'{api_base}/api/v1/customers/widget/checkout/notify/',
            transaction_id=str(checkout.id),
            tranmode=PAGE_TRANMODE,
        )
    except Exception as exc:
        logger.error('Course checkout %s: the page could not be opened: %s', checkout.id, exc)
        CourseCheckout.objects.filter(id=checkout.id).update(
            status=CourseCheckout.STATUS_FAILED, failure_reason='page_failed', updated_at=timezone.now(),
        )
        raise CheckoutError('הסליקה אינה זמינה כרגע. נסו שוב בעוד כמה דקות.', status_code=503)
    logger.info('Course checkout %s opened: %s payments, ₪%s', checkout.id, len(payments), checkout.amount)
    return checkout, url


# ---------------------------------------------------------------------------
# 2. Tranzila's word, checked against Tranzila's report
# ---------------------------------------------------------------------------

def _row_time(row: dict) -> Optional[datetime]:
    return report_transaction_time(row)


def verify_page_row(checkout: CourseCheckout, index: str, confirmation_code) -> tuple[str, Optional[dict]]:
    """
    ('verified' | 'charged_at_page' | 'unverified' | 'unavailable', row).

    Only 'verified' may lead to a charge by us. 'charged_at_page' means the
    money already moved on the page (the payer changed the tranmode): nothing
    may be charged on top of it.
    """
    index = str(index or '').strip()
    if not index.isdigit():
        return 'unverified', None
    service = TranzilaService.for_terminal(checkout.page_terminal)
    if service is None or service.credential_error():
        return 'unavailable', None
    try:
        found = service.find_transaction(index)
    except Exception as exc:  # network, keys — never a reason to trust the POST
        logger.error('Course checkout %s: report lookup failed: %s', checkout.id, exc)
        return 'unavailable', None
    if not found.get('success'):
        logger.error('Course checkout %s: report lookup failed: %s', checkout.id, found.get('error'))
        return 'unavailable', None
    row = found.get('transaction')
    if not row:
        return 'unverified', None

    reasons = []
    mode = str(row.get('tranmode') or '').strip().upper()
    if not is_tranzila_approved(row.get('processor_response_code') or row.get('response_code')):
        reasons.append('not approved')
    if report_transaction_amount(row) != checkout.page_sum:
        reasons.append(f'sum {report_transaction_amount(row)} != {checkout.page_sum}')
    if not same_authorization_number(row.get('authorization_number'), confirmation_code):
        reasons.append('approval number differs from the notify')
    made_at = _row_time(row)
    if made_at is None or made_at < checkout.created_at - CLOCK_SKEW:
        reasons.append('made before this checkout')
    taken = (
        CourseCheckout.objects
        .filter(page_terminal=checkout.page_terminal, page_index=index)
        .exclude(id=checkout.id)
        .exists()
    )
    if taken:
        reasons.append('index belongs to another checkout')
    if reasons:
        logger.error('Course checkout %s: row %s not accepted: %s', checkout.id, index, '; '.join(reasons))
        return 'unverified', row
    if mode in CHARGED_AT_PAGE_TRANMODES:
        return 'charged_at_page', row
    if mode not in TOKEN_ROW_TRANMODES:
        logger.error('Course checkout %s: row %s has tranmode %s', checkout.id, index, mode)
        return 'unverified', row
    if not str(row.get('credit_card_token') or '').strip() or not (
        row.get('expiration_month') and row.get('expiration_year')
    ):
        logger.error('Course checkout %s: row %s has no token or expiry', checkout.id, index)
        return 'unverified', row
    return 'verified', row


def _normalized_year(value) -> Optional[int]:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year + 2000 if year < 100 else year


def _apply_verdict(checkout: CourseCheckout, verdict: str, row: Optional[dict]) -> None:
    """Inside the caller's transaction, with `checkout` locked and still page_open."""
    if verdict == 'unavailable':
        return  # stays page_open with its number; the poll asks again
    if verdict == 'verified':
        checkout.page_tranmode = str(row.get('tranmode') or '')[:10]
        checkout.card_token = str(row.get('credit_card_token') or '').strip()[:100]
        checkout.card_expire_month = int(row.get('expiration_month'))
        checkout.card_expire_year = _normalized_year(row.get('expiration_year'))
        checkout.status = CourseCheckout.STATUS_VERIFIED
    elif verdict == 'charged_at_page':
        checkout.page_tranmode = str(row.get('tranmode') or '')[:10]
        checkout.status = CourseCheckout.STATUS_REVIEW
        checkout.review_reason = 'charged_at_page'
    else:
        checkout.status = CourseCheckout.STATUS_REVIEW
        checkout.review_reason = 'unverified_page'
    checkout.save()


def _uuid_or_none(value) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def handle_notify(data) -> dict:
    """Tranzila's notify for a course checkout page. Always answers; never trusts the POST alone."""
    tranzila = TranzilaService.iframe()
    parsed = tranzila.parse_webhook_response(data)
    checkout_id = _uuid_or_none(invoice_id_from_pdesc(str(data.get('pdesc') or '')))
    index = str(data.get('index') or '').strip()[:40]
    confirmation = str(parsed.get('confirmation_code') or '').strip()[:40]
    if checkout_id is None:
        logger.warning('Course checkout notify: bad pdesc %r', data.get('pdesc'))
        return {'success': False, 'error': 'unknown checkout'}
    with transaction.atomic():
        checkout = CourseCheckout.objects.select_for_update().filter(id=checkout_id).first()
        if checkout is None:
            logger.warning('Course checkout notify: unknown pdesc %r', data.get('pdesc'))
            return {'success': False, 'error': 'unknown checkout'}
        if checkout.status != CourseCheckout.STATUS_PAGE_OPEN:
            return {'success': True, 'status': checkout.status}
        if index and not checkout.page_index:
            checkout.page_index = index
            checkout.page_confirmation_code = confirmation
            checkout.card_last4 = str(parsed.get('card_last4') or '')[:4]
        if not parsed.get('is_successful'):
            checkout.status = CourseCheckout.STATUS_DECLINED
            checkout.failure_reason = (parsed.get('error_message') or 'הכרטיס לא אושר')[:500]
            checkout.save()
            return {'success': False, 'status': checkout.status}
        verdict, row = verify_page_row(checkout, checkout.page_index, checkout.page_confirmation_code)
        checkout.save()
        _apply_verdict(checkout, verdict, row)
    if checkout.status == CourseCheckout.STATUS_VERIFIED:
        settle_checkout(checkout.id)
        checkout.refresh_from_db()
    return {'success': True, 'status': checkout.status}


def retry_verification(checkout_id, *, index: str = '', confirmation_code: str = '') -> None:
    """
    From the widget's poll: a page that has a number but no verdict yet (the
    report was unreachable, or the notify never came and the result page
    passed the number on). The verdict is the report's, as in handle_notify.
    """
    with transaction.atomic():
        checkout = CourseCheckout.objects.select_for_update().filter(id=checkout_id).first()
        if checkout is None or checkout.status != CourseCheckout.STATUS_PAGE_OPEN:
            return
        if index and not checkout.page_index:
            checkout.page_index = str(index).strip()[:40]
            checkout.page_confirmation_code = str(confirmation_code or '').strip()[:40]
            checkout.save(update_fields=['page_index', 'page_confirmation_code', 'updated_at'])
        if not checkout.page_index:
            return
        verdict, row = verify_page_row(checkout, checkout.page_index, checkout.page_confirmation_code)
        _apply_verdict(checkout, verdict, row)
    if checkout.status == CourseCheckout.STATUS_VERIFIED:
        settle_checkout(checkout.id)


# ---------------------------------------------------------------------------
# 3. The charge
# ---------------------------------------------------------------------------

def _refuse_before_charge(checkout: CourseCheckout, claim, payments, reason: str) -> None:
    """Nothing was charged: the claim goes, the payments are failed with the reason."""
    TranzilaTransaction.objects.filter(pk=claim.pk, is_successful=False).delete()
    with transaction.atomic():
        CourseCheckout.objects.filter(id=checkout.id).update(
            status=CourseCheckout.STATUS_FAILED, failure_reason=reason[:500], updated_at=timezone.now(),
        )
        Payment.objects.filter(id__in=[p.id for p in payments], status__in=('pending', 'failed')).update(
            status='failed', failure_reason=reason[:500], updated_at=timezone.now(),
        )
    logger.error('Course checkout %s refused before charging: %s', checkout.id, reason)


def settle_checkout(checkout_id) -> None:
    """Charge a verified checkout once and activate what it paid for."""
    from apps.customers.trial_credit import credit_still_held_by
    from apps.customers.widget_views import precheck_widget_capacity, widget_charge_items

    with transaction.atomic():
        checkout = CourseCheckout.objects.select_for_update().filter(id=checkout_id).first()
        if checkout is None or checkout.status != CourseCheckout.STATUS_VERIFIED:
            return
        checkout.status = CourseCheckout.STATUS_CHARGING
        checkout.save(update_fields=['status', 'updated_at'])

    try:
        with transaction.atomic():
            claim = TranzilaTransaction.objects.create(
                transaction_id='', confirmation_code='', transaction_type='recurring_setup',
                response_code='', response_message='', response_data={},
                request_data={'course_checkout': str(checkout.id), 'amount': str(checkout.amount)},
                idempotency_key=f'course_checkout_{checkout.id}', is_successful=False,
                tranzila_terminal=checkout.token_terminal,
            )
    except IntegrityError:
        logger.error('Course checkout %s: another request holds the charge', checkout.id)
        return

    payments = list(
        checkout.payments.select_related('child', 'family', 'lesson__course__branch', 'bundle').order_by('created_at')
    )
    if any(p.status not in ('pending', 'failed') for p in payments):
        _refuse_before_charge(checkout, claim, [], 'אחד התשלומים כבר אינו ממתין לתשלום')
        return
    if sum((p.final_amount or Decimal('0')) for p in payments) != checkout.amount:
        _refuse_before_charge(checkout, claim, payments, 'הסכום השתנה מאז שנפתח עמוד התשלום')
        return
    capacity_error = precheck_widget_capacity([str(p.id) for p in payments], single_lessons=True)
    if capacity_error:
        _refuse_before_charge(checkout, claim, payments, capacity_error)
        return
    for payment in payments:
        if (payment.trial_credit_amount or Decimal('0')) > 0 and payment.trial_credit_source_id:
            if not credit_still_held_by(payment):
                _refuse_before_charge(checkout, claim, payments, 'הקיזוז של שיעור הניסיון נוצל בהרשמה אחרת')
                return

    if checkout.amount > 0:
        client = TranzilaService.for_saved_card(checkout.token_terminal)
        if client is None:
            result = {'success': False, 'never_sent': True, 'error': f'למסוף {checkout.token_terminal} אין מפתחות'}
        else:
            Payment.objects.filter(id__in=[p.id for p in payments]).update(status='processing', updated_at=timezone.now())
            items = []
            for payment in payments:
                items.extend(widget_charge_items(payment))
            result = client.charge_with_token(
                token=checkout.card_token,
                amount=checkout.amount,
                description=f'הרשמה לחוגים — {payments[0].family.name if payments[0].family else ""}'[:100],
                transaction_id=str(checkout.id),
                items=items,
                expire_month=checkout.card_expire_month,
                expire_year=checkout.card_expire_year,
                duplicate_guard_key=f'checkout-{checkout.id}',
            )
    else:
        # Nothing is due today: the card was checked on the page and is kept
        # for the standing orders. No charge.
        result = {'success': True, 'transaction_id': '', 'confirmation_code': '', 'response_code': '000',
                  'raw_response': {'no_charge': True}}
    outcome = token_charge_outcome(result)

    if outcome == TOKEN_CHARGED:
        try:
            _complete(checkout, claim, payments, result)
        except Exception as exc:
            # The card is charged and the record of it did not go through. The
            # claim keeps the transaction's facts and blocks every other charge;
            # the office completes the registration by hand.
            logger.exception('Course checkout %s charged but not recorded', checkout.id)
            TranzilaTransaction.objects.filter(pk=claim.pk).update(
                transaction_id=str(result.get('transaction_id') or '')[:100],
                confirmation_code=str(result.get('confirmation_code') or '')[:100],
                response_message=f'charged; activation failed: {exc}'[:1000],
            )
            CourseCheckout.objects.filter(id=checkout.id).update(
                status=CourseCheckout.STATUS_REVIEW, review_reason='charged_not_recorded',
                failure_reason=str(exc)[:500], charge_transaction=claim, updated_at=timezone.now(),
            )
        return
    if outcome == TOKEN_DECLINED:
        _declined(checkout, claim, payments, result)
        return
    if outcome == TOKEN_SETUP_PROBLEM:
        TranzilaTransaction.objects.filter(pk=claim.pk, is_successful=False).delete()
        Payment.objects.filter(id__in=[p.id for p in payments], status='processing').update(
            status='pending', updated_at=timezone.now(),
        )
        CourseCheckout.objects.filter(id=checkout.id).update(
            status=CourseCheckout.STATUS_REVIEW,
            review_reason='setup',
            failure_reason=str(result.get('error') or '')[:500],
            updated_at=timezone.now(),
        )
        logger.error('Course checkout %s not charged — setup problem: %s', checkout.id, result.get('error'))
        return
    # No answer, or one that says nothing certain: the card may be charged.
    # The claim stays; the payments stay processing; the office checks.
    claim.response_message = str(result.get('error') or '')[:1000]
    claim.save(update_fields=['response_message'])
    CourseCheckout.objects.filter(id=checkout.id).update(
        status=CourseCheckout.STATUS_UNCERTAIN, failure_reason=str(result.get('error') or '')[:500],
        charge_transaction=claim, updated_at=timezone.now(),
    )
    logger.error('Course checkout %s charge uncertain: %s', checkout.id, result.get('error'))


def _complete(checkout: CourseCheckout, claim, payments, result: dict) -> None:
    from apps.customers.widget_views import activate_paid_widget_payment

    trial_enrollments = []
    subscription_payments = []
    with transaction.atomic():
        claim.transaction_id = str(result.get('transaction_id') or '')[:100]
        claim.confirmation_code = str(result.get('confirmation_code') or '')[:100]
        claim.response_code = str(result.get('response_code') or '000')[:10]
        claim.response_data = result.get('raw_response') or {}
        claim.is_successful = True
        claim.response_timestamp = timezone.now()
        claim.save()
        for payment in payments:
            locked = (
                Payment.objects.select_for_update(of=('self',))
                .select_related('child', 'family', 'lesson__course__branch', 'bundle')
                .get(id=payment.id)
            )
            if locked.status == 'completed':
                continue
            enrollment_id = activate_paid_widget_payment(
                locked,
                tranzila_txn=claim,
                token=checkout.card_token,
                expiry_month=checkout.card_expire_month,
                expiry_year=checkout.card_expire_year,
                tranzila_terminal=checkout.token_terminal,
            )
            if enrollment_id:
                trial_enrollments.append(enrollment_id)
            else:
                subscription_payments.append(locked)
        CourseCheckout.objects.filter(id=checkout.id).update(
            status=CourseCheckout.STATUS_COMPLETED, charge_transaction=claim, updated_at=timezone.now(),
        )
    logger.info('Course checkout %s charged: ₪%s, transaction %s', checkout.id, checkout.amount, claim.transaction_id)

    # The receipt and the messages come after the record, never inside it: a
    # failure there must not undo a charge that happened.
    try:
        from apps.customers.checkout_invoice import issue_widget_checkout_invoice

        charged = (
            Payment.objects
            .filter(id__in=[p.id for p in payments], status='completed', final_amount__gt=0)
            .select_related('child', 'family', 'parent', 'branch', 'lesson__course', 'bundle', 'tranzila_transaction')
            .prefetch_related('bundle__lessons')
        )
        if charged.exists():
            issue_widget_checkout_invoice(charged)
    except Exception:
        logger.exception('Course checkout %s: receipt failed (non-fatal)', checkout.id)
    for enrollment_id in trial_enrollments:
        try:
            from apps.enrollments.trial_reminders import stamp_and_notify_trial_enrollment

            stamp_and_notify_trial_enrollment(enrollment_id)
        except Exception:
            logger.exception('Course checkout %s: trial WhatsApp failed (non-fatal)', checkout.id)
    notified_children = set()
    for payment in subscription_payments:
        if payment.child_id in notified_children:
            continue
        notified_children.add(payment.child_id)
        try:
            from apps.core.payment_service import PaymentService

            PaymentService()._send_registration_whatsapp(payment)
        except Exception:
            logger.exception('Course checkout %s: registration WhatsApp failed (non-fatal)', checkout.id)


def _declined(checkout: CourseCheckout, claim, payments, result: dict) -> None:
    from apps.customers.models import Child
    from apps.customers.widget_views import _status_after_failed_charge, mark_child_groups_stale

    TranzilaTransaction.objects.filter(pk=claim.pk, is_successful=False).delete()
    error = str(result.get('error') or 'התשלום נדחה')[:500]
    with transaction.atomic():
        Payment.objects.filter(id__in=[p.id for p in payments], status__in=('pending', 'processing', 'failed')).update(
            status='failed', failure_reason=error, failure_code=str(result.get('response_code') or '')[:50],
            updated_at=timezone.now(),
        )
        for child_id, is_trial in {(p.child_id, p.trial_lesson_date is not None) for p in payments if p.child_id}:
            child = Child.objects.get(pk=child_id)
            Child.objects.filter(pk=child_id).update(status=_status_after_failed_charge(child, is_trial))
            mark_child_groups_stale(child_id)
        CourseCheckout.objects.filter(id=checkout.id).update(
            status=CourseCheckout.STATUS_DECLINED, failure_reason=error, updated_at=timezone.now(),
        )
    logger.warning('Course checkout %s declined: %s', checkout.id, error)


# ---------------------------------------------------------------------------
# 4. What the widget sees
# ---------------------------------------------------------------------------

def checkout_status(checkout_id, *, index: str = '', confirmation_code: str = '') -> Optional[dict]:
    checkout_id = _uuid_or_none(checkout_id)
    if checkout_id is None:
        return None
    checkout = CourseCheckout.objects.filter(id=checkout_id).first()
    if checkout is None:
        return None
    if checkout.status == CourseCheckout.STATUS_PAGE_OPEN and (
        (index and not checkout.page_index)
        or (checkout.page_index and timezone.now() - checkout.updated_at >= RETRY_VERIFY_AFTER)
    ):
        retry_verification(checkout.id, index=index, confirmation_code=confirmation_code)
        checkout.refresh_from_db()
    return {
        'checkout_id': str(checkout.id),
        'status': checkout.status,
        # A refusal before any charge says why (the class filled up, the price
        # changed); the other reasons are for the office, not the parent.
        'message': (
            checkout.failure_reason
            if checkout.status == CourseCheckout.STATUS_FAILED and checkout.failure_reason
            and checkout.failure_reason != 'page_failed'
            else MESSAGES.get(checkout.status, '')
        ),
        'amount': str(checkout.amount),
        'payments': [
            {'payment_id': str(p.id), 'status': p.status}
            for p in checkout.payments.only('id', 'status').order_by('created_at')
        ],
    }
