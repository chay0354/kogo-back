"""The morning brief: read the last one, build a fresh one, or let the cron build it."""
import logging
import os

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.daily_brief import build_daily_brief
from apps.core.models import DailyBriefSnapshot
from apps.core.permissions import IsManager

logger = logging.getLogger(__name__)

# The screen keeps the last few so a morning can be compared with the one before.
KEEP_SNAPSHOTS = 14


def store_brief(brief: dict) -> DailyBriefSnapshot:
    snapshot = DailyBriefSnapshot.objects.create(
        payload=brief,
        red_count=brief.get('red_count', 0),
        yellow_count=brief.get('yellow_count', 0),
        duration_ms=brief.get('duration_ms', 0),
    )
    keep = list(
        DailyBriefSnapshot.objects.order_by('-created_at').values_list('id', flat=True)[:KEEP_SNAPSHOTS]
    )
    DailyBriefSnapshot.objects.exclude(id__in=keep).delete()
    return snapshot


class DailyBriefView(APIView):
    """GET the stored brief; POST builds a fresh one."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        snapshot = DailyBriefSnapshot.objects.order_by('-created_at').first()
        if snapshot is None:
            return Response({'brief': None, 'stored_at': None})
        return Response({'brief': snapshot.payload, 'stored_at': snapshot.created_at})

    def post(self, request):
        include_external = str(request.data.get('include_external', '1')).lower() not in ('0', 'false', 'no')
        brief = build_daily_brief(include_external=include_external)
        snapshot = store_brief(brief)
        return Response({'brief': brief, 'stored_at': snapshot.created_at})


def _cron_authorized(request) -> bool:
    allowed = {
        value
        for value in (
            (getattr(settings, 'CRON_TOKEN', '') or '').strip(),
            (os.environ.get('CRON_SECRET') or '').strip(),
        )
        if value
    }
    auth = (request.headers.get('Authorization') or '').strip()
    bearer = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    provided = (request.headers.get('X-Cron-Token') or request.query_params.get('token') or '').strip()
    return bool(allowed) and (provided in allowed or bearer in allowed)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_daily_brief(request):
    """Build the brief for the morning. Same auth as every other cron here."""
    if not _cron_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    brief = build_daily_brief()
    store_brief(brief)
    return Response({
        'ok': True,
        'red_count': brief['red_count'],
        'yellow_count': brief['yellow_count'],
        'duration_ms': brief['duration_ms'],
    })
