"""Studio time-slot overlap checks for schedule events and lessons."""
from datetime import date, timedelta

from django.db.models import Q

from apps.courses.models import Lesson
from apps.scheduling.weekdays import (
    lesson_style_dow_from_date,
    normalized_weekly_repeat_lesson_dows,
    python_weekday_from_lesson_dow,
)


def _python_weekday_to_lesson_dow(python_weekday: int) -> int:
    """Lesson uses 0=Sunday..6=Saturday; Python weekday is Mon=0..Sun=6."""
    return (python_weekday + 1) % 7


def event_anchor_lesson_day_of_week(event) -> int:
    """Which lesson-style day_of_week (0=Sun) this event repeats on (weekly) or falls on (one_time)."""
    return lesson_style_dow_from_date(event.event_date)


def times_overlap(a_start, a_end, b_start, b_end) -> bool:
    if not all([a_start, a_end, b_start, b_end]):
        return False
    return not (a_end <= b_start or b_end <= a_start)


def iter_occurrence_dates_in_range(event, range_start: date, range_end: date):
    """Concrete calendar dates for this timed event within [range_start, range_end]."""
    if event.is_daily_event or not event.start_time or not event.end_time:
        return
    if event.event_type == 'one_time':
        if range_start <= event.event_date <= range_end:
            yield event.event_date
        return
    start_from = max(range_start, event.event_date)
    for lesson_dow in normalized_weekly_repeat_lesson_dows(event):
        py_wd = python_weekday_from_lesson_dow(lesson_dow)
        days_ahead = (py_wd - start_from.weekday()) % 7
        occ = start_from + timedelta(days=days_ahead)
        while occ <= range_end:
            yield occ
            occ += timedelta(days=7)


def _lesson_time_overlap_q(start_time, end_time):
    return ~(Q(end_time__lte=start_time) | Q(start_time__gte=end_time))


def lesson_conflicts_studio_slot(
    branch_id,
    room_id,
    lesson_style_dow: int,
    start_time,
    end_time,
    occurrence_date: date | None,
):
    """
    True if a scheduled lesson uses the same room/branch, same weekday, overlapping times.
    occurrence_date: for one-time events, the specific calendar date; for weekly validation use None
    (any recurring lesson on that weekday blocks the whole series).
    """
    if not branch_id or not room_id:
        return False

    base = (
        Q(
            day_of_week=lesson_style_dow,
            status='scheduled',
            branch_id=branch_id,
            room_id=room_id,
        )
        & _lesson_time_overlap_q(start_time, end_time)
    )
    qs = Lesson.objects.filter(base)

    if occurrence_date is not None:
        qs = qs.filter(
            Q(is_recurring=False, lesson_date=occurrence_date)
            | (
                Q(is_recurring=True)
                & (Q(lesson_date__isnull=True) | Q(lesson_date__lte=occurrence_date))
            )
        )

    return qs.exists()


def event_conflicts_other_events(candidate, exclude_pk=None):
    """
    Another active timed event overlaps same branch+studio+time pattern.
    Works for one_time and weekly (compares anchor weekday for weekly series).
    """
    from apps.scheduling.models import ScheduleEvent

    if candidate.is_daily_event or not candidate.studio_id or not candidate.branch_id:
        return False
    if not candidate.start_time or not candidate.end_time:
        return False

    others = ScheduleEvent.objects.filter(
        is_active=True,
        branch_id=candidate.branch_id,
        studio_id=candidate.studio_id,
        is_daily_event=False,
    ).exclude(start_time__isnull=True).exclude(end_time__isnull=True)

    if exclude_pk:
        others = others.exclude(pk=exclude_pk)

    cand_dow = event_anchor_lesson_day_of_week(candidate)

    for other in others:
        if not times_overlap(candidate.start_time, candidate.end_time, other.start_time, other.end_time):
            continue
        if other.event_type == 'weekly':
            if event_anchor_lesson_day_of_week(other) != cand_dow:
                continue
            return True
        if other.event_type == 'one_time':
            if candidate.event_type == 'one_time':
                if other.event_date == candidate.event_date:
                    return True
            else:
                # candidate weekly: conflicts if one_time falls on candidate's weekday
                if _python_weekday_to_lesson_dow(other.event_date.weekday()) == cand_dow:
                    return True
    return False


def event_conflicts_lessons(candidate):
    """Scheduled lessons block this event (same studio slot)."""
    if candidate.is_daily_event or not candidate.studio_id or not candidate.branch_id:
        return False
    if not candidate.start_time or not candidate.end_time:
        return False

    cand_dow = event_anchor_lesson_day_of_week(candidate)

    if candidate.event_type == 'one_time':
        return lesson_conflicts_studio_slot(
            candidate.branch_id,
            candidate.studio_id,
            cand_dow,
            candidate.start_time,
            candidate.end_time,
            candidate.event_date,
        )

    return lesson_conflicts_studio_slot(
        candidate.branch_id,
        candidate.studio_id,
        cand_dow,
        candidate.start_time,
        candidate.end_time,
        None,
    )


def timed_event_conflicts_lesson(
    branch,
    room,
    day_of_week,
    start_time,
    end_time,
    *,
    lesson_is_recurring=True,
    lesson_date=None,
):
    """
    Any active timed schedule event in the same studio blocks this lesson slot.

    - Weekly events: same weekday (lesson day_of_week) + overlapping times.
    - One-time events: overlapping times only if the lesson occurs on that event_date
      (non-recurring: lesson_date must match; recurring: lesson_date must match when set).
    """
    from apps.scheduling.models import ScheduleEvent

    if not branch or not room:
        return False

    bid = branch.id if hasattr(branch, 'id') else branch
    rid = room.id if hasattr(room, 'id') else room

    q = ScheduleEvent.objects.filter(
        is_active=True,
        branch_id=bid,
        studio_id=rid,
        is_daily_event=False,
    ).exclude(start_time__isnull=True).exclude(end_time__isnull=True)

    for ev in q:
        if not times_overlap(start_time, end_time, ev.start_time, ev.end_time):
            continue
        if ev.event_type == 'weekly':
            if event_anchor_lesson_day_of_week(ev) != day_of_week:
                continue
            return True
        if ev.event_type == 'one_time':
            if lesson_date is None or lesson_date != ev.event_date:
                continue
            if day_of_week != event_anchor_lesson_day_of_week(ev):
                continue
            return True
    return False
