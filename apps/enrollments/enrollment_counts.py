"""Helpers for distinguishing trial vs paying lesson enrollments."""
from __future__ import annotations

from datetime import date
from typing import Optional

from django.db.models import QuerySet

from apps.enrollments.models import LessonEnrollment

# Children on trial flow — enrolled for a test lesson, not paying subscribers yet.
TRIAL_CHILD_STATUSES = ('trial_signed', 'trial_completed')


def paying_enrollments(qs: QuerySet | None = None) -> QuerySet:
    """
    Active enrollments that count as paying subscribers (revenue / salary tiers).

    Trial signups stay on the lesson roster but are excluded from financial counts
    until the child converts to a paying status (e.g. active).
    """
    base = qs if qs is not None else LessonEnrollment.objects.all()
    return base.filter(status='active').exclude(child__status__in=TRIAL_CHILD_STATUSES)


def count_paying_enrollments(*, lesson=None, course=None, courses=None) -> int:
    """Count paying enrollments, optionally scoped to lesson/course(s)."""
    qs = paying_enrollments()
    if lesson is not None:
        qs = qs.filter(lesson=lesson)
    elif course is not None:
        qs = qs.filter(lesson__course=course)
    elif courses is not None:
        qs = qs.filter(lesson__course__in=courses)
    return qs.count()


def count_distinct_paying_children(*, course=None, courses=None) -> int:
    qs = paying_enrollments()
    if course is not None:
        qs = qs.filter(lesson__course=course)
    elif courses is not None:
        qs = qs.filter(lesson__course__in=courses)
    return qs.values('child_id').distinct().count()


def is_paying_enrollment(enrollment: LessonEnrollment) -> bool:
    return (
        enrollment.status == 'active'
        and enrollment.child.status not in TRIAL_CHILD_STATUSES
        and not enrollment.trial_lesson_date
    )


def counts_toward_capacity(
    enrollment: LessonEnrollment,
    *,
    occurrence_date: Optional[date] = None,
) -> bool:
    """
    Whether an enrollment occupies a seat for schedule capacity / max students.

    Paying students always count. Trial students count only on their trial_lesson_date.
    """
    if enrollment.status != 'active':
        return False
    if enrollment.trial_lesson_date:
        if occurrence_date is None:
            return False
        return enrollment.trial_lesson_date == occurrence_date
    return enrollment.child.status not in TRIAL_CHILD_STATUSES


def count_capacity_enrollments(
    *,
    lesson,
    occurrence_date: Optional[date] = None,
    enrollments=None,
) -> int:
    """Headcount for UI capacity (e.g. schedule card 3/20)."""
    if enrollments is None:
        enrollments = LessonEnrollment.objects.filter(
            lesson=lesson,
            status='active',
        ).select_related('child')
    return sum(
        1 for e in enrollments
        if counts_toward_capacity(e, occurrence_date=occurrence_date)
    )
