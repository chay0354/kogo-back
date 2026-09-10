"""
CRM side: managers keep the municipality lists, partners read their own branches.

Instructors never reach these endpoints. They see external students only as rows
inside the lesson payload they can already open, and they write only through
``mark_attendance``, which lands in ``ExternalStudentAttendance`` and nowhere else.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.core.permissions import ManagerWriteMixin
from apps.core.scoping import scope_branches
from apps.external_students.broadcast import BROADCAST_MAX_STUDENTS, broadcast_to_external_students
from apps.external_students.models import ExternalStudent
from apps.external_students.serializers import (
    ExternalBroadcastSerializer,
    ExternalStudentBulkSerializer,
    ExternalStudentSerializer,
)


class ExternalStudentViewSet(ManagerWriteMixin, viewsets.ModelViewSet):
    """
    /api/v1/external-students/students/ — the municipality roster.

    Managers write, partners read their own branches. Nothing here touches a
    Child, a Payment or a standing order.
    """

    # ManagerWriteMixin governs every method: partners read, managers write.
    # It reads request.method, so the custom POST actions below are manager-only
    # without each having to say so.
    serializer_class = ExternalStudentSerializer
    pagination_class = None
    throttle_scope = 'external_broadcast'

    def get_queryset(self):
        qs = (
            ExternalStudent.objects
            .select_related('lesson', 'lesson__course', 'lesson__course__branch')
            .annotate(attendance_total=Count('attendance_records'))
        )
        qs = scope_branches(qs, self.request.user, 'lesson__course__branch')

        branch_id = self.request.query_params.get('branch')
        if branch_id:
            qs = qs.filter(lesson__course__branch_id=branch_id)
        course_id = self.request.query_params.get('course')
        if course_id:
            qs = qs.filter(lesson__course_id=course_id)
        lesson_id = self.request.query_params.get('lesson')
        if lesson_id:
            qs = qs.filter(lesson_id=lesson_id)

        include_inactive = str(self.request.query_params.get('include_inactive', '')).lower() == 'true'
        if not include_inactive:
            qs = qs.filter(is_active=True)

        return qs.order_by('lesson__course__name', 'lesson__day_of_week', 'last_name', 'first_name')

    def get_throttles(self):
        # Only the broadcast opts into throttling; typing a paper list must not
        # hit a rate limit halfway through.
        if self.action == 'broadcast':
            return [ScopedRateThrottle()]
        return []

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Soft when there is history, hard when there is not.

        Attendance history is the thing this feature exists to produce, so a row
        that has any drops off the roster and keeps its past. A row created by a
        typo has nothing to preserve and should not leave a tombstone.
        """
        student = self.get_object()
        if student.attendance_records.exists():
            student.is_active = False
            student.end_date = student.end_date or timezone.localdate()
            student.updated_by = request.user
            student.save(update_fields=['is_active', 'end_date', 'updated_by', 'updated_at'])
            return Response({'deleted': False}, status=status.HTTP_200_OK)
        student.delete()
        return Response({'deleted': True}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk(self, request):
        """
        One lesson, many names, one transaction.

        A paper list is entered as a list. Doing it row by row would mean twenty
        round trips and a half-entered class if one of them fails.
        """
        payload = ExternalStudentBulkSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        lesson = payload.validated_data['lesson']
        rows = payload.validated_data['students']

        created, errors = [], []
        with transaction.atomic():
            for index, row in enumerate(rows):
                item = ExternalStudentSerializer(data={**row, 'lesson': str(lesson.id)})
                if not item.is_valid():
                    errors.append({'index': index, 'errors': item.errors})
                    continue
                created.append(item.save(created_by=request.user, updated_by=request.user))
            if errors:
                transaction.set_rollback(True)
                return Response(
                    {'created': 0, 'errors': errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(
            {'created': len(created), 'students': ExternalStudentSerializer(created, many=True).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['post'], url_path='broadcast')
    def broadcast(self, request):
        """
        Send one ManyChat flow to the selected students' phones.

        Manager only. Nothing goes out unless ``dry_run`` is explicitly false.
        """
        payload = ExternalBroadcastSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = payload.validated_data['student_ids']
        if len(ids) > BROADCAST_MAX_STUDENTS:
            return Response(
                {'error': f'ניתן לשלוח עד {BROADCAST_MAX_STUDENTS} תלמידים בבקשה אחת'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        students = list(
            self.get_queryset().filter(id__in=ids)
        )
        if not students:
            return Response({'error': 'לא נמצאו תלמידים'}, status=status.HTTP_400_BAD_REQUEST)

        result = broadcast_to_external_students(
            students,
            automation_id=payload.validated_data['automation_id'],
            dry_run=payload.validated_data['dry_run'],
            skip_phones=payload.validated_data.get('skip_phones') or [],
        )
        return Response(result)
