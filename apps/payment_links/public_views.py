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
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.tranzila_service import TranzilaService, invoice_id_from_pdesc
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
        link = PaymentLink.objects.prefetch_related('options').filter(slug=slug).first()
        if link is None:
            return Response({'error': 'הקישור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        if not link.is_open():
            return Response({'error': 'הקישור אינו פעיל יותר', 'closed': True}, status=status.HTTP_410_GONE)

        option_id = str(request.data.get('option_id') or '').strip()
        option = next((o for o in link.options.all() if str(o.id) == option_id and o.is_active), None)
        if option is None:
            return Response({'error': 'יש לבחור אפשרות תשלום'}, status=status.HTTP_400_BAD_REQUEST)

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

        ip = _client_ip(request)
        now = timezone.now()
        from django.db.models import Q
        settled = (
            PaymentLinkPayment.objects
            .filter(created_at__gte=now - ATTEMPT_WINDOW)
            .exclude(status=PaymentLinkPayment.STATUS_COMPLETED)
            .filter(Q(created_at__lte=now - ATTEMPT_SETTLE) | Q(status=PaymentLinkPayment.STATUS_FAILED))
        )
        if settled.filter(payer_phone=payer_phone).count() >= ATTEMPT_CAP:
            return Response({'error': 'יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if ip and settled.filter(ip_address=ip).count() >= IP_ATTEMPT_CAP:
            return Response({'error': 'יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        row = PaymentLinkPayment.objects.create(
            link=link,
            option=option,
            option_label=option.label,
            amount=money(option.amount),
            payer_name=payer_name,
            payer_phone=payer_phone,
            payer_email=payer_email,
            ip_address=ip,
        )

        front = link.public_url()
        try:
            iframe_url = TranzilaService.iframe().create_payment_request(
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
        row = PaymentLinkPayment.objects.filter(id=payment_id).select_related('link').first()
        if row is None:
            return Response({'error': 'לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'payment_id': str(row.id),
            'status': row.status,
            'amount': str(row.amount),
            'link_title': row.link.title,
            'failure_reason': row.failure_reason if row.status == PaymentLinkPayment.STATUS_FAILED else '',
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


def verify_transaction_with_tranzila(row: PaymentLinkPayment, txn_index: str) -> tuple[str, dict | None]:
    """
    Ask Tranzila whether this transaction really happened on our terminal.

    The notify POST itself is not authenticated (Tranzila sends no signature),
    so a row is marked completed only when the terminal's own transaction list
    shows an approved transaction with this index and this sum. Anything else
    lands on review for a person.

    Returns ('verified', txn_row) | ('unverified', None) | ('unavailable', None).
    """
    if not txn_index:
        return 'unverified', None
    try:
        service = TranzilaService.iframe()
        if service.credential_error():
            return 'unavailable', None
        today = timezone.localdate()
        listing = service.list_all_transactions(today - timedelta(days=1), today)
    except Exception as exc:  # network, auth — never a reason to trust the POST
        logger.error('payment link %s: transaction lookup failed: %s', row.id, exc)
        return 'unavailable', None
    if not isinstance(listing, dict) or listing.get('success') is False:
        return 'unavailable', None
    for txn in listing.get('transactions') or []:
        index = str(txn.get('index') or txn.get('transaction_index') or txn.get('id') or '').strip()
        if index != str(txn_index).strip():
            continue
        raw_sum = txn.get('sum', txn.get('amount', txn.get('transaction_sum')))
        try:
            txn_sum = money(Decimal(str(raw_sum).strip()))
        except (InvalidOperation, ValueError, TypeError):
            txn_sum = None
        pdesc = str(txn.get('pdesc') or '').strip()
        if pdesc and invoice_id_from_pdesc(pdesc) != str(row.id):
            continue
        if txn_sum is not None and txn_sum == money(row.amount):
            return 'verified', txn
        return 'unverified', txn
    return 'unverified', None


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

        txn_index = str(parsed.get('transaction_id') or '').strip()
        idempotency_key = f'paylink_{row.id}_{txn_index or "noindex"}'[:255]

        if row.status == PaymentLinkPayment.STATUS_COMPLETED:
            # A completed row is never downgraded, whatever the retry says. A
            # *different* approved index on it means a second charge went
            # through — record it and flag it, never lose it.
            if parsed.get('is_successful') and txn_index and txn_index != row.gateway_transaction_id:
                TranzilaTransaction.objects.get_or_create(
                    idempotency_key=idempotency_key,
                    defaults=_txn_defaults(parsed, request, txn_index),
                )
                row.review_reason = f'second_charge:{txn_index}'[:200]
                row.save(update_fields=['review_reason', 'updated_at'])
                logger.error('payment link %s: a second approved transaction %s was reported', row.id, txn_index)
            return Response({'success': True, 'message': 'Already processed'})

        # The same gateway index can belong to one payment only.
        if txn_index and PaymentLinkPayment.objects.filter(gateway_transaction_id=txn_index).exclude(id=row.id).exists():
            row.status = PaymentLinkPayment.STATUS_REVIEW
            row.review_reason = f'index_reused:{txn_index}'[:200]
            row.gateway_transaction_id = txn_index[:100]
            row.save(update_fields=['status', 'review_reason', 'gateway_transaction_id', 'updated_at'])
            logger.error('payment link %s: index %s already belongs to another payment', row.id, txn_index)
            return Response({'success': True, 'status': row.status})

        if not parsed.get('is_successful'):
            row.status = PaymentLinkPayment.STATUS_FAILED
            row.failure_code = str(parsed.get('response_code') or '')[:10]
            row.failure_reason = (parsed.get('error_message') or 'התשלום נדחה')[:500]
            row.gateway_transaction_id = txn_index[:100]
            row.save(update_fields=['status', 'failure_code', 'failure_reason', 'gateway_transaction_id', 'updated_at'])
            return Response({'success': False, 'status': row.status})

        txn, created = TranzilaTransaction.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults=_txn_defaults(parsed, request, txn_index),
        )
        row.tranzila_transaction = txn
        row.gateway_transaction_id = txn_index[:100]
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
            verdict, _txn_row = verify_transaction_with_tranzila(row, txn_index)
            if verdict == 'verified':
                row.status = PaymentLinkPayment.STATUS_COMPLETED
                row.review_reason = ''
            else:
                row.status = PaymentLinkPayment.STATUS_REVIEW
                row.review_reason = ('unverified_callback' if verdict == 'unverified' else 'verification_unavailable')
                logger.error('payment link %s: callback not confirmed by Tranzila (%s)', row.id, verdict)
        row.save()

    return Response({'success': True, 'status': row.status, 'new_transaction': created})


def _txn_defaults(parsed: dict, request, txn_index: str) -> dict:
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
