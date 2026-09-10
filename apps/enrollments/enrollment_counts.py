"""Helpers for distinguishing trial vs paying lesson enrollments."""
from __future__ import annotations

from datetime import date
from typing import Optional

from django.db.models import Q, QuerySet

from apps.enrollments.models import LessonEnrollment

# Children on trial flow — enrolled for a test lesson, not paying subscribers yet.
TRIAL_CHILD_STATUSES = ('trial_signed', 'trial_completed')


def paying_enrollments(qs: QuerySet | None = None) -> QuerySet:
    """
    Active enrollments that count as paying subscribers (revenue / salary tiers).

    Trial signups stay on the lesson roster but are excluded from financial counts
    until paid conversion clears trial_lesson_date.
    """
    base = qs if qs is not None else LessonEnrollment.objects.all()
    return (
        base.filter(status='active', trial_lesson_date__isnull=True)
        .exclude(child__status__in=TRIAL_CHILD_STATUSES)
    )


def paying_enrollment_q(prefix: str = '') -> Q:
    """
    The same rule as `paying_enrollments`, shaped for an annotation `filter=`.

    A seat is taken only by an active enrollment that is not a trial. Both halves
    matter: the row carries `trial_lesson_date` while the trial is booked, and the
    child's own status says `trial_signed` while they are in the trial flow. A
    count that checks only the child's status gives a trial a seat whenever the
    child is already active — which is exactly what happens when the office books
    a trial for a child who already attends something else.

    `prefix` is the path from the annotated model down to the enrollment, e.g.
    'enrollments' on Lesson, 'courses__lessons__enrollments' on CourseType.
    """
    field = f'{prefix}__' if prefix else ''
    return (
        Q(**{f'{field}status': 'active'})
        & Q(**{f'{field}trial_lesson_date__isnull': True})
        & ~Q(**{f'{field}child__status__in': TRIAL_CHILD_STATUSES})
    )


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

    Only paying students take a seat. Trial signups stay on the roster but
    never fill the class, including on their trial day.
    """
    if enrollment.status != 'active':
        return False
    if enrollment.trial_lesson_date:
        return False
    return enrollment.child.status not in TRIAL_CHILD_STATUSES


def occupies_a_trial_seat(enrollment, *, occurrence_date: Optional[date] = None) -> bool:
    """
    Whether this row puts a body in the room on `occurrence_date` as a trial.

    A trial is booked for one date. It is a body in that room on that day and on
    no other, which is why the date has to be part of the question.
    """
    if enrollment.status != 'active':
        return False
    if not enrollment.trial_lesson_date:
        return False
    if occurrence_date is None:
        return enrollment.trial_lesson_date >= date.today()
    return enrollment.trial_lesson_date == occurrence_date


def count_capacity_enrollments(
    *,
    lesson,
    occurrence_date: Optional[date] = None,
    enrollments=None,
    include_trials: bool = False,
) -> int:
    """
    Headcount for capacity.

    Two different questions share this counter, and they get different answers:

    * A paying registration asks how many paying students the class holds. A
      trial is a visitor for one day and never costs a subscriber their place,
      so `include_trials` is False and trials are not counted.
    * A trial booking asks how many bodies will be in the room that day —
      paying students plus everyone else trying the class out. Twenty paying
      children and five trials is twenty-five children in a room built for
      twenty, so `include_trials` is True and the trials booked for that date
      are counted with the payers.
    """
    from apps.enrollments.duplicate_students import collapse_duplicate_people

    if enrollments is None:
        rows = LessonEnrollment.objects.filter(lesson=lesson, status='active')
        enrollments = rows.select_related('child', 'child__family')
    taking_a_seat = [
        e for e in enrollments
        if counts_toward_capacity(e, occurrence_date=occurrence_date)
        or (include_trials and occupies_a_trial_seat(e, occurrence_date=occurrence_date))
    ]
    # One person, one seat. A child registered twice under two cards used to
    # hold two places in a class they attend once.
    return len(collapse_duplicate_people(taking_a_seat))


def trial_seats_left(*, lesson, occurrence_date: Optional[date] = None, capacity=None, enrollments=None):
    """
    Places a trial may still be booked into on `occurrence_date`, or None when
    the lesson has no capacity set. Never negative.
    """
    if capacity is None:
        course = getattr(lesson, 'course', None)
        room = getattr(lesson, 'room', None)
        caps = [int(c) for c in (getattr(course, 'capacity', None), getattr(room, 'capacity', None)) if c]
        capacity = min(caps) if caps else None
    if not capacity:
        return None
    taken = count_capacity_enrollments(
        lesson=lesson, occurrence_date=occurrence_date,
        enrollments=enrollments, include_trials=True,
    )
    return max(0, int(capacity) - taken)
