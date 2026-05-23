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

logger = logging.getLogger(__name__)


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
        response = super().create(request, *args, **kwargs)
        if response.status_code == status.HTTP_201_CREATED:
            try:
                enrollment_id = response.data.get('id') if isinstance(response.data, dict) else None
                if enrollment_id:
                    self._stamp_and_notify_trial_enrollment(enrollment_id)
            except Exception:
                logger.exception("Trial WhatsApp notification failed (non-fatal)")
        return response

    @staticmethod
    def _stamp_and_notify_trial_enrollment(enrollment_id: str) -> None:
        """
        After a manager creates a trial enrollment:
          • compute & store the upcoming trial lesson date (used by reminder scheduler)
          • fire the ManyChat 'trial' template
        """
        from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
        from apps.core.manychat_service import ManyChatService

        enrollment = (
            LessonEnrollment.objects
            .select_related('lesson', 'lesson__course', 'lesson__branch', 'child', 'child__family')
            .filter(id=enrollment_id)
            .first()
        )
        if not enrollment or not enrollment.lesson_id:
            return

        lesson = enrollment.lesson

        # Stamp the trial lesson date for reminder scheduling.
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
            return

        lookup_names = ctx.pop('lookup_names', None)
        result = ManyChatService().notify_registration(
            kind=ManyChatService.REGISTRATION_KIND_TRIAL,
            lookup_names=lookup_names,
            **ctx,
        )
        logger.info("Trial WhatsApp notification result: %s", result)


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

