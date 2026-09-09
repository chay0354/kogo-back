import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.enrollments.duplicate_students import duplicate_person_on_lesson, duplicate_roster_rows
from apps.enrollments.models import Enrollment, LessonEnrollment, TrialBlockedDate, TrialRegistrationPolicy
from apps.enrollments.person_match import contact_phone
from apps.enrollments.register_reminders import (
    GAP_ALERT_AFTER_DAYS,
    GAP_WINDOW_DAYS,
    instructor_register_gaps,
    send_due_register_reminders,
)
from apps.enrollments.serializers import (
    EnrollmentSerializer,
    LessonEnrollmentSerializer,
    TrialBlockedDateSerializer,
)
from apps.enrollments.trial_reminders import (
    configured_blocked_trial_lesson_dates,
    iter_upcoming_lesson_occurrences,
    reschedule_blocked_trial_enrollments,
    send_due_trial_reminders,
    stamp_and_notify_trial_enrollment,
)
from apps.core.permissions import IsManager, IsManagerOrPartner, ManagerWriteMixin
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


def _existing_row(lesson_id, child_id):
    """The (lesson, child) row a trial registration would collide with, if any."""
    if not lesson_id or not child_id:
        return None
    try:
        return (
            LessonEnrollment.objects
            .select_related('child', 'lesson', 'lesson__course', 'lesson__room')
            .filter(lesson_id=str(lesson_id), child_id=str(child_id))
            .first()
        )
    except (ValueError, DjangoValidationError):
        return None


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
        # A trial for a child who already has a row on this lesson — a trial
        # that passed, or an old registration — reuses that row; the unique
        # (lesson, child) rule would otherwise refuse it before anything runs.
        if _is_trial_registration(request, {}):
            existing = _existing_row(request.data.get('lesson'), request.data.get('child'))
            if existing is not None:
                return self._repeat_trial(request, existing)

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
            duplicate = duplicate_person_on_lesson(
                lesson,
                first_name=child.first_name,
                last_name=child.last_name,
                phone=contact_phone(child),
                exclude_child_id=child.id,
            )
            if duplicate is not None:
                return Response(
                    {
                        'error': f'{child.first_name} {child.last_name} כבר רשום לשיעור הזה עם אותו טלפון',
                        'existing_child_id': str(duplicate.child_id),
                        'existing_enrollment_id': str(duplicate.id),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            enrollment = serializer.save()
            status_code = status.HTTP_201_CREATED

        if not trial_registration:
            data = dict(self.get_serializer(enrollment).data)
            headers = self.get_success_headers(data)
            return Response(data, status=status_code, headers=headers)

        # A child who already had a trial gets the next number on this new row.
        from apps.enrollments.repeat_trial import next_trial_number
        wanted = next_trial_number(child, exclude_id=enrollment.id)
        if wanted != enrollment.trial_number:
            enrollment.trial_number = wanted
            enrollment.save(update_fields=['trial_number', 'updated_at'])
        return self._finish_trial(enrollment, status_code)

    def _repeat_trial(self, request, existing):
        """Another trial on a lesson the child already has a row for (the office only)."""
        from apps.enrollments.enrollment_counts import count_capacity_enrollments
        from apps.enrollments.repeat_trial import next_trial_number
        from apps.enrollments.trial_reminders import next_allowed_trial_date

        child = existing.child
        lesson = existing.lesson
        today = timezone.localdate()
        if not existing.trial_lesson_date and existing.status in ('active', 'payments_problem'):
            return Response({'error': 'הילד כבר רשום לשיעור הזה כתלמיד קבוע'}, status=status.HTTP_400_BAD_REQUEST)
        if existing.trial_lesson_date and existing.trial_lesson_date >= today and existing.status == 'active':
            return Response(
                {'error': f'הילד כבר רשום לשיעור ניסיון בשיעור הזה ב־{existing.trial_lesson_date:%d/%m/%Y} — את התאריך אפשר לשנות מכרטיס הילד'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_date = request.data.get('trial_lesson_date')
        if raw_date:
            try:
                trial_date = date.fromisoformat(str(raw_date))
            except ValueError:
                return Response({'trial_lesson_date': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
            if trial_date not in iter_upcoming_lesson_occurrences(lesson, count=8):
                return Response({'trial_lesson_date': 'תאריך שיעור הניסיון אינו זמין'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            trial_date = next_allowed_trial_date(lesson)
        if trial_date is None or trial_date < today:
            return Response({'trial_lesson_date': 'תאריך שיעור הניסיון אינו זמין'}, status=status.HTTP_400_BAD_REQUEST)
        if not lesson.room:
            return Response({'lesson': 'לא ניתן להירשם לשיעור ללא חדר מוגדר'}, status=status.HTTP_400_BAD_REQUEST)
        capacity = lesson.course.capacity or lesson.room.capacity
        if count_capacity_enrollments(lesson=lesson, occurrence_date=trial_date) >= capacity:
            return Response(
                {'lesson': f'השיעור מלא - קיבולת מקסימלית: {capacity} תלמידים'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing.trial_number = next_trial_number(child)
        existing.trial_lesson_date = trial_date
        existing.start_date = trial_date
        existing.end_date = None
        existing.status = 'active'
        existing.bundle = None  # a trial is one lesson; an old bundle pointer must not follow the row
        existing.trial_outcome = ''
        existing.trial_10am_reminder_sent_at = None
        existing.trial_followup_reminder_sent_at = None
        existing.trial_evening_reminder_sent_at = None
        existing.didnt_arrive_whatsapp_sent_at = None
        existing.save(update_fields=[
            'trial_number', 'trial_lesson_date', 'start_date', 'end_date', 'status', 'bundle', 'trial_outcome',
            'trial_10am_reminder_sent_at', 'trial_followup_reminder_sent_at', 'trial_evening_reminder_sent_at',
            'didnt_arrive_whatsapp_sent_at', 'updated_at',
        ])
        return self._finish_trial(existing, status.HTTP_200_OK)

    def _finish_trial(self, enrollment, status_code):
        """Stamp the date, tell the parent, mark the child — shared by first and repeat trials."""
        child = enrollment.child
        data = dict(self.get_serializer(enrollment).data)
        try:
            whatsapp_result = stamp_and_notify_trial_enrollment(str(enrollment.id))
        except Exception:
            logger.exception("Trial WhatsApp notification failed (non-fatal)")
            whatsapp_result = {'sent': False, 'reason': 'exception'}

        Child.objects.filter(pk=child.pk).update(status='trial_signed')
        enrollment.refresh_from_db(fields=['trial_lesson_date'])
        data['trial_lesson_date'] = enrollment.trial_lesson_date.isoformat() if enrollment.trial_lesson_date else None
        data['trial_applied'] = True
        data['trial_number'] = enrollment.trial_number
        data['repeat_trial'] = enrollment.trial_number > 1
        data['whatsapp'] = whatsapp_result or {'sent': False, 'reason': 'skipped'}
        logger.info(
            "Trial registration for child %s lesson %s number %s whatsapp=%s",
            child.pk, enrollment.lesson_id, enrollment.trial_number, data['whatsapp'],
        )
        headers = self.get_success_headers(data)
        return Response(data, status=status_code, headers=headers)

    @action(detail=False, methods=['get'], url_path='duplicates')
    def duplicates(self, request):
        """Registers holding the same person twice — same full name and phone."""
        rows = duplicate_roster_rows()
        return Response({'count': len(rows), 'duplicates': rows})

    @action(detail=True, methods=['get'], url_path='trial-dates')
    def trial_dates(self, request, pk=None):
        """Upcoming lesson dates staff can move a trial signup to.

        Pass ?lesson_id= to preview dates for a different שיעור before saving.
        """
        enrollment = self.get_object()
        lesson_id = (request.query_params.get('lesson_id') or '').strip()
        if lesson_id:
            try:
                lesson = Lesson.objects.select_related('course').get(pk=lesson_id)
            except (Lesson.DoesNotExist, ValueError, TypeError):
                return Response({'error': 'השיעור לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        else:
            lesson = enrollment.lesson
        day_names = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        dates = iter_upcoming_lesson_occurrences(lesson, count=8)
        current = enrollment.trial_lesson_date if str(enrollment.lesson_id) == str(lesson.id) else None
        from apps.enrollments.trial_reminders import blocked_trial_lesson_dates
        if current and current not in dates and current not in blocked_trial_lesson_dates():
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

    def _resolve_change_target(self, request):
        """(target_lessons, target_bundle) for a non-trial change body, or raise LookupError/ValueError."""
        from apps.courses.models import Course, LessonBundle
        from apps.enrollments.change_course import course_unit_lessons, matching_bundle

        course_id = (request.data.get('course_id') or request.data.get('course') or '').strip()
        new_lesson_id = (request.data.get('lesson_id') or request.data.get('lesson') or '').strip()
        bundle_id = (request.data.get('bundle_id') or request.data.get('bundle') or '').strip()
        if bundle_id:
            bundle = (
                LessonBundle.objects
                .select_related('course', 'course__branch')
                .prefetch_related('lessons', 'lessons__course', 'lessons__room')
                .get(pk=bundle_id)
            )
            targets = [lesson for lesson in bundle.lessons.all() if lesson.status != 'cancelled']
            targets.sort(key=lambda lesson: (lesson.day_of_week, str(lesson.start_time), str(lesson.id)))
            return targets, bundle
        if new_lesson_id:
            lesson = Lesson.objects.select_related('course', 'course__branch', 'room').get(pk=new_lesson_id)
            return [lesson], None
        course = Course.objects.select_related('branch').get(pk=course_id)
        targets = course_unit_lessons(course)
        return targets, matching_bundle(course, targets)

    @action(detail=True, methods=['post'], url_path='change-lesson/quote')
    def change_lesson_quote(self, request, pk=None):
        """What a change would cost — read-only; the dialog shows this before the office confirms."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.courses.models import Course, LessonBundle
        from apps.enrollments.change_pricing import quote_unit_change

        enrollment = self.get_object()
        if enrollment.trial_lesson_date:
            return Response({'direction': 'no_sto', 'blocked': 'שיעור ניסיון — אין תמחור'})
        try:
            target_lessons, target_bundle = self._resolve_change_target(request)
        except (Course.DoesNotExist, Lesson.DoesNotExist, LessonBundle.DoesNotExist, DjangoValidationError, TypeError):
            return Response({'error': 'החוג לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        if not target_lessons:
            return Response({'error': 'לא נמצאו שיעורים בחוג שנבחר'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            quote = quote_unit_change(enrollment=enrollment, target_lessons=target_lessons, target_bundle=target_bundle)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(quote)

    @action(detail=True, methods=['post'], url_path='cancel-scheduled-change', permission_classes=[IsAuthenticated, IsManager])
    def cancel_scheduled_change(self, request, pk=None):
        from apps.enrollments.change_pricing import ChangePricingError, cancel_scheduled_change, pending_change_for

        enrollment = self.get_object()
        change = pending_change_for(enrollment)
        if change is None:
            return Response({'error': 'אין החלפה מתוזמנת'}, status=status.HTTP_404_NOT_FOUND)
        try:
            cancel_scheduled_change(change)
        except ChangePricingError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'cancelled': True, 'id': str(change.id)})

    @action(detail=True, methods=['post'], url_path='change-lesson')
    def change_lesson(self, request, pk=None):
        """Move a child to another חוג.

        Twice/thrice-a-week courses are replaced as one unit. When the target
        costs a different monthly amount the office must have seen the quote:
        the body carries `expected_new_amount`; an upgrade charges the prorated
        difference now and schedules the new amount, a downgrade schedules the
        whole change for the next billing date.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.courses.models import Course, LessonBundle
        from apps.customers.serializers import _serialize_lesson_enrollment, propagate_scheduled_change
        from apps.enrollments.change_course import move_trial_enrollment, replace_course_unit, replace_unit
        from apps.enrollments.change_pricing import ChangePricingError, apply_unit_change

        enrollment = self.get_object()
        course_id = (request.data.get('course_id') or request.data.get('course') or '').strip()
        new_lesson_id = (request.data.get('lesson_id') or request.data.get('lesson') or '').strip()
        bundle_id = (request.data.get('bundle_id') or request.data.get('bundle') or '').strip()
        if not course_id and not new_lesson_id and not bundle_id:
            return Response({'error': 'יש לבחור חוג'}, status=status.HTTP_400_BAD_REQUEST)

        if enrollment.trial_lesson_date:
            if bundle_id:
                return Response(
                    {'error': 'שיעור ניסיון אפשר להעביר לשיעור בודד'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                if new_lesson_id:
                    new_lesson = (
                        Lesson.objects
                        .select_related('course', 'course__branch', 'room')
                        .get(pk=new_lesson_id)
                    )
                else:
                    new_course = Course.objects.select_related('branch').get(pk=course_id)
                    course_lessons = [
                        lesson for lesson in new_course.lessons.exclude(status='cancelled')
                        .select_related('course', 'room')
                        .order_by('day_of_week', 'start_time', 'id')
                    ]
                    if len(course_lessons) != 1:
                        return Response(
                            {'error': 'שיעור ניסיון אפשר להעביר לשיעור בודד'},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    new_lesson = course_lessons[0]
                result = move_trial_enrollment(
                    enrollment=enrollment,
                    new_lesson=new_lesson,
                    trial_date=request.data.get('trial_lesson_date'),
                )
            except (Course.DoesNotExist, Lesson.DoesNotExist, DjangoValidationError, TypeError):
                return Response({'error': 'החוג לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
            except ValueError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            kept = result['kept']
            primary = kept[0]
            data = dict(self.get_serializer(primary).data)
            data['enrollments'] = [_serialize_lesson_enrollment(row) for row in kept]
            data['removed_enrollment_ids'] = [str(row_id) for row_id in result['removed_ids']]
            return Response(data)

        raw_expected = request.data.get('expected_new_amount')
        expected = None
        if raw_expected not in (None, ''):
            try:
                expected = Decimal(str(raw_expected))
            except (InvalidOperation, ValueError):
                return Response({'error': 'סכום לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
            if not expected.is_finite():
                return Response({'error': 'סכום לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            target_lessons, target_bundle = self._resolve_change_target(request)
            if not target_lessons:
                return Response({'error': 'לא נמצאו שיעורים בחוג שנבחר'}, status=status.HTTP_400_BAD_REQUEST)
            if not bundle_id and not new_lesson_id and str(target_lessons[0].course_id) == str(enrollment.lesson.course_id):
                result = replace_course_unit(enrollment=enrollment, new_course=target_lessons[0].course)
                result = {**result, 'applied': 'now', 'charged': None, 'quote': None}
            else:
                result = apply_unit_change(
                    enrollment=enrollment,
                    target_lessons=target_lessons,
                    target_bundle=target_bundle,
                    expected_new_amount=expected,
                    created_by=request.user if request.user.is_authenticated else None,
                    # Charging a card and rescheduling a standing order are owner-level acts.
                    allow_pricing=IsManager().has_permission(request, self),
                )
        except (Course.DoesNotExist, Lesson.DoesNotExist, LessonBundle.DoesNotExist, DjangoValidationError, TypeError):
            return Response({'error': 'החוג לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        except ChangePricingError as exc:
            code = status.HTTP_409_CONFLICT if exc.processing else status.HTTP_400_BAD_REQUEST
            return Response({'error': str(exc), 'processing': exc.processing}, status=code)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        kept = result['kept']
        primary = next((row for row in kept if row.id == enrollment.id), kept[0])
        data = dict(self.get_serializer(primary).data)
        data['enrollments'] = propagate_scheduled_change([_serialize_lesson_enrollment(row) for row in kept])
        data['removed_enrollment_ids'] = [str(row_id) for row_id in result['removed_ids']]
        data['applied'] = result.get('applied', 'now')
        data['unchanged'] = bool(result.get('unchanged')) and result.get('applied') != 'scheduled'
        data['manual_collection'] = result.get('manual_collection')
        data['cleared_pending'] = bool(result.get('cleared_pending'))
        data['charged'] = result.get('charged')
        data['folded_into_next_month'] = bool(result.get('folded_into_next_month'))
        data['scheduled_change'] = result.get('scheduled_change')
        data['quote'] = result.get('quote')
        return Response(data)

    @action(detail=True, methods=['post'], url_path='drop-course')
    def drop_course(self, request, pk=None):
        """Remove the child from this חוג and cancel the standing order from the next charge."""
        from apps.enrollments.change_course import drop_course_unit

        enrollment = self.get_object()
        reason = (request.data.get('cancellation_reason') or '').strip()
        result = drop_course_unit(enrollment=enrollment, cancellation_reason=reason)
        return Response({
            'removed_enrollment_ids': [str(row_id) for row_id in result['removed_ids']],
            'cancelled_recurring_ids': result['cancelled_recurring_ids'],
            'child_status': result['child_status'],
        })


class TrialBlockedDateViewSet(ManagerWriteMixin, viewsets.ModelViewSet):
    """
    USAGE: Registered at /api/v1/enrollments/trial-blocked-dates/
    USAGE: The settings calendar of days on which no trial lesson can be booked.

    Partners read, managers write. Blocking a day that already holds trial
    bookings moves them to the next open date of the same lesson and tells the
    office how many moved and how many could not (no later slot found), so a
    parent is never left booked on a day the studio is closed.
    """
    queryset = TrialBlockedDate.objects.select_related('created_by')
    serializer_class = TrialBlockedDateSerializer
    pagination_class = None

    def perform_create(self, serializer):
        try:
            serializer.save(created_by=self.request.user if self.request.user.is_authenticated else None)
        except IntegrityError:
            # Two managers blocked the same day at once; the unique validator
            # only looks before the insert.
            raise ValidationError({'date': ['התאריך כבר חסום']})

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        blocked_on = response.data.get('date')
        # Move the trials booked on this day and report those alone — the sweep
        # also walks the configured dates, which are not news to the office.
        rows = [row for row in reschedule_blocked_trial_enrollments() if row.get('old_trial_date') == blocked_on]
        response.data = {
            **response.data,
            'moved': sum(1 for row in rows if row.get('moved')),
            'unmoved': sum(1 for row in rows if not row.get('moved')),
        }
        return response

    @action(detail=False, methods=['get'], url_path='configured')
    def configured(self, request):
        """The dates fixed in configuration — shown read-only beside the ones the office marks."""
        return Response({'dates': [day.isoformat() for day in sorted(configured_blocked_trial_lesson_dates())]})


def _cron_token_ok(request) -> bool:
    """
    The same door the billing cron uses — X-Cron-Token, ?token=, or the Bearer
    header Vercel Cron sends. Imported where it is used: apps.customers.views
    pulls in this module.
    """
    from apps.customers.views import _cron_request_authorized

    return _cron_request_authorized(request)


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_register_reminders(request):
    """
    Nudge instructors whose register is still open.

    Safe to run every hour of the teaching day: what is due is decided from the
    clock, and every message sent is written down, so a second run in the same
    hour sends nothing.
    """
    if not _cron_token_ok(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    dry_run = str(request.query_params.get('dry_run', '')).lower() in ('1', 'true', 'yes')
    summary = send_due_register_reminders(dry_run=dry_run)
    return Response({'ok': True, 'dry_run': dry_run, 'summary': summary})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsManagerOrPartner])
def register_gaps(request):
    """Registers still open, by instructor — what the office sees."""
    try:
        days = int(request.query_params.get('days') or GAP_WINDOW_DAYS)
    except (TypeError, ValueError):
        days = GAP_WINDOW_DAYS
    days = max(1, min(days, 60))
    instructor_id = (request.query_params.get('instructor_id') or '').strip() or None
    rows = instructor_register_gaps(days=days, instructor_id=instructor_id)
    return Response({
        'days': days,
        'alert_after_days': GAP_ALERT_AFTER_DAYS,
        'open_count': sum(row['open_count'] for row in rows),
        'needs_attention_count': sum(1 for row in rows if row['needs_attention']),
        'instructors': rows,
    })


@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
def cron_trial_reminders(request):
    """
    Scheduler-friendly endpoint for sending due trial reminders — and for
    retiring trials whose date has passed.

    Scheduled in vercel.json. Auth is the same door the billing and register
    crons use (X-Cron-Token, ?token=, or the Bearer header Vercel Cron sends):
    the earlier inline check accepted only the token header, which is not how
    Vercel calls, so the schedule would have run and been turned away.
    """
    if not _cron_token_ok(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

    dry_run = str(request.query_params.get('dry_run', '')).lower() in ('1', 'true', 'yes')
    summary = send_due_trial_reminders(dry_run=dry_run)
    return Response({'ok': True, 'dry_run': dry_run, 'summary': summary})



class TrialRegistrationPolicyView(APIView):
    """
    USAGE: GET/PUT /api/v1/enrollments/trial-registration-policy/
    USAGE: The studio-wide rule: are trial bookings open at all.

    Partners read, managers write. A lesson set open or closed by hand keeps
    its own answer when this flips — see Lesson.trial_registration_open.
    """
    def get_permissions(self):
        from rest_framework.permissions import SAFE_METHODS
        from apps.core.permissions import IsManager, IsManagerOrPartner
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated(), IsManagerOrPartner()]
        return [IsAuthenticated(), IsManager()]

    def get(self, request):
        policy = TrialRegistrationPolicy.current()
        return Response({'trials_open': policy.trials_open, 'updated_at': policy.updated_at})

    def put(self, request):
        value = request.data.get('trials_open')
        if not isinstance(value, bool):
            return Response({'error': 'trials_open חייב להיות true או false'}, status=status.HTTP_400_BAD_REQUEST)
        policy = TrialRegistrationPolicy.current()
        policy.trials_open = value
        policy.updated_by = request.user
        policy.save(update_fields=['trials_open', 'updated_by', 'updated_at'])
        return Response({'trials_open': policy.trials_open, 'updated_at': policy.updated_at})


class TrialRegistrationLessonsView(APIView):
    """
    USAGE: GET /api/v1/enrollments/trial-registration/lessons/?branch=&course_type=&age=
    USAGE: The lessons the office narrows down to — by branch, course type and
    USAGE: age — each with its own setting and what that comes to under the rule.

    The listing the settings screen drills through to find the one lesson
    whose trial button should go. Age is a single number: a course whose
    range holds it is shown, and a course with no range at either end is
    shown for every age.
    """
    def get_permissions(self):
        from apps.core.permissions import IsManagerOrPartner
        return [IsAuthenticated(), IsManagerOrPartner()]

    def get(self, request):
        from django.db.models import Q
        from apps.courses.models import Lesson
        from apps.core.scoping import scope_courses
        from apps.enrollments.trial_policy import trial_registration_open_for, trials_open_by_default

        qs = (
            Lesson.objects
            .filter(course__is_active=True, status='scheduled')
            .select_related('course', 'course__branch', 'course__course_type', 'instructor')
            .order_by('course__branch__name', 'course__name', 'day_of_week', 'start_time')
        )
        branch = (request.query_params.get('branch') or '').strip()
        if branch:
            qs = qs.filter(course__branch_id=branch)
        course_type = (request.query_params.get('course_type') or '').strip()
        if course_type:
            qs = qs.filter(course__course_type_id=course_type)
        age = (request.query_params.get('age') or '').strip()
        if age:
            try:
                years = int(age)
            except ValueError:
                return Response({'error': 'גיל לא תקין'}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(
                (Q(course__min_age__isnull=True) | Q(course__min_age__lte=years))
                & (Q(course__max_age__isnull=True) | Q(course__max_age__gte=years))
            )
        qs = scope_courses(qs, request.user, course_lookup='course')

        default = trials_open_by_default()
        rows = []
        for lesson in qs:
            course = lesson.course
            rows.append({
                'lesson_id': str(lesson.id),
                'course_id': str(course.id),
                'course_name': course.name,
                'course_display_id': course.display_id,
                'course_type_name': course.course_type.name if course.course_type_id else '',
                'branch_id': str(course.branch_id),
                'branch_name': course.branch.name,
                'min_age': course.min_age,
                'max_age': course.max_age,
                'day_of_week': lesson.day_of_week,
                'start_time': str(lesson.start_time)[:5],
                'end_time': str(lesson.end_time)[:5],
                'instructor_name': lesson.instructor.full_name if lesson.instructor_id else '',
                'override': lesson.trial_registration_open,
                'effective': trial_registration_open_for(lesson, default=default),
            })
        return Response({'trials_open_by_default': default, 'results': rows})
