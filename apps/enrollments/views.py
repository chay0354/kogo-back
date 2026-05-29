import logging

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from apps.enrollments.models import Enrollment, LessonEnrollment
from apps.enrollments.serializers import EnrollmentSerializer, LessonEnrollmentSerializer
from apps.enrollments.trial_reminders import (
    compute_trial_lesson_date,
    send_due_trial_reminders,
)
from apps.core.permissions import IsManager
from apps.courses.models import Lesson
from apps.customers.models import Child

logger = logging.getLogger(__name__)


def _is_trial_registration_request(data) -> bool:
    return str((data or {}).get('trial_registration', '')).lower() in ('1', 'true', 'yes')


class EnrollmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Course Enrollments
    
    USAGE: Available at /api/v1/enrollments/enrollments/
    USAGE: Used in Django admin via EnrollmentInline in ChildAdmin
    ⚠️ NOTE: This is the OLD enrollment model, LessonEnrollment is the newer one
    """
    queryset = Enrollment.objects.all().select_related('course', 'child', 'child__family')
    serializer_class = EnrollmentSerializer
    permission_classes = [IsAuthenticated, IsManager]
    
    def create(self, request, *args, **kwargs):
        """
        Create enrollment and handle duplicates
        
        USAGE: Handles creation logic with duplicate checking and reactivation
        """
        course_id = request.data.get('course')
        child_id = request.data.get('child')
        
        # Check if enrollment already exists
        existing = Enrollment.objects.filter(course_id=course_id, child_id=child_id).first()
        
        if existing:
            if existing.is_active:
                return Response({
                    'error': 'הילד כבר רשום לחוג זה',
                    'enrollment_id': str(existing.id)
                }, status=status.HTTP_400_BAD_REQUEST)
            else:
                # Reactivate existing enrollment
                existing.is_active = True
                existing.save()
                serializer = self.get_serializer(existing)
                return Response(serializer.data, status=status.HTTP_200_OK)
        
        # Create new enrollment
        return super().create(request, *args, **kwargs)


class LessonEnrollmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Lesson Enrollments
    
    USAGE: Available at /api/v1/enrollments/lesson-enrollments/
    USAGE: Not directly used by frontend, but data is accessed via Child queryset
    Used for enrolling children in specific lesson instances
    """
    queryset = LessonEnrollment.objects.all().select_related('lesson', 'lesson__course', 'child')
    serializer_class = LessonEnrollmentSerializer
    permission_classes = [IsAuthenticated, IsManager]

    def create(self, request, *args, **kwargs):
        trial_registration = _is_trial_registration_request(request.data)
        response = super().create(request, *args, **kwargs)

        if response.status_code == status.HTTP_201_CREATED and trial_registration:
            enrollment_id = response.data.get('id') if isinstance(response.data, dict) else None
            child_id = response.data.get('child') if isinstance(response.data, dict) else None
            whatsapp_result = None

            if enrollment_id:
                try:
                    whatsapp_result = self._stamp_and_notify_trial_enrollment(enrollment_id)
                except Exception:
                    logger.exception("Trial WhatsApp notification failed (non-fatal)")
                    whatsapp_result = {'sent': False, 'reason': 'exception'}

            if child_id:
                Child.objects.filter(id=child_id).update(status='trial_signed')

            if isinstance(response.data, dict):
                payload = dict(response.data)
                payload['whatsapp'] = whatsapp_result or {'sent': False, 'reason': 'skipped'}
                response.data = payload

        return response

    @staticmethod
    def _stamp_and_notify_trial_enrollment(enrollment_id: str) -> dict:
        """
        Immediately after trial signup (הרשם לניסיון):
          • store trial_lesson_date for later reminder cron
          • send ManyChat test-lesson-register flow to parent WhatsApp
        """
        from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
        from apps.core.manychat_service import ManyChatService

        enrollment = (
            LessonEnrollment.objects
            .select_related('lesson', 'lesson__course', 'lesson__branch', 'child', 'child__family')
            .prefetch_related('child__family__parents')
            .filter(id=enrollment_id)
            .first()
        )
        if not enrollment or not enrollment.lesson_id:
            return {'sent': False, 'reason': 'enrollment_not_found'}

        lesson = enrollment.lesson

        try:
            trial_date = compute_trial_lesson_date(lesson)
            if enrollment.trial_lesson_date != trial_date:
                enrollment.trial_lesson_date = trial_date
                enrollment.save(update_fields=['trial_lesson_date', 'updated_at'])
        except Exception:
            logger.exception("Failed to compute trial_lesson_date for enrollment %s", enrollment_id)

        child = enrollment.child
        ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
        if not ctx:
            return {'sent': False, 'reason': 'no_parent_phone'}

        if enrollment.trial_lesson_date:
            ctx['trial_date'] = enrollment.trial_lesson_date.strftime('%d/%m/%Y')

        lookup_names = ctx.pop('lookup_names', None)
        trial_date = ctx.pop('trial_date', '')
        result = ManyChatService().notify_registration(
            kind=ManyChatService.REGISTRATION_KIND_TRIAL,
            lookup_names=lookup_names,
            trial_date=trial_date,
            **ctx,
        )
        logger.info("Trial registration WhatsApp (instant): %s", result)
        return result


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_trial_reminders(request):
    """
    Scheduler-friendly endpoint for sending due trial reminders.

    Auth: requires X-Cron-Token header (or ?token=) matching settings.CRON_TOKEN.
    Vercel Cron config example (in vercel.json):
        { "crons": [{ "path": "/api/v1/enrollments/cron/trial-reminders/?token=...", "schedule": "*/30 * * * *" }] }
    """
    expected = (getattr(settings, 'CRON_TOKEN', '') or '').strip()
    provided = (
        request.headers.get('X-Cron-Token')
        or request.query_params.get('token')
        or ''
    ).strip()
    if not expected or provided != expected:
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    dry_run = str(request.query_params.get('dry_run', '')).lower() in ('1', 'true', 'yes')
    summary = send_due_trial_reminders(dry_run=dry_run)
    return Response({'ok': True, 'dry_run': dry_run, 'summary': summary})

