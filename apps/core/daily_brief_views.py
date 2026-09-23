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
        if key not in {entry['key'] for entry in check_catalogue()}:
            return Response({'error': f'אין בדיקה בשם {key}'}, status=status.HTTP_400_BAD_REQUEST)
        merge_into_today(started_marker(key))
        item = run_check(key)
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


# One cron call has to fit inside the platform's limit on a single request. The
# old cron built the whole brief in one go and saved it at the end, so when the
# request was cut nothing at all was kept — the morning's brief simply did not
# exist. Each call now does a slice and saves as it goes; the schedule calls it
# every few minutes around 9:00 until the day is done.
CRON_BRIEF_SECONDS = 12
CRON_AUDIT_SECONDS = 12


def started_marker(key: str) -> dict:
    """
    What a check leaves behind until it answers.

    Written before the check runs and replaced by its result. A check that
    outlives the request — cut by the platform mid-way — leaves this behind as
    its finding, and the next call moves on to the checks after it instead of
    starting the same doomed check again every few minutes.
    """
    from apps.core.daily_brief import YELLOW

    title = next((entry['title'] for entry in check_catalogue() if entry['key'] == key), key)
    return {
        'key': key,
        'title': title,
        'severity': YELLOW,
        'count': 1,
        'summary': 'הבדיקה התחילה ולא הספיקה להסתיים — כנראה ארוכה מדי לבקשה אחת.',
        'action': 'אפשר להריץ שוב מ"בדוק עכשיו". אם זה חוזר, לדווח למפתח.',
        'rows': [],
        'duration_ms': 0,
    }


def run_pending_checks(*, budget_seconds: float) -> int:
    """Run the checks today's brief does not have yet. Returns how many ran."""
    import time as _time

    from django.utils import timezone as dj_timezone

    today = dj_timezone.localtime(dj_timezone.now()).date().isoformat()
    snapshot = DailyBriefSnapshot.objects.order_by('-created_at').first()
    done = set()
    if snapshot is not None and (snapshot.payload or {}).get('for_date') == today:
        done = {item.get('key') for item in (snapshot.payload or {}).get('items') or []}

    started = _time.monotonic()
    ran = 0
    for entry in check_catalogue():
        # The audit's line is refreshed at the end of every call instead.
        if entry['key'] in done or entry['key'] == 'weekly_audit':
            continue
        if _time.monotonic() - started > budget_seconds:
            break
        merge_into_today(started_marker(entry['key']))
        merge_into_today(run_check(entry['key']))
        ran += 1
    return ran


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_daily_brief(request):
    """
    One slice of the morning: pending brief checks, then today's audit, then
    the audit's line in the brief. Called repeatedly; a finished day is cheap.
    """
    if not _cron_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    from apps.core.system_audit import advance_audit

    ran = run_pending_checks(budget_seconds=CRON_BRIEF_SECONDS)
    run = advance_audit(budget_seconds=CRON_AUDIT_SECONDS)
    merge_into_today(run_check('weekly_audit'))
    return Response({
        'ok': True,
        'brief_checks_run': ran,
        'audit_area': run.area,
        'audit_progress': f'{run.next_index}/{run.total_routes}',
        'audit_finished': bool(run.finished_at),
    })


class SystemAuditView(APIView):
    """GET the week; POST moves today's audit forward by one slice."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        from datetime import timedelta

        from django.utils import timezone as dj_timezone

        from apps.core.models import SystemAuditRun
        from apps.core.system_audit import AREAS, area_for_day, run_summary

        today = dj_timezone.localtime(dj_timezone.now()).date()
        runs = {run.day: run for run in SystemAuditRun.objects.filter(day__gt=today - timedelta(days=7))}
        week = []
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            area = area_for_day(day)
            run = runs.get(day)
            week.append(run_summary(run) if run else {
                'day': day.isoformat(), 'area': area.key, 'title': area.title, 'verdict': 'none',
            })
        return Response({
            'today': area_for_day(today).key,
            'week': week,
            'areas': [{'key': a.key, 'title': a.title, 'day': a.day} for a in AREAS],
        })

    def post(self, request):
        from apps.core.system_audit import advance_audit, run_summary

        run = advance_audit(budget_seconds=20)
        merge_into_today(run_check('weekly_audit'))
        return Response(run_summary(run))
