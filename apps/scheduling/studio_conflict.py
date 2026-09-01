"""Studio time-slot overlap checks for schedule events and lessons."""
from datetime import date, time as time_cls, timedelta

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


def _parse_time_value(value):
    """Accept a datetime.time (from a model field) or an 'HH:MM[:SS]' string (from weekly_day_times JSON)."""
    if isinstance(value, time_cls) or value is None:
        return value
    try:
        parts = [int(p) for p in str(value).split(':')]
        while len(parts) < 3:
            parts.append(0)
        h, m, s = parts[:3]
        return time_cls(hour=h, minute=m, second=s)
    except (TypeError, ValueError):
        return None


def event_day_time_pairs(event) -> list[tuple[int, object, object]]:
    """
    (lesson_dow, start_time, end_time) tuples this event actually occupies:
    - one_time: a single pair at its anchor weekday.
    - weekly: one pair per repeat day — the per-day override from weekly_day_times when
      present, otherwise the event's single start_time/end_time (legacy rentals not yet
      migrated, and non-rental weekly events which don't use per-day times at all).
    """
    if event.event_type != 'weekly':
        return [(event_anchor_lesson_day_of_week(event), event.start_time, event.end_time)]

    day_times = getattr(event, 'weekly_day_times', None) or {}
    pairs: list[tuple[int, object, object]] = []
    for dow in normalized_weekly_repeat_lesson_dows(event):
        entry = day_times.get(str(dow)) if isinstance(day_times, dict) else None
        if entry:
            s = _parse_time_value(entry.get('start_time'))
            e = _parse_time_value(entry.get('end_time'))
        else:
            s, e = event.start_time, event.end_time
        pairs.append((dow, s, e))
    return pairs


def occurrence_time_for_date(event, occurrence_date: date):
    """Resolve the (start_time, end_time) that applies to one concrete weekly occurrence date."""
    if event.event_type != 'weekly':
        return event.start_time, event.end_time
    dow = lesson_style_dow_from_date(occurrence_date)
    for pair_dow, start, end in event_day_time_pairs(event):
        if pair_dow == dow:
            return start, end
    return event.start_time, event.end_time


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
            course__branch_id=branch_id,
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
    Checks every (weekday, time) pair the candidate occupies against every pair the
    other event occupies (not just each event's anchor weekday), so a weekly series
    with different per-day times is checked day-by-day.
    """
    from apps.scheduling.models import ScheduleEvent

    if candidate.is_daily_event or not candidate.studio_id or not candidate.branch_id:
        return False

    cand_pairs = [(d, s, e) for d, s, e in event_day_time_pairs(candidate) if s and e]
    if not cand_pairs:
        return False

    others = ScheduleEvent.objects.filter(
        is_active=True,
        branch_id=candidate.branch_id,
        studio_id=candidate.studio_id,
        is_daily_event=False,
    )

    if exclude_pk:
        others = others.exclude(pk=exclude_pk)

    for other in others:
        other_pairs = [(d, s, e) for d, s, e in event_day_time_pairs(other) if s and e]
        for cand_dow, cand_start, cand_end in cand_pairs:
            for other_dow, other_start, other_end in other_pairs:
                if other.event_type == 'weekly' or candidate.event_type == 'weekly':
                    # A recurring series only conflicts on the weekday(s) it actually repeats on.
                    if cand_dow != other_dow:
                        continue
                else:
                    # Both one_time: only the exact same calendar date can conflict.
                    if other.event_date != candidate.event_date:
                        continue
                if times_overlap(cand_start, cand_end, other_start, other_end):
                    return True
    return False


def event_conflicts_lessons(candidate):
    """Scheduled lessons block this event (same studio slot), checked per day/time pair."""
    if candidate.is_daily_event or not candidate.studio_id or not candidate.branch_id:
        return False

    for dow, start, end in event_day_time_pairs(candidate):
        if not start or not end:
            continue
        occurrence_date = candidate.event_date if candidate.event_type == 'one_time' else None
        if lesson_conflicts_studio_slot(
            candidate.branch_id,
            candidate.studio_id,
            dow,
            start,
            end,
            occurrence_date,
        ):
            return True
    return False


DAY_NAMES_HE = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']


def list_studio_occupants(
    *,
    room_id,
    day_of_week,
    start_time,
    end_time,
    exclude_lesson_ids=None,
    exclude_course_id=None,
    lesson_date=None,
):
    """Lessons and timed events already using this studio slot (warning only)."""
    occupants: list[dict] = []
    if not room_id or day_of_week is None or not start_time or not end_time:
        return occupants

    lessons = (
        Lesson.objects.filter(
            day_of_week=day_of_week,
            status='scheduled',
            room_id=room_id,
        )
        .exclude(Q(end_time__lte=start_time) | Q(start_time__gte=end_time))
        .select_related('course')
    )
    if exclude_lesson_ids:
        lessons = lessons.exclude(pk__in=list(exclude_lesson_ids))
    if exclude_course_id:
        lessons = lessons.exclude(course_id=exclude_course_id)

    for lesson in lessons:
        occupants.append({
            'kind': 'lesson',
            'name': lesson.course.name,
            'day_of_week': lesson.day_of_week,
            'day_name': DAY_NAMES_HE[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
            'start_time': lesson.start_time.strftime('%H:%M'),
            'end_time': lesson.end_time.strftime('%H:%M'),
        })

    from apps.scheduling.models import ScheduleEvent

    events = ScheduleEvent.objects.filter(
        is_active=True,
        studio_id=room_id,
        is_daily_event=False,
    )
    for ev in events:
        for ev_dow, ev_start, ev_end in event_day_time_pairs(ev):
            if not ev_start or not ev_end:
                continue
            if not times_overlap(start_time, end_time, ev_start, ev_end):
                continue
            if ev.event_type == 'weekly':
                if ev_dow != day_of_week:
                    continue
            elif ev.event_type == 'one_time':
                if lesson_date is None or lesson_date != ev.event_date:
                    continue
                if day_of_week != ev_dow:
                    continue
            else:
                continue
            occupants.append({
                'kind': 'event',
                'name': ev.name or ev.renter_name or 'אירוע או שכירות בלוח',
                'day_of_week': ev_dow,
                'day_name': DAY_NAMES_HE[ev_dow] if 0 <= ev_dow < 7 else '',
                'start_time': ev_start.strftime('%H:%M') if hasattr(ev_start, 'strftime') else str(ev_start)[:5],
                'end_time': ev_end.strftime('%H:%M') if hasattr(ev_end, 'strftime') else str(ev_end)[:5],
            })
            break

    return occupants


def list_instructor_occupants(
    *,
    instructor_id,
    day_of_week,
    start_time,
    end_time,
    exclude_lesson_ids=None,
    exclude_course_id=None,
):
    """Lessons already assigned to this instructor in the same slot (warning only)."""
    occupants: list[dict] = []
    if not instructor_id or day_of_week is None or not start_time or not end_time:
        return occupants

    lessons = (
        Lesson.objects.filter(
            day_of_week=day_of_week,
            status='scheduled',
            instructor_id=instructor_id,
        )
        .exclude(Q(end_time__lte=start_time) | Q(start_time__gte=end_time))
        .select_related('course')
    )
    if exclude_lesson_ids:
        lessons = lessons.exclude(pk__in=list(exclude_lesson_ids))
    if exclude_course_id:
        lessons = lessons.exclude(course_id=exclude_course_id)

    for lesson in lessons:
        occupants.append({
            'kind': 'instructor',
            'name': lesson.course.name,
            'day_of_week': lesson.day_of_week,
            'day_name': DAY_NAMES_HE[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
            'start_time': lesson.start_time.strftime('%H:%M'),
            'end_time': lesson.end_time.strftime('%H:%M'),
        })
    return occupants


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

    - Weekly events: conflict if the lesson's weekday matches any of the event's
      (per-day-time-aware) repeat days, with overlapping times.
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
    )

    for ev in q:
        for ev_dow, ev_start, ev_end in event_day_time_pairs(ev):
            if not ev_start or not ev_end:
                continue
            if not times_overlap(start_time, end_time, ev_start, ev_end):
                continue
            if ev.event_type == 'weekly':
                if ev_dow != day_of_week:
                    continue
                return True
            if ev.event_type == 'one_time':
                if lesson_date is None or lesson_date != ev.event_date:
                    continue
                if day_of_week != ev_dow:
                    continue
                return True
    return False
