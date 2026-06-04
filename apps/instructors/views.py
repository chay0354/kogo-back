from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Prefetch, Count
from django.utils import timezone
from datetime import datetime, date, timedelta
from decimal import Decimal

from apps.instructors.models import Instructor, InstructorBonus
from apps.instructors.serializers import (
    InstructorListSerializer, InstructorDetailSerializer,
    InstructorCreateUpdateSerializer, InstructorBonusSerializer
)
from apps.instructors.utils import (
    calculate_instructor_monthly_metrics, calculate_lesson_profitability
)
from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment
from apps.core.permissions import IsManager
from apps.core.scoping import is_scoped_manager, assigned_course_ids
from apps.scheduling.models import LessonCancellation
from apps.instructors.utils import calculate_lesson_salary_with_override


class InstructorViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Instructor CRUD and financial calculations
    
    USAGE: Available at /api/v1/instructors/
    USAGE: Used by frontend/src/app/instructors/page.tsx
    """
    queryset = Instructor.objects.filter(is_active=True).select_related('primary_branch').prefetch_related(
        'branch_assignments__branch',
        'salary_tiers',
        'bonuses'
    )
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['first_name', 'last_name', 'phone', 'email', 'specialization']
    ordering_fields = ['first_name', 'last_name', 'created_at']
    ordering = ['last_name', 'first_name']
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            return InstructorListSerializer
        elif self.action == 'retrieve':
            return InstructorDetailSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return InstructorCreateUpdateSerializer
        return InstructorListSerializer
    
    def get_queryset(self):
        """Apply filters to queryset"""
        queryset = super().get_queryset()

        # Scoped managers: only instructors who teach one of their assigned courses.
        if is_scoped_manager(self.request.user):
            course_ids = assigned_course_ids(self.request.user)
            instr_ids = (
                Lesson.objects.filter(course_id__in=course_ids)
                .exclude(instructor__isnull=True)
                .values_list('instructor_id', flat=True)
                .distinct()
            )
            queryset = queryset.filter(id__in=instr_ids)
        
        # Filter by branch
        branch_id = self.request.query_params.get('branch')
        if branch_id and branch_id != 'all':
            # Include instructors who:
            # 1. Have this as primary branch
            # 2. Are explicitly assigned to this branch (instructor_branches table)
            # 3. Teach lessons in this branch
            queryset = queryset.filter(
                Q(primary_branch_id=branch_id) | 
                Q(branch_assignments__branch_id=branch_id) |
                Q(lessons__course__branch_id=branch_id)
            ).distinct()
        
        # Note: min_students and max_students filters are applied after metrics calculation in list()
        
        return queryset
    
    def list(self, request, *args, **kwargs):
        """
        List instructors with financial metrics
        
        Query params:
        - search: Search by name, phone, email, specialization
        - branch: Filter by branch ID
        - min_students: Minimum number of students (all, 1, 5, 10, 15, 20)
        - max_students: Maximum number of students (all, 10, 15, 20, 25, 30+)
        - month: Month for financial calculations (YYYY-MM format)
        - simple: If 'true', use simplified approximation (salary_per_lesson × lesson_count × 4)
        """
        queryset = self.filter_queryset(self.get_queryset())

        # Auto-finalize previous month snapshots (idempotent) so ended months become immutable automatically.
        from apps.instructors.utils import ensure_previous_month_finalized_for_all
        ensure_previous_month_finalized_for_all()
        
        # Get filter parameters
        month = request.query_params.get('month', None)
        branch_id = request.query_params.get('branch', None)
        if branch_id == 'all':
            branch_id = None
        
        # Check if simple approximation mode is requested
        use_simple = request.query_params.get('simple', '').lower() == 'true'
        
        # Calculate metrics for each instructor
        instructors_data = []
        for instructor in queryset:
            if use_simple and branch_id:
                # Simplified calculation for branch view: 
                # salary = (instructor salary per lesson) × (lesson count in branch) × 4
                metrics = self._calculate_simple_metrics(instructor, branch_id)
            else:
                # Full calendar-month accurate calculation
                metrics = calculate_instructor_monthly_metrics(instructor, month, branch_id)
            
            # Calculate bonuses for the selected month
            bonuses_amount = Decimal('0.00')
            if month:
                # Filter bonuses by the selected month (YYYY-MM format)
                bonuses = instructor.bonuses.filter(
                    bonus_date__year=int(month.split('-')[0]),
                    bonus_date__month=int(month.split('-')[1])
                )
                bonuses_amount = sum(bonus.amount for bonus in bonuses)
            
            # Serialize instructor with metrics
            serializer = self.get_serializer(instructor)
            instructor_dict = serializer.data
            
            # Add calculated metrics
            instructor_dict.update(metrics)
            instructor_dict['bonuses_amount'] = str(bonuses_amount)
            
            instructors_data.append(instructor_dict)
        
        # Apply student count filters if specified
        min_students = request.query_params.get('min_students')
        max_students = request.query_params.get('max_students')
        
        if min_students and min_students != 'all':
            try:
                min_val = int(min_students)
                instructors_data = [i for i in instructors_data if i['students_count'] >= min_val]
            except ValueError:
                pass
        
        if max_students and max_students != 'all':
            try:
                if max_students.endswith('+'):
                    # For "30+" format, no upper limit
                    pass
                else:
                    max_val = int(max_students)
                    instructors_data = [i for i in instructors_data if i['students_count'] <= max_val]
            except ValueError:
                pass
        
        # Calculate summary statistics
        total_instructors = len(instructors_data)
        total_revenue = sum(Decimal(str(i.get('revenue', '0') or '0')) for i in instructors_data)
        total_salary = sum(Decimal(str(i.get('salary', '0') or '0')) for i in instructors_data)
        total_profit = total_revenue - total_salary
        
        return Response({
            'instructors': instructors_data,
            'summary': {
                'total_instructors': total_instructors,
                'total_revenue': str(total_revenue),
                'total_salary': str(total_salary),
                'total_profit': str(total_profit)
            }
        })
    
    def _calculate_simple_metrics(self, instructor, branch_id):
        """
        Simplified approximation for branch view:
        - Count lessons in branch
        - Salary = instructor fixed salary × lesson count × 4
        - Students = unique children enrolled in those lessons
        """
        from apps.courses.models import Lesson
        from apps.instructors.utils import calculate_lesson_salary
        
        lessons = Lesson.objects.filter(
            instructor=instructor,
            branch_id=branch_id,
            status='scheduled',
            is_recurring=True
        ).prefetch_related('enrollments')
        
        total_lessons = lessons.count()
        unique_students = set()
        total_salary = Decimal('0.00')
        
        for lesson in lessons:
            # Count active + payments_problem for student load
            active_enrollments = lesson.enrollments.filter(status__in=['active', 'payments_problem'])
            student_count = active_enrollments.count()
            
            for enrollment in active_enrollments:
                unique_students.add(enrollment.child_id)
            
            # Calculate per-lesson salary (respects tiers/override)
            per_lesson_salary = calculate_lesson_salary(student_count, instructor)
            if lesson.instructor_salary_override:
                per_lesson_salary = lesson.instructor_salary_override
            
            # Approximate monthly: per-lesson × 4 weeks
            total_salary += per_lesson_salary * Decimal('4')
        
        return {
            'lessons_count': total_lessons,
            'students_count': len(unique_students),
            'revenue': Decimal('0.00'),  # Not calculated in simple mode
            'salary': total_salary,
            'profit': Decimal('0.00'),  # Not calculated in simple mode
            'cancelled_count': 0,
            'avg_attendance_rate': Decimal('0.00'),
            'salary_is_finalized': False,
        }
    
    def retrieve(self, request, *args, **kwargs):
        """
        Retrieve single instructor with detailed financial information
        """
        from apps.core.models import InstructorMonthlySnapshot
        from apps.instructors.serializers import InstructorMonthlySnapshotSerializer
        
        instructor = self.get_object()
        
        # Get month parameter (YYYY-MM). Used for profitability and totals.
        month = request.query_params.get('month', None)
        
        # Calculate overall metrics
        metrics = calculate_instructor_monthly_metrics(instructor, month)
        
        # Get all lessons with profitability
        lessons = Lesson.objects.filter(
            instructor=instructor,
            is_recurring=True
        ).select_related('course', 'course__branch', 'room').prefetch_related('enrollments')
        
        lessons_data = []
        unique_courses = {}
        
        for lesson in lessons:
            lesson_profit = calculate_lesson_profitability(lesson, instructor, month=month)
            lessons_data.append(lesson_profit)
            
            # Track unique courses
            if lesson.course and lesson.course.id not in unique_courses:
                unique_courses[lesson.course.id] = {
                    'id': str(lesson.course.id),
                    'name': lesson.course.name,
                    'course_type': lesson.course.course_type.name if lesson.course.course_type else None
                }
        
        # Get last 6 monthly snapshots
        snapshots = InstructorMonthlySnapshot.objects.filter(
            instructor=instructor
        ).order_by('-month')[:6]
        
        snapshots_serializer = InstructorMonthlySnapshotSerializer(snapshots, many=True)
        
        # Serialize instructor
        serializer = self.get_serializer(instructor, context={
            'lessons': lessons_data,
            'courses': list(unique_courses.values())
        })
        
        instructor_dict = serializer.data
        
        # Add financial metrics
        instructor_dict['total_students'] = metrics['students_count']
        instructor_dict['total_revenue'] = str(metrics['revenue'])
        instructor_dict['total_salary'] = str(metrics['salary'])
        instructor_dict['total_profit'] = str(metrics['profit'])
        
        # Add monthly snapshots
        instructor_dict['monthly_snapshots'] = snapshots_serializer.data
        
        return Response(instructor_dict)
    
    def destroy(self, request, *args, **kwargs):
        """Soft delete instructor only if they have no active lessons"""
        instructor = self.get_object()
        
        # Check if instructor has any active lessons
        has_lessons = Lesson.objects.filter(
            instructor=instructor,
            status='scheduled'
        ).exists()
        
        if has_lessons:
            return Response(
                {'error': 'לא ניתן למחוק מדריך עם שיעורים פעילים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Proceed with soft delete
        instructor.is_active = False
        instructor.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=True, methods=['post'])
    def add_bonus(self, request, pk=None):
        """
        Add a bonus to a specific instructor
        
        POST /api/v1/instructors/{id}/add_bonus/
        Body: {
            "bonus_type": "one_time",
            "amount": 500,
            "bonus_date": "2025-01-01",
            "description": "Performance bonus",
            "notes": "Great work"
        }
        """
        instructor = self.get_object()
        
        # Add instructor to data
        data = request.data.copy()
        data['instructor'] = instructor.id
        
        serializer = InstructorBonusSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def bulk_bonus(self, request):
        """
        Add bonuses to multiple instructors at once
        
        POST /api/v1/instructors/bulk_bonus/
        Body: {
            "instructor_ids": ["uuid1", "uuid2"],
            "bonus_type": "monthly",
            "amount": 300,
            "bonus_date": "2025-01-01",
            "description": "Monthly bonus"
        }
        """
        instructor_ids = request.data.get('instructor_ids', [])
        bonus_type = request.data.get('bonus_type')
        amount = request.data.get('amount')
        bonus_date = request.data.get('bonus_date')
        description = request.data.get('description', '')
        notes = request.data.get('notes', '')
        
        if not instructor_ids or not bonus_type or not amount or not bonus_date:
            return Response({
                'error': 'חסרים שדות נדרשים: instructor_ids, bonus_type, amount, bonus_date'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check for duplicates (same instructor, month, type)
        bonus_month = bonus_date[:7]  # YYYY-MM
        existing_bonuses = InstructorBonus.objects.filter(
            instructor_id__in=instructor_ids,
            bonus_type=bonus_type,
            bonus_date__startswith=bonus_month
        )
        
        if existing_bonuses.exists():
            duplicate_instructors = [b.instructor.full_name for b in existing_bonuses]
            return Response({
                'error': f'בונוס כבר קיים עבור: {", ".join(duplicate_instructors)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create bonuses
        bonuses_created = []
        for instructor_id in instructor_ids:
            bonus_data = {
                'instructor': instructor_id,
                'bonus_type': bonus_type,
                'amount': amount,
                'bonus_date': bonus_date,
                'description': description,
                'notes': notes
            }
            serializer = InstructorBonusSerializer(data=bonus_data)
            if serializer.is_valid():
                bonus = serializer.save()
                bonuses_created.append(serializer.data)
            else:
                # Rollback if any fails
                InstructorBonus.objects.filter(id__in=[b['id'] for b in bonuses_created]).delete()
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        return Response({
            'message': f'נוצרו {len(bonuses_created)} בונוסים בהצלחה',
            'bonuses': bonuses_created
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'])
    def financial_summary(self, request):
        """
        Get aggregated financial summary for all instructors
        
        GET /api/v1/instructors/financial_summary/?month=2025-01
        """
        month = request.query_params.get('month', None)
        
        instructors = self.get_queryset()
        
        total_lessons = 0
        total_students = 0
        total_revenue = Decimal('0.00')
        total_salary = Decimal('0.00')
        total_profit = Decimal('0.00')
        
        for instructor in instructors:
            metrics = calculate_instructor_monthly_metrics(instructor, month)
            total_lessons += metrics['lessons_count']
            total_students += metrics['students_count']
            total_revenue += metrics['revenue']
            total_salary += metrics['salary']
            total_profit += metrics['profit']
        
        return Response({
            'month': month or datetime.now().strftime('%Y-%m'),
            'total_instructors': instructors.count(),
            'total_lessons': total_lessons,
            'total_students': total_students,
            'total_revenue': str(total_revenue),
            'total_salary': str(total_salary),
            'total_profit': str(total_profit)
        })
    
    @action(detail=True, methods=['get'])
    def current_salary(self, request, pk=None):
        """
        Get current month salary for an instructor (dynamically calculated).
        Only occurred lessons (status != 'cancelled' AND lesson_date < today) count.
        
        GET /api/v1/instructors/{id}/current_salary/?year=2025&month=12
        """
        instructor = self.get_object()
        
        # Get year and month from query params (default to current)
        now = timezone.now()
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', now.month))
        
        today = timezone.now().date()

        month_start = date(year, month, 1)
        # Compute month end
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        # For "current" month, only count occurrences that already happened
        effective_end = min(month_end, today - timedelta(days=1))
        if effective_end < month_start:
            effective_end = month_start - timedelta(days=1)

        # 1) Non-recurring lessons: one occurrence on lesson_date
        non_recurring = Lesson.objects.filter(
            instructor=instructor,
            is_recurring=False,
            lesson_date__gte=month_start,
            lesson_date__lte=effective_end,
        ).exclude(status='cancelled').select_related('course').prefetch_related('enrollments')

        # 2) Recurring lessons: count occurrences by weekday in [month_start, effective_end], starting from lesson.lesson_date
        recurring = Lesson.objects.filter(
            instructor=instructor,
            is_recurring=True,
            status='scheduled',
        ).select_related('course').prefetch_related('enrollments')

        # Preload cancellations for this instructor/month range
        cancellations = LessonCancellation.objects.filter(
            lesson__in=recurring,
            occurrence_date__gte=month_start,
            occurrence_date__lte=effective_end,
        )
        cancelled_set = {(c.lesson_id, c.occurrence_date) for c in cancellations}

        def count_weekday_occurrences(start_d: date, end_d: date, target_py_weekday: int):
            if end_d < start_d:
                return 0, None
            days_ahead = (target_py_weekday - start_d.weekday()) % 7
            first = start_d + timedelta(days=days_ahead)
            if first > end_d:
                return 0, None
            delta_days = (end_d - first).days
            return (delta_days // 7) + 1, first

        total_occurrences = 0
        total_salary = Decimal('0.00')

        # Salary for non-recurring (single occurrence each)
        for lesson in non_recurring:
            student_count = lesson.enrollments.filter(status__in=['active', 'payments_problem']).count()
            per_occ_salary = calculate_lesson_salary_with_override(
                student_count,
                instructor,
                salary_override=lesson.instructor_salary_override,
            )
            total_occurrences += 1
            total_salary += Decimal(per_occ_salary)

        # Salary for recurring occurrences in range
        for lesson in recurring:
            # Determine start date for this lesson
            start_from = month_start
            if lesson.lesson_date and lesson.lesson_date > start_from:
                start_from = lesson.lesson_date
            if effective_end < start_from:
                continue

            target_py_weekday = (lesson.day_of_week - 1) % 7
            occ_count, first = count_weekday_occurrences(start_from, effective_end, target_py_weekday)
            if occ_count <= 0:
                continue

            # Subtract cancelled occurrences
            cancelled_count = 0
            if first:
                d = first
                for _ in range(occ_count):
                    if (lesson.id, d) in cancelled_set:
                        cancelled_count += 1
                    d = d + timedelta(days=7)

            effective_count = max(0, occ_count - cancelled_count)
            if effective_count == 0:
                continue

            student_count = lesson.enrollments.filter(status__in=['active', 'payments_problem']).count()
            per_occ_salary = calculate_lesson_salary_with_override(
                student_count,
                instructor,
                salary_override=lesson.instructor_salary_override,
            )
            total_occurrences += effective_count
            total_salary += Decimal(per_occ_salary) * Decimal(effective_count)
        
        return Response({
            'instructor_id': str(instructor.id),
            'instructor_name': instructor.full_name,
            'year': year,
            'month': month,
            'lesson_count': total_occurrences,
            'payment_per_lesson': str(instructor.fixed_salary_per_lesson),
            'total_salary': str(total_salary),
            'is_finalized': False,
            'calculated_at': timezone.now().isoformat()
        })
    
    @action(detail=True, methods=['get'])
    def salary_history(self, request, pk=None):
        """
        Get finalized salary history for an instructor.
        Returns all months where is_finalized=True.
        
        GET /api/v1/instructors/{id}/salary_history/
        """
        from apps.core.models import InstructorMonthlySnapshot
        
        instructor = self.get_object()
        
        # Get finalized snapshots
        snapshots = InstructorMonthlySnapshot.objects.filter(
            instructor=instructor,
            is_finalized=True
        ).order_by('-month')
        
        history = []
        for snapshot in snapshots:
            # Parse YYYY-MM format
            year, month = snapshot.month.split('-')
            history.append({
                'id': str(snapshot.id),
                'instructor_id': str(instructor.id),
                'year': int(year),
                'month': int(month),
                'lesson_count': snapshot.lesson_count,
                'payment_per_lesson': str(snapshot.payment_per_lesson) if snapshot.payment_per_lesson else '0',
                'total_salary': str(snapshot.total_salary),
                'is_finalized': snapshot.is_finalized,
                'calculated_at': snapshot.calculated_at.isoformat()
            })
        
        return Response(history)
    
    @action(detail=False, methods=['post'])
    def finalize_month(self, request):
        """
        Finalize salary for a specific month (manager only).
        Creates or updates InstructorMonthlySnapshot with is_finalized=True.
        
        POST /api/v1/instructors/finalize_month/
        Body: {
            "year": 2025,
            "month": 12,
            "instructor_ids": ["uuid1", "uuid2"]  // optional, defaults to all
        }
        """
        from apps.core.models import InstructorMonthlySnapshot
        from apps.instructors.utils import calculate_instructor_salary_for_month, calculate_instructor_revenue_for_month
        from decimal import Decimal
        
        year = request.data.get('year')
        month = request.data.get('month')
        instructor_ids = request.data.get('instructor_ids', None)
        
        if not year or not month:
            return Response(
                {'error': 'שדות year ו-month נדרשים'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get instructors to finalize
        if instructor_ids:
            instructors = Instructor.objects.filter(id__in=instructor_ids, is_active=True)
        else:
            instructors = Instructor.objects.filter(is_active=True)
        
        finalized_count = 0
        errors = []
        
        today = timezone.now().date()
        month_str = f"{int(year)}-{int(month):02d}"
        current_month_str = today.strftime('%Y-%m')
        # If finalizing current month early, only count occurrences that already happened.
        effective_end = (today - timedelta(days=1)) if month_str == current_month_str else None

        for instructor in instructors:
            try:
                total_salary, total_occurrences, lesson_templates = calculate_instructor_salary_for_month(
                    instructor,
                    month_str,
                    effective_end=effective_end,
                )
                total_revenue, total_students, _ = calculate_instructor_revenue_for_month(
                    instructor,
                    month_str,
                    effective_end=effective_end,
                )
                profit = total_revenue - total_salary
                payment_per_lesson = (total_salary / Decimal(total_occurrences)) if total_occurrences else Decimal('0.00')
                
                # Create or update snapshot
                snapshot, created = InstructorMonthlySnapshot.objects.update_or_create(
                    instructor=instructor,
                    month=month_str,
                    defaults={
                        'lesson_count': lesson_templates,  # Number of lesson templates
                        'payment_per_lesson': payment_per_lesson,
                        'total_salary': total_salary,
                        'total_students': total_students,
                        'total_revenue': total_revenue,
                        'profit': profit,
                        'is_finalized': True,
                        'total_lessons': lesson_templates,  # Same as lesson_count
                    }
                )
                finalized_count += 1
            except Exception as e:
                errors.append({
                    'instructor_id': str(instructor.id),
                    'instructor_name': instructor.full_name,
                    'error': str(e)
                })
        
        response_data = {
            'message': f'סופו {finalized_count} חודשי שכר',
            'year': year,
            'month': month,
            'finalized_count': finalized_count
        }
        
        if errors:
            response_data['errors'] = errors
        
        return Response(response_data)


class InstructorBonusViewSet(viewsets.ModelViewSet):
    """
    ViewSet for InstructorBonus CRUD operations
    
    USAGE: Available at /api/v1/instructor-bonuses/
    """
    queryset = InstructorBonus.objects.all().select_related('instructor')
    serializer_class = InstructorBonusSerializer
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['instructor__first_name', 'instructor__last_name', 'description']
    ordering_fields = ['bonus_date', 'amount', 'created_at']
    ordering = ['-bonus_date']
    
    def get_queryset(self):
        """Apply filters to queryset"""
        queryset = super().get_queryset()
        
        # Filter by instructor
        instructor_id = self.request.query_params.get('instructor')
        if instructor_id:
            queryset = queryset.filter(instructor_id=instructor_id)
        
        # Filter by bonus type
        bonus_type = self.request.query_params.get('bonus_type')
        if bonus_type:
            queryset = queryset.filter(bonus_type=bonus_type)
        
        return queryset
