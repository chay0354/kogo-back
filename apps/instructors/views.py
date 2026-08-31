from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from django.db.models import Q, Prefetch, Count, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import datetime, date, timedelta
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlparse
import hashlib
import logging
import requests

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

logger = logging.getLogger(__name__)


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
    ordering = ['first_name', 'last_name']
    
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
                        'display_id': lesson.course.display_id,
                        'name': lesson.course.name,
                        'course_type': lesson.course.course_type.name if lesson.course.course_type else None,
                    }

        snapshots = InstructorMonthlySnapshot.objects.filter(
            instructor=instructor
        ).order_by('-month')[:6]
        snapshots_serializer = InstructorMonthlySnapshotSerializer(snapshots, many=True)

        serializer = self.get_serializer(instructor, context={
            'request': request,
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


# A manager uploads whatever their designer or their phone produced, so the
# stored copy is bounded on both ends: anything bigger than this never reaches
# Pillow, and what is kept is at most a few hundred KB. The cap also matches the
# bucket's own file size limit, so nothing is refused only at the far end.
INSTRUCTOR_PHOTO_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
INSTRUCTOR_PHOTO_MAX_PIXELS = 512
INSTRUCTOR_PHOTO_BUCKET = 'instructor-photos'
INSTRUCTOR_PHOTO_CACHE_SECONDS = 31536000


def _prepare_instructor_photo(upload):
    """
    Validate and shrink an uploaded instructor photo.

    Returns (image_bytes, content_type, error_message); exactly one of the bytes
    and the error message is set.
    """
    from PIL import Image, UnidentifiedImageError

    if upload is None:
        return None, None, 'לא נבחר קובץ תמונה'

    if upload.size > INSTRUCTOR_PHOTO_MAX_UPLOAD_BYTES:
        return None, None, 'הקובץ גדול מדי. ניתן להעלות תמונה של עד 2MB'

    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return None, None, 'הקובץ שנבחר אינו תמונה תקינה'

    # Instructor photos are cut-outs on no background, so the alpha channel has
    # to survive. A picture that never had one is kept as JPEG, which is far
    # smaller than the same photo re-encoded as PNG.
    has_alpha = image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info)
    image = image.convert('RGBA' if has_alpha else 'RGB')
    image.thumbnail((INSTRUCTOR_PHOTO_MAX_PIXELS, INSTRUCTOR_PHOTO_MAX_PIXELS))

    buffer = BytesIO()
    if has_alpha:
        image.save(buffer, format='PNG', optimize=True)
        return buffer.getvalue(), 'image/png', None

    image.save(buffer, format='JPEG', quality=85, optimize=True)
    return buffer.getvalue(), 'image/jpeg', None


def _instructor_photo_object_name(instructor_id, content_type):
    """Named after the instructor, so replacing a photo overwrites the old one."""
    extension = 'png' if content_type == 'image/png' else 'jpg'
    return f'{instructor_id}.{extension}'


def _supabase_storage_headers():
    """
    Service-role credentials for the storage API.

    The service role key bypasses RLS, so it stays server-side: it is never
    serialized, never returned to a caller and never logged.
    """
    key = settings.SUPABASE_SERVICE_ROLE_KEY
    return {'apikey': key, 'Authorization': f'Bearer {key}'}


def _put_instructor_photo(object_name, image_bytes, content_type):
    """Upload the photo to the public bucket, replacing whatever was there."""
    url = f'{settings.SUPABASE_URL.rstrip("/")}/storage/v1/object/{INSTRUCTOR_PHOTO_BUCKET}/{object_name}'
    return requests.post(
        url,
        headers={
            **_supabase_storage_headers(),
            'Content-Type': content_type,
            'x-upsert': 'true',
            # Objects default to no-cache, which would make every widget card
            # revalidate. The saved URL is versioned, so a year is safe.
            'Cache-Control': f'max-age={INSTRUCTOR_PHOTO_CACHE_SECONDS}',
        },
        data=image_bytes,
        timeout=20,
    )


def _remove_instructor_photo(object_name):
    url = f'{settings.SUPABASE_URL.rstrip("/")}/storage/v1/object/{INSTRUCTOR_PHOTO_BUCKET}/{object_name}'
    return requests.delete(url, headers=_supabase_storage_headers(), timeout=20)


def _stored_photo_object_name(instructor):
    """
    The object a saved photo URL points at.

    Read back from the URL rather than rebuilt, so that replacing a transparent
    PNG with a photograph removes the PNG instead of orphaning it in the bucket.
    """
    if not instructor.photo_url:
        return None
    marker = f'/public/{INSTRUCTOR_PHOTO_BUCKET}/'
    path = urlparse(instructor.photo_url).path
    if marker not in path:
        return None
    return path.split(marker, 1)[1]


def _instructor_photo_public_url(object_name, version):
    """
    What the CRM and the widget put in an img src.

    The bucket is public, so the browser reads this straight from Supabase's CDN
    and never through us. The version defeats that CDN on replacement.
    """
    base = settings.SUPABASE_URL.rstrip('/')
    return f'{base}/storage/v1/object/public/{INSTRUCTOR_PHOTO_BUCKET}/{object_name}?v={version}'


class InstructorPhotoView(APIView):
    """
    The instructor's photo, shown in the CRM and in the public enrolment widget.

    POST   /api/v1/instructors/{id}/photo/  — managers only; multipart field "photo"
    DELETE /api/v1/instructors/{id}/photo/  — managers only

    There is no read route here on purpose. The image lives in a public Supabase
    bucket, so every payload carries only its URL and the browser fetches it from
    the CDN — nothing about showing a photo goes through this backend.
    """

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request, instructor_id):
        instructor = Instructor.objects.filter(id=instructor_id).first()
        if instructor is None:
            return Response({'error': 'מדריך לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        image_bytes, content_type, error = _prepare_instructor_photo(request.FILES.get('photo'))
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        object_name = _instructor_photo_object_name(instructor.id, content_type)
        try:
            upload = _put_instructor_photo(object_name, image_bytes, content_type)
        except requests.RequestException:
            logger.exception('[INSTRUCTOR PHOTO] upload failed for %s', instructor.id)
            return Response(
                {'error': 'שמירת התמונה נכשלה. נסה שוב.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if upload.status_code >= 400:
            # The body can carry the storage key back in an error echo, so only
            # the status is recorded.
            logger.error(
                '[INSTRUCTOR PHOTO] storage rejected upload for %s: %s',
                instructor.id, upload.status_code,
            )
            return Response(
                {'error': 'שמירת התמונה נכשלה. נסה שוב.'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        replaced = _stored_photo_object_name(instructor)
        if replaced and replaced != object_name:
            self._discard_object(replaced, instructor.id)

        version = hashlib.sha256(image_bytes).hexdigest()[:16]
        instructor.photo_url = _instructor_photo_public_url(object_name, version)
        instructor.save(update_fields=['photo_url', 'updated_at'])

        return Response({'photo_url': instructor.photo_url})

    def delete(self, request, instructor_id):
        instructor = Instructor.objects.filter(id=instructor_id).first()
        if instructor is None:
            return Response({'error': 'מדריך לא נמצא'}, status=status.HTTP_404_NOT_FOUND)

        object_name = _stored_photo_object_name(instructor)
        if object_name:
            self._discard_object(object_name, instructor.id)

        if instructor.photo_url:
            instructor.photo_url = None
            instructor.save(update_fields=['photo_url', 'updated_at'])

        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _discard_object(object_name, instructor_id):
        """
        Drop an object we no longer point at.

        Failure is logged and swallowed: a manager who removed a photo must not
        keep seeing it just because the bucket call did not go through, and the
        row is the thing the CRM and the widget actually read.
        """
        try:
            removed = _remove_instructor_photo(object_name)
        except requests.RequestException:
            logger.exception('[INSTRUCTOR PHOTO] delete failed for %s', instructor_id)
            return
        if removed.status_code >= 400:
            logger.error(
                '[INSTRUCTOR PHOTO] storage rejected delete for %s: %s',
                instructor_id, removed.status_code,
            )


class MyBranchesView(APIView):
    """
    Branches the signed-in instructor is allowed to work in.

    GET /api/v1/instructors/my-branches/

    Built from the instructor's actual assignments — primary_branch plus every
    InstructorBranch row — not from whichever branches happen to have a lesson
    today. An instructor who teaches in three branches should still be able to
    switch to a branch that is quiet this morning.

    Managers and partners get their own scoped branch list instead, so the
    switcher works for them too.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.core.models import Branch, UserProfile
        from apps.core.scoping import (
            instructor_for_user,
            is_scoped_partner,
            partner_branch_ids,
        )

        user = request.user

        if is_scoped_partner(user):
            qs = Branch.objects.filter(id__in=partner_branch_ids(user))
        else:
            instructor = instructor_for_user(user)
            if instructor is not None:
                assigned = list(
                    instructor.branch_assignments.values_list('branch_id', flat=True)
                )
                if instructor.primary_branch_id:
                    assigned.append(instructor.primary_branch_id)
                qs = Branch.objects.filter(id__in=set(assigned))
            else:
                role = getattr(getattr(user, 'profile', None), 'role', None)
                # A manager (or anyone not tied to an instructor record) sees all.
                qs = Branch.objects.all() if role == UserProfile.ROLE_MANAGER else Branch.objects.none()

        branches = qs.select_related('city').order_by('name')
        return Response({
            'branches': [
                {
                    'id': str(b.id),
                    'name': b.name,
                    'city': b.city.name if b.city_id else '',
                }
                for b in branches
            ]
        })


# Attendance was first taken in the app on this date. Anything earlier was kept
# on paper, so it is not counted as a register that was never filled in.
ATTENDANCE_TRACKING_START = date(2026, 9, 1)


class MyDashboardView(APIView):
    """
    The instructor's own numbers.

    GET /api/v1/instructors/my-dashboard/?date_from=&date_to=&branch_id=

    Everything here is counted from real rows — there is no estimate and no
    placeholder. "Student" means an active enrolment of an active child, so
    walk-ins, trial signups and children who left are all out.

    Instructors see only their own lessons; managers looking at the same screen
    see the whole scope they already have.
    """

    permission_classes = [IsAuthenticated]

    # Below this many active students a group is worth a second look.
    LOW_GROUP_THRESHOLD = 8

    def get(self, request):
        from apps.core.models import UserProfile
        from apps.core.scoping import (
            instructor_for_user,
            instructor_login_q,
            resolve_viewable_user,
        )
        from apps.enrollments.models import LessonAttendance

        # A head instructor may hold links to colleagues' accounts. The id is
        # re-checked server-side; an unlinked id raises rather than falling back
        # to the caller's own data, which would silently show the wrong numbers.
        subject = resolve_viewable_user(request, request.query_params.get('as_user'))

        today = date.today()
        date_from = parse_date(request.query_params.get('date_from') or '') or (
            today - timedelta(days=180)
        )
        date_to = parse_date(request.query_params.get('date_to') or '') or today
        if date_from > date_to:
            date_from, date_to = date_to, date_from

        # The branch limit is applied before the role is read, so a link tied to
        # one branch stays tied to it however the rest of this narrows down.
        lessons = Lesson.objects.select_related(
            'course', 'course__branch', 'course__course_type'
        ).filter(subject.branch_q(), course__is_active=True)

        instructor = instructor_for_user(subject.user)
        if instructor is not None:
            lessons = lessons.filter(instructor_login_q(subject.user))
        elif getattr(getattr(subject.user, 'profile', None), 'role', None) != UserProfile.ROLE_MANAGER:
            return Response(self._empty_payload(request, subject.user))

        branch_id = request.query_params.get('branch_id')
        if branch_id and branch_id != 'all':
            lessons = lessons.filter(course__branch_id=branch_id)

        lessons = list(lessons)
        lesson_ids = [lesson.id for lesson in lessons]
        if not lesson_ids:
            return Response(self._empty_payload(request, subject.user))

        enrollments = list(
            LessonEnrollment.objects
            .filter(lesson_id__in=lesson_ids, status='active', child__status='active')
            .values('lesson_id', 'child_id', 'start_date', 'end_date')
        )

        # --- trend: distinct children taught in each month of the range ---
        # A child in two of this instructor's groups is one student, not two.
        monthly_trend = []
        cursor = date(date_from.year, date_from.month, 1)
        last = date(date_to.year, date_to.month, 1)
        while cursor <= last:
            if cursor.month == 12:
                month_end = date(cursor.year, 12, 31)
            else:
                month_end = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
            children = {
                e['child_id'] for e in enrollments
                if (e['start_date'] is None or e['start_date'] <= month_end)
                and (e['end_date'] is None or e['end_date'] >= cursor)
            }
            monthly_trend.append({
                'month': cursor.strftime('%Y-%m'),
                'students': len(children),
            })
            cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)

        # --- per-group headcount, as of today ---
        current = {}
        for e in enrollments:
            if e['start_date'] and e['start_date'] > today:
                continue
            if e['end_date'] and e['end_date'] < today:
                continue
            current.setdefault(e['lesson_id'], set()).add(e['child_id'])

        groups = []
        for lesson in lessons:
            count = len(current.get(lesson.id, ()))
            groups.append({
                'lesson_id': str(lesson.id),
                'course_name': lesson.course.name if lesson.course_id else '',
                'branch_name': lesson.course.branch.name if lesson.course_id and lesson.course.branch_id else '',
                'day_of_week': lesson.day_of_week,
                'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
                'active_students': count,
                'is_low': count < self.LOW_GROUP_THRESHOLD,
            })
        groups.sort(key=lambda g: (g['active_students'], g['course_name']))

        total_active = len({cid for ids in current.values() for cid in ids})

        # --- occurrences still waiting for attendance ---
        # Only dates that have already happened: a lesson later today is not
        # "missing", it simply has not been taught yet.
        # A group with nobody on it is skipped below, so its registers — years of
        # rows on a long-running group — are never worth reading.
        rostered_ids = [lesson.id for lesson in lessons if current.get(lesson.id)]
        cancelled = {
            row for row in LessonCancellation.objects
            .filter(lesson_id__in=rostered_ids, occurrence_date__gte=date_from, occurrence_date__lte=date_to)
            .values_list('lesson_id', 'occurrence_date')
        }
        marked = {}
        for lesson_id, occurrence_date, child_id in (
            LessonAttendance.objects
            .filter(lesson_id__in=rostered_ids, occurrence_date__gte=date_from, occurrence_date__lte=date_to)
            .exclude(status='not_marked')
            .values_list('lesson_id', 'occurrence_date', 'child_id')
        ):
            marked.setdefault((lesson_id, occurrence_date), set()).add(child_id)

        window_end = min(date_to, today)
        # Registers were not kept in the app before this date, so every lesson
        # behind it would read as missing attendance for the rest of time.
        window_start = max(date_from, ATTENDANCE_TRACKING_START)
        unmarked = []
        for lesson in lessons:
            roster = current.get(lesson.id, set())
            if not roster:
                continue  # nothing to mark
            for occ in self._occurrences(lesson, window_start, window_end):
                if (lesson.id, occ) in cancelled:
                    continue
                done = marked.get((lesson.id, occ), set())
                if roster.issubset(done):
                    continue
                unmarked.append({
                    'lesson_id': str(lesson.id),
                    'date': occ.isoformat(),
                    'course_name': lesson.course.name if lesson.course_id else '',
                    'branch_name': lesson.course.branch.name if lesson.course_id and lesson.course.branch_id else '',
                    'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
                    'missing': len(roster - done),
                })
        unmarked.sort(key=lambda u: (u['date'], u['start_time']), reverse=True)

        branch_names = {}
        for lesson in lessons:
            if lesson.course_id and lesson.course.branch_id:
                branch_names[str(lesson.course.branch_id)] = lesson.course.branch.name

        return Response({
            'branches': [{'id': bid, 'name': name} for bid, name in sorted(branch_names.items(), key=lambda kv: kv[1])],
            'monthly_trend': monthly_trend,
            'groups': groups,
            'unmarked_lessons': unmarked[:40],
            'unmarked_total': len(unmarked),
            'total_active_students': total_active,
            'low_group_threshold': self.LOW_GROUP_THRESHOLD,
            'date_from': date_from.isoformat(),
            'date_to': date_to.isoformat(),
            'subject': self._subject(request, subject.user),
        })

    def _empty_payload(self, request, subject):
        """Same keys as a populated answer, so the client never special-cases."""
        return {
            'branches': [],
            'monthly_trend': [],
            'groups': [],
            'unmarked_lessons': [],
            'unmarked_total': 0,
            'total_active_students': 0,
            'low_group_threshold': self.LOW_GROUP_THRESHOLD,
            'subject': self._subject(request, subject),
        }

    @staticmethod
    def _subject(request, subject):
        return {
            'id': str(subject.id),
            'name': f"{subject.first_name} {subject.last_name}".strip() or subject.username,
            'is_self': subject.id == request.user.id,
        }

    @staticmethod
    def _occurrences(lesson, start, end):
        """Dates this lesson actually falls on, same weekly rule the schedule uses."""
        if not lesson.is_recurring:
            if lesson.lesson_date and start <= lesson.lesson_date <= end:
                yield lesson.lesson_date
            return
        if lesson.day_of_week is None:
            return
        first = start
        if lesson.lesson_date and lesson.lesson_date > first:
            first = lesson.lesson_date
        target = (lesson.day_of_week - 1) % 7
        occ = first + timedelta(days=(target - first.weekday()) % 7)
        while occ <= end:
            yield occ
            occ = occ + timedelta(days=7)


class LoginDiagnosticsView(APIView):
    """
    Why an instructor does or does not see their lessons.

    GET /api/v1/instructors/login-diagnostics/    (manager only)

    A worker's schedule is built by matching Instructor.email against the
    email or username they sign in with. When those two drift apart — a typo,
    a stray space, a second Instructor row, a login created before the
    instructor record — the person signs in successfully and sees an empty
    week, with nothing anywhere saying why.

    This lists both sides and the exact reason for each mismatch. It exposes
    nothing a manager cannot already see on the users and instructors screens.
    """

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        from django.contrib.auth import get_user_model

        from apps.core.models import UserProfile
        from apps.core.scoping import instructor_login_q

        User = get_user_model()

        instructors = list(Instructor.objects.all())
        by_ident = {}
        for inst in instructors:
            key = (inst.email or '').strip().casefold()
            if key:
                by_ident.setdefault(key, []).append(inst)

        lesson_counts = {
            row['instructor_id']: row['n']
            for row in Lesson.objects.filter(course__is_active=True)
            .values('instructor_id')
            .annotate(n=Count('id'))
        }

        accounts = []
        for user in User.objects.select_related('profile').order_by('username'):
            role = getattr(getattr(user, 'profile', None), 'role', None)
            if role != UserProfile.ROLE_WORKER:
                continue

            idents = [
                (v or '').strip().casefold()
                for v in (user.email, user.username)
                if (v or '').strip()
            ]
            matched = []
            for ident in idents:
                for inst in by_ident.get(ident, []):
                    if inst not in matched:
                        matched.append(inst)

            lessons = Lesson.objects.filter(
                instructor_login_q(user), course__is_active=True
            ).count()

            problem = None
            if not idents:
                problem = 'למשתמש אין אימייל ואין שם משתמש'
            elif not matched:
                problem = 'אין רשומת מדריך שהשם משתמש שלה תואם לכניסה הזו'
            elif len(matched) > 1:
                problem = 'יותר מרשומת מדריך אחת עם אותו שם משתמש'
            elif lessons == 0:
                problem = 'המדריך מזוהה, אך אין שיעורים פעילים המשויכים אליו'

            accounts.append({
                'user_id': str(user.id),
                'username': user.username,
                'email': user.email,
                'name': f'{user.first_name} {user.last_name}'.strip(),
                'is_active': user.is_active,
                'matched_instructors': [
                    {
                        'id': str(i.id),
                        'name': f'{i.first_name} {i.last_name}'.strip(),
                        'email': i.email,
                        'lessons': lesson_counts.get(i.id, 0),
                    }
                    for i in matched
                ],
                'visible_lessons': lessons,
                'problem': problem,
            })

        # Instructor records with lessons but no way to sign in.
        user_idents = set()
        for user in User.objects.all():
            for v in (user.email, user.username):
                v = (v or '').strip().casefold()
                if v:
                    user_idents.add(v)

        orphans = []
        for inst in instructors:
            key = (inst.email or '').strip().casefold()
            n = lesson_counts.get(inst.id, 0)
            if key and key in user_idents:
                continue
            orphans.append({
                'id': str(inst.id),
                'name': f'{inst.first_name} {inst.last_name}'.strip(),
                'email': inst.email,
                'lessons': n,
                'problem': 'לרשומת המדריך אין שם משתמש' if not key
                else 'אין חשבון כניסה עם שם המשתמש הזה',
            })

        return Response({
            'accounts': accounts,
            'instructors_without_login': orphans,
            'summary': {
                'worker_accounts': len(accounts),
                'with_problem': sum(1 for a in accounts if a['problem']),
                'instructors_without_login': len(orphans),
            },
        })


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
