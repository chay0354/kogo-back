"""
Dashboard API Views
Provides aggregated analytics data for dashboard UI
"""
import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import models
from django.db.models import Sum, Count, Q, Avg, Max
from django.utils import timezone
from datetime import datetime, timedelta, date
from decimal import Decimal

from apps.core.permissions import IsManager

logger = logging.getLogger(__name__)
from apps.core.models import (
    Branch,
    InstructorMonthlySnapshot,
    BranchMonthlySnapshot,
    LessonMonthlySnapshot
)
from apps.instructors.models import Instructor
from apps.courses.models import Course, Lesson, CourseType
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment, LessonAttendance


def parse_date_filters(request):
    """Parse date_from and date_to from request params"""
    date_from_str = request.query_params.get('date_from')
    date_to_str = request.query_params.get('date_to')
    
    if date_from_str:
        try:
            date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date()
        except ValueError:
            date_from = None
    else:
        # Default to start of current month
        today = timezone.now().date()
        date_from = today.replace(day=1)
    
    if date_to_str:
        try:
            date_to = datetime.strptime(date_to_str, '%Y-%m-%d').date()
        except ValueError:
            date_to = None
    else:
        # Default to end of current month
        date_to = timezone.now().date()
    
    return date_from, date_to


def dates_to_month_list(date_from, date_to):
    """Convert date range to list of month strings (YYYY-MM)"""
    months = []
    current = date_from.replace(day=1)
    end = date_to.replace(day=1)
    
    while current <= end:
        months.append(current.strftime('%Y-%m'))
        # Move to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    
    return months


class DashboardViewSet(viewsets.ViewSet):
    """
    Dashboard data endpoints
    All endpoints require authentication and manager role
    """
    permission_classes = [IsAuthenticated, IsManager]
    
    @action(detail=False, methods=['get'], url_path='financial')
    def financial_data(self, request):
        """
        Financial dashboard data
        Query params:
        - branch_id: Filter by branch (default: 'all')
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        
        Note: Current month snapshots are refreshed dynamically on each request.
              Past month snapshots use existing data (immutable).
        """
        branch_id = request.query_params.get('branch_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        
        # Auto-refresh current month snapshots (dynamic calculation)
        from apps.instructors.utils import generate_monthly_snapshots
        current_month = timezone.now().strftime('%Y-%m')
        if current_month in months:
            try:
                generate_monthly_snapshots(current_month, finalize=False)
            except Exception as e:
                # Log error but continue - use existing snapshot if refresh fails
                logger.error(f"Failed to refresh current month snapshots: {e}")
        
        # Query BranchMonthlySnapshot (now includes refreshed current month)
        snapshots = BranchMonthlySnapshot.objects.filter(month__in=months)
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(branch_id=branch_id)
        
        snapshots = snapshots.select_related('branch')
        
        # === LOGGING: Financial Overview KPIs ===
        # NOTE: use logger (not print) so non-ASCII branch names don't crash the
        # response on Windows consoles whose default codec is cp1252.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("FINANCIAL OVERVIEW - KPI CALCULATION")
            logger.debug("Branch filter: %s", branch_id)
            logger.debug("Date range: %s to %s", date_from, date_to)
            logger.debug("Months included: %s", months)
            logger.debug("Number of snapshots found: %s", snapshots.count())
            for snap in snapshots:
                logger.debug(
                    "  Snapshot: %s | %s | Revenue: %s | Costs: %s | Profit: %s",
                    snap.branch.name, snap.month, snap.total_revenue,
                    snap.instructor_costs, snap.profit,
                )

        # Aggregate KPIs
        total_revenue = snapshots.aggregate(Sum('total_revenue'))['total_revenue__sum'] or Decimal('0.00')
        total_expenses = snapshots.aggregate(Sum('instructor_costs'))['instructor_costs__sum'] or Decimal('0.00')
        net_profit = total_revenue - total_expenses

        from apps.scheduling.studio_rental_finance import aggregate_studio_rental_revenue

        rental_agg = aggregate_studio_rental_revenue(
            date_from,
            date_to,
            branch_id if branch_id and branch_id != 'all' else None,
            None,
        )
        rental_total = rental_agg['total'] or Decimal('0.00')
        total_revenue += rental_total
        net_profit += rental_total

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Total Revenue: %s", total_revenue)
            logger.debug("Total Expenses: %s", total_expenses)
            logger.debug("Net Profit: %s", net_profit)
        
        # Expense breakdown
        total_instructor_salaries = snapshots.aggregate(Sum('instructor_salaries'))['instructor_salaries__sum'] or Decimal('0.00')
        total_instructor_bonuses = snapshots.aggregate(Sum('instructor_bonuses'))['instructor_bonuses__sum'] or Decimal('0.00')
        total_operational_costs = snapshots.aggregate(Sum('operational_costs'))['operational_costs__sum'] or Decimal('0.00')
        
        # Revenue by branch
        revenue_by_branch = []
        branch_data = snapshots.values('branch__name', 'branch_id').annotate(
            revenue=Sum('total_revenue'),
            expenses=Sum('instructor_costs'),
            profit=Sum('profit')
        ).order_by('-revenue')
        
        for item in branch_data:
            bid = str(item['branch_id'])
            extra = float(rental_agg['by_branch_id'].get(bid, 0) or 0)
            revenue_by_branch.append({
                'branch_name': item['branch__name'],
                'branch_id': bid,
                'revenue': float(item['revenue'] or 0) + extra,
                'expenses': float(item['expenses'] or 0),
                'profit': float(item['profit'] or 0) + extra
            })

        # Branches with rental revenue but no snapshot row in range
        known_ids = {r['branch_id'] for r in revenue_by_branch}
        for bid, amt in rental_agg['by_branch_id'].items():
            if bid in known_ids:
                continue
            b = Branch.objects.filter(pk=bid).first()
            if not b:
                continue
            extra = float(amt or 0)
            revenue_by_branch.append({
                'branch_name': b.name,
                'branch_id': bid,
                'revenue': extra,
                'expenses': 0.0,
                'profit': extra,
            })
        
        # Monthly trends (aggregate by month)
        monthly_trends = []
        for month in sorted(months):
            month_snaps = snapshots.filter(month=month)
            month_revenue = month_snaps.aggregate(Sum('total_revenue'))['total_revenue__sum'] or Decimal('0.00')
            month_expenses = month_snaps.aggregate(Sum('instructor_costs'))['instructor_costs__sum'] or Decimal('0.00')
            
            extra_month = float(rental_agg['by_month'].get(month, 0) or 0)
            monthly_trends.append({
                'month': month,
                'revenue': float(month_revenue) + extra_month,
                'expenses': float(month_expenses)
            })
        
        # Revenue by instructor (top 8)
        instructor_snapshots = InstructorMonthlySnapshot.objects.filter(month__in=months)
        if branch_id and branch_id != 'all':
            instructor_snapshots = instructor_snapshots.filter(
                instructor__primary_branch_id=branch_id
            )
        
        instructor_data = instructor_snapshots.values(
            'instructor__first_name',
            'instructor__last_name',
            'instructor_id'
        ).annotate(
            revenue=Sum('total_revenue'),
            salary=Sum('total_salary'),
            profit=Sum('profit')
        ).order_by('-profit')[:8]
        
        revenue_by_instructor = []
        for item in instructor_data:
            revenue_by_instructor.append({
                'instructor_name': f"{item['instructor__first_name']} {item['instructor__last_name']}",
                'instructor_id': str(item['instructor_id']),
                'revenue': float(item['revenue'] or 0),
                'salary': float(item['salary'] or 0),
                'profit': float(item['profit'] or 0)
            })
        
        return Response({
            'kpis': {
                'total_revenue': float(total_revenue),
                'total_expenses': float(total_expenses),
                'net_profit': float(net_profit)
            },
            'expense_breakdown': {
                'instructor_salaries': float(total_instructor_salaries),
                'instructor_bonuses': float(total_instructor_bonuses),
                'operational_costs': float(total_operational_costs),
                'total': float(total_expenses)
            },
            'revenue_by_branch': revenue_by_branch,
            'monthly_trends': monthly_trends,
            'revenue_by_instructor': revenue_by_instructor
        })
    
    @action(detail=False, methods=['get'], url_path='instructors')
    def instructors_data(self, request):
        """
        Instructors dashboard data
        Query params:
        - instructor_id: Filter by instructor (default: 'all')
        - branch_id: Filter by branch (default: 'all')
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        
        Note: Current month snapshots are refreshed dynamically on each request.
              Past month snapshots use existing data (immutable).
        """
        instructor_id = request.query_params.get('instructor_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        
        # Auto-refresh current month snapshots (dynamic calculation)
        from apps.instructors.utils import generate_monthly_snapshots
        current_month = timezone.now().strftime('%Y-%m')
        if current_month in months:
            try:
                generate_monthly_snapshots(current_month, finalize=False)
            except Exception as e:
                # Log error but continue - use existing snapshot if refresh fails
                logger.error(f"Failed to refresh current month snapshots: {e}")
        
        # Query InstructorMonthlySnapshot (now includes refreshed current month)
        snapshots = InstructorMonthlySnapshot.objects.filter(month__in=months)
        
        if instructor_id and instructor_id != 'all':
            snapshots = snapshots.filter(instructor_id=instructor_id)
        
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(instructor__primary_branch_id=branch_id)
        
        snapshots = snapshots.select_related('instructor', 'instructor__primary_branch')
        
        # KPIs
        active_instructors = snapshots.values('instructor_id').distinct().count()
        total_lessons = snapshots.aggregate(Sum('total_lessons'))['total_lessons__sum'] or 0
        total_salary = snapshots.aggregate(Sum('total_salary'))['total_salary__sum'] or Decimal('0.00')
        total_bonuses = snapshots.aggregate(Sum('total_bonuses'))['total_bonuses__sum'] or Decimal('0.00')
        total_profit = snapshots.aggregate(Sum('profit'))['profit__sum'] or Decimal('0.00')
        
        # Instructor comparison data (all instructors with their aggregated metrics)
        instructor_comparison = []
        instructor_data = snapshots.values(
            'instructor_id',
            'instructor__first_name',
            'instructor__last_name',
            'instructor__primary_branch__name'
        ).annotate(
            lessons=Sum('total_lessons'),
            students=Sum('total_students'),
            revenue=Sum('total_revenue'),
            salary=Sum('total_salary'),
            profit=Sum('profit')
        ).order_by('-profit')
        
        for item in instructor_data:
            instructor_comparison.append({
                'instructor_id': str(item['instructor_id']),
                'name': f"{item['instructor__first_name']} {item['instructor__last_name']}",
                'branch': item['instructor__primary_branch__name'] or '',
                'lessons': item['lessons'] or 0,
                'students': item['students'] or 0,
                'revenue': float(item['revenue'] or 0),
                'salary': float(item['salary'] or 0),
                'profit': float(item['profit'] or 0),
                # Mock occupancy and attendance for now
                'occupancy': 75,
                'attendance': 85
            })
        
        # Top performers
        top_profit = instructor_comparison[0] if instructor_comparison else None
        top_students = max(instructor_comparison, key=lambda x: x['students']) if instructor_comparison else None
        top_lessons = max(instructor_comparison, key=lambda x: x['lessons']) if instructor_comparison else None
        
        return Response({
            'kpis': {
                'active_instructors': active_instructors,
                'total_bonuses': float(total_bonuses),
                'total_salary': float(total_salary),
                'total_profit': float(total_profit)
            },
            'top_performers': {
                'highest_profit': top_profit,
                'most_students': top_students,
                'most_lessons': top_lessons
            },
            'instructor_comparison': instructor_comparison[:8],
            'instructor_details': instructor_comparison
        })
    
    @action(detail=False, methods=['get'], url_path='students')
    def students_data(self, request):
        """
        Students dashboard data
        Query params:
        - search_query: Search by name
        - course_id: Filter by course
        - branch_id: Filter by branch
        - student_status: Filter by status
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        - quit_date_from / quit_date_to: optional; when both set, limits churn (אחוז נשירה)
          and נושרים לפי תחום to this window instead of date_from/date_to.
        """
        from apps.customers.status_history_models import ChildStatusHistory
        
        search_query = request.query_params.get('search_query', '')
        course_id = request.query_params.get('course_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        student_status = request.query_params.get('student_status', 'all')
        date_from, date_to = parse_date_filters(request)
        
        # Optional narrower window for quit / churn only (defaults to main date range)
        quit_df_str = request.query_params.get('quit_date_from')
        quit_dt_str = request.query_params.get('quit_date_to')
        if quit_df_str and quit_dt_str:
            try:
                quit_date_from = datetime.strptime(quit_df_str, '%Y-%m-%d').date()
                quit_date_to = datetime.strptime(quit_dt_str, '%Y-%m-%d').date()
                if quit_date_from > quit_date_to:
                    quit_date_from, quit_date_to = quit_date_to, quit_date_from
            except ValueError:
                quit_date_from, quit_date_to = date_from, date_to
        else:
            quit_date_from, quit_date_to = date_from, date_to
        
        # Get all children
        children = Child.objects.all()
        
        if search_query:
            children = children.filter(
                Q(first_name__icontains=search_query) | Q(last_name__icontains=search_query)
            )
        
        if student_status and student_status != 'all':
            children = children.filter(status=student_status)
        
        # Apply branch/course filters if needed
        if course_id != 'all' or branch_id != 'all':
            filtered_enrollments = LessonEnrollment.objects.filter(status='active')
            if course_id != 'all':
                filtered_enrollments = filtered_enrollments.filter(lesson__course_id=course_id)
            if branch_id != 'all':
                filtered_enrollments = filtered_enrollments.filter(lesson__branch_id=branch_id)
            filtered_child_ids = filtered_enrollments.values_list('child_id', flat=True).distinct()
            children = children.filter(id__in=filtered_child_ids)
        
        # KPI 1: Active Students
        active_students = children.filter(status='active').count()
        
        # KPI 2: Credit Problems (not_paid OR payment_problem)
        credit_problems = children.filter(
            Q(status='not_paid') | Q(status='payment_problem')
        ).count()
        
        # KPI 3: Ghost Students
        ghost_students = children.filter(status='ghost').count()
        
        # KPI 4: Signed for Trial
        signed_for_trial = children.filter(status='trial_signed').count()
        
        # KPI 5: Done Trial
        done_trial = children.filter(status='trial_completed').count()
        
        # Abnormal Attendance by Branch
        abnormal_by_branch = []
        if branch_id == 'all':
            # Get all branches with abnormal attendance count
            from apps.core.models import Branch
            branches = Branch.objects.filter(is_active=True)
            for branch in branches:
                count = children.filter(
                    absent_irregularly=True,
                    family__branch=branch
                ).count()
                if count > 0:
                    abnormal_by_branch.append({
                        'branch_id': str(branch.id),
                        'branch_name': branch.name,
                        'count': count
                    })
        else:
            # Single branch filter
            from apps.core.models import Branch
            try:
                branch = Branch.objects.get(id=branch_id)
                count = children.filter(
                    absent_irregularly=True,
                    family__branch=branch
                ).count()
                abnormal_by_branch.append({
                    'branch_id': str(branch.id),
                    'branch_name': branch.name,
                    'count': count
                })
            except Branch.DoesNotExist:
                pass
        
        # Quit Percentage - Children who changed from 'active' to other statuses
        # Apply branch/course filters to quit percentage data
        quit_data = []
        status_changes = ChildStatusHistory.objects.filter(
            previous_status='active',
            changed_at__date__gte=quit_date_from,
            changed_at__date__lte=quit_date_to
        ).exclude(new_status='active')
        
        # Filter by branch/course if specified
        if course_id != 'all' or branch_id != 'all':
            # Get child IDs that match the filters
            child_ids_for_quit = children.values_list('id', flat=True)
            status_changes = status_changes.filter(child_id__in=child_ids_for_quit)
        
        # Group by target status
        quit_by_status = status_changes.values('new_status').annotate(
            count=Count('id')
        ).order_by('-count')
        
        # Hebrew status labels mapping - comprehensive list
        status_labels = {
            'active': 'פעיל',
            'ghost': 'רפאים',
            'non_active': 'לא פעיל',
            'inactive': 'לא פעיל',
            'payment_problem': 'בעיית תשלום',
            'not_paid': 'לא שולם',
            'trial_signed': 'נרשם לניסיון',
            'trial_completed': 'השלים ניסיון',
            'paused': 'מושהה',
            'sign_in': 'הרשמה',
            'pending': 'ממתין',
        }
        
        total_quit = status_changes.count()
        for item in quit_by_status:
            percentage = (item['count'] / total_quit * 100) if total_quit > 0 else 0
            status_key = item['new_status']
            
            # Get child details for this status
            children_with_status = status_changes.filter(new_status=status_key).select_related('child')
            child_details = []
            for change in children_with_status:
                child_details.append({
                    'id': str(change.child.id),
                    'full_name': change.child.full_name,
                    'id_number': change.child.id_number or '',
                    'changed_at': change.changed_at.isoformat(),
                })
            
            quit_data.append({
                'status': status_labels.get(status_key, status_key),  # Use Hebrew label
                'status_key': status_key,  # Keep original key for reference
                'count': item['count'],
                'percentage': round(percentage, 1),
                'children': child_details  # Add child details
            })

        # Quit Percentage breakdown by course type (each dropout counted once
        # per distinct course type the child was enrolled in).
        quit_by_course_type = []
        if total_quit > 0:
            child_ids_in_quit = list(
                status_changes.values_list('child_id', flat=True).distinct()
            )

            child_course_types: dict = {}
            if child_ids_in_quit:
                enrollment_rows = LessonEnrollment.objects.filter(
                    child_id__in=child_ids_in_quit,
                    lesson__course__course_type__isnull=False,
                ).values_list(
                    'child_id',
                    'lesson__course__course_type_id',
                    'lesson__course__course_type__name',
                ).distinct()

                for child_id, ct_id, ct_name in enrollment_rows:
                    if not ct_id:
                        continue
                    child_course_types.setdefault(child_id, set()).add((str(ct_id), ct_name))

            agg: dict = {}
            unknown_count = 0
            for change in status_changes.only('id', 'child_id'):
                types = child_course_types.get(change.child_id)
                if not types:
                    unknown_count += 1
                    continue
                for ct_id, ct_name in types:
                    entry = agg.setdefault(
                        ct_id,
                        {'course_type_id': ct_id, 'course_type_name': ct_name, 'count': 0},
                    )
                    entry['count'] += 1

            quit_by_course_type = sorted(
                agg.values(), key=lambda x: x['count'], reverse=True
            )

            if unknown_count > 0:
                quit_by_course_type.append({
                    'course_type_id': None,
                    'course_type_name': 'ללא תחום',
                    'count': unknown_count,
                })

        # Student list (top 10)
        student_list = []
        active_enrollments = LessonEnrollment.objects.filter(status='active')
        if course_id != 'all':
            active_enrollments = active_enrollments.filter(lesson__course_id=course_id)
        if branch_id != 'all':
            active_enrollments = active_enrollments.filter(lesson__branch_id=branch_id)
        
        active_student_ids = active_enrollments.values_list('child_id', flat=True).distinct()
        top_students = children.filter(id__in=active_student_ids)[:10]
        
        for child in top_students:
            # Get child's enrollment info
            enrollment = active_enrollments.filter(child=child).select_related(
                'lesson__branch', 'lesson__course'
            ).first()
            
            # Calculate attendance for this child
            child_attendance = LessonAttendance.objects.filter(
                child=child,
                occurrence_date__gte=date_from,
                occurrence_date__lte=date_to
            )
            child_total = child_attendance.count()
            child_present = child_attendance.filter(status='present').count()
            child_attendance_rate = (child_present / child_total * 100) if child_total > 0 else 0
            
            student_list.append({
                'id': str(child.id),
                'name': child.full_name,
                'branch': enrollment.lesson.branch.name if enrollment else '',
                'course': enrollment.lesson.course.name if enrollment else '',
                'attendance': round(child_attendance_rate, 1),
                'is_trial': child.status in ['trial_signed', 'trial_completed']
            })
        
        return Response({
            'kpis': {
                'active_students': active_students,
                'credit_problems': credit_problems,
                'ghost_students': ghost_students,
                'signed_for_trial': signed_for_trial,
                'done_trial': done_trial
            },
            'abnormal_attendance_by_branch': abnormal_by_branch,
            'quit_percentage': {
                'total_quit': total_quit,
                'by_status': quit_data,
                'by_course_type': quit_by_course_type,
            },
            'student_list': student_list
        })
    
    @action(detail=False, methods=['get'], url_path='courses')
    def courses_data(self, request):
        """
        Courses dashboard data
        Query params:
        - course_id: Filter by course
        - branch_id: Filter by branch
        - city_id: Filter by city
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        
        Note: Current month snapshots are refreshed dynamically on each request.
              Past month snapshots use existing data (immutable).
        """
        course_id = request.query_params.get('course_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        city_id = request.query_params.get('city_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        
        # Auto-refresh current month snapshots (dynamic calculation)
        from apps.instructors.utils import generate_monthly_snapshots
        current_month = timezone.now().strftime('%Y-%m')
        if current_month in months:
            try:
                generate_monthly_snapshots(current_month, finalize=False)
            except Exception as e:
                # Log error but continue - use existing snapshot if refresh fails
                logger.error(f"Failed to refresh current month snapshots: {e}")
        
        # Query LessonMonthlySnapshot grouped by course (now includes refreshed current month)
        snapshots = LessonMonthlySnapshot.objects.filter(month__in=months)
        
        if course_id and course_id != 'all':
            snapshots = snapshots.filter(course_id=course_id)
        
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(branch_id=branch_id)
        
        if city_id and city_id != 'all':
            snapshots = snapshots.filter(branch__city_id=city_id)
        
        snapshots = snapshots.select_related('course', 'branch')
        
        # KPIs - Count actual courses, not snapshot records
        # Total courses: All active courses (with applied filters)
        courses_query = Course.objects.filter(is_active=True)
        if course_id and course_id != 'all':
            courses_query = courses_query.filter(id=course_id)
        if branch_id and branch_id != 'all':
            courses_query = courses_query.filter(branch_id=branch_id)
        if city_id and city_id != 'all':
            courses_query = courses_query.filter(branch__city_id=city_id)
        
        total_courses = courses_query.count()
        
        # Active courses: Courses that have snapshots in the period (have lessons)
        # If no snapshots exist, fall back to actual lessons in the date range
        active_course_ids = snapshots.values_list('course_id', flat=True).distinct()
        
        if active_course_ids:
            active_courses = len(set(active_course_ids))
        else:
            # Fallback: Count courses with actual lessons in the date range
            from apps.courses.models import Lesson
            lessons_query = Lesson.objects.filter(
                scheduled_date__gte=date_from,
                scheduled_date__lte=date_to,
                course__is_active=True
            )
            if course_id and course_id != 'all':
                lessons_query = lessons_query.filter(course_id=course_id)
            if branch_id and branch_id != 'all':
                lessons_query = lessons_query.filter(branch_id=branch_id)
            if city_id and city_id != 'all':
                lessons_query = lessons_query.filter(branch__city_id=city_id)
            
            active_courses = lessons_query.values('course_id').distinct().count()
        
        # Aggregate by course - properly handle multiple months
        latest_month = max(months) if months else None
        
        course_list = []
        low_occupancy_courses = []
        full_capacity_count = 0
        low_occupancy_count = 0
        
        # Get unique courses from snapshots
        unique_courses = snapshots.values_list('course_id', flat=True).distinct()
        
        for cid in unique_courses:
            course_snapshots = snapshots.filter(course_id=cid)
            first_snapshot = course_snapshots.first()
            
            if not first_snapshot:
                continue
                
            course_name = first_snapshot.course.name
            branch_name = first_snapshot.branch.name
            
            # Students: Use latest month or max to avoid inflated numbers
            if latest_month:
                latest_course_snapshots = course_snapshots.filter(month=latest_month)
                students = latest_course_snapshots.aggregate(Sum('enrolled_students'))['enrolled_students__sum'] or 0
            else:
                # If no latest month, use max students across months
                students = course_snapshots.aggregate(Max('enrolled_students'))['enrolled_students__max'] or 0
            
            # Revenue and profit: Sum across all months
            revenue = float(course_snapshots.aggregate(Sum('revenue'))['revenue__sum'] or 0)
            profit = float(course_snapshots.aggregate(Sum('profit'))['profit__sum'] or 0)
            lessons_count = course_snapshots.count()
            
            # Occupancy calculation
            capacity = 20  # Mock capacity per lesson
            occupancy = min(100, (students / capacity * 100)) if capacity > 0 else 0
            
            if occupancy >= 90:
                full_capacity_count += 1
            if occupancy < 50:
                low_occupancy_count += 1
                low_occupancy_courses.append({
                    'course_id': str(cid),
                    'name': course_name,
                    'branch': branch_name,
                    'occupancy': round(occupancy, 1)
                })
            
            course_list.append({
                'course_id': str(cid),
                'name': course_name,
                'branch': branch_name,
                'lessons': lessons_count,
                'students': int(students),
                'occupancy': round(occupancy, 1),
                'revenue': revenue,
                'profit': profit
            })
        
        # Sort course_list by revenue
        course_list.sort(key=lambda x: x['revenue'], reverse=True)
        
        # Top 5 courses by students
        top_courses = sorted(course_list, key=lambda x: x['students'], reverse=True)[:5]
        
        return Response({
            'kpis': {
                'total_courses': total_courses,
                'active_courses': active_courses,
                'full_capacity': full_capacity_count,
                'low_occupancy': low_occupancy_count
            },
            'top_courses': top_courses,
            'low_occupancy_courses': low_occupancy_courses[:5],
            'course_list': course_list
        })
    
    @action(detail=False, methods=['get'], url_path='branches')
    def branches_data(self, request):
        """
        Branches dashboard data
        Query params:
        - branch_id: Filter by branch
        - city_id: Filter by city
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        
        Note: Current month snapshots are refreshed dynamically on each request.
              Past month snapshots use existing data (immutable).
        """
        branch_id = request.query_params.get('branch_id', 'all')
        city_id = request.query_params.get('city_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        
        # Auto-refresh current month snapshots (dynamic calculation)
        from apps.instructors.utils import generate_monthly_snapshots
        current_month = timezone.now().strftime('%Y-%m')
        if current_month in months:
            try:
                generate_monthly_snapshots(current_month, finalize=False)
            except Exception as e:
                # Log error but continue - use existing snapshot if refresh fails
                logger.error(f"Failed to refresh current month snapshots: {e}")
        
        # Query BranchMonthlySnapshot (now includes refreshed current month)
        snapshots = BranchMonthlySnapshot.objects.filter(month__in=months)
        
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(branch_id=branch_id)
        
        # Apply city filter
        if city_id and city_id != 'all':
            snapshots = snapshots.filter(branch__city_id=city_id)
        
        snapshots = snapshots.select_related('branch', 'branch__city')
        
        # === LOGGING: Branches Data KPIs ===
        # NOTE: use logger (not print) so non-ASCII branch names don't crash the
        # response on Windows consoles whose default codec is cp1252.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("BRANCHES DATA - KPI CALCULATION")
            logger.debug("Branch filter: %s", branch_id)
            logger.debug("City filter: %s", city_id)
            logger.debug("Date range: %s to %s", date_from, date_to)
            logger.debug("Months included: %s", months)
            logger.debug("Number of snapshots found: %s", snapshots.count())
            for snap in snapshots:
                logger.debug(
                    "  Snapshot: %s | %s | Revenue: %s | Costs: %s | Profit: %s",
                    snap.branch.name, snap.month, snap.total_revenue,
                    snap.instructor_costs, snap.profit,
                )
        
        # KPIs - Only total_students and total_profit (removed active_branches and avg_room_utilization)
        # For students: Count only children whose status is NOT ghost, non_active, or sign_in
        # Use direct query on Child model for accurate filtering
        children_query = Child.objects.exclude(
            status__in=['ghost', 'non_active', 'sign_in']
        )
        
        # Apply branch filter
        if branch_id and branch_id != 'all':
            children_query = children_query.filter(family__branch_id=branch_id)
        
        # Apply city filter
        if city_id and city_id != 'all':
            children_query = children_query.filter(family__branch__city_id=city_id)
        
        total_students = children_query.count()
        
        # Total profit is sum across all months in the period
        total_profit_agg = snapshots.aggregate(Sum('profit'))
        total_profit = total_profit_agg['profit__sum'] or Decimal('0.00')

        from apps.scheduling.studio_rental_finance import aggregate_studio_rental_revenue

        rental_agg = aggregate_studio_rental_revenue(
            date_from,
            date_to,
            branch_id if branch_id and branch_id != 'all' else None,
            city_id if city_id and city_id != 'all' else None,
        )
        rental_dec = rental_agg['total'] or Decimal('0.00')
        total_profit = total_profit + rental_dec
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Total Students (from Child model): %s", total_students)
            logger.debug("Total Profit aggregation result: %s", total_profit_agg)
            logger.debug("Total Profit (sum of snapshots.profit): %s", total_profit)
        
        # Branch data - aggregate properly across months
        # Get all unique branches
        branch_ids = snapshots.values_list('branch_id', flat=True).distinct()
        
        branch_list = []
        
        for bid in branch_ids:
            branch_snapshots = snapshots.filter(branch_id=bid)
            first_snapshot = branch_snapshots.first()
            if not first_snapshot:
                continue
            
            branch_name = first_snapshot.branch.name
            
            # Students: Count only children whose status is NOT ghost, non_active, or sign_in
            students_count = Child.objects.filter(
                family__branch_id=bid
            ).exclude(
                status__in=['ghost', 'non_active', 'sign_in']
            ).count()
            
            # Revenue, profit, and spending: Sum across all months
            revenue = float(branch_snapshots.aggregate(Sum('total_revenue'))['total_revenue__sum'] or 0)
            profit = float(branch_snapshots.aggregate(Sum('profit'))['profit__sum'] or 0)
            rental_extra = float(rental_agg['by_branch_id'].get(str(bid), 0) or 0)
            revenue += rental_extra
            profit += rental_extra
            spending = float(branch_snapshots.aggregate(Sum('instructor_costs'))['instructor_costs__sum'] or 0)
            lessons_count = branch_snapshots.count()  # Number of month records
            
            # Removed profit_margin, rooms, and room_utilization
            branch_info = {
                'branch_id': str(bid),
                'name': branch_name,
                'students': students_count,
                'lessons': lessons_count * 4,  # Approximate lessons per month
                'revenue': revenue,
                'profit': profit,
                'spending': spending
            }
            
            branch_list.append(branch_info)

        # Branches that only appear via studio rental revenue (no snapshot rows)
        seen_branch_ids = {str(x['branch_id']) for x in branch_list}
        for bid_str, amt in rental_agg['by_branch_id'].items():
            if bid_str in seen_branch_ids:
                continue
            b = Branch.objects.filter(pk=bid_str).select_related('city').first()
            if not b:
                continue
            if city_id and city_id != 'all' and str(b.city_id) != str(city_id):
                continue
            rental_extra = float(amt or 0)
            students_count = Child.objects.filter(family__branch_id=bid_str).exclude(
                status__in=['ghost', 'non_active', 'sign_in']
            ).count()
            branch_list.append({
                'branch_id': bid_str,
                'name': b.name,
                'students': students_count,
                'lessons': 0,
                'revenue': rental_extra,
                'profit': rental_extra,
                'spending': 0.0,
            })
        
        # Sort by revenue
        branch_list.sort(key=lambda x: x['revenue'], reverse=True)
        
        # Branch comparison - now includes spending
        branch_comparison = []
        for item in branch_list:
            branch_comparison.append({
                'branch': item['name'],
                'revenue': item['revenue'],
                'profit': item['profit'],
                'spending': item['spending']
            })
        
        # Discount breakdown by type for filtered branches
        from apps.core.revenue_service import RevenueService
        from apps.customers.models import Payment, PaymentDiscountSnapshot
        
        revenue_service = RevenueService()
        
        # Get discount metrics for all filtered branches
        discount_breakdown = []
        total_discounts = {
            'early_signup': Decimal('0.00'),
            'second_child': Decimal('0.00'),
            'additional_lesson': Decimal('0.00'),
            'fixed_final_price': Decimal('0.00')
        }
        
        # Query payments in the date range for filtered branches
        payment_filters = Q(
            status='completed',
            payment_date__date__gte=date_from,
            payment_date__date__lte=date_to
        )
        
        if branch_id and branch_id != 'all':
            payment_filters &= Q(branch_id=branch_id)
        
        if city_id and city_id != 'all':
            payment_filters &= Q(branch__city_id=city_id)
        
        # Get discount breakdown from PaymentDiscountSnapshot
        snapshots = PaymentDiscountSnapshot.objects.filter(
            payment__in=Payment.objects.filter(payment_filters)
        )
        
        # Aggregate by discount type with counts
        early_signup_data = snapshots.filter(
            discount_type='early_signup'
        ).aggregate(total=Sum('amount_deducted'), count=Count('id'))
        
        second_child_data = snapshots.filter(
            discount_type='second_child'
        ).aggregate(total=Sum('amount_deducted'), count=Count('id'))
        
        additional_lesson_data = snapshots.filter(
            discount_type='additional_lesson'
        ).aggregate(total=Sum('amount_deducted'), count=Count('id'))
        
        fixed_final_price_data = snapshots.filter(
            discount_type='fixed_final_price'
        ).aggregate(total=Sum('amount_deducted'), count=Count('id'))
        
        discount_breakdown = [
            {
                'type': 'רישום מוקדם',
                'amount': float(early_signup_data['total'] or 0),
                'count': early_signup_data['count']
            },
            {
                'type': 'ילד שני',
                'amount': float(second_child_data['total'] or 0),
                'count': second_child_data['count']
            },
            {
                'type': 'שיעור נוסף',
                'amount': float(additional_lesson_data['total'] or 0),
                'count': additional_lesson_data['count']
            },
            {
                'type': 'מחיר קבוע',
                'amount': float(fixed_final_price_data['total'] or 0),
                'count': fixed_final_price_data['count']
            }
        ]
        
        # Filter out zero amounts for cleaner visualization
        discount_breakdown = [d for d in discount_breakdown if d['amount'] > 0]
        
        return Response({
            'kpis': {
                'total_students': total_students,
                'total_profit': float(total_profit)
            },
            'branch_comparison': branch_comparison,
            'branch_list': branch_list,
            'discount_breakdown': discount_breakdown
        })
    
    @action(detail=False, methods=['post'], url_path='refresh-current-month')
    def refresh_current_month(self, request):
        """
        Manually refresh current month snapshots
        
        Note: This endpoint is for manual refresh only.
        Dashboard endpoints automatically refresh current month on each view.
        Use this if you need to force a refresh outside of dashboard viewing.
        """
        from apps.instructors.utils import generate_monthly_snapshots
        
        today = timezone.now().date()
        current_month = today.strftime('%Y-%m')
        
        try:
            # Generate snapshots for current month (not finalized)
            summary = generate_monthly_snapshots(current_month, finalize=False)
            
            return Response({
                'success': True,
                'message': f'נתוני {current_month} עודכנו בהצלחה',
                'summary': summary
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': f'שגיאה בעדכון נתונים: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

