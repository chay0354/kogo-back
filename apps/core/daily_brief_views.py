"""The morning brief: read the last one, build a fresh one, or let the cron build it."""
import logging
import os

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.daily_brief import build_daily_brief, check_catalogue, run_check, summarise
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


def merge_into_today(item: dict) -> DailyBriefSnapshot:
    """
    Fold one check's answer into today's brief.

    The screen asks for the checks one at a time — a single request per check is
    what survives the platform's limit on request length — so each answer is
    stored as it arrives. A run that is interrupted leaves the answers it did
    get, instead of leaving the office with nothing.
    """
    from django.utils import timezone as dj_timezone

    today = dj_timezone.localtime(dj_timezone.now()).date()
    snapshot = DailyBriefSnapshot.objects.order_by('-created_at').first()
    same_day = snapshot is not None and (snapshot.payload or {}).get('for_date') == today.isoformat()
    items = list((snapshot.payload or {}).get('items') or []) if same_day else []
    items = [existing for existing in items if existing.get('key') != item['key']]
    items.append(item)

    order = {entry['key']: index for index, entry in enumerate(check_catalogue())}
    items.sort(key=lambda entry: order.get(entry.get('key'), 999))
    brief = summarise(items, day=today)

    if same_day and snapshot is not None:
        snapshot.payload = brief
        snapshot.red_count = brief['red_count']
        snapshot.yellow_count = brief['yellow_count']
        snapshot.save(update_fields=['payload', 'red_count', 'yellow_count'])
        return snapshot
    return store_brief(brief)


class DailyBriefCheckView(APIView):
    """Run one check and keep its answer. The screen walks the list itself."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        return Response({'checks': check_catalogue()})

    def post(self, request):
        key = str(request.data.get('key') or '').strip()
        try:
            item = run_check(key)
        except KeyError:
            return Response({'error': f'אין בדיקה בשם {key}'}, status=status.HTTP_400_BAD_REQUEST)
        snapshot = merge_into_today(item)
        return Response({'item': item, 'brief': snapshot.payload, 'stored_at': snapshot.created_at})


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
