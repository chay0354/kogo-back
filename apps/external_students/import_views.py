"""
The upload → read → review → apply surface.

Managers only, all the way through — not ``ManagerWriteMixin``. A partner can
read a roster, but bringing a file in and deciding what it means to the register
is the office's job, and the owner said so.
"""
from __future__ import annotations

import logging

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes as permission_classes_for
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.models import Branch
from apps.core.permissions import IsManager
from apps.core.scoping import scope_branches
from apps.external_students import roster_import as service
from apps.external_students.import_serializers import (
    ExternalRosterImportDetailSerializer,
    ExternalRosterImportListSerializer,
)
from apps.external_students.models import (
    ExternalRosterImport,
    ExternalRosterImportRow,
    ExternalRosterImportUnit,
)

logger = logging.getLogger(__name__)

# Comfortably above both real files (1.5MB and 18KB) and far below anything that
# would strain a request.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
EXTENSIONS = {
    '.xlsx': ExternalRosterImport.KIND_XLSX,
    '.xlsm': ExternalRosterImport.KIND_XLSX,
    '.pdf': ExternalRosterImport.KIND_PDF,
}


class ExternalRosterImportViewSet(viewsets.ModelViewSet):
    """/api/v1/external-students/imports/ — one municipality sheet at a time."""

    permission_classes = [IsAuthenticated, IsManager]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = None
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = (
            ExternalRosterImport.objects
            .select_related('branch')
            .prefetch_related('units__rows', 'units__matched_lessons')
        )
        qs = scope_branches(qs, self.request.user, 'branch')
        branch_id = self.request.query_params.get('branch')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return qs.order_by('-created_at')

    def get_serializer_class(self):
        if self.action == 'list':
            return ExternalRosterImportListSerializer
        return ExternalRosterImportDetailSerializer

    def create(self, request, *args, **kwargs):
        """
        Take the file, work out what groups it holds, and hand back the count.

        No participant is read here. For a spreadsheet that means one small
        model call describing the layout; for a scan it means splitting the
        pages. Either way the request stays short.
        """
        upload = request.FILES.get('file')
        branch_id = request.data.get('branch')
        if upload is None:
            return Response({'error': 'לא צורף קובץ'}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_UPLOAD_BYTES:
            return Response(
                {'error': 'הקובץ גדול מדי. ניתן להעלות עד 8MB'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = (upload.name or '').lower()
        kind = next((k for ext, k in EXTENSIONS.items() if name.endswith(ext)), None)
        if kind is None:
            return Response(
                {'error': 'ניתן להעלות קובץ אקסל או PDF בלבד'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch = scope_branches(Branch.objects.all(), request.user).filter(pk=branch_id).first()
        if branch is None:
            return Response({'error': 'סניף לא נמצא'}, status=status.HTTP_400_BAD_REQUEST)
        if not branch.is_external:
            return Response(
                {'error': 'ניתן לייבא רשימות רק בסניף חיצוני'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        roster_import = ExternalRosterImport.objects.create(
            branch=branch,
            kind=kind,
            original_filename=(upload.name or '')[:255],
            byte_size=upload.size,
            created_by=request.user,
        )
        file_bytes = upload.read()
        try:
            if kind == ExternalRosterImport.KIND_XLSX:
                service.segment_spreadsheet(roster_import, file_bytes)
            else:
                service.segment_scan(roster_import, file_bytes)
        except service.ImportError_ as exc:
            roster_import.status = ExternalRosterImport.STATUS_FAILED
            roster_import.error = str(exc)
            roster_import.save(update_fields=['status', 'error', 'updated_at'])
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 — the manager needs the reason
            logger.exception('Roster upload failed for import %s', roster_import.id)
            roster_import.status = ExternalRosterImport.STATUS_FAILED
            roster_import.error = str(exc)[:500]
            roster_import.save(update_fields=['status', 'error', 'updated_at'])
            return Response(
                {'error': 'לא הצלחנו לקרוא את מבנה הקובץ'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        roster_import.refresh_from_db()
        return Response(
            ExternalRosterImportDetailSerializer(roster_import).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='parse-next')
    def parse_next(self, request, pk=None):
        """Read exactly one group, then answer. The client calls until done."""
        roster_import = self.get_object()
        if not roster_import.is_open:
            return Response(
                {'error': 'הייבוא כבר נסגר'}, status=status.HTTP_400_BAD_REQUEST,
            )

        unit = service.claim_next_unit(roster_import)
        if unit is None:
            roster_import.refresh_from_db()
            return Response(ExternalRosterImportDetailSerializer(roster_import).data)

        service.parse_unit(unit)
        roster_import.refresh_from_db()
        return Response(ExternalRosterImportDetailSerializer(roster_import).data)

    @action(detail=True, methods=['get'], url_path='review')
    def review(self, request, pk=None):
        """Everything the manager needs to decide, including what would be lost."""
        roster_import = self.get_object()
        data = ExternalRosterImportDetailSerializer(roster_import).data
        data['diff_digest'] = service.diff_digest(roster_import)
        data['blocking_units'] = [str(u.id) for u in service.blocking_units(roster_import)]
        data['bulk_removal_lessons'] = service.bulk_removal_lessons(roster_import)
        return Response(data)

    @action(detail=True, methods=['patch'], url_path='units/(?P<unit_id>[0-9a-f-]+)')
    def update_unit(self, request, pk=None, unit_id=None):
        """Answer the question an ambiguous group asked, or skip it entirely."""
        roster_import = self.get_object()
        unit = roster_import.units.filter(pk=unit_id).first()
        if unit is None:
            return Response({'error': 'קבוצה לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)

        if request.data.get('status') == ExternalRosterImportUnit.STATUS_SKIPPED:
            unit.status = ExternalRosterImportUnit.STATUS_SKIPPED
            unit.save(update_fields=['status', 'updated_at'])
            service._refresh_progress(roster_import)
            roster_import.refresh_from_db()
            return Response(ExternalRosterImportDetailSerializer(roster_import).data)

        lesson_ids = request.data.get('lesson_ids')
        if lesson_ids is not None:
            from apps.courses.models import Lesson

            lessons = list(
                Lesson.objects.filter(
                    id__in=lesson_ids, course__branch=roster_import.branch,
                )
            )
            if len(lessons) != len(set(lesson_ids)):
                return Response(
                    {'error': 'אחד השיעורים אינו שייך לסניף הזה'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            with transaction.atomic():
                unit.matched_lessons.set(lessons)
                unit.match_state = ExternalRosterImportUnit.MATCH_CONFIRMED
                unit.save(update_fields=['match_state', 'updated_at'])
                # The plan depends on which lessons this group means, so it is
                # rebuilt rather than patched.
                people = [
                    {'first_name': r.first_name, 'last_name': r.last_name, 'phone': r.phone}
                    for r in unit.rows.exclude(action=ExternalRosterImportRow.ACTION_REMOVE)
                ]
                unit.rows.all().delete()
                service._write_rows(unit, people, lessons)

        roster_import.refresh_from_db()
        return Response(ExternalRosterImportDetailSerializer(roster_import).data)

    @action(detail=True, methods=['patch'], url_path='rows/(?P<row_id>[0-9a-f-]+)')
    def update_row(self, request, pk=None, row_id=None):
        """Fix a name the file got wrong, or change what happens to one child."""
        roster_import = self.get_object()
        row = ExternalRosterImportRow.objects.filter(
            pk=row_id, unit__roster_import=roster_import,
        ).first()
        if row is None:
            return Response({'error': 'שורה לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)

        for field in ('first_name', 'last_name', 'phone'):
            if field in request.data:
                setattr(row, field, str(request.data[field])[:60])
        if request.data.get('action') in dict(ExternalRosterImportRow.ACTION_CHOICES):
            row.action = request.data['action']
        row.edited = True
        row.save()
        return Response(ExternalRosterImportDetailSerializer(roster_import).data)

    @action(detail=True, methods=['post'], url_path='apply')
    def apply(self, request, pk=None):
        """Write the plan, once the manager confirms the picture they were shown."""
        roster_import = self.get_object()
        if roster_import.status == ExternalRosterImport.STATUS_APPLIED:
            return Response({'error': 'הייבוא כבר הוחל'}, status=status.HTTP_400_BAD_REQUEST)

        expected = request.data.get('expected_digest')
        current = service.diff_digest(roster_import)
        if expected != current:
            data = ExternalRosterImportDetailSerializer(roster_import).data
            data['diff_digest'] = current
            data['error'] = 'הרשימה השתנתה מאז הבדיקה. יש לעבור עליה שוב.'
            return Response(data, status=status.HTTP_409_CONFLICT)

        try:
            result = service.apply_import(
                roster_import,
                user=request.user,
                confirmed_bulk_lessons=request.data.get('confirmed_bulk_lessons') or [],
            )
        except service.ImportError_ as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        roster_import.refresh_from_db()
        payload = ExternalRosterImportDetailSerializer(roster_import).data
        payload['result'] = result
        return Response(payload)

    def destroy(self, request, *args, **kwargs):
        """Throw the import away and release everything held from the file."""
        roster_import = self.get_object()
        service.release_files(roster_import)
        roster_import.status = ExternalRosterImport.STATUS_DISCARDED
        roster_import.save(update_fields=['status', 'updated_at'])
        return Response({'discarded': True}, status=status.HTTP_200_OK)


@api_view(['POST', 'GET'])
@permission_classes_for([AllowAny])
def cron_roster_imports(request):
    """
    The sweeper: advance whatever nobody is watching.

    Same door the billing cron uses. It exists so that closing the tab halfway
    through a file is not a decision — the import finishes on its own and is
    waiting in review when the manager comes back.
    """
    from apps.customers.views import _cron_request_authorized

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    summary = service.sweep()
    return Response({'ok': True, 'summary': summary})
