import logging
from datetime import datetime
from io import StringIO

from django.conf import settings
from django.core import management
from django.http import HttpResponse, JsonResponse
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.core.permissions import IsSuperUser

logger = logging.getLogger(__name__)

# Internal Django bookkeeping tables — excluded so a restore doesn't collide
# with the target environment's own auth/session/content-type rows.
DUMPDATA_EXCLUDES = [
    'contenttypes',
    'auth.permission',
    'admin.logentry',
    'sessions.session',
    'django_celery_beat',
]


class DatabaseBackupView(APIView):
    """
    Generate a full data dump of the database and return it as a downloadable file.

    Uses Django's `dumpdata` (pure Python, via the ORM) rather than the `pg_dump`
    binary — the backend runs on both Fly.io and Vercel serverless, and `pg_dump`
    is not guaranteed to be present in either runtime. This trades the standard
    pg_dump SQL/custom format for a JSON fixture that restores via `loaddata` and
    works identically everywhere the app runs.

    Superuser-only — this dump contains full PII (children, parents) and payment
    records.

    GET /api/v1/core/devops/backup/
    """
    permission_classes = [IsAuthenticated, IsSuperUser]

    def get(self, request):
        logger.info(
            "DB backup requested by user_id=%s email=%s",
            getattr(request.user, 'id', None),
            getattr(request.user, 'email', None),
        )

        buffer = StringIO()
        management.call_command(
            'dumpdata',
            exclude=DUMPDATA_EXCLUDES,
            indent=2,
            stdout=buffer,
        )

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'kogomalo_backup_{timestamp}.json'

        response = HttpResponse(buffer.getvalue(), content_type='application/json')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class EnvInfoView(APIView):
    """
    Dev-only diagnostic: which env file (.env vs .env.local) supplied config,
    and which Tranzila terminals are effectively active — so a developer running
    the app locally can tell at a glance whether a real charge could go through.

    Returns 404 whenever DEBUG is off, so this is never reachable in a deployed
    environment (Vercel/Fly.io production or preview) even if someone finds the URL.

    Reports terminal *names* only — never public/secret keys.

    GET /api/v1/core/devops/env-info/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        if not settings.DEBUG:
            return HttpResponse(status=404)

        return JsonResponse({
            'active_env_file': getattr(settings, 'ACTIVE_ENV_FILE', 'unknown'),
            'tranzila_iframe_terminal': settings.TRANZILA_TERMINAL,
            'tranzila_charge_terminal': settings.TRANZILA_PROD_TERMINAL,
        })
