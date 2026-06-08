from datetime import timedelta

from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Prefetch
from apps.courses.models import CourseType, Course, Lesson
from apps.courses.serializers import (
    CourseTypeSerializer, 
    CourseTypeWithStatsSerializer,
    CourseTypeDetailsSerializer,
    CourseSerializer,
    CourseWithLessonsSerializer,
    LessonSerializer,
    CourseListSerializer,
    LessonListSerializer
)
from apps.instructors.models import Instructor
from apps.enrollments.models import LessonEnrollment
from apps.core.permissions import IsManager, IsManagerOrPartner, StaffAccessMixin
from apps.core.scoping import scope_courses, is_scoped_partner, partner_branch_ids


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
        if is_scoped_partner(self.request.user):
            branch_ids = partner_branch_ids(self.request.user)
            if not branch_ids:
                return queryset.none()
            queryset = queryset.filter(courses__branch_id__in=branch_ids, courses__is_active=True).distinct()

        nested_courses = Course.objects.filter(is_active=True).select_related(
            'branch', 'instructor'
        ).prefetch_related('instructor__salary_tiers')

        if self.action == 'details':
            # Prefetch all related data for details view
            queryset = queryset.prefetch_related(
                Prefetch(
                    'courses',
                    queryset=nested_courses.prefetch_related(
                        Prefetch(
                            'lessons',
                            queryset=Lesson.objects.select_related(
                                'room', 'instructor'
                            ).prefetch_related(
                                'instructor__salary_tiers',
                                Prefetch(
                                    'enrollments',
                                    queryset=LessonEnrollment.objects.filter(status='active')
                                )
                            )
                        )
                    )
                )
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
        Get detailed view of course type with all courses and lessons
        Includes instructor salary tiers for financial calculations
        """
        course_type = self.get_object()
        serializer = self.get_serializer(course_type)
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
        )
        queryset = scope_courses(queryset, self.request.user)

        course_type_id = self.request.query_params.get('course_type', None)
        if course_type_id:
            queryset = queryset.filter(course_type_id=course_type_id)
        
        branch_id = self.request.query_params.get('branch_id', None)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset.order_by('course_type', 'name')
    
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
            'course', 'course__branch', 'room', 'instructor'
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
