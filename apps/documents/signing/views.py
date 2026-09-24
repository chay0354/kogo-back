"""
The signing screens' API, the public certificate, and the sign-pending cron.

  GET  /api/v1/documents/signing/status/                      manager
  GET  /api/v1/documents/signing/originals/?purpose=&kind=&q=&date_from=&date_to=&delivery=&printed=&limit=&offset=
                                                              manager
  GET  /api/v1/documents/signing/originals/{id}/file/         manager — the stored bytes, logged
  GET  /api/v1/documents/signing/originals/export/?<the same filters>&offset=&limit=
                                                              manager — a zip of stored files, logged
  POST /api/v1/documents/signing/originals/{id}/print-original/                 manager
  GET  /api/v1/documents/signing/archive/status/              manager
  POST /api/v1/documents/signing/archive/run/                 manager
  GET  /api/v1/documents/signing/certificate/                 public
  GET  /api/v1/documents/cron/sign-pending/                   the courses' cron auth
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import datetime, time

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManager
from apps.documents.models import SignedFileAccess, SignedOriginal
from apps.documents.signing import SigningUnavailable, archive_enabled, consent_enforced, enabled

logger = logging.getLogger(__name__)

MAX_PAGE = 200
# A Vercel function's response is capped at about 4.5 MB and a signed PDF is
# about 80 KB: forty files and their manifest stay well under it.
MAX_EXPORT = 40
ARCHIVE = SignedOriginal.PURPOSE_ARCHIVE


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
    # The originals only: an archive batch signs hundreds of copies in a day,
    # and this screen is about what was issued (archive/status/ is about those).
    originals = SignedOriginal.objects.exclude(purpose=ARCHIVE)
    last = originals.filter(signed_at__isnull=False).order_by('-signed_at').values_list(
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
            'held': originals.filter(delivery=SignedOriginal.DELIVERY_HELD).count(),
            'paper_pending': originals.filter(
                delivery=SignedOriginal.DELIVERY_PAPER, paper_original_printed_at__isnull=True,
            ).count(),
            'signed_today': originals.filter(signed_at__gte=today_start).count(),
        },
    })


def _int(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


class _BadFilter(Exception):
    pass


def _date_param(params, name: str):
    raw = str(params.get(name) or '').strip()
    if not raw:
        return None
    try:
        day = parse_date(raw)
    except ValueError:
        day = None
    if day is None:
        raise _BadFilter(f'{name} חייב להיות תאריך בפורמט YYYY-MM-DD')
    return day


def _filtered_originals(params):
    """
    The stored rows the filters ask for — shared by the list and the export.

    purpose: original | archive (an original is every row that is not an
    archive copy, including the NULL a previous deployment wrote);
    kind: ir | store | formal; q: part of the number or of the customer's name;
    date_from / date_to: the document's date, inclusive; delivery and printed
    as before. An unknown value is a 400 rather than an unfiltered answer.
    """
    qs = SignedOriginal.objects.all()
    purpose = (params.get('purpose') or '').strip()
    if purpose == SignedOriginal.PURPOSE_ARCHIVE:
        qs = qs.filter(purpose=ARCHIVE)
    elif purpose == SignedOriginal.PURPOSE_ORIGINAL:
        qs = qs.exclude(purpose=ARCHIVE)
    elif purpose:
        raise _BadFilter(f'purpose לא מוכר: {purpose}')
    kind = (params.get('kind') or '').strip()
    if kind:
        if kind not in {choice for choice, _label in SignedOriginal.KIND_CHOICES}:
            raise _BadFilter(f'kind לא מוכר: {kind}')
        qs = qs.filter(kind=kind)
    delivery = (params.get('delivery') or '').strip()
    if delivery:
        allowed = {choice for choice, _label in SignedOriginal.DELIVERY_CHOICES}
        if delivery not in allowed:
            raise _BadFilter(f'delivery לא מוכר: {delivery}')
        qs = qs.filter(delivery=delivery)
    printed = (params.get('printed') or '').strip().lower()
    if printed in ('false', '0', 'no'):
        qs = qs.filter(paper_original_printed_at__isnull=True)
    elif printed in ('true', '1', 'yes'):
        qs = qs.filter(paper_original_printed_at__isnull=False)
    query = (params.get('q') or '').strip()
    if query:
        qs = qs.filter(Q(number__icontains=query) | Q(customer_name__icontains=query))
    date_from = _date_param(params, 'date_from')
    if date_from:
        qs = qs.filter(document_date__gte=date_from)
    date_to = _date_param(params, 'date_to')
    if date_to:
        qs = qs.filter(document_date__lte=date_to)
    return qs


LIST_FIELDS = (
    'id', 'number', 'kind', 'purpose', 'document_type_label', 'customer_name', 'document_date', 'total',
    'delivery', 'delivery_reason', 'signed_at', 'sent_at', 'paper_original_printed_at', 'sha256', 'size',
)


def _purpose(row: SignedOriginal) -> str:
    return ARCHIVE if row.is_archive_copy else SignedOriginal.PURPOSE_ORIGINAL


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def signed_originals(request):
    """
    The stored rows, newest first — the "for hand delivery" panel is ?delivery=paper&printed=false.

    Originals and archive copies alike unless `purpose` narrows it; an archive
    copy is always delivery 'none', so it is never on the paper or held lists.
    """
    params = request.query_params
    try:
        qs = _filtered_originals(params).order_by('-created_at', '-id')
    except _BadFilter as bad:
        return Response({'error': str(bad)}, status=status.HTTP_400_BAD_REQUEST)

    limit = _int(params.get('limit'), 50, 1, MAX_PAGE)
    offset = _int(params.get('offset'), 0, 0, 10 ** 9)
    rows = qs.only(*LIST_FIELDS)[offset:offset + limit]
    return Response({
        'count': qs.count(),
        'results': [
            {
                'id': str(row.id),
                'number': row.number,
                'kind': row.kind,
                'purpose': _purpose(row),
                'document_type_label': row.document_type_label,
                'customer_name': row.customer_name,
                'document_date': row.document_date.isoformat() if row.document_date else None,
                'total': str(row.total) if row.total is not None else None,
                'delivery': row.delivery,
                'delivery_reason': row.delivery_reason,
                'signed_at': row.signed_at,
                'sent_at': row.sent_at,
                'paper_original_printed_at': row.paper_original_printed_at,
                'sha256': row.sha256,
                'size': row.size,
            }
            for row in rows
        ],
    })


# ASCII only: the name goes into a header, and every number kogo issues is ASCII anyway.
_UNSAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')


def _file_name(number: str) -> str:
    """'<number>.pdf', with anything a file system or a header would choke on replaced."""
    return f'{_UNSAFE_NAME.sub("_", number or "").strip("._") or "document"}.pdf'


def _log_access(request, row: SignedOriginal, action: str) -> None:
    from apps.signatures.capture import client_ip

    SignedFileAccess.objects.create(
        original=row,
        user=request.user if getattr(request.user, 'is_authenticated', False) else None,
        action=action,
        ip=client_ip(request),
    )


FILE_NOT_FOUND = 'הקובץ החתום לא נמצא'
FILE_BROKEN = 'הקובץ השמור אינו תואם לטביעת האצבע שלו ולכן לא הורד. יש לפנות לתמיכה'


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def original_file(request, original_id):
    """
    The stored signed file, exactly as it was signed — an original or an archive copy.

    Not "הדפס מקור": nothing about the original's one print or its delivery
    changes. The download is logged (SignedFileAccess) before the bytes go.
    X-Content-SHA256 is the fingerprint taken when it was signed.
    """
    row = SignedOriginal.objects.filter(pk=original_id).first()
    if row is None or not row.is_signed or row.pdf is None:
        return Response({'error': FILE_NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)
    if not row.pdf_intact():
        logger.error('Signing: %s — the stored file does not match its SHA-256; not handed out', row.number)
        return Response({'error': FILE_BROKEN}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    _log_access(request, row, SignedFileAccess.ACTION_DOWNLOAD)
    logger.info('Signing: %s downloaded by user %s', row.number, request.user.pk)
    response = HttpResponse(bytes(row.pdf), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{_file_name(row.number)}"'
    response['X-Content-SHA256'] = row.sha256
    return response


MANIFEST_COLUMNS = (
    'number', 'purpose', 'kind', 'document_type_label', 'customer_name', 'document_date', 'total',
    'sha256', 'signed_at',
)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def originals_export(request):
    """
    A zip of stored signed files, one page at a time: the PDFs by number and manifest.csv.

    The list's filters, then offset and limit (at most MAX_EXPORT). Only rows
    that are signed; oldest first, so a batch signing new copies meanwhile adds
    to the end and never shifts a page already fetched. X-Export-Total is how
    many files the filters match, X-Export-Next-Offset where the next page
    starts ('' on the last). Every file is logged (SignedFileAccess) before the
    zip goes. A file that no longer matches its SHA-256 is left out — and
    logged as an error — rather than handed out.
    """
    from apps.documents.register import _text

    params = request.query_params
    try:
        qs = _filtered_originals(params).filter(signed_at__isnull=False).order_by('created_at', 'id')
    except _BadFilter as bad:
        return Response({'error': str(bad)}, status=status.HTTP_400_BAD_REQUEST)
    limit = _int(params.get('limit'), MAX_EXPORT, 1, MAX_EXPORT)
    offset = _int(params.get('offset'), 0, 0, 10 ** 9)
    total = qs.count()
    rows = list(qs[offset:offset + limit])

    manifest = io.StringIO()
    writer = csv.writer(manifest, lineterminator='\r\n')
    writer.writerow(MANIFEST_COLUMNS)
    buffer = io.BytesIO()
    included = []
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            if not row.pdf_intact():
                logger.error('Signing: %s — the stored file does not match its SHA-256; left out of the export',
                             row.number)
                continue
            archive.writestr(_file_name(row.number), bytes(row.pdf))
            writer.writerow([
                _text(row.number), _purpose(row), row.kind, _text(row.document_type_label),
                _text(row.customer_name), row.document_date.isoformat() if row.document_date else '',
                f'{row.total:.2f}' if row.total is not None else '', row.sha256,
                timezone.localtime(row.signed_at).isoformat() if row.signed_at else '',
            ])
            included.append(row)
        # UTF-8 with a byte-order mark: how Excel knows the Hebrew is UTF-8 (register.register_csv).
        archive.writestr('manifest.csv', manifest.getvalue().encode('utf-8-sig'))

    for row in included:
        _log_access(request, row, SignedFileAccess.ACTION_EXPORT)
    logger.info('Signing: %s stored files exported by user %s (offset %s of %s)',
                len(included), request.user.pk, offset, total)
    next_offset = offset + len(rows)
    first, last = offset + 1, offset + len(rows)
    response = HttpResponse(buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="signed-documents-{first}-{last}.zip"'
    response['X-Export-Total'] = str(total)
    response['X-Export-Next-Offset'] = str(next_offset) if rows and next_offset < total else ''
    return response


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
    try:
        # The one print is already recorded on the row; its log line must never cost it.
        _log_access(request, row, SignedFileAccess.ACTION_DOWNLOAD)
    except Exception:
        logger.exception('Signing: the print of %s was not logged (non-fatal)', row.number)
    response = HttpResponse(bytes(row.pdf), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{row.number}.pdf"'
    return response


ARCHIVE_DISABLED = 'העתקי הארכיון כבויים — יש להפעיל את SIGNING_ARCHIVE_ENABLED'
ARCHIVE_UNAVAILABLE = 'שירות החתימה לא זמין כרגע — לא נחתמו מסמכים נוספים; נסו שוב מאוחר יותר'


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManager])
def archive_status(request):
    """Where the signed archive of documents issued before signing stands, per kind. No network calls."""
    from apps.documents.signing.archive import archive_status as status_of_archive

    return Response({'enabled': archive_enabled(), **status_of_archive()})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsManager])
def archive_run(request):
    """
    Sign the next batch of documents into the archive.

    Body: {limit?: 1..50 (default 25), since?: 'YYYY-MM-DD'}. 409 while
    SIGNING_ARCHIVE_ENABLED is off; 503 — with what the batch did before it
    stopped — when the key or the certificate is out of reach.
    """
    from apps.documents.signing.archive import (
        DEFAULT_BATCH, MAX_BATCH, ArchiveDisabled, run_archive_batch,
    )

    if not archive_enabled():
        return Response({'error': ARCHIVE_DISABLED}, status=status.HTTP_409_CONFLICT)
    data = request.data if isinstance(request.data, dict) else {}
    limit = _int(data.get('limit'), DEFAULT_BATCH, 1, MAX_BATCH)
    try:
        since = _date_param(data, 'since')
    except _BadFilter as bad:
        return Response({'error': str(bad)}, status=status.HTTP_400_BAD_REQUEST)
    try:
        result = run_archive_batch(limit=limit, since=since)
    except ArchiveDisabled:
        return Response({'error': ARCHIVE_DISABLED}, status=status.HTTP_409_CONFLICT)
    logger.info('Signing archive: batch run by user %s', request.user.pk)
    if result['unavailable']:
        return Response({'error': ARCHIVE_UNAVAILABLE, **result}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(result)


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
