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
from django.db.models import Sum, Count, Q, Avg, Max, F
from django.db.models.functions import TruncMonth
from django.utils import timezone
from datetime import datetime, timedelta, date
from decimal import Decimal

from apps.core.permissions import IsManager, IsManagerOrPartner
from apps.core.scoping import (
    is_scoped_partner,
    partner_branch_ids,
    partner_course_ids,
    partner_instructor_ids,
)

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
    All endpoints require authentication and manager role.
    """
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def _scope(self, request):
        """Partners see dashboard data only for assigned branches.

        Returns (scoped, course_ids, branch_ids, instructor_ids); the id lists
        are always real lists when scoped=True so they're safe for __in filters.
        """
        user = request.user
        if is_scoped_partner(user):
            branch_ids = partner_branch_ids(user)
            if not branch_ids:
                return True, [], [], []
            return True, partner_course_ids(user), branch_ids, partner_instructor_ids(user)
        return False, None, None, None
    
    @action(detail=False, methods=['get'], url_path='financial')
    def financial_data(self, request):
        """
        Financial dashboard data
        Query params:
        - branch_id: Filter by branch (default: 'all')
        - date_from: Start date (YYYY-MM-DD)
        - date_to: End date (YYYY-MM-DD)
        
        Note: Reads precomputed monthly snapshots. Refresh current month via
              POST /core/dashboard/refresh-current-month/ or the Celery beat task.
        """
        branch_id = request.query_params.get('branch_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)

        scoped, _c_ids, scoped_branch_ids, scoped_instr_ids = self._scope(request)

        snapshots = BranchMonthlySnapshot.objects.filter(month__in=months)
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(branch_id=branch_id)
        if scoped:
            snapshots = snapshots.filter(branch_id__in=scoped_branch_ids)

        from apps.scheduling.studio_rental_finance import aggregate_studio_rental_revenue

        rental_agg = aggregate_studio_rental_revenue(
            date_from,
            date_to,
            branch_id if branch_id and branch_id != 'all' else None,
            None,
            branch_ids=scoped_branch_ids if scoped else None,
        )
        rental_total = rental_agg['total'] or Decimal('0.00')

        kpi_agg = snapshots.aggregate(
            total_revenue=Sum('total_revenue'),
            total_expenses=Sum('instructor_costs'),
            instructor_salaries=Sum('instructor_salaries'),
            instructor_bonuses=Sum('instructor_bonuses'),
            operational_costs=Sum('operational_costs'),
        )
        total_revenue = (kpi_agg['total_revenue'] or Decimal('0.00')) + rental_total
        total_expenses = kpi_agg['total_expenses'] or Decimal('0.00')
        net_profit = total_revenue - total_expenses

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("FINANCIAL OVERVIEW - KPI CALCULATION")
            logger.debug("Branch filter: %s", branch_id)
            logger.debug("Date range: %s to %s", date_from, date_to)
            logger.debug("Months included: %s", months)
            logger.debug("Total Revenue: %s", total_revenue)
            logger.debug("Total Expenses: %s", total_expenses)
            logger.debug("Net Profit: %s", net_profit)

        total_instructor_salaries = kpi_agg['instructor_salaries'] or Decimal('0.00')
        total_instructor_bonuses = kpi_agg['instructor_bonuses'] or Decimal('0.00')
        total_operational_costs = kpi_agg['operational_costs'] or Decimal('0.00')

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

        known_ids = {r['branch_id'] for r in revenue_by_branch}
        missing_branch_ids = [
            bid for bid in rental_agg['by_branch_id'] if bid not in known_ids
        ]
        extra_branches = {
            str(branch.id): branch
            for branch in Branch.objects.filter(pk__in=missing_branch_ids).only('id', 'name')
        }
        for bid in missing_branch_ids:
            branch = extra_branches.get(bid)
            if not branch:
                continue
            extra = float(rental_agg['by_branch_id'].get(bid, 0) or 0)
            revenue_by_branch.append({
                'branch_name': branch.name,
                'branch_id': bid,
                'revenue': extra,
                'expenses': 0.0,
                'profit': extra,
            })

        monthly_by_month = {
            row['month']: row
            for row in snapshots.values('month').annotate(
                revenue=Sum('total_revenue'),
                expenses=Sum('instructor_costs'),
            )
        }
        monthly_trends = []
        for month in sorted(months):
            row = monthly_by_month.get(month, {})
            extra_month = float(rental_agg['by_month'].get(month, 0) or 0)
            monthly_trends.append({
                'month': month,
                'revenue': float(row.get('revenue') or 0) + extra_month,
                'expenses': float(row.get('expenses') or 0)
            })
        
        # Revenue by instructor (top 8)
        instructor_snapshots = InstructorMonthlySnapshot.objects.filter(month__in=months)
        if branch_id and branch_id != 'all':
            instructor_snapshots = instructor_snapshots.filter(
                instructor__primary_branch_id=branch_id
            )
        if scoped:
            instructor_snapshots = instructor_snapshots.filter(
                instructor_id__in=scoped_instr_ids
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
        
        Note: Reads precomputed monthly snapshots. Refresh current month via
              POST /core/dashboard/refresh-current-month/ or the Celery beat task.
        """
        instructor_id = request.query_params.get('instructor_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        # Query InstructorMonthlySnapshot (now includes refreshed current month)
        snapshots = InstructorMonthlySnapshot.objects.filter(month__in=months)
        
        if instructor_id and instructor_id != 'all':
            snapshots = snapshots.filter(instructor_id=instructor_id)
        
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(instructor__primary_branch_id=branch_id)

        scoped, _c_ids, _b_ids, scoped_instr_ids = self._scope(request)
        if scoped:
            snapshots = snapshots.filter(instructor_id__in=scoped_instr_ids)
        
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
        - quit_date_from / quit_date_to: optional; limits churn (אחוז נשירה)
          and נושרים לפי תחום. Defaults to all-time when omitted.
        - quit_chart_breakdown: course_type (default) or course — bar chart grouping
        - quit_chart_filter_id: optional; limits bar chart to one course type or course
        """
        from apps.customers.status_history_models import ChildStatusHistory
        
        search_query = request.query_params.get('search_query', '')
        course_id = request.query_params.get('course_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        student_status = request.query_params.get('student_status', 'all')
        quit_chart_breakdown = request.query_params.get('quit_chart_breakdown', 'course_type')
        if quit_chart_breakdown not in ('course_type', 'course'):
            quit_chart_breakdown = 'course_type'
        quit_chart_filter_id = request.query_params.get('quit_chart_filter_id', 'all')
        date_from, date_to = parse_date_filters(request)
        
        # Optional window for quit / churn only (defaults to all-time, not main date range)
        quit_df_str = request.query_params.get('quit_date_from')
        quit_dt_str = request.query_params.get('quit_date_to')
        if quit_df_str and quit_dt_str:
            try:
                quit_date_from = datetime.strptime(quit_df_str, '%Y-%m-%d').date()
                quit_date_to = datetime.strptime(quit_dt_str, '%Y-%m-%d').date()
                if quit_date_from > quit_date_to:
                    quit_date_from, quit_date_to = quit_date_to, quit_date_from
            except ValueError:
                quit_date_from = date(2020, 1, 1)
                quit_date_to = timezone.now().date()
        else:
            quit_date_from = date(2020, 1, 1)
            quit_date_to = timezone.now().date()
        
        scoped, scoped_course_ids, scoped_branch_ids, _i_ids = self._scope(request)

        # Base children queryset (search/status/scoped manager — before branch/course KPI filter)
        base_children = Child.objects.all()
        if scoped:
            base_children = base_children.filter(
                lesson_enrollments__lesson__course_id__in=scoped_course_ids
            ).distinct()
        
        if search_query:
            base_children = base_children.filter(
                Q(first_name__icontains=search_query) | Q(last_name__icontains=search_query)
            )
        
        if student_status and student_status != 'all':
            base_children = base_children.filter(status=student_status)

        # KPIs: branch/course filters use currently active enrollments only
        children = base_children
        if course_id != 'all' or branch_id != 'all':
            filtered_enrollments = LessonEnrollment.objects.filter(status='active')
            if scoped:
                filtered_enrollments = filtered_enrollments.filter(
                    lesson__course_id__in=scoped_course_ids
                )
            if course_id != 'all':
                filtered_enrollments = filtered_enrollments.filter(lesson__course_id=course_id)
            if branch_id != 'all':
                filtered_enrollments = filtered_enrollments.filter(lesson__course__branch_id=branch_id)
            filtered_child_ids = filtered_enrollments.values_list('child_id', flat=True).distinct()
            children = children.filter(id__in=filtered_child_ids)

        # Churn scope: any enrollment in branch/course (includes kids who already quit)
        quit_scope_child_ids = None
        if course_id != 'all' or branch_id != 'all':
            quit_enrollments = LessonEnrollment.objects.all()
            if scoped:
                quit_enrollments = quit_enrollments.filter(
                    lesson__course_id__in=scoped_course_ids
                )
            if course_id != 'all':
                quit_enrollments = quit_enrollments.filter(lesson__course_id=course_id)
            if branch_id != 'all':
                quit_enrollments = quit_enrollments.filter(lesson__course__branch_id=branch_id)
            quit_scope_child_ids = base_children.filter(
                id__in=quit_enrollments.values_list('child_id', flat=True).distinct()
            ).values_list('id', flat=True)
        elif scoped:
            quit_scope_child_ids = base_children.values_list('id', flat=True)
        
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
        
        # Abnormal Attendance by Branch (via active lesson enrollment branch, not family branch)
        abnormal_by_branch = []
        abnormal_base = base_children.filter(absent_irregularly=True)
        abnormal_enrollment_q = Q(lesson_enrollments__status='active')
        if course_id != 'all':
            abnormal_enrollment_q &= Q(lesson_enrollments__lesson__course_id=course_id)

        abnormal_counts = {
            str(row['branch_id']): row['count']
            for row in abnormal_base.filter(abnormal_enrollment_q).values(
                branch_id=F('lesson_enrollments__lesson__course__branch_id'),
                branch_name=F('lesson_enrollments__lesson__course__branch__name'),
            ).annotate(count=Count('pk', distinct=True))
            if row['branch_id']
        }

        if branch_id == 'all':
            branches = Branch.objects.filter(is_active=True)
            if scoped and scoped_branch_ids:
                branches = branches.filter(id__in=scoped_branch_ids)
            for branch in branches:
                bid = str(branch.id)
                abnormal_by_branch.append({
                    'branch_id': bid,
                    'branch_name': branch.name,
                    'count': abnormal_counts.get(bid, 0),
                })
        else:
            try:
                branch = Branch.objects.get(id=branch_id)
                abnormal_by_branch.append({
                    'branch_id': str(branch.id),
                    'branch_name': branch.name,
                    'count': abnormal_counts.get(str(branch.id), 0),
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
        
        if quit_scope_child_ids is not None:
            status_changes = status_changes.filter(child_id__in=quit_scope_child_ids)
        
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
        status_change_rows = list(status_changes.select_related('child'))
        changes_by_status = {}
        for change in status_change_rows:
            changes_by_status.setdefault(change.new_status, []).append(change)

        for item in quit_by_status:
            percentage = (item['count'] / total_quit * 100) if total_quit > 0 else 0
            status_key = item['new_status']
            child_details = [
                {
                    'id': str(change.child.id),
                    'full_name': change.child.full_name,
                    'id_number': change.child.id_number or '',
                    'changed_at': change.changed_at.isoformat(),
                }
                for change in changes_by_status.get(status_key, [])
            ]

            quit_data.append({
                'status': status_labels.get(status_key, status_key),
                'status_key': status_key,
                'count': item['count'],
                'percentage': round(percentage, 1),
                'children': child_details,
            })

        # Quit breakdown by course type or course (bar chart only)
        quit_by_course_type = []
        quit_by_course = []

        def _build_quit_breakdown(breakdown_by, filter_id):
            if total_quit <= 0:
                return []

            child_ids_in_quit = list(
                status_changes.values_list('child_id', flat=True).distinct()
            )
            if not child_ids_in_quit:
                return []

            enrollment_rows = LessonEnrollment.objects.filter(
                child_id__in=child_ids_in_quit,
            )
            if course_id != 'all':
                enrollment_rows = enrollment_rows.filter(lesson__course_id=course_id)
            if branch_id != 'all':
                enrollment_rows = enrollment_rows.filter(lesson__course__branch_id=branch_id)
            if filter_id != 'all':
                if breakdown_by == 'course_type':
                    enrollment_rows = enrollment_rows.filter(
                        lesson__course__course_type_id=filter_id
                    )
                else:
                    enrollment_rows = enrollment_rows.filter(lesson__course_id=filter_id)

            child_groups: dict = {}
            if breakdown_by == 'course_type':
                rows = enrollment_rows.filter(
                    lesson__course__course_type__isnull=False,
                ).values_list(
                    'child_id',
                    'lesson__course__course_type_id',
                    'lesson__course__course_type__name',
                ).distinct()
                for child_id, group_id, group_name in rows:
                    if not group_id:
                        continue
                    child_groups.setdefault(child_id, set()).add((str(group_id), group_name))
                unknown_label = 'ללא תחום'
                agg_key = lambda gid, gname: gid
                item_builder = lambda gid, gname: {
                    'course_type_id': gid,
                    'course_type_name': gname,
                    'count': 0,
                }
            else:
                rows = enrollment_rows.values_list(
                    'child_id',
                    'lesson__course_id',
                    'lesson__course__name',
                ).distinct()
                for child_id, group_id, group_name in rows:
                    if not group_id:
                        continue
                    child_groups.setdefault(child_id, set()).add((str(group_id), group_name))
                unknown_label = 'ללא חוג'
                agg_key = lambda gid, gname: gid
                item_builder = lambda gid, gname: {
                    'course_id': gid,
                    'course_name': gname,
                    'count': 0,
                }

            agg: dict = {}
            unknown_count = 0
            for change in status_change_rows:
                groups = child_groups.get(change.child_id)
                if not groups:
                    unknown_count += 1
                    continue
                for group_id, group_name in groups:
                    entry = agg.setdefault(
                        agg_key(group_id, group_name),
                        item_builder(group_id, group_name),
                    )
                    entry['count'] += 1

            result = sorted(agg.values(), key=lambda x: x['count'], reverse=True)
            if unknown_count > 0:
                if breakdown_by == 'course_type':
                    result.append({
                        'course_type_id': None,
                        'course_type_name': unknown_label,
                        'count': unknown_count,
                    })
                else:
                    result.append({
                        'course_id': None,
                        'course_name': unknown_label,
                        'count': unknown_count,
                    })
            return result

        type_filter = quit_chart_filter_id if quit_chart_breakdown == 'course_type' else 'all'
        course_filter = quit_chart_filter_id if quit_chart_breakdown == 'course' else 'all'
        quit_by_course_type = _build_quit_breakdown('course_type', type_filter)
        quit_by_course = _build_quit_breakdown('course', course_filter)

        # Monthly attendance rate from start of year through current month (respects filters)
        today = timezone.now().date()
        attendance_year_start = date(today.year, 1, 1)
        attendance_end = min(date_to, today) if date_to else today

        attendance_qs = LessonAttendance.objects.filter(
            occurrence_date__gte=attendance_year_start,
            occurrence_date__lte=attendance_end,
        ).exclude(status='not_marked').filter(child__in=children)

        if course_id != 'all':
            attendance_qs = attendance_qs.filter(lesson__course_id=course_id)
        if branch_id != 'all':
            attendance_qs = attendance_qs.filter(lesson__course__branch_id=branch_id)

        attendance_by_month = []
        monthly_attendance = {
            row['month'].strftime('%Y-%m'): row
            for row in attendance_qs.annotate(month=TruncMonth('occurrence_date')).values('month').annotate(
                total=Count('id'),
                present=Count('id', filter=Q(status='present')),
            )
        }
        for month_str in dates_to_month_list(attendance_year_start, attendance_end):
            row = monthly_attendance.get(month_str, {})
            total = row.get('total', 0) or 0
            present = row.get('present', 0) or 0
            attendance_by_month.append({
                'month': month_str,
                'attendance_rate': round((present / total * 100), 1) if total > 0 else 0,
                'total_records': total,
                'present_count': present,
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
                'by_course': quit_by_course,
            },
            'attendance_by_month': attendance_by_month,
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
        
        Note: Reads precomputed monthly snapshots. Refresh current month via
              POST /core/dashboard/refresh-current-month/ or the Celery beat task.
        """
        course_id = request.query_params.get('course_id', 'all')
        branch_id = request.query_params.get('branch_id', 'all')
        city_id = request.query_params.get('city_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        scoped, scoped_course_ids, _b_ids, _i_ids = self._scope(request)

        # Query LessonMonthlySnapshot grouped by course (now includes refreshed current month)
        snapshots = LessonMonthlySnapshot.objects.filter(month__in=months)
        if scoped:
            snapshots = snapshots.filter(course_id__in=scoped_course_ids)
        
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
        if scoped:
            courses_query = courses_query.filter(id__in=scoped_course_ids)
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
                lessons_query = lessons_query.filter(course__branch_id=branch_id)
            if city_id and city_id != 'all':
                lessons_query = lessons_query.filter(course__branch__city_id=city_id)
            
            active_courses = lessons_query.values('course_id').distinct().count()
        
        # Aggregate by course in one grouped query (avoid per-course snapshot filters)
        latest_month = max(months) if months else None

        if latest_month:
            students_by_course = {
                row['course_id']: row['students'] or 0
                for row in snapshots.filter(month=latest_month).values('course_id').annotate(
                    students=Sum('enrolled_students'),
                )
            }
        else:
            students_by_course = {
                row['course_id']: row['students'] or 0
                for row in snapshots.values('course_id').annotate(
                    students=Max('enrolled_students'),
                )
            }

        course_list = []
        low_occupancy_courses = []
        full_capacity_count = 0
        low_occupancy_count = 0

        course_rows = snapshots.values(
            'course_id',
            'course__name',
            'branch__name',
        ).annotate(
            revenue_total=Sum('revenue'),
            profit_total=Sum('profit'),
            snapshot_rows=Count('id'),
        ).order_by('-revenue_total')

        for row in course_rows:
            cid = row['course_id']
            students = int(students_by_course.get(cid, 0) or 0)
            revenue = float(row['revenue_total'] or 0)
            profit = float(row['profit_total'] or 0)
            lessons_count = row['snapshot_rows'] or 0
            course_name = row['course__name']
            branch_name = row['branch__name']

            capacity = 20
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
                'students': students,
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
        
        Note: Reads precomputed monthly snapshots. Refresh current month via
              POST /core/dashboard/refresh-current-month/ or the Celery beat task.
        """
        branch_id = request.query_params.get('branch_id', 'all')
        city_id = request.query_params.get('city_id', 'all')
        date_from, date_to = parse_date_filters(request)
        months = dates_to_month_list(date_from, date_to)
        scoped, _c_ids, scoped_branch_ids, _i_ids = self._scope(request)

        # Query BranchMonthlySnapshot (now includes refreshed current month)
        snapshots = BranchMonthlySnapshot.objects.filter(month__in=months)
        
        if branch_id and branch_id != 'all':
            snapshots = snapshots.filter(branch_id=branch_id)
        if scoped:
            snapshots = snapshots.filter(branch_id__in=scoped_branch_ids)
        
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
        if scoped:
            children_query = children_query.filter(family__branch_id__in=scoped_branch_ids)
        
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
            branch_ids=scoped_branch_ids if scoped else None,
        )
        rental_dec = rental_agg['total'] or Decimal('0.00')
        total_profit = total_profit + rental_dec
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Total Students (from Child model): %s", total_students)
            logger.debug("Total Profit aggregation result: %s", total_profit_agg)
            logger.debug("Total Profit (sum of snapshots.profit): %s", total_profit)
        
        excluded_child_statuses = ['ghost', 'non_active', 'sign_in']

        branch_rows = snapshots.values(
            'branch_id',
            'branch__name',
        ).annotate(
            revenue_total=Sum('total_revenue'),
            profit_total=Sum('profit'),
            spending_total=Sum('instructor_costs'),
            month_rows=Count('id'),
        )

        branch_ids_in_snapshots = [row['branch_id'] for row in branch_rows]
        student_counts = {
            str(row['family__branch_id']): row['count']
            for row in Child.objects.filter(family__branch_id__in=branch_ids_in_snapshots).exclude(
                status__in=excluded_child_statuses
            ).values('family__branch_id').annotate(count=Count('id'))
            if row['family__branch_id']
        }

        branch_list = []
        for row in branch_rows:
            bid = row['branch_id']
            bid_str = str(bid)
            rental_extra = float(rental_agg['by_branch_id'].get(bid_str, 0) or 0)
            revenue = float(row['revenue_total'] or 0) + rental_extra
            profit = float(row['profit_total'] or 0) + rental_extra
            branch_list.append({
                'branch_id': bid_str,
                'name': row['branch__name'],
                'students': student_counts.get(bid_str, 0),
                'lessons': (row['month_rows'] or 0) * 4,
                'revenue': revenue,
                'profit': profit,
                'spending': float(row['spending_total'] or 0),
            })

        seen_branch_ids = {item['branch_id'] for item in branch_list}
        missing_rental_ids = [
            bid_str for bid_str in rental_agg['by_branch_id']
            if bid_str not in seen_branch_ids
        ]
        extra_branches = {
            str(b.id): b
            for b in Branch.objects.filter(pk__in=missing_rental_ids).select_related('city')
        }
        if missing_rental_ids:
            rental_student_counts = {
                str(row['family__branch_id']): row['count']
                for row in Child.objects.filter(family__branch_id__in=missing_rental_ids).exclude(
                    status__in=excluded_child_statuses
                ).values('family__branch_id').annotate(count=Count('id'))
                if row['family__branch_id']
            }
        else:
            rental_student_counts = {}

        for bid_str in missing_rental_ids:
            b = extra_branches.get(bid_str)
            if not b:
                continue
            if city_id and city_id != 'all' and str(b.city_id) != str(city_id):
                continue
            rental_extra = float(rental_agg['by_branch_id'].get(bid_str, 0) or 0)
            branch_list.append({
                'branch_id': bid_str,
                'name': b.name,
                'students': rental_student_counts.get(bid_str, 0),
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
        Manually refresh current month snapshots.

        Dashboard GET endpoints read existing snapshots only (fast). Use this
        endpoint or the Celery beat task to regenerate current-month data.
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

