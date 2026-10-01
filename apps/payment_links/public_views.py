"""
The payer's side of a payment link: read the link, start a payment, the
Tranzila callback, and a status poll. No auth (the slug is the capability),
throttled, and the DB caps attempts per IP / phone so the guard holds across
serverless instances where DRF's cache throttle does not.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.tranzila_service import (
    CHARGE_TRANMODES,
    TranzilaService,
    invoice_id_from_pdesc,
    is_tranzila_approved,
    report_transaction_amount,
    report_transaction_time,
    same_authorization_number,
)
from apps.customers.models import TranzilaTransaction
from apps.payment_links.models import PaymentLink, PaymentLinkPayment, money
from apps.payment_links.serializers import PublicPaymentLinkSerializer

logger = logging.getLogger(__name__)

# Across instances: too many unfinished attempts from one phone, or from one
# IP, in the window means someone is hammering the page. A row younger than
# SETTLE is a parent still typing, not an attempt — and a school hall full of
# parents shares one IP, so that cap is looser than the per-phone one.
ATTEMPT_WINDOW = timedelta(minutes=10)
ATTEMPT_SETTLE = timedelta(minutes=2)
ATTEMPT_CAP = 5
IP_ATTEMPT_CAP = 20


def _client_ip(request) -> str | None:
    forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    return forwarded or request.META.get('REMOTE_ADDR') or None


def _clean_phone(value: str) -> str:
    return re.sub(r'\D', '', value or '')[:15]


def _api_base() -> str:
    base = (getattr(settings, 'CRM_API_BASE_URL', '') or '').strip().rstrip('/')
    if not base or 'localhost' in base or '127.0.0.1' in base:
        return ''
    return base


class _PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]


class PublicPaymentLinkView(_PublicView):
    throttle_scope = 'payment_link_view'

    def get(self, request, slug: str):
        link = PaymentLink.objects.prefetch_related('options').filter(slug=slug).first()
        if link is None:
            return Response({'error': 'הקישור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        if not link.is_open():
            return Response({'error': 'הקישור אינו פעיל יותר', 'closed': True}, status=status.HTTP_410_GONE)
        return Response(PublicPaymentLinkSerializer(link).data)


class PublicPaymentStartView(_PublicView):
    throttle_scope = 'payment_link_start'

    def post(self, request, slug: str):
        link = (
            PaymentLink.objects.prefetch_related('options')
            .select_related('business_customer', 'business_category', 'target_invoice')
            .filter(slug=slug).first()
        )
        if link is None:
            return Response({'error': 'הקישור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        if not link.is_open():
            return Response({'error': 'הקישור אינו פעיל יותר', 'closed': True}, status=status.HTTP_410_GONE)

        option_id = str(request.data.get('option_id') or '').strip()
        option = next((o for o in link.options.all() if str(o.id) == option_id and o.is_active), None)
        if option is None:
            return Response({'error': 'יש לבחור אפשרות תשלום'}, status=status.HTTP_400_BAD_REQUEST)

        business_charge = link.kind == PaymentLink.KIND_BUSINESS_CHARGE
        if business_charge:
            customer = link.business_customer
            payer_name = (customer.full_name if customer else '')[:120]
            payer_phone = _clean_phone(customer.phone if customer else '')
            payer_email = (customer.email if customer else '')[:254]
            try:
                from apps.payment_links.business_charge import validate_business_charge_link

                validate_business_charge_link(link, option.amount)
            except ValueError as exc:
                return Response({'error': str(exc), 'closed': True}, status=status.HTTP_409_CONFLICT)
        else:
            payer_name = str(request.data.get('payer_name') or '').strip()[:120]
            payer_phone = _clean_phone(str(request.data.get('payer_phone') or ''))
            payer_email = str(request.data.get('payer_email') or '').strip()[:254]
            if len(payer_name) < 2:
                return Response({'error': 'יש להזין שם'}, status=status.HTTP_400_BAD_REQUEST)
            if len(payer_phone) < 9:
                return Response({'error': 'יש להזין טלפון תקין'}, status=status.HTTP_400_BAD_REQUEST)
        if payer_email and '@' not in payer_email:
            return Response({'error': 'כתובת מייל לא תקינה'}, status=status.HTTP_400_BAD_REQUEST)

        api_base = _api_base()
        if not api_base:
            # Without a public API base Tranzila could not call back and the
            # payment would stay pending forever. Refuse rather than fall back to
            # the customers webhook, which cannot resolve our row.
            logger.error('payment link start refused: CRM_API_BASE_URL is not set')
            return Response({'error': 'הסליקה אינה זמינה כרגע'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        # A business charge opens only with its own switch; every other link
        # with the general one. Refused before a payment row is written.
        hosted_page_open = bool(getattr(
            settings, 'BUSINESS_CHARGE_ENABLED' if business_charge else 'TRANZILA_HOSTED_PAGE_ENABLED', False,
        ))
        if not hosted_page_open:
            return Response({'error': 'הסליקה אינה זמינה כרגע'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        business_terminal = (
            getattr(settings, 'BUSINESS_CHARGE_TRANZILA_TERMINAL', '')
            if business_charge else ''
        )
        tranzila = TranzilaService.iframe(terminal=business_terminal or None)
        if business_charge and (tranzila.terminal or '').strip().lower() != 'cogolive':
            logger.error('business charge refused: hosted terminal is not cogolive')
            return Response(
                {'error': 'הגבייה העסקית זמינה רק במסוף Cogolive'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ip = _client_ip(request)
        now = timezone.now()
        from django.db.models import Q
        settled = (
            PaymentLinkPayment.objects
            .filter(created_at__gte=now - ATTEMPT_WINDOW)
            .exclude(status=PaymentLinkPayment.STATUS_COMPLETED)
            .filter(Q(created_at__lte=now - ATTEMPT_SETTLE) | Q(status=PaymentLinkPayment.STATUS_FAILED))
        )
        if payer_phone and settled.filter(payer_phone=payer_phone).count() >= ATTEMPT_CAP:
            return Response({'error': 'יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if ip and settled.filter(ip_address=ip).count() >= IP_ATTEMPT_CAP:
            return Response({'error': 'יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        with transaction.atomic():
            if business_charge:
                link = PaymentLink.objects.select_for_update().get(pk=link.pk)
                if not link.is_open() or link.payments.filter(status=PaymentLinkPayment.STATUS_PENDING).exists():
                    return Response(
                        {'error': 'כבר התחיל ניסיון תשלום בקישור הזה. יש לבדוק את מצבו לפני ניסיון נוסף.'},
                        status=status.HTTP_409_CONFLICT,
                    )
            row = PaymentLinkPayment.objects.create(
                link=link,
                option=option,
                option_label=option.label,
                amount=money(option.amount),
                payer_name=payer_name,
                payer_phone=payer_phone,
                payer_email=payer_email,
                ip_address=ip,
                # The notify and the report lookup must go to this same
                # terminal: a business charge runs on its own (cogolive). A
                # general link keeps learning it from the notify, as before.
                tranzila_terminal=(tranzila.terminal or '')[:40] if business_charge else '',
            )

        front = link.public_url()
        try:
            iframe_url = tranzila.create_payment_request(
                hosted_page_allowed=hosted_page_open,
                amount=row.amount,
                currency='ILS',
                description=f'{link.title} — {option.label}'[:80],
                customer_name=payer_name,
                customer_email=payer_email,
                customer_phone=payer_phone,
                success_url=f'{front}/result?p={row.id}&r=ok',
                error_url=f'{front}/result?p={row.id}&r=fail',
                callback_url=f'{api_base}/api/v1/payment-links/public/callback/',
                transaction_id=str(row.id),
            )
        except Exception as exc:
            logger.error('payment link %s: Tranzila handshake failed: %s', link.slug, exc)
            row.status = PaymentLinkPayment.STATUS_FAILED
            row.failure_reason = 'handshake_failed'
            row.save(update_fields=['status', 'failure_reason', 'updated_at'])
            return Response({'error': 'הסליקה אינה זמינה כרגע. נסו שוב מאוחר יותר.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({'payment_id': str(row.id), 'amount': str(row.amount), 'iframe_url': iframe_url})


class PublicPaymentStatusView(_PublicView):
    throttle_scope = 'payment_link_status'

    def get(self, request, payment_id):
        row = PaymentLinkPayment.objects.filter(id=payment_id).select_related('link', 'formal_document').first()
        if row is None:
            return Response({'error': 'לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'payment_id': str(row.id),
            'status': row.status,
            'amount': str(row.amount),
            'link_title': row.link.title,
            'failure_reason': row.failure_reason if row.status == PaymentLinkPayment.STATUS_FAILED else '',
            'document_ready': bool(row.formal_document_id),
            'document_number': row.formal_document.document_number if row.formal_document_id else '',
            'document_pending': bool(row.status == PaymentLinkPayment.STATUS_COMPLETED and not row.formal_document_id),
        })


def _reported_sum(raw, parsed_amount) -> Decimal:
    """
    Tranzila echoes `sum` the way the iframe was built — shekels with decimals
    ("50.00"). Read it as such; fall back to the service's parse only when the
    raw value is unreadable.
    """
    try:
        return money(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError, TypeError):
        try:
            return money(parsed_amount or 0)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0.00')


# A payment link paid before 25.9.2026 kept no terminal. Such a record holds a
# transaction number only when it was paid about when the report's row was
# made (the same day, give or take a day): numbers repeat across terminals,
# and an old payment of the test terminal must not claim today's charge that
# happens to carry the same number.
UNTERMINALED_PAYMENT_WINDOW = timedelta(days=1)


def link_payment_holds_index(txn_index: str, terminal: str, made_at=None, *, exclude_id=None) -> bool:
    """
    Whether a completed payment-link payment was paid by this transaction
    number: one on this terminal, or one that kept no terminal and was paid
    within UNTERMINALED_PAYMENT_WINDOW of `made_at` (the report row's time).
    With no row time to compare (`made_at` None), a record without a
    terminal counts as before — the caller refuses a row without a time
    anyway. A record with neither terminal nor payment time holds nothing.
    """
    paid = PaymentLinkPayment.objects.filter(
        gateway_transaction_id=txn_index, status=PaymentLinkPayment.STATUS_COMPLETED,
    )
    if exclude_id is not None:
        paid = paid.exclude(id=exclude_id)
    if paid.filter(tranzila_terminal=terminal).exists():
        return True
    unterminaled = paid.filter(tranzila_terminal='')
    if made_at is None:
        return unterminaled.exists()
    return unterminaled.filter(
        paid_at__gte=made_at - UNTERMINALED_PAYMENT_WINDOW, paid_at__lte=made_at + UNTERMINALED_PAYMENT_WINDOW,
    ).exists()


def _index_paid_for_something_else(row_id, txn_index: str, terminal: str, made_at=None) -> bool:
    """
    True when this transaction number already paid for another order.

    A notify is public, so it can quote a real transaction that paid for
    something else — one of ours, or the other website that shares the
    terminal. Only rows that were actually paid count, so a forged notify
    left pending cannot block the real one. Matched on the terminal, since
    numbers repeat across terminals; a payment link paid before 25.9.2026
    kept no terminal, so a completed one with this number counts only when
    it was paid about when the report's row was made (`made_at`).
    """
    from apps.store.models import StoreInvoice

    if (
        StoreInvoice.objects.filter(tranzila_transaction_id=txn_index, tranzila_terminal=terminal)
        .exclude(id=row_id)
        .exclude(payment_status__in=['pending', 'failed'])
        .exists()
    ):
        return True
    # A store order paid twice keeps its second, real charge beside its own
    # number (other_transactions, state second_charge): paid, for that order.
    if (
        StoreInvoice.objects.filter(
            other_transactions__contains=[{'index': txn_index, 'terminal': terminal, 'state': 'second_charge'}],
        )
        .exclude(id=row_id)
        .exists()
    ):
        return True
    return link_payment_holds_index(txn_index, terminal, made_at, exclude_id=row_id)


# A report row may predate the row it pays for by this much (clock skew
# between our server and Tranzila's), never more.
TRANSACTION_CLOCK_SKEW = timedelta(minutes=10)


def verify_transaction_with_tranzila(
    row, txn_index: str, *, confirmation_code, service: TranzilaService | None = None,
) -> tuple[str, dict | None]:
    """
    Ask Tranzila whether this transaction really paid for this row.

    The notify POST itself is not authenticated (Tranzila sends no signature),
    and the hosted-page terminal also takes the other website's payments. A
    row is marked paid only when the terminal's own report shows, under this
    number:
      * an approved charge — tranmode A or AK only (a J2 check comes back
        approved with the same sum and moves no money);
      * of exactly this sum;
      * with the approval number the notify reported;
      * made after this row was created;
      * that no other order of ours already holds.
    Anything else lands on review for a person.

    `row` needs `id`, `amount` (shekels) and `created_at`. Returns
    ('verified', txn_row) | ('unverified', txn_row or None) | ('unavailable', None).
    """
    txn_index = str(txn_index or '').strip()
    if not txn_index.isdigit():
        return 'unverified', None
    try:
        # The terminal the row was paid on (a payment link passes its own);
        # numbers repeat across terminals, so another one's report proves nothing.
        service = service or TranzilaService.iframe()
        if service.credential_error():
            return 'unavailable', None
        found = service.find_transaction(txn_index)
    except Exception as exc:  # network, auth — never a reason to trust the POST
        logger.error('payment %s: transaction lookup failed: %s', row.id, exc)
        return 'unavailable', None
    if not found.get('success'):
        logger.error('payment %s: transaction lookup failed: %s', row.id, found.get('error'))
        return 'unavailable', None
    txn = found.get('transaction')
    if not txn:
        return 'unverified', None

    reasons = []
    if not is_tranzila_approved(txn.get('processor_response_code') or txn.get('response_code')):
        reasons.append('not approved')
    if str(txn.get('tranmode') or '').strip().upper() not in CHARGE_TRANMODES:
        reasons.append(f"tranmode {txn.get('tranmode')!r} is not a charge")
    if report_transaction_amount(txn) != money(row.amount):
        reasons.append(f'sum {report_transaction_amount(txn)} != {money(row.amount)}')
    if not same_authorization_number(txn.get('authorization_number'), confirmation_code):
        reasons.append('approval number differs from the notify')
    made_at = report_transaction_time(txn)
    if made_at is None:
        reasons.append('no transaction time on the report')
    elif made_at < row.created_at - TRANSACTION_CLOCK_SKEW:
        reasons.append(f'transaction made before this order ({made_at.isoformat()})')
    pdesc = str(txn.get('pdesc') or '').strip()
    if pdesc and invoice_id_from_pdesc(pdesc) != str(row.id):
        reasons.append('pdesc of another order')
    if _index_paid_for_something_else(row.id, txn_index, service.terminal, made_at):
        reasons.append('index already paid for another order')
    if reasons:
        logger.error('payment %s: transaction %s not accepted: %s', row.id, txn_index, '; '.join(reasons))
        return 'unverified', txn
    return 'verified', txn


def _amounts_match(locked: Decimal, reported) -> bool:
    try:
        return money(locked) == money(reported)
    except Exception:
        return False


@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def payment_link_callback(request):
    """
    Tranzila notify for payment-link payments. Public, not throttled (Tranzila
    retries). Always answers 200 with a JSON verdict, except 400 on a bad
    signature, so the gateway does not keep retrying a row we have settled.
    """
    tranzila = TranzilaService.iframe()
    # The hosted page runs on this terminal; its transaction numbers mean
    # something only together with it.
    terminal = (tranzila.terminal or '')[:40]
    signature = request.headers.get('X-Tranzila-Signature', '')
    parsed = tranzila.parse_webhook_response(request.data)
    if signature and not tranzila.verify_webhook_signature(parsed, signature):
        logger.error('payment link callback: invalid signature')
        return Response({'success': False, 'error': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

    row_id = invoice_id_from_pdesc(request.data.get('pdesc', ''))
    if not row_id:
        return Response({'success': False, 'error': 'missing pdesc'})

    with transaction.atomic():
        row = (
            PaymentLinkPayment.objects.select_for_update(of=('self',))
            .filter(id=row_id).first()
            if _is_uuid(row_id) else None
        )
        if row is None:
            logger.warning('payment link callback: unknown pdesc %s', row_id)
            return Response({'success': False, 'error': 'unknown payment'})

        # The page this row was opened on. A business charge runs on its own
        # terminal (BUSINESS_CHARGE_TRANZILA_TERMINAL); reading the notify
        # against the general one sent every such payment to review.
        if row.tranzila_terminal:
            tranzila = TranzilaService.iframe(terminal=row.tranzila_terminal)
            terminal = (tranzila.terminal or '')[:40]

        txn_index = str(parsed.get('transaction_id') or '').strip()
        idempotency_key = f'paylink_{row.id}_{txn_index or "noindex"}'[:255]

        if row.status == PaymentLinkPayment.STATUS_COMPLETED:
            # A completed row is never downgraded, whatever the retry says. A
            # *different* approved index on it means a second charge went
            # through — record it and flag it, never lose it.
            if parsed.get('is_successful') and txn_index and txn_index != row.gateway_transaction_id:
                TranzilaTransaction.objects.get_or_create(
                    idempotency_key=idempotency_key,
                    defaults=_txn_defaults(parsed, request, txn_index, terminal),
                )
                row.review_reason = f'second_charge:{txn_index}'[:200]
                row.save(update_fields=['review_reason', 'updated_at'])
                logger.error('payment link %s: a second approved transaction %s was reported', row.id, txn_index)
            if row.link.kind == PaymentLink.KIND_BUSINESS_CHARGE and not row.formal_document_id:
                from apps.payment_links.business_charge import ensure_business_charge_document

                transaction.on_commit(lambda: ensure_business_charge_document(row.id))
            return Response({'success': True, 'message': 'Already processed'})

        # The same gateway index can belong to one payment only.
        if txn_index and (
            PaymentLinkPayment.objects
            .filter(Q(tranzila_terminal=terminal) | Q(tranzila_terminal=''), gateway_transaction_id=txn_index)
            .exclude(id=row.id)
            .exists()
        ):
            row.status = PaymentLinkPayment.STATUS_REVIEW
            row.review_reason = f'index_reused:{txn_index}'[:200]
            row.gateway_transaction_id = txn_index[:100]
            row.tranzila_terminal = terminal
            row.save(update_fields=['status', 'review_reason', 'gateway_transaction_id', 'tranzila_terminal', 'updated_at'])
            logger.error('payment link %s: index %s already belongs to another payment', row.id, txn_index)
            return Response({'success': True, 'status': row.status})

        if not parsed.get('is_successful'):
            row.status = PaymentLinkPayment.STATUS_FAILED
            row.failure_code = str(parsed.get('response_code') or '')[:10]
            row.failure_reason = (parsed.get('error_message') or 'התשלום נדחה')[:500]
            row.gateway_transaction_id = txn_index[:100]
            row.tranzila_terminal = terminal
            row.save(update_fields=[
                'status', 'failure_code', 'failure_reason', 'gateway_transaction_id', 'tranzila_terminal', 'updated_at',
            ])
            return Response({'success': False, 'status': row.status})

        txn, created = TranzilaTransaction.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults=_txn_defaults(parsed, request, txn_index, terminal),
        )
        row.tranzila_transaction = txn
        row.gateway_transaction_id = txn_index[:100]
        row.tranzila_terminal = terminal
        row.gateway_confirmation_code = str(parsed.get('confirmation_code') or '')[:100]
        row.card_last4 = str(parsed.get('card_last4') or '')[:4]
        row.card_type = str(parsed.get('card_type') or '')[:30]
        row.reported_amount = _reported_sum(request.data.get('sum'), parsed.get('amount'))
        row.paid_at = timezone.now()

        currency = str(request.data.get('currency') or parsed.get('currency') or '1').strip().upper()
        if not _amounts_match(row.amount, row.reported_amount):
            # Money moved, but not the sum this row was created for. Keep the
            # facts, do not count it as income until a person looks.
            row.status = PaymentLinkPayment.STATUS_REVIEW
            row.review_reason = f'amount_mismatch: locked {row.amount} reported {row.reported_amount}'[:200]
            logger.error('payment link %s: %s', row.id, row.review_reason)
        elif currency not in ('1', 'ILS', 'NIS'):
            row.status = PaymentLinkPayment.STATUS_REVIEW
            row.review_reason = f'currency:{currency}'[:200]
        else:
            # The POST is unauthenticated; only Tranzila's own ledger makes it income.
            verdict, _txn_row = verify_transaction_with_tranzila(
                row, txn_index, confirmation_code=parsed.get('confirmation_code'), service=tranzila,
            )
            if verdict == 'verified':
                row.status = PaymentLinkPayment.STATUS_COMPLETED
                row.review_reason = ''
            else:
                row.status = PaymentLinkPayment.STATUS_REVIEW
                row.review_reason = ('unverified_callback' if verdict == 'unverified' else 'verification_unavailable')
                logger.error('payment link %s: callback not confirmed by Tranzila (%s)', row.id, verdict)
        row.save()

        if row.status == PaymentLinkPayment.STATUS_COMPLETED and row.link.kind == PaymentLink.KIND_BUSINESS_CHARGE:
            from apps.payment_links.business_charge import ensure_business_charge_document

            transaction.on_commit(lambda: ensure_business_charge_document(row.id))

    return Response({'success': True, 'status': row.status, 'new_transaction': created})


def _txn_defaults(parsed: dict, request, txn_index: str, terminal: str) -> dict:
    return {
        'transaction_id': txn_index[:100],
        'confirmation_code': str(parsed.get('confirmation_code') or '')[:100],
        'transaction_type': 'charge',
        'response_code': str(parsed.get('response_code') or '')[:10],
        'response_message': '',
        'request_data': {'pdesc': request.data.get('pdesc', '')},
        'response_data': _jsonable(parsed.get('raw_payload') or {}),
        'is_successful': True,
        'response_timestamp': timezone.now(),
        'tranzila_terminal': terminal,
    }


def _is_uuid(value: str) -> bool:
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _jsonable(payload) -> dict:
    if hasattr(payload, 'dict'):
        payload = payload.dict()
    return {str(k): (v if isinstance(v, (str, int, float, bool)) or v is None else str(v)) for k, v in dict(payload).items()}
