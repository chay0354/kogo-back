from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from django.db.models import Count, Q, Prefetch
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from apps.core.models import City, Branch, Room, BranchFile
from apps.core.permissions import IsManager
from apps.core.serializers import (
    CitySerializer, BranchSerializer, BranchListSerializer, 
    BranchDetailSerializer, BranchWithStatsSerializer,
    RoomSerializer, BranchFileSerializer
)


class CityViewSet(viewsets.ModelViewSet):
    """
    USAGE: Registered at /api/v1/core/cities/
    USAGE: Used by frontend for city selection in branch forms
    """
    queryset = City.objects.all()
    serializer_class = CitySerializer
    permission_classes = [IsAuthenticated, IsManager]


class BranchViewSet(viewsets.ModelViewSet):
    """
    USAGE: Available at /api/v1/core/branches/ endpoint
    USAGE: Supports list with statistics, detail view, create, update, delete
    """
    queryset = Branch.objects.filter(is_active=True).select_related('city')
    permission_classes = [IsAuthenticated, IsManager]
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            # Check if simple list is requested (for dropdowns)
            if self.request.query_params.get('simple') == 'true':
                return BranchListSerializer
            # Check if statistics are requested
            if self.request.query_params.get('with_stats') == 'true':
                return BranchWithStatsSerializer
            return BranchSerializer
        elif self.action == 'retrieve':
            return BranchDetailSerializer
        return BranchSerializer
    
    def get_queryset(self):
        """Optimize queryset based on action"""
        queryset = super().get_queryset()
        
        if self.action == 'list' and self.request.query_params.get('with_stats') == 'true':
            # Annotate with counts for statistics
            queryset = queryset.annotate(
                families_count=Count('families', distinct=True),
                courses_count=Count('courses', filter=Q(courses__is_active=True), distinct=True),
                instructors_count=Count('primary_instructors', filter=Q(primary_instructors__is_active=True), distinct=True),
                rooms_count=Count('rooms', filter=Q(rooms__is_active=True), distinct=True)
            )
        elif self.action == 'retrieve':
            # Prefetch related data for detail view
            queryset = queryset.prefetch_related(
                Prefetch('rooms', queryset=Room.objects.filter(is_active=True))
            )
        
        return queryset
    
    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """
        Get detailed statistics for a branch
        GET /api/v1/core/branches/{id}/statistics/
        """
        branch = self.get_object()
        
        # Count families
        families_count = branch.families.count()
        
        # Count active courses (via lessons, not course.branch, since course.branch is optional)
        courses_count = branch.lessons.filter(
            status='scheduled',
            course__is_active=True
        ).values('course').distinct().count()
        
        # Count lessons
        lessons_count = branch.lessons.filter(status='scheduled').count()
        
        # Count active instructors (deduplicated: primary + assigned)
        # Use set union to avoid counting same instructor twice if they're both primary AND assigned
        primary_ids = set(branch.primary_instructors.filter(is_active=True).values_list('id', flat=True))
        assigned_ids = set(branch.instructor_assignments.filter(
            instructor__is_active=True
        ).values_list('instructor_id', flat=True))
        instructors_count = len(primary_ids | assigned_ids)
        
        # Count active rooms
        rooms_count = branch.rooms.filter(is_active=True).count()
        
        # Count products (if available)
        products_count = 0
        try:
            if hasattr(branch, 'store_products'):
                products_count = branch.store_products.count()
        except Exception:
            # Store app might not be installed or migrations not run
            products_count = 0
        
        # Count active students (children with active lesson enrollments in this branch)
        from apps.enrollments.models import LessonEnrollment
        active_students = LessonEnrollment.objects.filter(
            lesson__branch=branch,
            status='active'
        ).values('child').distinct().count()
        
        # Calculate monthly revenue from actual collected payments
        from apps.core.revenue_service import RevenueService
        from apps.instructors.utils import _parse_month_str, _month_start_end

        today = timezone.now().date()
        month_str = today.strftime('%Y-%m')
        year, m = _parse_month_str(month_str)
        month_start, month_end = _month_start_end(year, m)

        revenue_service = RevenueService()
        monthly_revenue = revenue_service.get_branch_revenue(
            branch_id=str(branch.id),
            start_date=month_start,
            end_date=month_end
        )
        
        # Calculate monthly costs
        monthly_costs = 0
        if branch.monthly_cost:
            monthly_costs += float(branch.monthly_cost)
        if branch.cleaning_cost:
            monthly_costs += float(branch.cleaning_cost)
        
        # Calculate instructor costs:
        # Current-month dynamic salary cost for this branch (calendar-month accurate, cancellations-aware),
        # aligned with the /instructors list "שכר" calculation.
        from apps.instructors.utils import calculate_branch_instructor_costs_for_month
        effective_end = today - timedelta(days=1)
        instructor_costs = calculate_branch_instructor_costs_for_month(branch, month_str, effective_end=effective_end)
        monthly_costs += float(instructor_costs)
        
        # Calculate profit
        profit = float(monthly_revenue) - monthly_costs
        
        return Response({
            'branch_id': str(branch.id),
            'branch_name': branch.name,
            'families_count': families_count,
            'courses_count': courses_count,
            'lessons_count': lessons_count,
            'instructors_count': instructors_count,
            'rooms_count': rooms_count,
            'products_count': products_count,
            'active_students': active_students,
            'monthly_revenue': float(monthly_revenue),
            'monthly_costs': monthly_costs,
            'revenue_calculation_method': 'full_price_no_discounts',
            'profit': profit,
        })
    
    def perform_destroy(self, instance):
        """Soft delete - set is_active to False, but only if no active relationships"""
        from apps.courses.models import Course, Lesson
        from apps.customers.models import Child, Family
        
        # Check for active courses (via lessons since course.branch is optional)
        active_courses = Lesson.objects.filter(
            branch=instance,
            status='scheduled',
            course__is_active=True
        ).values('course').distinct().exists()
        
        if active_courses:
            return Response(
                {'error': 'לא ניתן למחוק סניף עם חוגים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check for active lessons
        active_lessons = Lesson.objects.filter(
            branch=instance,
            status='scheduled'
        ).exists()
        
        if active_lessons:
            return Response(
                {'error': 'לא ניתן למחוק סניף עם שיעורים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check for active children
        active_children = Child.objects.filter(
            family__branch=instance,
            status='active'
        ).exists()
        
        if active_children:
            return Response(
                {'error': 'לא ניתן למחוק סניף עם ילדים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check for families
        families = Family.objects.filter(branch=instance).exists()
        
        if families:
            return Response(
                {'error': 'לא ניתן למחוק סניף עם משפחות רשומות'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Proceed with soft delete
        instance.is_active = False
        instance.save()


class RoomViewSet(viewsets.ModelViewSet):
    """
    USAGE: Registered at /api/v1/core/rooms/
    USAGE: Supports filtering by branch_id
    """
    queryset = Room.objects.filter(is_active=True).select_related('branch')
    serializer_class = RoomSerializer
    permission_classes = [IsAuthenticated, IsManager]
    
    def get_queryset(self):
        """Filter by branch if branch_id is provided"""
        queryset = super().get_queryset()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        return queryset
    
    def perform_destroy(self, instance):
        """Soft delete - set is_active to False"""
        instance.is_active = False
        instance.save()


class BranchFileViewSet(viewsets.ModelViewSet):
    """
    USAGE: Registered at /api/v1/core/branch-files/
    USAGE: Handles file uploads for branches
    """
    queryset = BranchFile.objects.all().select_related('branch')
    serializer_class = BranchFileSerializer
    parser_classes = (MultiPartParser, FormParser)
    permission_classes = [IsAuthenticated, IsManager]
    
    def get_queryset(self):
        """Filter by branch if branch_id is provided"""
        queryset = super().get_queryset()
        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        return queryset
    
    def destroy(self, request, *args, **kwargs):
        """Delete file from storage and database"""
        instance = self.get_object()
        
        # Delete the file from storage
        if instance.file:
            instance.file.delete(save=False)
        
        # Delete the database record
        instance.delete()
        
        return Response(status=status.HTTP_204_NO_CONTENT)
