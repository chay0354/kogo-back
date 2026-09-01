from datetime import timedelta

from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Prefetch, Count, Q, Sum
from apps.courses.models import CourseType, Course, Lesson, LessonBundle, LessonPriceOption
from apps.courses.serializers import (
    CourseTypeSerializer,
    CourseTypeWithStatsSerializer,
    CourseTypeDetailsSerializer,
    CourseSerializer,
    CourseWithLessonsSerializer,
    LessonWithEnrollmentsSerializer,
    LessonSerializer,
    LessonBundleSerializer,
    LessonPriceOptionSerializer,
    CourseListSerializer,
    LessonListSerializer
)
from apps.instructors.models import Instructor
from apps.core.models import LessonMonthlySnapshot
from apps.enrollments.models import LessonEnrollment
from apps.core.permissions import IsManager, IsManagerOrPartner, StaffAccessMixin
from apps.core.scoping import (
    scope_courses,
    is_scoped_partner,
    partner_branch_ids,
    is_scoped_instructor,
    instructor_course_ids,
)
from apps.enrollments.enrollment_counts import TRIAL_CHILD_STATUSES


def _course_type_stats_q(user):
    """Build scoped Q filters for course-type stat annotations."""
    course_q = Q(courses__is_active=True)
    lesson_q = course_q & Q(courses__lessons__is_recurring=True)
    enrollment_q = (
        course_q
        & Q(courses__lessons__enrollments__status='active')
        & ~Q(courses__lessons__enrollments__child__status__in=TRIAL_CHILD_STATUSES)
    )

    if is_scoped_partner(user):
        branch_ids = partner_branch_ids(user)
        if not branch_ids:
            return None
        branch_q = Q(courses__branch_id__in=branch_ids)
        course_q &= branch_q
        lesson_q &= branch_q
        enrollment_q &= branch_q
    elif is_scoped_instructor(user):
        course_ids = instructor_course_ids(user)
        if not course_ids:
            return None
        id_q = Q(courses__id__in=course_ids)
        course_q &= id_q
        lesson_q &= id_q
        enrollment_q &= id_q

    return course_q, lesson_q, enrollment_q


class CourseTypeViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing course types (תחומים)
    
    Endpoints:
    - GET /api/courses/types/ - List all active course types with stats
    - POST /api/courses/types/ - Create new course type
    - GET /api/courses/types/:id/ - Get single course type
    - PATCH /api/courses/types/:id/ - Update course type
    - DELETE /api/courses/types/:id/ - Soft delete (set is_active=False)
    - GET /api/courses/types/:id/details/ - Get detailed view with courses and lessons
    """
    queryset = CourseType.objects.filter(is_active=True).order_by('name')
    pagination_class = None  # Disable pagination for simpler responses
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            return CourseTypeWithStatsSerializer
        elif self.action == 'details':
            return CourseTypeDetailsSerializer
        return CourseTypeSerializer
    
    def get_queryset(self):
        """Optimize queryset based on action"""
        queryset = super().get_queryset()

        nested_courses = Course.objects.filter(is_active=True).select_related(
            'branch', 'instructor'
        ).prefetch_related('instructor__salary_tiers')

        if is_scoped_partner(self.request.user):
            branch_ids = partner_branch_ids(self.request.user)
            if not branch_ids:
                return queryset.none()
            queryset = queryset.filter(courses__branch_id__in=branch_ids, courses__is_active=True).distinct()
            # Partners must only see groups (courses/lessons) at their own branches
            nested_courses = nested_courses.filter(branch_id__in=branch_ids)

        if self.action == 'list':
            stats_q = _course_type_stats_q(self.request.user)
            if stats_q is None:
                return queryset.none()
            course_q, lesson_q, enrollment_q = stats_q
            visible_courses = scope_courses(
                Course.objects.filter(is_active=True),
                self.request.user,
            ).select_related('branch')
            queryset = queryset.annotate(
                courses_count=Count('courses', filter=course_q, distinct=True),
                lessons_count=Count('courses__lessons', filter=lesson_q, distinct=True),
                students_count=Count('courses__lessons__enrollments', filter=enrollment_q),
            ).prefetch_related(
                Prefetch('courses', queryset=visible_courses),
            )
        elif self.action == 'details':
            # Course-level fields only — lessons are fetched lazily per course via
            # CourseViewSet.lessons_detail, not nested here (see that action's
            # docstring for why: this used to prefetch every lesson + every active
            # enrollment for every course in the type in one request).
            queryset = queryset.prefetch_related(
                Prefetch('courses', queryset=nested_courses)
            )
        
        return queryset
    
    def destroy(self, request, *args, **kwargs):
        """Soft delete: set is_active to False instead of deleting"""
        instance = self.get_object()
        instance.is_active = False
        instance.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=True, methods=['get'])
    def details(self, request, pk=None):
        """
        Get detailed, course-level (not lesson-level) view of a course type.

        Lessons are NOT nested here — fetch them per course, on demand, via
        CourseViewSet.lessons_detail. This used to prefetch every lesson and every
        active enrollment for every course in the type and tally them in a Python
        loop; for a type with 100+ courses that was O(all enrollments in the type)
        done twice (DB fetch + Python aggregation), which is what made this
        endpoint take 30+ seconds. Course-level counts/financials below are each a
        single batched query instead, following the same pattern already used in
        CourseViewSet.list().
        """
        course_type = self.get_object()
        course_ids = [c.id for c in course_type.courses.all()]

        course_enrollment_counts = dict(
            LessonEnrollment.objects.filter(
                status='active', lesson__course_id__in=course_ids,
            )
            .values_list('lesson__course_id')
            .annotate(c=Count('child_id', distinct=True))
            .values_list('lesson__course_id', 'c')
        )
        lessons_counts = dict(
            Lesson.objects.filter(course_id__in=course_ids)
            .values_list('course_id')
            .annotate(c=Count('id'))
            .values_list('course_id', 'c')
        )

        current_month = timezone.now().strftime('%Y-%m')
        course_financials = {
            row['course_id']: row
            for row in LessonMonthlySnapshot.objects.filter(
                course_id__in=course_ids, month=current_month,
            )
            .values('course_id')
            .annotate(
                monthly_revenue=Sum('revenue'),
                monthly_salary=Sum('instructor_salary'),
                monthly_profit=Sum('profit'),
            )
        }

        serializer = self.get_serializer(
            course_type,
            context={
                **self.get_serializer_context(),
                'course_enrollment_counts': course_enrollment_counts,
                'lessons_counts': lessons_counts,
                'course_financials': course_financials,
            },
        )
        return Response(serializer.data)


class CourseViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing courses (חוגים)
    
    USAGE: Used by frontend in multiple places:
    - frontend/src/components/dialogs/EnrollToLessonDialog.tsx
    - frontend/src/app/customers/page.tsx
    - frontend/src/app/courses/page.tsx
    """
    serializer_class = CourseSerializer
    pagination_class = None  # Disable pagination
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    
    def get_queryset(self):
        """Filter active courses, optionally by course type or branch"""
        queryset = Course.objects.filter(is_active=True).select_related(
            'course_type', 'branch', 'instructor'
        ).prefetch_related('managers', 'instructor__salary_tiers')
        queryset = scope_courses(queryset, self.request.user)

        course_type_id = self.request.query_params.get('course_type', None)
        if course_type_id:
            queryset = queryset.filter(course_type_id=course_type_id)
        
        branch_id = self.request.query_params.get('branch_id', None)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset.order_by('course_type', 'name')

    def list(self, request, *args, **kwargs):
        """List with batched per-course counts (avoids per-course queries against a remote DB)."""
        from django.db.models import Count
        from apps.enrollments.enrollment_counts import paying_enrollments

        courses = list(self.filter_queryset(self.get_queryset()))
        course_ids = [c.id for c in courses]

        lessons_counts = dict(
            Lesson.objects.filter(course_id__in=course_ids, status='scheduled')
            .values_list('course_id')
            .annotate(c=Count('id'))
            .values_list('course_id', 'c')
        )
        paying_children_counts = dict(
            paying_enrollments()
            .filter(lesson__course_id__in=course_ids)
            .values_list('lesson__course_id')
            .annotate(c=Count('child_id', distinct=True))
            .values_list('lesson__course_id', 'c')
        )

        context = self.get_serializer_context()
        context['lessons_counts'] = lessons_counts
        context['paying_children_counts'] = paying_children_counts
        serializer = CourseSerializer(courses, many=True, context=context)
        return Response(serializer.data)
    
    def destroy(self, request, *args, **kwargs):
        """Soft delete: set is_active to False, but only if no active lessons"""
        instance = self.get_object()
        
        # Check if course has any active lessons
        active_lessons = Lesson.objects.filter(course=instance, status='scheduled').exists()
        if active_lessons:
            return Response(
                {'error': 'לא ניתן למחוק חוג עם שיעורים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Proceed with soft delete
        instance.is_active = False
        instance.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def lessons_detail(self, request, pk=None):
        """
        Lessons (with room/instructor/enrollment detail) for a single course.

        Lazy-load counterpart to CourseTypeViewSet.details(), which no longer
        nests this data for every course in a type — the frontend fetches it
        here only when a specific course's row is expanded (or an edit/add-lesson/
        bundles dialog for that course is opened).
        """
        course = self.get_object()
        lessons = course.lessons.select_related('room', 'instructor').prefetch_related(
            'instructor__salary_tiers',
            Prefetch('enrollments', queryset=LessonEnrollment.objects.filter(status='active')),
        )
        serializer = LessonWithEnrollmentsSerializer(lessons, many=True, context=self.get_serializer_context())
        return Response(serializer.data)


class LessonViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing lessons (שיעורים)
    
    USAGE: Available at /api/courses/lessons/
    """
    serializer_class = LessonSerializer
    pagination_class = None  # Disable pagination
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    
    def get_queryset(self):
        """Filter lessons, optionally by course, instructor, room"""
        queryset = Lesson.objects.select_related(
            'course', 'course__branch', 'course__course_type', 'room', 'instructor'
        )
        queryset = scope_courses(queryset, self.request.user, 'course')

        course_id = self.request.query_params.get('course', None)
        if course_id:
            queryset = queryset.filter(course_id=course_id)

        instructor_id = self.request.query_params.get('instructor_id', None)
        if instructor_id:
            queryset = queryset.filter(instructor_id=instructor_id)
        
        room_id = self.request.query_params.get('room_id', None)
        if room_id:
            queryset = queryset.filter(room_id=room_id)
        
        status_filter = self.request.query_params.get('status', None)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        else:
            # Default to scheduled lessons
            queryset = queryset.filter(status='scheduled')
        
        is_recurring = self.request.query_params.get('is_recurring', None)
        if is_recurring is not None:
            queryset = queryset.filter(is_recurring=is_recurring.lower() == 'true')
        
        return queryset.order_by('day_of_week', 'start_time')

    def list(self, request, *args, **kwargs):
        """List with batched per-lesson enrollment counts (avoids one query per lesson)."""
        from django.db.models import Count
        from apps.enrollments.enrollment_counts import paying_enrollments

        lessons = list(self.filter_queryset(self.get_queryset()))
        lesson_ids = [lesson.id for lesson in lessons]
        enrollment_counts = dict(
            paying_enrollments()
            .filter(lesson_id__in=lesson_ids)
            .values_list('lesson_id')
            .annotate(c=Count('id'))
            .values_list('lesson_id', 'c')
        )

        context = self.get_serializer_context()
        context['paying_enrollment_counts'] = enrollment_counts
        serializer = LessonSerializer(lessons, many=True, context=context)
        return Response(serializer.data)

    def _next_occurrence_date(self, day_of_week: int, start_from=None):
        """
        Compute the next date (>= start_from) that matches our day_of_week mapping.
        Our model uses: 0=Sunday ... 6=Saturday.
        Python date.weekday(): 0=Monday ... 6=Sunday.
        """
        if start_from is None:
            start_from = timezone.now().date()

        # Convert to python weekday: Sunday(0)->6, Monday(1)->0, ..., Saturday(6)->5
        target_py_weekday = (day_of_week - 1) % 7
        days_ahead = (target_py_weekday - start_from.weekday()) % 7
        return start_from + timedelta(days=days_ahead)

    @action(detail=False, methods=['get'], url_path='room-conflicts')
    def room_conflicts(self, request):
        """Occupied studio slots for a CRM warning — does not block saving."""
        from datetime import datetime

        from apps.scheduling.studio_conflict import list_studio_occupants

        room_id = (request.query_params.get('room') or '').strip()
        day_raw = request.query_params.get('day_of_week')
        start_raw = (request.query_params.get('start_time') or '').strip()
        end_raw = (request.query_params.get('end_time') or '').strip()
        exclude_course = (request.query_params.get('exclude_course') or '').strip() or None
        exclude_raw = request.query_params.getlist('exclude')
        exclude_ids = []
        for chunk in exclude_raw:
            exclude_ids.extend(part for part in str(chunk).split(',') if part.strip())

        def parse_clock(value):
            for fmt in ('%H:%M:%S', '%H:%M'):
                try:
                    return datetime.strptime(value, fmt).time()
                except ValueError:
                    continue
            return None

        try:
            day_of_week = int(day_raw)
        except (TypeError, ValueError):
            return Response({'conflicts': []})

        start_time = parse_clock(start_raw)
        end_time = parse_clock(end_raw)
        if not room_id or start_time is None or end_time is None:
            return Response({'conflicts': []})

        return Response({
            'conflicts': list_studio_occupants(
                room_id=room_id,
                day_of_week=day_of_week,
                start_time=start_time,
                end_time=end_time,
                exclude_lesson_ids=exclude_ids or None,
                exclude_course_id=exclude_course,
            )
        })

    def perform_create(self, serializer):
        instance = serializer.save()

        # Ensure recurring lessons have a "start date" for schedule visibility.
        if instance.is_recurring and not instance.lesson_date:
            instance.lesson_date = self._next_occurrence_date(instance.day_of_week)
            instance.save(update_fields=['lesson_date'])

    def perform_update(self, serializer):
        instance = serializer.save()

        # Keep start date aligned for recurring lessons
        if instance.is_recurring:
            # If day_of_week changed or lesson_date missing, reset start date to next occurrence from today
            if not instance.lesson_date or 'day_of_week' in getattr(serializer, 'validated_data', {}):
                instance.lesson_date = self._next_occurrence_date(instance.day_of_week)
                instance.save(update_fields=['lesson_date'])

    def destroy(self, request, *args, **kwargs):
        """Delete lesson only if no active children are enrolled"""
        instance = self.get_object()
        
        # Check if lesson has any active enrollments
        active_enrollments = LessonEnrollment.objects.filter(
            lesson=instance, 
            status='active'
        ).exists()
        
        if active_enrollments:
            return Response(
                {'error': 'לא ניתן למחוק שיעור עם תלמידים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Proceed with deletion
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LessonBundleViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing lesson bundles (מסלולים משולבים) — combined-price
    packages of 2+ lessons belonging to the same course.

    USAGE: /api/courses/bundles/?course=<id> — used by the admin
    ManageLessonBundlesDialog and read by the widget/EnrollToLessonDialog.
    """
    serializer_class = LessonBundleSerializer
    pagination_class = None
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get_queryset(self):
        queryset = LessonBundle.objects.select_related('course').prefetch_related(
            'lessons', 'lessons__instructor', 'lessons__room'
        )
        queryset = scope_courses(queryset, self.request.user, 'course')

        course_id = self.request.query_params.get('course', None)
        if course_id:
            queryset = queryset.filter(course_id=course_id)

        if self.request.query_params.get('is_active', None) is not None:
            is_active = self.request.query_params['is_active'].lower() == 'true'
            queryset = queryset.filter(is_active=is_active)

        return queryset.order_by('name')

    def destroy(self, request, *args, **kwargs):
        """Soft delete: set is_active to False instead of deleting, to preserve billing history."""
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class LessonPriceOptionViewSet(viewsets.ModelViewSet):
    """
    Extra catalog prices for a single lesson — same slot, different widget title/price.
    """
    serializer_class = LessonPriceOptionSerializer
    pagination_class = None
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get_queryset(self):
        queryset = LessonPriceOption.objects.select_related('lesson', 'lesson__course')
        queryset = scope_courses(queryset, self.request.user, 'lesson__course')

        lesson_id = self.request.query_params.get('lesson')
        if lesson_id:
            queryset = queryset.filter(lesson_id=lesson_id)

        if self.request.query_params.get('is_active') is not None:
            is_active = self.request.query_params['is_active'].lower() == 'true'
            queryset = queryset.filter(is_active=is_active)

        return queryset.order_by('sort_order', 'display_title')

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        return Response(status=status.HTTP_204_NO_CONTENT)


# Legacy viewsets for backward compatibility
class CourseListViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Simple read-only viewset for courses (legacy)
    Maintained for backward compatibility with existing frontend code
    """
    queryset = Course.objects.filter(is_active=True).select_related('course_type', 'branch')
    serializer_class = CourseListSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get_queryset(self):
        return scope_courses(super().get_queryset(), self.request.user)


class LessonListViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Simple read-only viewset for lessons (legacy)
    Maintained for backward compatibility
    """
    queryset = Lesson.objects.filter(status='scheduled').select_related('course')
    serializer_class = LessonListSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get_queryset(self):
        return scope_courses(super().get_queryset(), self.request.user, 'course')
