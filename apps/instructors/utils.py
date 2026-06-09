"""
Utility functions for instructor financial calculations
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
import calendar
from typing import Optional
from django.db import transaction
from django.db.models import Count, Q, Sum
from apps.enrollments.enrollment_counts import TRIAL_CHILD_STATUSES, is_paying_enrollment
from apps.enrollments.models import LessonEnrollment


# Constants
DEFAULT_LESSON_SALARY = Decimal('250.00')
LESSONS_PER_MONTH = 4


SALARY_STUDENT_STATUSES = ("active", "payments_problem")
REVENUE_ENROLLMENT_STATUSES = ("active",)  # revenue approximated as "paid" enrollments only


def calculate_lesson_salary(student_count, instructor):
    """
    Calculate salary for a lesson based on student count and instructor's salary model
    
    Args:
        student_count: Number of enrolled students
        instructor: Instructor instance
    
    Returns:
        Decimal: Salary amount for the lesson
    
    Note:
        If student_count is 0 and instructor has tiered model, they get the lowest tier salary.
        Instructors are paid even if they have 0 students enrolled.
    """
    if instructor.salary_model_type == 'tiered_by_students':
        # Check if instructor has salary tiers defined
        salary_tiers = instructor.salary_tiers.all().order_by('min_students')
        if salary_tiers.exists():
            # Special case: 0 students - pay the lowest tier
            if student_count == 0:
                first_tier = salary_tiers.first()
                return first_tier.salary_per_lesson
            
            # Find the matching tier
            for tier in salary_tiers:
                if tier.max_students is None:
                    # This is the "and above" tier
                    if student_count >= tier.min_students:
                        return tier.salary_per_lesson
                else:
                    # Regular tier with min and max
                    if tier.min_students <= student_count <= tier.max_students:
                        return tier.salary_per_lesson
            
            # If no tier matches (shouldn't happen with proper tier setup), use first tier
            return salary_tiers.first().salary_per_lesson
        else:
            # No tiers defined, fall back to fixed or default
            return instructor.fixed_salary_per_lesson or DEFAULT_LESSON_SALARY
    
    # Fixed per lesson model - always paid regardless of student count
    return instructor.fixed_salary_per_lesson or DEFAULT_LESSON_SALARY


def calculate_lesson_salary_with_override(student_count, instructor, salary_override=None):
    """
    Calculate salary for a lesson, respecting lesson-level override when provided.
    """
    if salary_override is not None:
        return salary_override
    return calculate_lesson_salary(student_count, instructor)


def get_course_team_monthly_salary(course) -> Optional[Decimal]:
    """Monthly instructor pay configured on the team (Course.instructor_salary_override)."""
    if course is None or course.instructor_salary_override is None:
        return None
    return Decimal(str(course.instructor_salary_override))


def _count_recurring_lesson_templates(course) -> int:
    count = course.lessons.filter(is_recurring=True).exclude(status='cancelled').count()
    return count or 1


def get_lesson_monthly_salary_share(lesson) -> Optional[Decimal]:
    """Split course monthly instructor pay evenly across recurring lesson slots."""
    course = getattr(lesson, 'course', None)
    monthly = get_course_team_monthly_salary(course)
    if monthly is None:
        return None
    return monthly / Decimal(_count_recurring_lesson_templates(course))


def get_lesson_price(lesson):
    """Monthly subscription price for the group (course); not per meeting."""
    return lesson.course.price if lesson.course else Decimal('0.00')


def get_lesson_price_for_course_index(lesson, course_index: int) -> Decimal:
    """
    Pick the lesson price for a student based on which course this is for them.

    course_index is 1-based: 1 = first course (regular price), 2 = student's
    second course, 3 = third, etc.

    Resolution order:
    1. If course_index >= 2, look in lesson.additional_course_prices for an exact
       match. If none, fall back to the closest tier with course_index <= the
       requested index (so a tier defined at "3" also covers index 4 unless a
       higher tier overrides).
    2. Otherwise, fall back to the course monthly price (course_index <= 1).
    """
    base_price = lesson.course.price if lesson.course else Decimal('0.00')

    try:
        idx = int(course_index)
    except (TypeError, ValueError):
        return base_price
    if idx <= 1:
        return base_price

    tiers = list(lesson.additional_course_prices or [])
    if not tiers:
        # Backwards-compat: lesson_price_override historically meant "second course price"
        if lesson.lesson_price_override and idx >= 2:
            return Decimal(str(lesson.lesson_price_override))
        return base_price

    best = None
    best_index = -1
    for tier in tiers:
        try:
            tier_index = int(tier.get('course_index'))
            tier_price = Decimal(str(tier.get('price')))
        except (TypeError, ValueError, InvalidOperation):
            continue
        if tier_index <= idx and tier_index > best_index:
            best = tier_price
            best_index = tier_index
    if best is not None:
        return best

    if lesson.lesson_price_override and idx >= 2:
        return Decimal(str(lesson.lesson_price_override))
    return base_price


def _enrollment_overlaps_range(enrollment: LessonEnrollment, start_d: date, end_d: date) -> bool:
    """
    An enrollment contributes if its [start_date, end_date] overlaps the requested range.
    Null endpoints are treated as open intervals.
    """
    if enrollment.start_date and enrollment.start_date > end_d:
        return False
    if enrollment.end_date and enrollment.end_date < start_d:
        return False
    return True


def _count_enrollments_for_period(lesson, start_d: date, end_d: date, statuses: tuple[str, ...]) -> int:
    """
    Count enrollments for a lesson that:
    - have status in statuses
    - overlap the requested [start_d, end_d] range
    Uses prefetched enrollments when available.
    """
    enrollments = getattr(lesson, "enrollments", None)
    if enrollments is None:
        qs = LessonEnrollment.objects.filter(lesson=lesson, status__in=statuses).exclude(
            child__status__in=TRIAL_CHILD_STATUSES,
        )
        return qs.filter(
            Q(start_date__isnull=True) | Q(start_date__lte=end_d),
            Q(end_date__isnull=True) | Q(end_date__gte=start_d),
        ).count()

    cnt = 0
    for e in enrollments.all():
        if e.status in statuses and _enrollment_overlaps_range(e, start_d, end_d):
            if getattr(e, 'child', None) and e.child.status in TRIAL_CHILD_STATUSES:
                continue
            cnt += 1
    return cnt


def _unique_students_for_period(lesson, start_d: date, end_d: date, statuses: tuple[str, ...]) -> set:
    """
    Unique child_ids for enrollments matching status + date overlap.
    """
    s: set = set()
    enrollments = getattr(lesson, "enrollments", None)
    if enrollments is None:
        qs = LessonEnrollment.objects.filter(lesson=lesson, status__in=statuses).exclude(
            child__status__in=TRIAL_CHILD_STATUSES,
        ).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=end_d),
            Q(end_date__isnull=True) | Q(end_date__gte=start_d),
        )
        return set(qs.values_list("child_id", flat=True))

    for e in enrollments.all():
        if e.status in statuses and _enrollment_overlaps_range(e, start_d, end_d):
            if getattr(e, 'child', None) and e.child.status in TRIAL_CHILD_STATUSES:
                continue
            s.add(e.child_id)
    return s


def _parse_month_str(month: str):
    """
    Parse YYYY-MM into (year:int, month:int)
    """
    year_s, month_s = month.split('-')
    return int(year_s), int(month_s)


def _month_start_end(year: int, month: int):
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def _count_weekday_in_range(start_d: date, end_d: date, target_py_weekday: int):
    """
    Count occurrences of a Python weekday (Mon=0..Sun=6) in [start_d, end_d].
    """
    if end_d < start_d:
        return 0
    days_ahead = (target_py_weekday - start_d.weekday()) % 7
    first = start_d + timedelta(days=days_ahead)
    if first > end_d:
        return 0
    return ((end_d - first).days // 7) + 1


def _lesson_occurrence_count_for_month(lesson, month_start: date, month_end: date, effective_end: Optional[date] = None):
    """
    Return how many times a lesson occurs in the requested month.

    - If lesson.is_recurring: treated as weekly recurring by day_of_week
      - If lesson_date is set, use it as START date (count from that date forward)
      - If lesson_date is null, count all occurrences in the month
    - If not recurring and lesson_date is set: treated as single dated occurrence
    - effective_end (optional): cap occurrences at this date (used for finalization)
    """
    end_cap = effective_end if effective_end is not None else month_end
    if end_cap < month_start:
        return 0

    # Weekly recurring template
    if getattr(lesson, 'is_recurring', False):
        # Lesson.day_of_week is stored as 0=Sunday..6=Saturday.
        # Python weekday is 0=Mon..6=Sun, so map:
        target_py_weekday = (lesson.day_of_week - 1) % 7  # Sunday->6, Monday->0, ...
        
        # If lesson_date is set, use it as the START date
        start_from = month_start
        if lesson.lesson_date and lesson.lesson_date > month_start:
            start_from = lesson.lesson_date
        
        # Count occurrences from start_from to end_cap
        return _count_weekday_in_range(start_from, end_cap, target_py_weekday)

    # Single dated occurrence (non-recurring)
    if lesson.lesson_date:
        return 1 if (month_start <= lesson.lesson_date <= end_cap) else 0

    return 0


def _batch_load_cancellations(
    lessons_queryset, 
    month_start: date, 
    month_end: date, 
    effective_end: Optional[date] = None
) -> dict:
    """
    Batch-load cancellation counts for multiple lessons (avoids N+1 queries).
    
    Returns:
        dict: {lesson_id: cancelled_count}
    """
    from apps.scheduling.models import LessonCancellation
    from django.db.models import Count
    
    end_cap = effective_end if effective_end is not None else month_end
    
    cancellations = LessonCancellation.objects.filter(
        lesson__in=lessons_queryset.filter(is_recurring=True),
        occurrence_date__gte=month_start,
        occurrence_date__lte=end_cap,
    ).values('lesson_id').annotate(cnt=Count('id'))
    
    return {c['lesson_id']: c['cnt'] for c in cancellations}


def calculate_effective_occurrences(
    lesson, 
    month_start: date, 
    month_end: date, 
    effective_end: Optional[date] = None,
    cancellations_dict: Optional[dict] = None
) -> int:
    """
    SINGLE SOURCE OF TRUTH: Calculate effective lesson occurrences in a month.
    
    Returns base occurrences minus any date-specific cancellations (LessonCancellation).
    
    Args:
        lesson: Lesson instance
        month_start: First day of month
        month_end: Last day of month
        effective_end: Optional cap date (for current-month dynamic calculations)
        cancellations_dict: Pre-loaded {lesson_id: cancelled_count} to avoid N+1 queries.
                           If None, will query individually (slower but works for single lessons).
    
    Returns:
        int: Number of effective occurrences (after subtracting cancellations)
    """
    # Get base occurrence count
    occ = _lesson_occurrence_count_for_month(lesson, month_start, month_end, effective_end=effective_end)
    if occ <= 0:
        return 0
    
    # Subtract cancellations for recurring lessons
    if not getattr(lesson, 'is_recurring', False):
        return occ  # Non-recurring lessons don't have per-occurrence cancellations
    
    # Use pre-loaded cancellations if available (batch mode - faster)
    if cancellations_dict is not None:
        cancelled_count = cancellations_dict.get(lesson.id, 0)
    else:
        # Fallback: individual query (slower, for single-lesson calculations)
        from apps.scheduling.models import LessonCancellation
        end_cap = effective_end if effective_end is not None else month_end
        cancelled_count = LessonCancellation.objects.filter(
            lesson=lesson,
            occurrence_date__gte=month_start,
            occurrence_date__lte=end_cap,
        ).count()
    
    return max(0, int(occ) - int(cancelled_count))


def calculate_instructor_salary_for_month(instructor, month: str, branch_id=None, effective_end: Optional[date] = None):
    """
    Calculate instructor salary for a given month using per-lesson rules:
    - Uses course.instructor_salary_override as monthly team pay (counted once per course)
    - Uses tiered pricing if defined (salary tiers)
    - Uses lesson.instructor_salary_override when provided ("שכר חריג")
    - Excludes cancelled lessons (status='cancelled')
    - Counts occurrences for recurring templates by weekday
    - If effective_end is provided, caps occurrences up to that date (current-month dynamic calc)
    """
    from apps.courses.models import Lesson

    year, m = _parse_month_str(month)
    month_start, month_end = _month_start_end(year, m)

    qs = Lesson.objects.filter(instructor=instructor).exclude(status='cancelled')
    if branch_id:
        qs = qs.filter(course__branch_id=branch_id)

    # Include:
    # - Dated lessons inside the month
    # - Undated recurring templates (lesson_date is null) that represent weekly slots
    qs = qs.filter(
        Q(lesson_date__gte=month_start, lesson_date__lte=month_end) |
        Q(lesson_date__isnull=True, is_recurring=True)
    ).select_related('course').prefetch_related('enrollments')

    total_salary = Decimal('0.00')
    total_occurrences = 0
    total_lesson_templates = 0  # Count of unique lesson slots
    courses_with_monthly_pay = set()

    # Batch-load cancellations for all lessons (avoids N+1 queries)
    cancellations_dict = _batch_load_cancellations(qs, month_start, month_end, effective_end=effective_end)

    for lesson in qs:
        # Count all lessons as templates (regardless of occurrences)
        total_lesson_templates += 1
        
        # Use single source of truth for effective occurrences
        occ = calculate_effective_occurrences(
            lesson, month_start, month_end, 
            effective_end=effective_end,
            cancellations_dict=cancellations_dict
        )
        if occ <= 0:
            continue

        course = lesson.course
        course_monthly = get_course_team_monthly_salary(course)
        if course_monthly is not None:
            if course.id not in courses_with_monthly_pay:
                courses_with_monthly_pay.add(course.id)
                total_salary += course_monthly
            total_occurrences += occ
            continue

        # For salary tiers and load we include both active + payments_problem
        end_cap = effective_end if effective_end is not None else month_end
        student_count = _count_enrollments_for_period(lesson, month_start, end_cap, SALARY_STUDENT_STATUSES)
        per_occ_salary = calculate_lesson_salary_with_override(
            student_count,
            instructor,
            salary_override=lesson.instructor_salary_override,
        )
        total_salary += Decimal(str(per_occ_salary)) * Decimal(occ)
        total_occurrences += occ

    return total_salary, total_occurrences, total_lesson_templates


def calculate_branch_instructor_costs_for_month(branch, month: str, effective_end: Optional[date] = None) -> Decimal:
    """
    Current total instructor salary cost for a branch in a given month.
    This matches the /instructors "שכר" calculation semantics:
    - calendar-month accurate occurrences
    - subtract date-specific cancellations (LessonCancellation)
    - for current month, caller should pass effective_end=today-1 to count only occurred lessons so far
    - student count for salary tiers uses LessonEnrollment statuses: active + payments_problem
    """
    from apps.courses.models import Lesson
    from apps.scheduling.models import LessonCancellation

    year, m = _parse_month_str(month)
    month_start, month_end = _month_start_end(year, m)

    lessons = Lesson.objects.filter(course__branch=branch).exclude(status='cancelled').filter(
        Q(lesson_date__gte=month_start, lesson_date__lte=month_end) |
        Q(lesson_date__isnull=True, is_recurring=True)
    ).select_related('instructor', 'course', 'course__branch').prefetch_related('enrollments', 'instructor__salary_tiers')

    # Batch-load cancellations for all lessons (avoids N+1 queries)
    cancellations_dict = _batch_load_cancellations(lessons, month_start, month_end, effective_end=effective_end)

    total = Decimal('0.00')
    courses_with_monthly_pay = set()

    for lesson in lessons:
        if not lesson.instructor:
            continue

        # Use single source of truth for effective occurrences
        occ = calculate_effective_occurrences(
            lesson, month_start, month_end,
            effective_end=effective_end,
            cancellations_dict=cancellations_dict
        )
        if occ <= 0:
            continue

        course = lesson.course
        course_monthly = get_course_team_monthly_salary(course)
        if course_monthly is not None:
            if course.id not in courses_with_monthly_pay:
                courses_with_monthly_pay.add(course.id)
                total += course_monthly
            continue

        end_cap = effective_end if effective_end is not None else month_end
        student_count = _count_enrollments_for_period(lesson, month_start, end_cap, SALARY_STUDENT_STATUSES)
        per_occ_salary = calculate_lesson_salary_with_override(
            student_count,
            lesson.instructor,
            salary_override=lesson.instructor_salary_override,
        )
        total += Decimal(str(per_occ_salary)) * Decimal(occ)

    return total


def calculate_instructor_revenue_for_month(instructor, month: str, branch_id=None, effective_end: Optional[date] = None):
    """
    Calculate **monthly** revenue for a given month based on actual collected payments.
    
    This uses Payment.final_amount (actual money collected) rather than lesson list prices,
    ensuring revenue reflects real collected amounts after discounts.
    
    Args:
        instructor: Instructor instance
        month: Month string in format 'YYYY-MM'
        branch_id: Optional branch filter
        effective_end: Optional end date for the calculation
    
    Returns:
        Total revenue as Decimal
    """
    from apps.customers.models import Payment
    from django.db.models import Sum

    year, m = _parse_month_str(month)
    month_start, month_end = _month_start_end(year, m)
    
    # Use effective_end if provided (for mid-month calculations)
    end_date = effective_end if effective_end and effective_end < month_end else month_end

    # Build query filters
    filters = Q(
        lesson__instructor=instructor,
        status='completed',
        payment_date__date__gte=month_start,
        payment_date__date__lte=end_date
    )
    
    if branch_id:
        filters &= Q(branch_id=branch_id)

    # Sum actual collected amounts (Payment.final_amount)
    result = Payment.objects.filter(filters).aggregate(
        total=Sum('final_amount')
    )

    return result['total'] or Decimal('0.00')


def calculate_instructor_students_for_month(instructor, month: str, branch_id=None) -> int:
    """
    Students count for instructor list:
    - based on LessonEnrollment rows for instructor lessons
    - includes statuses: active + payments_problem
    - unique across lessons
    """
    from apps.courses.models import Lesson
    year, m = _parse_month_str(month)
    month_start, month_end = _month_start_end(year, m)

    qs = Lesson.objects.filter(instructor=instructor).exclude(status='cancelled')
    if branch_id:
        qs = qs.filter(course__branch_id=branch_id)

    qs = qs.filter(
        Q(lesson_date__gte=month_start, lesson_date__lte=month_end) |
        Q(lesson_date__isnull=True, is_recurring=True)
    ).prefetch_related('enrollments')

    unique_students: set = set()
    for lesson in qs:
        unique_students.update(_unique_students_for_period(lesson, month_start, month_end, SALARY_STUDENT_STATUSES))
    return len(unique_students)


def calculate_instructor_monthly_metrics(instructor, month=None, branch_id=None):
    """
    Calculate financial metrics for an instructor for a specific month
    
    Args:
        instructor: Instructor instance
        month: String in YYYY-MM format, defaults to current month
        branch_id: Optional branch ID to filter lessons by specific branch
    
    Returns:
        dict: Dictionary with financial metrics
    """
    if month is None:
        now = datetime.now()
        month = now.strftime('%Y-%m')

    # Decide whether month is current or past
    from django.utils import timezone
    from apps.core.models import InstructorMonthlySnapshot

    today = timezone.now().date()
    current_month = today.strftime('%Y-%m')

    # Past months must be immutable: use finalized snapshot only (or auto-finalize if missing).
    salary_is_finalized = False
    if month < current_month:
        snap = InstructorMonthlySnapshot.objects.filter(instructor=instructor, month=month, is_finalized=True).first()
        if snap:
            salary_is_finalized = True
            return {
                'lessons_count': snap.lesson_count or snap.total_lessons,
                'students_count': snap.total_students,
                'revenue': snap.total_revenue,
                'salary': snap.total_salary,
                'bonuses': snap.total_bonuses,
                'profit': snap.profit,
                'cancelled_count': snap.cancelled_count,
                'avg_attendance_rate': snap.avg_attendance_rate,
                'salary_is_finalized': True,
            }

        # Auto-finalize the ended month if snapshot is missing (one-time, then immutable).
        _auto_finalize_month_for_instructor(instructor, month)
        snap2 = InstructorMonthlySnapshot.objects.filter(instructor=instructor, month=month, is_finalized=True).first()
        if snap2:
            return {
                'lessons_count': snap2.lesson_count or snap2.total_lessons,
                'students_count': snap2.total_students,
                'revenue': snap2.total_revenue,
                'salary': snap2.total_salary,
                'bonuses': snap2.total_bonuses,
                'profit': snap2.profit,
                'cancelled_count': snap2.cancelled_count,
                'avg_attendance_rate': snap2.avg_attendance_rate,
                'salary_is_finalized': True,
            }

        # If finalization failed for some reason, return a safe empty value.
        return {
            'lessons_count': 0,
            'students_count': 0,
            'revenue': Decimal('0.00'),
            'salary': Decimal('0.00'),
            'bonuses': Decimal('0.00'),
            'profit': Decimal('0.00'),
            'cancelled_count': 0,
            'avg_attendance_rate': Decimal('0.00'),
            'salary_is_finalized': False,
        }

    # Current / future month: dynamic calculation using the new rules.
    # For viewing current month, show PROJECTED salary (all lessons in month, not just occurred)
    # Don't cap by effective_end - count all lessons that will happen in the month
    effective_end = None  # Count all occurrences in the month (projected)

    total_salary, salary_occ, lesson_templates = calculate_instructor_salary_for_month(
        instructor,
        month,
        branch_id=branch_id,
        effective_end=effective_end,  # None = count full month
    )
    total_revenue = calculate_instructor_revenue_for_month(
        instructor, month, branch_id=branch_id, effective_end=effective_end
    )
    total_students = calculate_instructor_students_for_month(instructor, month, branch_id=branch_id)
    
    # Calculate base revenue (theoretical revenue before discounts) for comparison
    from apps.customers.models import Payment
    year_val, m_val = _parse_month_str(month)
    month_start_calc, month_end_calc = _month_start_end(year_val, m_val)
    
    base_revenue_result = Payment.objects.filter(
        lesson__instructor=instructor,
        status='completed',
        payment_date__date__gte=month_start_calc,
        payment_date__date__lte=month_end_calc
    ).aggregate(total=Sum('base_amount'))
    base_revenue = base_revenue_result['total'] or Decimal('0.00')
    
    # Calculate bonuses for this instructor in this month
    from apps.instructors.models import InstructorBonus
    year, m = _parse_month_str(month)
    bonuses = InstructorBonus.objects.filter(
        instructor=instructor,
        bonus_date__year=year,
        bonus_date__month=m
    )
    total_bonuses = sum(bonus.amount for bonus in bonuses)

    profit = total_revenue - total_salary
    payment_per_lesson = (total_salary / Decimal(salary_occ)) if salary_occ else Decimal('0.00')

    # Save/update snapshot for current month (not finalized) so we keep a running record
    if month == current_month:
        InstructorMonthlySnapshot.objects.update_or_create(
            instructor=instructor,
            month=month,
            defaults={
                'lesson_count': lesson_templates,  # Number of lesson templates (weekly slots)
                'payment_per_lesson': payment_per_lesson,
                'total_salary': total_salary,
                'total_bonuses': total_bonuses,
                'total_students': total_students,
                'base_revenue': base_revenue,
                'total_discounts': base_revenue - total_revenue,
                'total_revenue': total_revenue,
                'profit': profit,
                'is_finalized': False,
                'total_lessons': lesson_templates,  # Same as lesson_count
                'cancelled_count': 0,
                'avg_attendance_rate': Decimal('0.00'),
            }
        )

    return {
        # "lessons_count" now means number of lesson templates (weekly slots)
        'lessons_count': lesson_templates,
        'students_count': total_students,
        'base_revenue': base_revenue,
        'total_discounts': base_revenue - total_revenue,
        'revenue': total_revenue,
        'salary': total_salary,
        'bonuses': total_bonuses,
        'profit': profit,
        'cancelled_count': 0,
        'avg_attendance_rate': Decimal('0.00'),
        'salary_is_finalized': salary_is_finalized,
    }


def _auto_finalize_month_for_instructor(instructor, month: str):
    """
    Finalize (snapshot) a past month for a single instructor if not already finalized.
    Idempotent + safe to call multiple times.
    """
    from apps.core.models import InstructorMonthlySnapshot
    from apps.customers.models import Payment

    if InstructorMonthlySnapshot.objects.filter(instructor=instructor, month=month, is_finalized=True).exists():
        return

    total_salary, occurrences, lesson_templates = calculate_instructor_salary_for_month(instructor, month, effective_end=None)
    total_revenue = calculate_instructor_revenue_for_month(instructor, month, effective_end=None)
    total_students = calculate_instructor_students_for_month(instructor, month)
    profit = total_revenue - total_salary
    payment_per_lesson = (total_salary / Decimal(occurrences)) if occurrences else Decimal('0.00')
    
    # Calculate base revenue for comparison
    year_val, m_val = _parse_month_str(month)
    month_start_calc, month_end_calc = _month_start_end(year_val, m_val)
    
    base_revenue_result = Payment.objects.filter(
        lesson__instructor=instructor,
        status='completed',
        payment_date__date__gte=month_start_calc,
        payment_date__date__lte=month_end_calc
    ).aggregate(total=Sum('base_amount'))
    base_revenue = base_revenue_result['total'] or Decimal('0.00')
    total_discounts = base_revenue - total_revenue

    with transaction.atomic():
        # If someone else finalized concurrently, don't overwrite.
        if InstructorMonthlySnapshot.objects.select_for_update().filter(instructor=instructor, month=month, is_finalized=True).exists():
            return
        InstructorMonthlySnapshot.objects.update_or_create(
            instructor=instructor,
            month=month,
            defaults={
                'lesson_count': lesson_templates,  # Number of lesson templates
                'payment_per_lesson': payment_per_lesson,
                'total_salary': total_salary,
                'total_students': total_students,
                'base_revenue': base_revenue,
                'total_discounts': total_discounts,
                'total_revenue': total_revenue,
                'profit': profit,
                'is_finalized': True,
                'total_lessons': lesson_templates,  # Same as lesson_count
            }
        )


def ensure_previous_month_finalized_for_all():
    """
    Ensures the previous month is finalized for all active instructors.
    This is lightweight and idempotent; it only writes missing snapshots.
    """
    from django.utils import timezone
    from apps.instructors.models import Instructor
    from apps.core.models import InstructorMonthlySnapshot

    today = timezone.now().date()
    first_of_month = today.replace(day=1)
    prev_month_end = first_of_month - timedelta(days=1)
    prev_month = prev_month_end.strftime('%Y-%m')

    instructors = Instructor.objects.filter(is_active=True)
    finalized_ids = set(
        InstructorMonthlySnapshot.objects.filter(month=prev_month, is_finalized=True).values_list('instructor_id', flat=True)
    )

    for instructor in instructors:
        if instructor.id in finalized_ids:
            continue
        _auto_finalize_month_for_instructor(instructor, prev_month)


def _calculate_lesson_discounts(lesson, month_start: date, month_end: date, monthly_price: Decimal) -> Decimal:
    """
    Calculate total discounts for a lesson based on actual completed payments.
    
    This uses the new Payment-based discount system where discounts are
    automatically calculated and stored at payment time via DiscountService.
    
    Args:
        lesson: Lesson instance
        month_start: Start date of the month
        month_end: End date of the month
        monthly_price: Base monthly price of the lesson (unused, kept for compatibility)
    
    Returns:
        Decimal: Total discount amount for this lesson in this month
    """
    from apps.customers.models import Payment
    
    # Sum actual discount amounts from completed payments for this lesson in this month
    result = Payment.objects.filter(
        lesson=lesson,
        status='completed',
        payment_date__date__gte=month_start,
        payment_date__date__lte=month_end
    ).aggregate(
        total=Sum('discount_amount')
    )
    
    return result['total'] or Decimal('0.00')


def calculate_lesson_profitability(lesson, instructor, month: Optional[str] = None, cancellations_dict: Optional[dict] = None):
    """
    Calculate profitability for a single lesson based on actual collected payments.
    
    Revenue is calculated from Payment.final_amount (actual money collected)
    rather than lesson price × enrollment count.
    
    Args:
        lesson: Lesson instance
        instructor: Instructor instance
        month: Optional month string (YYYY-MM), defaults to current month
        cancellations_dict: Optional pre-loaded {lesson_id: cancelled_count} for batch operations
    
    Returns:
        dict: Dictionary with lesson financial data including discounts
    """
    from apps.customers.models import Payment
    
    if month is None:
        month_str = datetime.now().strftime('%Y-%m')
    else:
        month_str = month
    year, m = _parse_month_str(month_str)
    month_start, month_end = _month_start_end(year, m)

    # Use single source of truth for effective occurrences
    occurrences_in_month = calculate_effective_occurrences(
        lesson, month_start, month_end, 
        effective_end=None,
        cancellations_dict=cancellations_dict
    )
    # Note: If all occurrences are cancelled, occurrences_in_month will be 0,
    # which correctly results in 0 salary. No fallback needed.

    # Count students for enrollment and salary calculations
    revenue_enrollment_count = _count_enrollments_for_period(
        lesson, month_start, month_end, REVENUE_ENROLLMENT_STATUSES
    )
    salary_student_count = _count_enrollments_for_period(lesson, month_start, month_end, SALARY_STUDENT_STATUSES)
    monthly_price = get_lesson_price(lesson)
    
    # Calculate theoretical base revenue (for comparison)
    base_revenue = monthly_price * Decimal(revenue_enrollment_count)
    
    # Get actual revenue and discounts from completed payments
    payments = Payment.objects.filter(
        lesson=lesson,
        status='completed',
        payment_date__date__gte=month_start,
        payment_date__date__lte=month_end
    )
    
    payment_totals = payments.aggregate(
        actual_revenue=Sum('final_amount'),
        total_discounts=Sum('discount_amount'),
        base_amount=Sum('base_amount')
    )
    
    # Use actual collected revenue (no fallback - if no payments, revenue is 0)
    revenue = payment_totals['actual_revenue'] or Decimal('0.00')
    total_discounts = payment_totals['total_discounts'] or Decimal('0.00')
    
    # Calculate salary: course monthly pay split by slot, or per-occurrence tier/override
    course_monthly_share = get_lesson_monthly_salary_share(lesson)
    if course_monthly_share is not None:
        salary = course_monthly_share
        salary_override = lesson.course.instructor_salary_override
    else:
        salary_override = lesson.instructor_salary_override
        salary_per_occurrence = calculate_lesson_salary_with_override(
            salary_student_count, instructor, salary_override=salary_override
        )
        salary = salary_per_occurrence * Decimal(occurrences_in_month)
    
    # Calculate profit
    profit = revenue - salary
    
    return {
        'lesson_id': str(lesson.id),
        'course_name': lesson.course.name if lesson.course else '',
        'course_id': str(lesson.course.id) if lesson.course else None,
        'day_of_week': lesson.day_of_week,
        'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
        'end_time': lesson.end_time.strftime('%H:%M') if lesson.end_time else '',
        'branch_name': lesson.course.branch.name if lesson.course and lesson.course.branch_id else '',
        'branch_id': str(lesson.course.branch.id) if lesson.course and lesson.course.branch_id else None,
        'room_name': lesson.room.name if lesson.room else '',
        'student_count': salary_student_count,
        'lesson_price': str(monthly_price),
        'base_revenue': str(base_revenue),
        'total_discounts': str(total_discounts),
        'revenue': str(revenue),
        'salary': str(salary),
        'profit': str(profit),
        'status': lesson.status,
        'salary_override': bool(salary_override),
        'salary_override_amount': str(salary_override) if salary_override is not None else None,
        'revenue_calculation_method': 'actual_payments',
    }


def generate_monthly_snapshots(month, finalize=False):
    """
    Generate monthly snapshots for all instructors, lessons, and branches
    This should be run as a scheduled task (e.g., via Celery)
    
    Args:
        month: String in YYYY-MM format
        finalize: Boolean - if True, marks snapshots as finalized (immutable for past months)
    
    Returns:
        dict: Summary of created snapshots
    """
    from apps.instructors.models import Instructor, InstructorBonus
    from apps.courses.models import Lesson
    from apps.core.models import (
        Branch,
        InstructorMonthlySnapshot, LessonMonthlySnapshot, BranchMonthlySnapshot
    )
    from django.utils import timezone
    
    # Determine if we should finalize based on the month
    today = timezone.now().date()
    current_month = today.strftime('%Y-%m')
    
    # Auto-finalize past months, never finalize current/future months
    should_finalize = finalize and (month < current_month)
    
    instructors_created = 0
    lessons_created = 0
    branches_created = 0
    
    # Parse month for bonus filtering
    year, m = _parse_month_str(month)
    
    # Generate instructor snapshots
    instructors = Instructor.objects.filter(is_active=True)
    for instructor in instructors:
        metrics = calculate_instructor_monthly_metrics(instructor, month)
        
        snapshot, created = InstructorMonthlySnapshot.objects.update_or_create(
            instructor=instructor,
            month=month,
            defaults={
                'total_lessons': metrics['lessons_count'],
                'total_students': metrics['students_count'],
                'base_revenue': metrics.get('base_revenue', Decimal('0.00')),
                'total_discounts': metrics.get('total_discounts', Decimal('0.00')),
                'total_revenue': metrics['revenue'],
                'total_salary': metrics['salary'],
                'total_bonuses': metrics['bonuses'],
                'profit': metrics['profit'],
                'cancelled_count': metrics['cancelled_count'],
                'avg_attendance_rate': metrics['avg_attendance_rate'],
                'is_finalized': should_finalize
            }
        )
        if created:
            instructors_created += 1
    
    # Clean up stale snapshots: if a lesson template itself is cancelled (Lesson.status='cancelled'),
    # remove its monthly snapshots so branch aggregation won't include it.
    cancelled_lessons = Lesson.objects.filter(status='cancelled')
    if cancelled_lessons.exists():
        LessonMonthlySnapshot.objects.filter(lesson__in=cancelled_lessons, month=month).delete()

    # Generate snapshots for active lessons
    lessons = Lesson.objects.exclude(
        status='cancelled'
    ).select_related('instructor', 'course', 'course__branch')

    # Batch-load cancellations for all lessons in this month (optimizes snapshot generation)
    month_start, month_end = _month_start_end(year, m)
    cancellations_dict = _batch_load_cancellations(lessons, month_start, month_end, effective_end=None)

    for lesson in lessons:
        if lesson.instructor:
            profitability = calculate_lesson_profitability(
                lesson, lesson.instructor, month=month,
                cancellations_dict=cancellations_dict
            )

            snapshot, created = LessonMonthlySnapshot.objects.update_or_create(
                lesson=lesson,
                month=month,
                defaults={
                    'instructor': lesson.instructor,
                    'course': lesson.course,
                    'branch': lesson.course.branch,
                    'enrolled_students': profitability['student_count'],
                    'base_revenue': Decimal(profitability['base_revenue']),
                    'total_discounts': Decimal(profitability['total_discounts']),
                    'revenue': Decimal(profitability['revenue']),
                    'instructor_salary': Decimal(profitability['salary']),
                    'profit': Decimal(profitability['profit']),
                    'is_finalized': should_finalize
                }
            )
            if created:
                lessons_created += 1

    # Generate branch snapshots
    branches = Branch.objects.all()
    for branch in branches:
        # Aggregate from lesson snapshots
        lesson_snaps = LessonMonthlySnapshot.objects.filter(
            branch=branch,
            month=month
        )
        
        total_students = sum(ls.enrolled_students for ls in lesson_snaps)
        base_revenue = sum(ls.base_revenue for ls in lesson_snaps)
        total_discounts = sum(ls.total_discounts for ls in lesson_snaps)
        total_revenue = sum(ls.revenue for ls in lesson_snaps)
        
        # COMPONENT 1: Instructor salaries from lessons
        instructor_salaries = sum(ls.instructor_salary for ls in lesson_snaps)
        
        # COMPONENT 2: Instructor bonuses for this branch in this month
        # Get all instructors teaching in this branch
        instructor_ids = lesson_snaps.values_list('instructor_id', flat=True).distinct()
        bonuses = InstructorBonus.objects.filter(
            instructor_id__in=instructor_ids,
            bonus_date__year=year,
            bonus_date__month=m
        )
        instructor_bonuses = sum(bonus.amount for bonus in bonuses)
        
        # COMPONENT 3: Branch operational costs (cleaning, monthly expenses)
        operational_costs = Decimal('0.00')
        if branch.cleaning_cost:
            operational_costs += branch.cleaning_cost
        if branch.monthly_cost:
            operational_costs += branch.monthly_cost
        
        # TOTAL EXPENSES = Salaries + Bonuses + Operational Costs
        total_expenses = instructor_salaries + instructor_bonuses + operational_costs
        profit = total_revenue - total_expenses
        
        # Count active courses - using same filter logic as lesson snapshots
        active_courses = Lesson.objects.filter(
            branch=branch
        ).exclude(
            status='cancelled'
        ).values('course').distinct().count()
        
        snapshot, created = BranchMonthlySnapshot.objects.update_or_create(
            branch=branch,
            month=month,
            defaults={
                'total_students': total_students,
                'base_revenue': base_revenue,
                'total_discounts': total_discounts,
                'total_revenue': total_revenue,
                'instructor_salaries': instructor_salaries,
                'instructor_bonuses': instructor_bonuses,
                'operational_costs': operational_costs,
                'instructor_costs': total_expenses,  # Total of all 3 components
                'profit': profit,
                'active_courses_count': active_courses,
                'is_finalized': should_finalize
            }
        )
        if created:
            branches_created += 1
    
    return {
        'month': month,
        'instructors_created': instructors_created,
        'lessons_created': lessons_created,
        'branches_created': branches_created
    }

