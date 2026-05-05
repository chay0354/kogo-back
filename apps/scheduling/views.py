from datetime import date as date_cls, timedelta

from django.utils import timezone
from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManager
from apps.core.models import UserProfile
from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment, LessonAttendance
from apps.scheduling.models import LessonCancellation, ScheduleEvent
from .serializers import (
    LessonListSerializer,
    LessonDetailSerializer,  # kept for other uses
    LessonCancelSerializer,
    AttendanceMarkSerializer,
    AttendanceSerializer,
)
from .event_serializers import (
    ScheduleEventSerializer,
    ScheduleEventListSerializer,
)


class LessonViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing lessons with role-based access control.
    
    - Managers see all lessons
    - Workers see only lessons they instruct (matched by email)
    - Only managers can cancel lessons
    """
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter lessons based on user role"""
        user = self.request.user
        qs = Lesson.objects.select_related(
            'course', 'course__course_type', 'instructor', 'branch', 'room'
        ).prefetch_related('enrollments')
        
        try:
            user_role = user.profile.role
        except UserProfile.DoesNotExist:
            return qs.none()
        
        if user_role == UserProfile.ROLE_MANAGER:
            # Managers see all lessons
            return qs
        elif user_role == UserProfile.ROLE_WORKER:
            # Workers see only their own lessons (matched by email)
            return qs.filter(instructor__email__iexact=user.email)
        
        return qs.none()
    
    def get_serializer_class(self):
        """Use detailed serializer for retrieve, simple for list"""
        if self.action == 'retrieve':
            return LessonDetailSerializer
        return LessonListSerializer

    def _parse_occurrence_date(self, value):
        if not value:
            return None
        try:
            return date_cls.fromisoformat(str(value))
        except ValueError:
            return None

    def _build_lesson_detail_payload(self, lesson, occ_date=None):
        """
        Build a lesson detail payload for a specific occurrence date.
        For recurring lessons, occ_date is required to resolve cancellation/attendance correctly.
        For non-recurring lessons, occ_date defaults to lesson.lesson_date.
        """
        if not lesson.is_recurring and occ_date is None:
            occ_date = lesson.lesson_date

        cancellation = None
        if lesson.is_recurring and occ_date:
            cancellation = LessonCancellation.objects.filter(lesson=lesson, occurrence_date=occ_date).first()

        # Attendance: per occurrence_date when available, otherwise legacy bucket (None)
        attendance_qs = lesson.attendance_records.select_related('child')
        if occ_date:
            attendance_qs = attendance_qs.filter(occurrence_date=occ_date)
        else:
            attendance_qs = attendance_qs.filter(occurrence_date__isnull=True)

        enrollments = lesson.enrollments.filter(status='active').select_related('child')

        data = LessonDetailSerializer(lesson).data
        if occ_date:
            data['lesson_date'] = occ_date.isoformat()

        if lesson.is_recurring and occ_date:
            if cancellation:
                data['status'] = 'cancelled'
                data['cancellation_reason'] = cancellation.reason or None
                data['cancelled_at'] = cancellation.created_at.isoformat()
            else:
                data['status'] = 'scheduled'
                data['cancellation_reason'] = None
                data['cancelled_at'] = None

        # Filter enrollments to exclude invisible ghost children
        from datetime import timedelta
        
        visible_enrollments = []
        for e in enrollments:
            # Check if ghost child is visible (within 30 days of creation from the lesson date)
            if e.child.status == 'ghost' and occ_date:
                # Calculate days between lesson date and ghost child creation
                days_since_creation = (occ_date - e.child.created_at.date()).days
                if days_since_creation > 30:
                    continue  # Skip invisible ghost children (older than 30 days from lesson date)
            
            visible_enrollments.append({
                'id': str(e.id),
                'child_id': str(e.child.id),
                'child_name': e.child.full_name,
                'child_status': e.child.status,
            })
        
        data['enrollments'] = visible_enrollments
        data['attendance'] = AttendanceSerializer(attendance_qs, many=True).data
        return data
    
    def list(self, request, *args, **kwargs):
        """
        List lessons with optional filters:
        - start_date: Filter lessons on or after this date
        - end_date: Filter lessons on or before this date
        - branch_id: Filter by branch
        - city_id: Filter by branch's city
        - instructor_id: Filter by instructor
        - status: Filter by status
        """
        queryset = self.get_queryset()
        
        # Apply filters
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        branch_id = request.query_params.get('branch_id')
        city_id = request.query_params.get('city_id')
        instructor_id = request.query_params.get('instructor_id')
        lesson_status = request.query_params.get('status')
        
        # Date filtering:
        # - Non-recurring lessons are filtered by lesson_date range
        # - Recurring lessons should appear in every week view from their start date onward,
        #   so we do NOT apply lesson_date__gte(start_date) to them (otherwise they'd disappear after week 1).
        if start_date or end_date:
            non_recurring_q = Q(is_recurring=False)
            if start_date:
                non_recurring_q &= Q(lesson_date__gte=start_date)
            if end_date:
                non_recurring_q &= Q(lesson_date__lte=end_date)

            recurring_q = Q(is_recurring=True)
            # If recurring has a start date, don't show it before that date.
            # If no start date exists, treat as always active.
            if end_date:
                recurring_q &= (Q(lesson_date__isnull=True) | Q(lesson_date__lte=end_date))

            queryset = queryset.filter(non_recurring_q | recurring_q)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        if city_id:
            queryset = queryset.filter(branch__city_id=city_id)
        if instructor_id:
            queryset = queryset.filter(instructor_id=instructor_id)
        if lesson_status:
            queryset = queryset.filter(status=lesson_status)
        
        # If both start and end are provided, expand recurring lessons into concrete occurrences.
        # This is required so the weekly schedule shows the lesson in every week (and cancellations can be per-date).
        if start_date and end_date:
            try:
                start = date_cls.fromisoformat(start_date)
                end = date_cls.fromisoformat(end_date)
            except ValueError:
                return Response({'error': 'תאריכים לא תקינים'}, status=status.HTTP_400_BAD_REQUEST)

            # Preload cancellations for the range
            cancellations = LessonCancellation.objects.filter(
                occurrence_date__gte=start,
                occurrence_date__lte=end,
                lesson__in=queryset,
            )
            cancel_map = {(str(c.lesson_id), c.occurrence_date): c for c in cancellations}

            expanded = []
            for lesson in queryset:
                if not lesson.is_recurring:
                    # Non-recurring: keep as-is (must match the range already via filters)
                    data = LessonListSerializer(lesson).data
                    expanded.append(data)
                    continue

                # Recurring: generate occurrence dates within [start, end], but not before lesson.lesson_date (start date)
                start_from = start
                if lesson.lesson_date and lesson.lesson_date > start_from:
                    start_from = lesson.lesson_date

                # Map our day_of_week (0=Sunday) to python weekday (0=Mon..6=Sun)
                target_py_weekday = (lesson.day_of_week - 1) % 7
                days_ahead = (target_py_weekday - start_from.weekday()) % 7
                first_occ = start_from + timedelta(days=days_ahead)

                occ = first_occ
                while occ <= end:
                    c = cancel_map.get((str(lesson.id), occ))
                    data = LessonListSerializer(lesson).data
                    # Override occurrence date and per-occurrence cancellation status
                    data['lesson_date'] = occ.isoformat()
                    if c:
                        data['status'] = 'cancelled'
                        data['cancellation_reason'] = c.reason or None
                        data['cancelled_at'] = c.created_at.isoformat()
                    else:
                        data['status'] = 'scheduled'
                        data['cancellation_reason'] = None
                        data['cancelled_at'] = None
                    expanded.append(data)
                    occ = occ + timedelta(days=7)

            # Sort by date/time
            expanded.sort(key=lambda x: (x.get('lesson_date') or '', x.get('start_time') or ''))
            return Response(expanded)

        # Fallback: no range -> return lessons as stored (legacy behavior)
        queryset = queryset.order_by('lesson_date', 'start_time')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """
        Retrieve lesson details.
        For recurring lessons, pass ?date=YYYY-MM-DD to get details for a specific occurrence,
        including date-specific cancellation/attendance.
        """
        lesson = self.get_object()
        occ_date = self._parse_occurrence_date(request.query_params.get('date'))
        if request.query_params.get('date') and not occ_date:
            return Response({'error': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        if lesson.is_recurring and not occ_date:
            return Response({'error': 'חסר פרמטר date לשיעור חוזר'}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self._build_lesson_detail_payload(lesson, occ_date=occ_date))
    
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsManager])
    def cancel(self, request, pk=None):
        """
        Cancel a lesson (manager only).
        Updates lesson status to 'cancelled'.
        Salary impact is calculated dynamically when retrieving salary data.
        """
        lesson = self.get_object()
        
        serializer = LessonCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reason = (serializer.validated_data.get('reason') or '').strip()
        occ_date = serializer.validated_data.get('date')

        if lesson.is_recurring:
            if not occ_date:
                return Response({'error': 'חסר שדה date לביטול מופע של שיעור חוזר'}, status=status.HTTP_400_BAD_REQUEST)

            cancellation, created = LessonCancellation.objects.get_or_create(
                lesson=lesson,
                occurrence_date=occ_date,
                defaults={'reason': reason, 'created_by': request.user},
            )
            if not created:
                return Response({'error': 'המופע כבר מבוטל'}, status=status.HTTP_400_BAD_REQUEST)

            return Response(self._build_lesson_detail_payload(lesson, occ_date=occ_date))

        # Non-recurring: keep legacy cancel on the lesson itself
        if lesson.status == 'cancelled':
            return Response({'error': 'השיעור כבר מבוטל'}, status=status.HTTP_400_BAD_REQUEST)

        lesson.status = 'cancelled'
        lesson.cancellation_reason = reason or None
        lesson.cancelled_at = timezone.now()
        lesson.save()
        result_serializer = LessonDetailSerializer(lesson)
        return Response(result_serializer.data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsManager])
    def restore(self, request, pk=None):
        """
        Restore a cancelled lesson (manager only).
        Sets status back to 'scheduled' and clears cancellation reason.
        """
        lesson = self.get_object()

        occ_date = self._parse_occurrence_date(request.data.get('date') or request.query_params.get('date'))
        if (request.data.get('date') or request.query_params.get('date')) and not occ_date:
            return Response({'error': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        if lesson.is_recurring:
            if not occ_date:
                return Response({'error': 'חסר שדה date להחזרת מופע של שיעור חוזר'}, status=status.HTTP_400_BAD_REQUEST)

            deleted, _ = LessonCancellation.objects.filter(lesson=lesson, occurrence_date=occ_date).delete()
            if deleted == 0:
                return Response({'error': 'המופע אינו מבוטל'}, status=status.HTTP_400_BAD_REQUEST)

            return Response(self._build_lesson_detail_payload(lesson, occ_date=occ_date))

        # Non-recurring restore
        if lesson.status != 'cancelled':
            return Response({'error': 'השיעור אינו מבוטל'}, status=status.HTTP_400_BAD_REQUEST)

        lesson.status = 'scheduled'
        lesson.cancellation_reason = None
        lesson.cancelled_at = None
        lesson.save()
        result_serializer = LessonDetailSerializer(lesson)
        return Response(result_serializer.data)
    
    @action(detail=True, methods=['get'])
    def attendance(self, request, pk=None):
        """Get attendance records for a lesson"""
        lesson = self.get_object()
        occ_date = self._parse_occurrence_date(request.query_params.get('date'))
        if request.query_params.get('date') and not occ_date:
            return Response({'error': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        if lesson.is_recurring and not occ_date:
            return Response({'error': 'חסר פרמטר date לשיעור חוזר'}, status=status.HTTP_400_BAD_REQUEST)

        if not lesson.is_recurring and occ_date is None:
            occ_date = lesson.lesson_date

        attendance_records = lesson.attendance_records.select_related('child').filter(occurrence_date=occ_date)
        serializer = AttendanceSerializer(attendance_records, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def mark_attendance(self, request, pk=None):
        """
        Mark attendance for students in a lesson.
        Accepts a list of {child_id, status} objects.
        
        Only managers and the assigned instructor can mark attendance.
        Disabled for cancelled lessons.
        """
        lesson = self.get_object()
        
        occ_date = self._parse_occurrence_date(request.data.get('date') or request.query_params.get('date'))
        if (request.data.get('date') or request.query_params.get('date')) and not occ_date:
            return Response({'error': 'תאריך לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        # For recurring lessons, attendance must be per occurrence date
        if lesson.is_recurring and not occ_date:
            return Response({'error': 'חסר שדה date לסימון נוכחות בשיעור חוזר'}, status=status.HTTP_400_BAD_REQUEST)

        if not lesson.is_recurring and occ_date is None:
            occ_date = lesson.lesson_date

        # Check if occurrence is cancelled
        if lesson.is_recurring and occ_date:
            if LessonCancellation.objects.filter(lesson=lesson, occurrence_date=occ_date).exists():
                return Response({'error': 'לא ניתן לסמן נוכחות בשיעור מבוטל'}, status=status.HTTP_400_BAD_REQUEST)

        # Non-recurring legacy cancelled
        if (not lesson.is_recurring) and lesson.status == 'cancelled':
            return Response({'error': 'לא ניתן לסמן נוכחות בשיעור מבוטל'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate request data
        attendance_list = request.data.get('attendance', [])
        if not isinstance(attendance_list, list):
            return Response(
                {'error': 'נדרש מערך של נוכחות'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Mark attendance for each student
        results = []
        for item in attendance_list:
            serializer = AttendanceMarkSerializer(data=item)
            if serializer.is_valid():
                child_id = serializer.validated_data['child_id']
                attendance_status = serializer.validated_data['status']
                
                # Get previous status if exists
                try:
                    previous_attendance = LessonAttendance.objects.get(
                        lesson=lesson,
                        child_id=child_id,
                        occurrence_date=occ_date
                    )
                    previous_status = previous_attendance.status
                except LessonAttendance.DoesNotExist:
                    previous_status = None
                
                # Update or create attendance record
                attendance, created = LessonAttendance.objects.update_or_create(
                    lesson=lesson,
                    child_id=child_id,
                    occurrence_date=occ_date,
                    defaults={'status': attendance_status}
                )
                
                # Handle absence tracking
                from apps.enrollments.models import ChildAbsence
                from apps.customers.models import Child
                from datetime import timedelta
                
                child = Child.objects.get(id=child_id)
                
                if attendance_status == 'absent':
                    # Create ChildAbsence record
                    ChildAbsence.objects.get_or_create(
                        child=child,
                        lesson=lesson,
                        occurrence_date=occ_date,
                        defaults={'course': lesson.course}
                    )
                    
                    # Check for irregular absence pattern (3 absences with < 8 days between them)
                    recent_absences = ChildAbsence.objects.filter(
                        child=child
                    ).order_by('-occurrence_date')[:3]
                    
                    if recent_absences.count() >= 3:
                        absence_dates = [abs.occurrence_date for abs in recent_absences]
                        # Check if any 2 consecutive absences have < 8 days gap
                        irregular = False
                        for i in range(len(absence_dates) - 1):
                            days_diff = abs((absence_dates[i] - absence_dates[i + 1]).days)
                            if days_diff < 8:
                                irregular = True
                                break
                        
                        if irregular:
                            child.absent_irregularly = True
                            child.save(update_fields=['absent_irregularly'])
                
                elif attendance_status == 'present':
                    # If changing from absent to present, delete the ChildAbsence record
                    if previous_status == 'absent':
                        ChildAbsence.objects.filter(
                            child=child,
                            lesson=lesson,
                            occurrence_date=occ_date
                        ).delete()
                    
                    # Reset absent_irregularly flag when child is present
                    if child.absent_irregularly:
                        child.absent_irregularly = False
                        child.save(update_fields=['absent_irregularly'])
                
                results.append({
                    'child_id': str(child_id),
                    'status': attendance_status,
                    'success': True
                })
            else:
                results.append({
                    'error': serializer.errors,
                    'success': False
                })
        
        return Response({'results': results})


class ScheduleEventViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing schedule events.
    
    - Managers can create, update, and delete events
    - Workers can view events
    """
    permission_classes = [IsAuthenticated]
    queryset = ScheduleEvent.objects.select_related('branch', 'studio').all()
    
    def get_serializer_class(self):
        """Use detailed serializer for create/update/retrieve, simple for list"""
        if self.action in ['create', 'update', 'partial_update', 'retrieve']:
            return ScheduleEventSerializer
        return ScheduleEventListSerializer
    
    def get_permissions(self):
        """Only managers can create, update, or delete events"""
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsManager()]
        return [IsAuthenticated()]
    
    def get_queryset(self):
        """Filter events based on query parameters and user role"""
        from apps.core.models import UserProfile
        
        queryset = super().get_queryset()
        
        # Role-based filtering: workers see only their assigned events
        user = self.request.user
        try:
            user_role = user.profile.role
            if user_role == UserProfile.ROLE_WORKER:
                # Filter events where user's email matches an assigned instructor's email
                from apps.instructors.models import Instructor
                instructor = Instructor.objects.filter(email__iexact=user.email).first()
                if instructor:
                    queryset = queryset.filter(assigned_instructors=instructor)
                else:
                    # No instructor match, show no events
                    queryset = queryset.none()
        except (UserProfile.DoesNotExist, AttributeError):
            pass
        
        # Date range filtering
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        branch_id = self.request.query_params.get('branch_id')
        city_id = self.request.query_params.get('city_id')
        
        # For date filtering, we need special logic for weekly events
        if start_date or end_date:
            try:
                start = date_cls.fromisoformat(start_date) if start_date else None
                end = date_cls.fromisoformat(end_date) if end_date else None
                
                # Build Q object for date filtering
                date_q = Q()
                
                # One-time events: must be within date range
                one_time_q = Q(event_type='one_time')
                if start:
                    one_time_q &= Q(event_date__gte=start)
                if end:
                    one_time_q &= Q(event_date__lte=end)
                
                # Weekly events: include if they started before or during the range
                weekly_q = Q(event_type='weekly')
                if end:
                    # Only exclude weekly events that start after the end date
                    weekly_q &= Q(event_date__lte=end)
                # Don't filter by start date for weekly events - they repeat into the future
                
                date_q = one_time_q | weekly_q
                queryset = queryset.filter(date_q)
            except ValueError:
                pass
        
        if branch_id:
            queryset = queryset.filter(Q(branch_id=branch_id) | Q(branch__isnull=True))
        
        if city_id:
            queryset = queryset.filter(Q(city_id=city_id) | Q(city__isnull=True))
        
        return queryset.filter(is_active=True)
    
    def list(self, request, *args, **kwargs):
        """
        List events with optional date range expansion for weekly events.
        Similar to lesson expansion logic.
        """
        queryset = self.get_queryset()
        
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        # If both dates provided, expand weekly events
        if start_date and end_date:
            try:
                start = date_cls.fromisoformat(start_date)
                end = date_cls.fromisoformat(end_date)
            except ValueError:
                return Response({'error': 'תאריכים לא תקינים'}, status=status.HTTP_400_BAD_REQUEST)
            
            expanded = []
            for event in queryset:
                if event.event_type == 'one_time':
                    # One-time events: include as-is if in range
                    if start <= event.event_date <= end:
                        data = ScheduleEventListSerializer(event).data
                        expanded.append(data)
                elif event.event_type == 'weekly':
                    # Weekly events: generate occurrences
                    start_from = max(start, event.event_date)
                    
                    # Get day of week from event_date
                    event_weekday = event.event_date.weekday()
                    days_ahead = (event_weekday - start_from.weekday()) % 7
                    first_occ = start_from + timedelta(days=days_ahead)
                    
                    occ = first_occ
                    while occ <= end:
                        data = ScheduleEventListSerializer(event).data
                        data['event_date'] = occ.isoformat()
                        expanded.append(data)
                        occ = occ + timedelta(days=7)
            
            # Sort by date and time
            expanded.sort(key=lambda x: (
                x.get('event_date') or '',
                x.get('start_time') or '00:00:00'
            ))
            return Response(expanded)
        
        # No range: return as stored
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
