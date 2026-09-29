"""
The endpoints Michal Kagan's site calls, with a key of its own.

    POST /api/v1/documents/integrations/michal/documents/
         {"kind": "payment", "external_id", "customer": {...}, "payment": {...}}
         {"kind": "refund",  "external_id", "refund": {...}}
         → 201 a new document, 200 the one already issued for that id.
    GET  /api/v1/documents/integrations/michal/documents/<number>/pdf/
         → the document's PDF, a copy once originals are signed. Only her documents.

The key is MICHAL_INTEGRATION_API_KEY, sent as X-Integration-Key or as a
Bearer token and compared in constant time. With no key set, every request is
refused. A key says who is calling, not that the money arrived: her site asks
only after its own check with Tranzila, and the document records the
transaction it was issued for.
"""
from __future__ import annotations

import hmac
import logging

from django.conf import settings
from django.http import HttpResponse
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.documents.michal.service import (
    METHOD_LABELS,
    MichalDocumentError,
    issue_payment_document,
    issue_refund_credit,
    michal_document,
)

logger = logging.getLogger(__name__)


def _key_ok(request) -> bool:
    expected = (getattr(settings, 'MICHAL_INTEGRATION_API_KEY', '') or '').strip()
    if not expected:
        return False
    provided = (request.headers.get('X-Integration-Key') or '').strip()
    if not provided:
        auth = request.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            provided = auth[7:].strip()
    return bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())


def _denied() -> Response:
    return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)


class CustomerSerializer(serializers.Serializer):
    external_id = serializers.CharField(max_length=64)
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    computerized_docs_consent = serializers.BooleanField(required=False, default=False)


class PaymentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    paid_at = serializers.DateTimeField()
    method = serializers.ChoiceField(choices=list(METHOD_LABELS), required=False, default='credit_card')
    card_last_four = serializers.RegexField(r'^\d{4}$', required=False, allow_blank=True)
    transaction_id = serializers.CharField(max_length=64, required=False, allow_blank=True)
    confirmation_code = serializers.CharField(max_length=64, required=False, allow_blank=True)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True)
    session_date = serializers.DateField(required=False, allow_null=True)


class RefundSerializer(serializers.Serializer):
    payment_external_id = serializers.CharField(max_length=64)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0.01)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class DocumentRequestSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=['payment', 'refund'])
    external_id = serializers.CharField(max_length=64)
    customer = CustomerSerializer(required=False)
    payment = PaymentSerializer(required=False)
    refund = RefundSerializer(required=False)

    def validate(self, attrs):
        if attrs['kind'] == 'payment' and not (attrs.get('customer') and attrs.get('payment')):
            raise serializers.ValidationError('תשלום צריך customer ו-payment.')
        if attrs['kind'] == 'refund' and not attrs.get('refund'):
            raise serializers.ValidationError('החזר צריך refund.')
        return attrs


def _document_payload(doc, *, duplicate: bool) -> dict:
    return {
        'duplicate': duplicate,
        'document_id': str(doc.pk),
        'document_number': doc.document_number,
        'document_type': doc.document_type,
        'document_date': str(doc.document_date),
        'total_amount': str(doc.total_amount),
        'linked_document_number': doc.linked_document_number or None,
    }


class MichalDocumentsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _key_ok(request):
            return _denied()
        serializer = DocumentRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'error': 'invalid', 'details': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data
        try:
            issued = issue_payment_document(data) if data['kind'] == 'payment' else issue_refund_credit(data)
        except MichalDocumentError as exc:
            return Response({'error': str(exc)}, status=exc.status)
        except Exception:
            logger.exception('Michal document request %s failed', data.get('external_id'))
            return Response({'error': 'internal'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(
            _document_payload(issued.document, duplicate=not issued.created),
            status=status.HTTP_201_CREATED if issued.created else status.HTTP_200_OK,
        )


class MichalDocumentPdfView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, number):
        if not _key_ok(request):
            return _denied()
        doc = michal_document(number)
        if doc is None:
            return Response({'error': 'not found'}, status=status.HTTP_404_NOT_FOUND)
        from apps.documents.document_pdf import generate_document_pdf
        from apps.documents.signing.service import office_copy

        # As the office's own print: once originals are signed and stored at
        # issue, every later print is a copy; the original is the mailed file.
        pdf_bytes = generate_document_pdf(doc, copy=office_copy())
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{doc.document_number}.pdf"'
        return response
