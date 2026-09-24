"""
The signing screens' API, the public certificate, and the sign-pending cron.

  GET  /api/v1/documents/signing/status/                      manager
  GET  /api/v1/documents/signing/originals/?delivery=&printed=&limit=&offset=   manager
  POST /api/v1/documents/signing/originals/{id}/print-original/                 manager
  GET  /api/v1/documents/signing/certificate/                 public
  GET  /api/v1/documents/cron/sign-pending/                   the courses' cron auth
"""
from __future__ import annotations

import logging
from datetime import datetime, time

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManager
from apps.documents.models import SignedOriginal
from apps.documents.signing import SigningUnavailable, consent_enforced, enabled

logger = logging.getLogger(__name__)

MAX_PAGE = 200


def _certificate_or_none():
    from apps.documents.signing.certificate import load_certificate

    try:
        return load_certificate()
    except SigningUnavailable:
        logger.error('Signing: the configured certificate cannot be read')
        return None


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def signing_status(request):
    """Whether documents are being signed, with what, and what waits for the office. No network calls."""
    from apps.documents.signing.backends import backend_name, configured_key_id
    from apps.documents.signing.certificate import fingerprint_sha256, subject_text

    certificate = _certificate_or_none()
    today_start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
    last = SignedOriginal.objects.filter(signed_at__isnull=False).order_by('-signed_at').values_list(
        'signed_at', flat=True,
    ).first()
    return Response({
        'enabled': enabled(),
        'consent_enforced': consent_enforced(),
        'backend': backend_name(),
        'key_id': configured_key_id(),
        'cert_fingerprint': fingerprint_sha256(certificate) if certificate is not None else '',
        'cert_subject': subject_text(certificate) if certificate is not None else '',
        'last_signed_at': last,
        'counts': {
            'held': SignedOriginal.objects.filter(delivery=SignedOriginal.DELIVERY_HELD).count(),
            'paper_pending': SignedOriginal.objects.filter(
                delivery=SignedOriginal.DELIVERY_PAPER, paper_original_printed_at__isnull=True,
            ).count(),
            'signed_today': SignedOriginal.objects.filter(signed_at__gte=today_start).count(),
        },
    })


def _int(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def signed_originals(request):
    """The originals, newest first — the "for hand delivery" panel is ?delivery=paper&printed=false."""
    params = request.query_params
    qs = SignedOriginal.objects.all().order_by('-created_at')
    delivery = (params.get('delivery') or '').strip()
    if delivery:
        allowed = {choice for choice, _label in SignedOriginal.DELIVERY_CHOICES}
        if delivery not in allowed:
            return Response({'error': f'delivery לא מוכר: {delivery}'}, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(delivery=delivery)
    printed = (params.get('printed') or '').strip().lower()
    if printed in ('false', '0', 'no'):
        qs = qs.filter(paper_original_printed_at__isnull=True)
    elif printed in ('true', '1', 'yes'):
        qs = qs.filter(paper_original_printed_at__isnull=False)

    limit = _int(params.get('limit'), 50, 1, MAX_PAGE)
    offset = _int(params.get('offset'), 0, 0, 10 ** 9)
    rows = qs.only(
        'id', 'number', 'kind', 'document_type_label', 'customer_name', 'document_date', 'total',
        'delivery', 'delivery_reason', 'signed_at', 'sent_at', 'paper_original_printed_at',
    )[offset:offset + limit]
    return Response({
        'count': qs.count(),
        'results': [
            {
                'id': str(row.id),
                'number': row.number,
                'kind': row.kind,
                'document_type_label': row.document_type_label,
                'customer_name': row.customer_name,
                'document_date': row.document_date.isoformat() if row.document_date else None,
                'total': str(row.total) if row.total is not None else None,
                'delivery': row.delivery,
                'delivery_reason': row.delivery_reason,
                'signed_at': row.signed_at,
                'sent_at': row.sent_at,
                'paper_original_printed_at': row.paper_original_printed_at,
            }
            for row in rows
        ],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsManager])
def print_original(request, original_id):
    """The stored signed original, once, to hand over on paper. After that: 409, every print is a copy."""
    from apps.documents.signing.service import PrintRefused, print_original as hand_out

    try:
        row = hand_out(original_id, request.user)
    except SignedOriginal.DoesNotExist:
        return Response({'error': 'המקור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
    except PrintRefused as refused:
        return Response({'error': str(refused)}, status=refused.status)
    logger.info('Signing: the original %s was printed for hand delivery by user %s', row.number, request.user.pk)
    response = HttpResponse(bytes(row.pdf), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{row.number}.pdf"'
    return response


def _setup_allowed(request) -> bool:
    """A manager, or the setup token from SIGNING_ADMIN_TOKEN (compared in constant time)."""
    import hmac
    from django.conf import settings

    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and IsManager().has_permission(request, None):
        return True
    expected = (getattr(settings, 'SIGNING_ADMIN_TOKEN', '') or '').strip()
    given = (request.headers.get('X-Signing-Admin-Token') or '').strip()
    return bool(expected) and bool(given) and hmac.compare_digest(expected, given)


@api_view(['POST'])
@permission_classes([AllowAny])
def signing_selftest(request):
    """
    Sign a sample through the deployment's own key and validate it.

    The one check that proves the Vercel → Google handshake and the key work
    where they will actually run. Returns the signed sample so it can be opened
    in Adobe Acrobat. No database writes, no mail.
    """
    import base64
    from apps.documents.signing.certificate import fingerprint_sha256, subject_text
    from apps.documents.signing.selftest import run_selftest

    if not _setup_allowed(request):
        return Response({'error': 'אין הרשאה'}, status=status.HTTP_403_FORBIDDEN)
    try:
        result = run_selftest()
    except SigningUnavailable as exc:
        logger.warning('Signing self-test failed: %s', exc)
        return Response({'ok': False, 'error': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({
        'ok': True,
        'backend': result.backend.name,
        'key_id': result.backend.key_id,
        'cert_subject': subject_text(result.certificate),
        'cert_fingerprint': fingerprint_sha256(result.certificate),
        'pdf_base64': base64.b64encode(result.pdf).decode('ascii'),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def signing_issue_certificate(request):
    """
    Build the self-issued certificate for the deployment's key and return it.

    Nothing is stored: the PEM is public, and it goes into the code (or
    SIGNING_CERT_PEM) by hand, so a certificate never changes silently.
    """
    from apps.documents.signing.backends import get_backend
    from apps.documents.signing.certificate import (
        build_self_issued_certificate, certificate_pem, fingerprint_sha256, subject_text,
    )

    if not _setup_allowed(request):
        return Response({'error': 'אין הרשאה'}, status=status.HTTP_403_FORBIDDEN)
    try:
        backend = get_backend()
        certificate = build_self_issued_certificate(backend)
    except SigningUnavailable as exc:
        logger.warning('Signing: the certificate could not be built: %s', exc)
        return Response({'ok': False, 'error': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({
        'ok': True,
        'key_id': backend.key_id,
        'pem': certificate_pem(certificate),
        'subject': subject_text(certificate),
        'fingerprint_sha256': fingerprint_sha256(certificate),
    })


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def signing_certificate(request):
    """The public certificate and its fingerprint — what anyone checks a signed document against."""
    from apps.documents.signing.certificate import describe

    return Response(describe(_certificate_or_none()))


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_sign_pending(request):
    """
    Sign the originals still unsigned and mail the ones that became mailable.

    The courses' cron auth: X-Cron-Token, ?token= or a Bearer matching
    CRON_TOKEN / CRON_SECRET. GET as well as POST: Vercel Cron calls with GET.
    While DOCUMENT_SIGNING_ENABLED is off it answers {"summary": {"disabled": true}}.
    """
    from apps.customers.views import _cron_request_authorized
    from apps.documents.signing.service import sign_pending

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    limit = _int(request.query_params.get('limit'), 25, 1, MAX_PAGE)
    summary = sign_pending(limit=limit)
    return Response({'ok': True, 'summary': summary})
