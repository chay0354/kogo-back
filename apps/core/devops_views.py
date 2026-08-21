import logging
from datetime import datetime
from io import StringIO

from django.core import management
from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
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
