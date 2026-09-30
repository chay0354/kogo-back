"""
The quarterly backup as a call a scheduler can make (apps/documents/quarterly_backup.py).

    GET|POST /api/v1/documents/cron/quarterly-backup/[?quarter=YYYY-Qn]

Guarded like every other cron: X-Cron-Token, ?token= or a Bearer matching
CRON_TOKEN / CRON_SECRET. Deliberately not in vercel.json — when it runs is the
owner's decision; until then `manage.py quarterly_backup` does the same from a
machine. Only to a bucket: a serverless function has no directory worth
keeping, so with SIGNING_QUARTERLY_BACKUP_BUCKET unset it answers 409.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_quarterly_backup(request):
    from apps.customers.views import _cron_request_authorized
    from apps.documents.quarterly_backup import BackupInputError, quarterly_bucket, run_quarterly_backup

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    if not quarterly_bucket():
        return Response({'error': 'SIGNING_QUARTERLY_BACKUP_BUCKET is not set'}, status=status.HTTP_409_CONFLICT)
    try:
        result = run_quarterly_backup((request.query_params.get('quarter') or '').strip() or None)
    except BackupInputError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK if result['ok'] else status.HTTP_502_BAD_GATEWAY)
