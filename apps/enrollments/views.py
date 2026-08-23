import logging

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from apps.enrollments.models import Enrollment, LessonEnrollment
from apps.enrollments.serializers import EnrollmentSerializer, LessonEnrollmentSerializer
from apps.enrollments.trial_reminders import (
    iter_upcoming_lesson_occurrences,
    send_due_trial_reminders,
    stamp_and_notify_trial_enrollment,
)
from apps.core.permissions import IsManager, IsManagerOrPartner
from apps.courses.models import Lesson
from apps.customers.models import Child

logger = logging.getLogger(__name__)


def _is_trial_registration(request, validated_data) -> bool:
    """Accept trial flag from validated payload or raw request (bool/string)."""
    raw = validated_data.get('trial_registration', request.data.get('trial_registration'))
    if raw is True:
        return True
    if raw in (False, None, ''):
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


class EnrollmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Course Enrollments
    
    USAGE: Available at /api/v1/enrollments/enrollments/
    USAGE: Used in Django admin via EnrollmentInline in ChildAdmin
    ⚠️ NOTE: This is the OLD enrollment model, LessonEnrollment is the newer one
    """
    queryset = Enrollment.objects.all().select_related('course', 'child', 'child__family')
    serializer_class = EnrollmentSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    
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
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        trial_registration = _is_trial_registration(request, serializer.validated_data)
        lesson = serializer.validated_data['lesson']
        child = serializer.validated_data['child']

        existing = LessonEnrollment.objects.filter(lesson=lesson, child=child).first()
        if existing:
            for field in ('status', 'start_date', 'end_date', 'notes'):
                if field in serializer.validated_data:
                    setattr(existing, field, serializer.validated_data[field])
            existing.save()
            enrollment = existing
            status_code = status.HTTP_200_OK
        else:
            enrollment = serializer.save()
            status_code = status.HTTP_201_CREATED

        data = dict(self.get_serializer(enrollment).data)
        whatsapp_result = None

        if trial_registration:
            try:
                whatsapp_result = stamp_and_notify_trial_enrollment(str(enrollment.id))
            except Exception:
                logger.exception("Trial WhatsApp notification failed (non-fatal)")
                whatsapp_result = {'sent': False, 'reason': 'exception'}

            Child.objects.filter(pk=child.pk).update(status='trial_signed')
            data['trial_applied'] = True
            data['whatsapp'] = whatsapp_result or {'sent': False, 'reason': 'skipped'}
            logger.info(
                "Trial registration for child %s lesson %s whatsapp=%s",
                child.pk,
                lesson.pk,
                data['whatsapp'],
            )

        headers = self.get_success_headers(data)
        return Response(data, status=status_code, headers=headers)

    @action(detail=True, methods=['get'], url_path='trial-dates')
    def trial_dates(self, request, pk=None):
        """Upcoming lesson dates staff can move a trial signup to."""
        enrollment = self.get_object()
        lesson = enrollment.lesson
        day_names = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        dates = iter_upcoming_lesson_occurrences(lesson, count=8)
        current = enrollment.trial_lesson_date
        if current and current not in dates:
            dates = [current] + dates
        day_name = day_names[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else ''
        start_time = lesson.start_time.strftime('%H:%M') if lesson.start_time else ''
        end_time = lesson.end_time.strftime('%H:%M') if lesson.end_time else ''
        return Response({
            'enrollment_id': str(enrollment.id),
            'lesson_id': str(lesson.id),
            'course_name': lesson.course.name,
            'day_name': day_name,
            'start_time': start_time,
            'end_time': end_time,
            'current_date': current.isoformat() if current else None,
            'dates': [
                {
                    'date': d.isoformat(),
                    'label': d.strftime('%d/%m/%Y'),
                    'is_current': bool(current and d == current),
                }
                for d in dates
            ],
        })

    @action(detail=True, methods=['post'], url_path='change-lesson')
    def change_lesson(self, request, pk=None):
        """Move a child to another lesson without changing what they pay."""
        from django.db import transaction
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.customers.models import Payment
        from apps.enrollments.enrollment_counts import count_capacity_enrollments

        enrollment = self.get_object()
        new_lesson_id = (request.data.get('lesson_id') or request.data.get('lesson') or '').strip()
        if not new_lesson_id:
            return Response({'error': 'יש לבחור שיעור'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            new_lesson = (
                Lesson.objects
                .select_related('course', 'course__branch', 'room')
                .get(pk=new_lesson_id)
            )
        except (Lesson.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            return Response({'error': 'השיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        old_lesson = enrollment.lesson
        if old_lesson.id == new_lesson.id:
            return Response(self.get_serializer(enrollment).data)

        if (
            LessonEnrollment.objects
            .filter(child=enrollment.child, lesson=new_lesson)
            .exclude(pk=enrollment.pk)
            .exists()
        ):
            return Response({'error': 'הילד כבר רשום לשיעור זה'}, status=status.HTTP_400_BAD_REQUEST)

        caps = []
        if new_lesson.course.capacity:
            caps.append(int(new_lesson.course.capacity))
        if new_lesson.room and new_lesson.room.capacity:
            caps.append(int(new_lesson.room.capacity))
        if caps:
            current = count_capacity_enrollments(lesson=new_lesson)
            capacity = min(caps)
            if current >= capacity:
                return Response(
                    {'error': f'השיעור מלא — קיבולת מקסימלית: {capacity} תלמידים'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        keep_bundle = bool(
            enrollment.bundle_id
            and enrollment.bundle.lessons.filter(pk=new_lesson.pk).exists()
        )

        with transaction.atomic():
            enrollment.lesson = new_lesson
            if enrollment.bundle_id and not keep_bundle:
                enrollment.bundle = None
            enrollment.save(update_fields=['lesson', 'bundle', 'updated_at'])
            payment_update = {'lesson': new_lesson}
            if not keep_bundle:
                payment_update['bundle'] = None
            Payment.objects.filter(child=enrollment.child, lesson=old_lesson).update(**payment_update)

        return Response(self.get_serializer(enrollment).data)


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

