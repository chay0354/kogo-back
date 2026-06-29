from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Prefetch, Count, Sum
from django.utils import timezone
from datetime import datetime, date, timedelta
from decimal import Decimal

from apps.instructors.models import Instructor, InstructorBonus
from apps.instructors.serializers import (
    InstructorListSerializer, InstructorDetailSerializer,
    InstructorCreateUpdateSerializer, InstructorBonusSerializer,
    InstructorDropdownSerializer,
)
from apps.instructors.utils import (
    calculate_instructor_monthly_metrics, calculate_lesson_profitability
)
from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment
from apps.core.permissions import IsManager, IsManagerOrPartner, ManagerWriteMixin
from apps.core.scoping import scope_instructors
from apps.scheduling.models import LessonCancellation
from apps.instructors.utils import calculate_lesson_salary_with_override


class InstructorViewSet(ManagerWriteMixin, viewsets.ModelViewSet):
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
        queryset = scope_instructors(queryset, self.request.user)

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
        - dropdown: If 'true', return id/name/salary only (fast, for pickers)
        """
        queryset = self.filter_queryset(self.get_queryset())

        if request.query_params.get('dropdown', '').lower() in ('1', 'true', 'yes'):
            serializer = InstructorDropdownSerializer(queryset, many=True)
            return Response(serializer.data)

        # Get filter parameters
        month = request.query_params.get('month', None)
        branch_id = request.query_params.get('branch', None)
        if branch_id == 'all':
            branch_id = None

        use_simple = request.query_params.get('simple', '').lower() == 'true'
        force_refresh = request.query_params.get('refresh', '').lower() in ('1', 'true', 'yes')
        target_month = month or timezone.now().strftime('%Y-%m')

        from apps.core.models import InstructorMonthlySnapshot, LessonMonthlySnapshot

        snapshot_map = {}
        branch_snapshot_metrics = {}
        if not force_refresh:
            if branch_id:
                branch_rows = LessonMonthlySnapshot.objects.filter(
                    month=target_month,
                    branch_id=branch_id,
                ).values('instructor_id').annotate(
                    lessons_count=Count('lesson_id', distinct=True),
                    students_count=Sum('enrolled_students'),
                    base_revenue=Sum('base_revenue'),
                    total_discounts=Sum('total_discounts'),
                    revenue=Sum('revenue'),
                    salary=Sum('instructor_salary'),
                    profit=Sum('profit'),
                )
                for row in branch_rows:
                    branch_snapshot_metrics[row['instructor_id']] = row
            else:
                snapshot_map = {
                    snap.instructor_id: snap
                    for snap in InstructorMonthlySnapshot.objects.filter(month=target_month)
                }

        def _metrics_from_branch_row(row):
            return {
                'lessons_count': row['lessons_count'] or 0,
                'students_count': int(row['students_count'] or 0),
                'base_revenue': row['base_revenue'] or Decimal('0.00'),
                'total_discounts': row['total_discounts'] or Decimal('0.00'),
                'revenue': row['revenue'] or Decimal('0.00'),
                'salary': row['salary'] or Decimal('0.00'),
                'bonuses': Decimal('0.00'),
                'profit': row['profit'] or Decimal('0.00'),
                'cancelled_count': 0,
                'avg_attendance_rate': Decimal('0.00'),
                'salary_is_finalized': False,
            }

        def _empty_metrics():
            return {
                'lessons_count': 0,
                'students_count': 0,
                'base_revenue': Decimal('0.00'),
                'total_discounts': Decimal('0.00'),
                'revenue': Decimal('0.00'),
                'salary': Decimal('0.00'),
                'bonuses': Decimal('0.00'),
                'profit': Decimal('0.00'),
                'cancelled_count': 0,
                'avg_attendance_rate': Decimal('0.00'),
                'salary_is_finalized': False,
            }

        instructors_list = list(queryset)
        base_rows = InstructorListSerializer(
            instructors_list,
            many=True,
            context={'request': request},
        ).data
        base_by_id = {row['id']: row for row in base_rows}

        instructors_data = []
        for instructor in instructors_list:
            snap = snapshot_map.get(instructor.id)
            branch_row = branch_snapshot_metrics.get(instructor.id)
            if use_simple and branch_id:
                metrics = self._calculate_simple_metrics(instructor, branch_id)
            elif branch_id and branch_row is not None:
                metrics = _metrics_from_branch_row(branch_row)
            elif branch_id:
                metrics = self._calculate_simple_metrics(instructor, branch_id)
            elif snap is not None:
                metrics = {
                    'lessons_count': snap.lesson_count or snap.total_lessons,
                    'students_count': snap.total_students,
                    'base_revenue': snap.base_revenue,
                    'total_discounts': snap.total_discounts,
                    'revenue': snap.total_revenue,
                    'salary': snap.total_salary,
                    'bonuses': snap.total_bonuses,
                    'profit': snap.profit,
                    'cancelled_count': snap.cancelled_count,
                    'avg_attendance_rate': snap.avg_attendance_rate,
                    'salary_is_finalized': snap.is_finalized,
                }
            elif force_refresh:
                metrics = calculate_instructor_monthly_metrics(instructor, month, branch_id)
            else:
                metrics = _empty_metrics()

            bonuses_amount = Decimal('0.00')
            if month:
                year_val, month_val = int(month.split('-')[0]), int(month.split('-')[1])
                bonuses_amount = sum(
                    (b.amount for b in instructor.bonuses.all()
                     if b.bonus_date.year == year_val and b.bonus_date.month == month_val),
                    Decimal('0.00'),
                )

            instructor_dict = dict(base_by_id[str(instructor.id)])
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
            course__branch_id=branch_id,
            status='scheduled',
            is_recurring=True
        ).prefetch_related('enrollments')
        
        total_lessons = lessons.count()
        unique_students = set()
        total_salary = Decimal('0.00')
        courses_with_monthly_pay = set()
        
        for lesson in lessons:
            active_enrollments = [
                e for e in lesson.enrollments.all()
                if e.status in ('active', 'payments_problem')
                and getattr(e, 'child', None)
                and e.child.status not in ('trial_signed', 'trial_completed')
            ]
            student_count = len(active_enrollments)

            for enrollment in active_enrollments:
                unique_students.add(enrollment.child_id)

            course = lesson.course
            if course and course.instructor_salary_override is not None:
                if course.id not in courses_with_monthly_pay:
                    courses_with_monthly_pay.add(course.id)
                    total_salary += Decimal(str(course.instructor_salary_override))
                continue
            
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
        from apps.core.models import InstructorMonthlySnapshot, LessonMonthlySnapshot
        from apps.instructors.serializers import InstructorMonthlySnapshotSerializer
        from apps.instructors.utils import lesson_profitability_from_snapshot, _batch_load_cancellations, _month_start_end, _parse_month_str
        
        instructor = self.get_object()
        month = request.query_params.get('month', None)
        target_month = month or timezone.now().strftime('%Y-%m')
        force_refresh = request.query_params.get('refresh', '').lower() in ('1', 'true', 'yes')

        snap = InstructorMonthlySnapshot.objects.filter(
            instructor=instructor,
            month=target_month,
        ).first()

        if snap is not None and not force_refresh:
            metrics = {
                'students_count': snap.total_students,
                'revenue': snap.total_revenue,
                'salary': snap.total_salary,
                'profit': snap.profit,
            }
        elif force_refresh:
            metrics = calculate_instructor_monthly_metrics(instructor, month)
        else:
            metrics = {
                'students_count': snap.total_students if snap else 0,
                'revenue': snap.total_revenue if snap else Decimal('0.00'),
                'salary': snap.total_salary if snap else Decimal('0.00'),
                'profit': snap.profit if snap else Decimal('0.00'),
            }

        lesson_snaps = list(LessonMonthlySnapshot.objects.filter(
            instructor=instructor,
            month=target_month,
            lesson__is_recurring=True,
        ).select_related(
            'lesson',
            'lesson__room',
            'course',
            'course__branch',
            'course__course_type',
            'branch',
        ))

        if lesson_snaps and not force_refresh:
            lessons_data = [lesson_profitability_from_snapshot(s) for s in lesson_snaps]
            unique_courses = {}
            for row in lessons_data:
                course_id = row.get('course_id')
                if course_id and course_id not in unique_courses:
                    unique_courses[course_id] = {
                        'id': course_id,
                        'name': row.get('course_name'),
                        'course_type': None,
                    }
            for snap in lesson_snaps:
                course = snap.course
                if course and str(course.id) in unique_courses and course.course_type:
                    unique_courses[str(course.id)]['course_type'] = course.course_type.name
        else:
            lessons = Lesson.objects.filter(
                instructor=instructor,
                is_recurring=True,
            ).select_related('course', 'course__branch', 'course__course_type', 'room').prefetch_related('enrollments')

            year_val, month_val = _parse_month_str(target_month)
            month_start, month_end = _month_start_end(year_val, month_val)
            cancellations_dict = _batch_load_cancellations(lessons, month_start, month_end, effective_end=None)

            lessons_data = []
            unique_courses = {}
            for lesson in lessons:
                lesson_profit = calculate_lesson_profitability(
                    lesson,
                    instructor,
                    month=target_month,
                    cancellations_dict=cancellations_dict,
                )
                lessons_data.append(lesson_profit)
                if lesson.course and str(lesson.course.id) not in unique_courses:
                    unique_courses[str(lesson.course.id)] = {
                        'id': str(lesson.course.id),
                        'name': lesson.course.name,
                        'course_type': lesson.course.course_type.name if lesson.course.course_type else None,
                    }

        snapshots = InstructorMonthlySnapshot.objects.filter(
            instructor=instructor
        ).order_by('-month')[:6]
        snapshots_serializer = InstructorMonthlySnapshotSerializer(snapshots, many=True)

        serializer = self.get_serializer(instructor, context={
            'lessons': lessons_data,
            'courses': list(unique_courses.values()),
        })
        instructor_dict = serializer.data
        instructor_dict['total_students'] = metrics['students_count']
        instructor_dict['total_revenue'] = str(metrics['revenue'])
        instructor_dict['total_salary'] = str(metrics['salary'])
        instructor_dict['total_profit'] = str(metrics['profit'])
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
        
        now = timezone.now()
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', now.month))
        
        today = timezone.now().date()
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        effective_end = min(month_end, today - timedelta(days=1))
        month_str = f'{year:04d}-{month:02d}'

        from apps.instructors.utils import calculate_instructor_salary_for_month

        if effective_end < month_start:
            total_occurrences = 0
            total_salary = Decimal('0.00')
        else:
            total_salary, total_occurrences, _ = calculate_instructor_salary_for_month(
                instructor,
                month_str,
                effective_end=effective_end,
            )
        
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
