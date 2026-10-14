"""
The students report (דוח תלמידים) — the PDF the owner prints from the students
tab (14.10.2026): for the branches he picks and the dates he picks, how many
students there were on every day of the period, and how they sit today — by
weekday, by instructor, and group by group against what the group can hold.

Who is a student is not decided here. It is the rule every dashboard counts by
(owner, 24.9.2026): a regular place in a lesson, not a trial, and the child is
פעיל or בעיה באשראי — `ACTIVE_STUDENT_CHILD_STATUSES`, the same filter as
`active_student_enrollments`. A child in two lessons of a branch is one student
of the branch, and a student of each day, instructor and group they are in.

The days before today are worked back from what the database kept:

* a place was held from the day it was taken — the earlier of its start date
  and the day it was created, but never before the trial it grew out of — and,
  once closed, up to the day before its end date (or, with no end date, the day
  it was last saved). A place still open today is held to this day, whatever
  stale end date it carries: the dashboard counts it today, so the report does.
* the child's status on a past day is read backwards from the status they hold
  now, through ChildStatusHistory: the status before the first change after that
  day. Anchoring on today is what makes the report's last point the figure the
  students tab shows for the same branches — the history is not complete
  (moves *into* פעיל are often not written), and reading it forwards drifted
  away from the live number.

Read only: nothing here writes.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from django.utils import timezone

from apps.courses.models import Lesson
from apps.customers.child_status import canonical_status
from apps.customers.models import Child
from apps.customers.status_history_models import ChildStatusHistory
from apps.enrollments.enrollment_counts import (
    ACTIVE_STUDENT_CHILD_STATUSES,
    resolve_lesson_capacity,
)
from apps.enrollments.models import LessonEnrollment

# A chart of a few hundred points reads the same as one of a thousand and is
# drawn far faster; longer periods are sampled every few days.
MAX_POINTS = 400

DAY_LABELS = dict(Lesson.DAY_OF_WEEK_CHOICES)

# The field heading of a course with no field set.
NO_FIELD = 'ללא תחום'

# A group this full or more is "strong"; under half is "weak". Used only to
# order and colour — every group is listed with its own figures.
WEAK_SHARE = 0.5


@dataclass(frozen=True)
class _Place:
    """One regular place, reduced to what the count needs."""
    child_id: str
    lesson_id: str
    branch_id: str
    opened: date            # first day held
    closed: Optional[date]  # first day no longer held; None = still held


def _local_day(value: Optional[datetime]) -> Optional[date]:
    if value is None:
        return None
    if timezone.is_naive(value):
        return value.date()
    return timezone.localtime(value).date()


def place_span(row: dict, today: date) -> tuple[date, Optional[date]]:
    """
    [opened, closed) for one enrolment row — see the module docstring.

    `row` carries status, start_date, end_date, enrolled_at, updated_at and
    trial_held_on.
    """
    created = _local_day(row.get('enrolled_at'))
    starts = [d for d in (row.get('start_date'), created) if d]
    opened = min(starts) if starts else today
    # A trial row the child then paid on keeps the day it was booked as its
    # creation day; the child was a visitor, not a student, until the trial.
    if row.get('trial_held_on') and row['trial_held_on'] > opened:
        opened = row['trial_held_on']

    if row.get('status') == 'active':
        # Held today, as the students tab counts it — even a place dated to
        # start next week, or one still carrying a trial's old end date.
        return min(opened, today), None

    closed = row.get('end_date') or _local_day(row.get('updated_at')) or opened
    # Not active means not held today, whatever date was typed on it.
    return opened, min(closed, today)


def student_spans(
    current_status: str,
    changes: Iterable[tuple[date, str]],
    lo: date,
    hi_excl: date,
) -> list[tuple[date, date]]:
    """
    The stretches of [lo, hi_excl) in which the child counted as a student.

    `changes` are (day, previous_status) for every status change on or after
    `lo`, oldest first. The status at the end of day D is the previous status of
    the first change made after D; with none, the status the child holds now.
    """
    spans: list[tuple[date, date]] = []

    def emit(start, end, status):
        if start >= end:
            return
        if canonical_status(status or '') in ACTIVE_STUDENT_CHILD_STATUSES:
            if spans and spans[-1][1] == start:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))

    start = lo
    last_day = None
    for day, previous in changes:
        if day == last_day:
            # Only the first change of a day says what the day before was.
            continue
        last_day = day
        if day < lo:
            continue
        if day > start:
            emit(start, min(day, hi_excl), previous)
            start = day
        if start >= hi_excl:
            return spans
    emit(start, hi_excl, current_status)
    return spans


def _intersect(a: list[tuple[date, date]], lo: date, hi: date) -> list[tuple[date, date]]:
    out = []
    for s, e in a:
        s2, e2 = max(s, lo), min(e, hi)
        if s2 < e2:
            out.append((s2, e2))
    return out


def _merge(spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def sample_days(lo: date, hi: date) -> list[date]:
    """The days the chart has a point for: every day, or every few on a long period; always the last."""
    total = (hi - lo).days + 1
    step = max(1, math.ceil(total / MAX_POINTS))
    days = [lo + timedelta(days=i) for i in range(0, total, step)]
    if days[-1] != hi:
        days.append(hi)
    return days


class _Scope:
    """The children of one scope (all chosen branches, or one branch) and the days each was a student."""

    def __init__(self):
        self.by_child: dict[str, list[tuple[date, date]]] = defaultdict(list)

    def add(self, child_id, spans):
        if spans:
            self.by_child[child_id].extend(spans)

    def figures(self, lo: date, hi: date, days: list[date]) -> dict:
        starts, ends = [], []
        at_start = at_end = joined = left = 0
        after_lo = lo + timedelta(days=1)
        hi_excl = hi + timedelta(days=1)
        for spans in self.by_child.values():
            merged = _merge(spans)
            in_start = any(s <= lo < e for s, e in merged)
            in_end = any(s <= hi < e for s, e in merged)
            at_start += in_start
            at_end += in_end
            # Was a student on some day after the first / before the last.
            if not in_start and any(s < hi_excl and e > after_lo for s, e in merged):
                joined += 1
            if not in_end and any(s < hi and e > lo for s, e in merged):
                left += 1
            for s, e in merged:
                starts.append(s)
                ends.append(e)
        starts.sort()
        ends.sort()
        series = [
            {'date': d.isoformat(), 'students': bisect_right(starts, d) - bisect_right(ends, d)}
            for d in days
        ]
        return {
            'students': at_end,
            'students_at_start': at_start,
            'joined': joined,
            'left': left,
            'series': series,
        }


def _instructor_name(lesson) -> str:
    instructor = lesson.instructor
    return (instructor.full_name if instructor else '') or 'ללא מדריך'


def _hhmm(value: Optional[time]) -> str:
    return value.strftime('%H:%M') if value else ''


def _on_offer(lesson, as_of: date) -> bool:
    """A weekly group that was running on `as_of` — listed even with nobody in it."""
    if lesson.lesson_date and not lesson.is_recurring:
        return False
    if not lesson.course.is_active:
        return False
    created = _local_day(lesson.created_at)
    if created and created > as_of:
        return False
    if lesson.status == 'cancelled':
        cancelled = _local_day(lesson.cancelled_at)
        return bool(cancelled and cancelled > as_of)
    return True


def _occupancy(groups: list[dict]) -> dict:
    """Seats taken and offered across the groups that have a capacity."""
    sized = [g for g in groups if g['capacity']]
    seats = sum(g['students'] for g in sized)
    capacity = sum(g['capacity'] for g in sized)
    return {
        'seats': seats,
        'capacity': capacity,
        'missing': sum(g['missing'] for g in sized),
        'fill': round(seats / capacity * 100, 1) if capacity else None,
        'groups': len(groups),
        'groups_without_capacity': len(groups) - len(sized),
        'weak_groups': sum(1 for g in sized if g['students'] < g['capacity'] * WEAK_SHARE),
    }


def _breakdowns(lessons: list, students_in: dict, include_branch: bool) -> dict:
    """By weekday, by instructor and group by group, for these lessons, on the as-of day."""
    groups = []
    days: dict[int, dict] = {}
    instructors: dict[str, dict] = {}
    for lesson in lessons:
        children = students_in.get(str(lesson.id), set())
        capacity = resolve_lesson_capacity(lesson)
        count = len(children)
        course_type = lesson.course.course_type if lesson.course.course_type_id else None
        group = {
            'lesson_id': str(lesson.id),
            'course_name': lesson.course.name,
            # The field (תחום) the groups are listed under (owner, 14.10.2026).
            'field_id': str(course_type.id) if course_type else None,
            'field_name': course_type.name if course_type else NO_FIELD,
            'day': lesson.day_of_week,
            'day_label': DAY_LABELS.get(lesson.day_of_week, ''),
            'start_time': _hhmm(lesson.start_time),
            'end_time': _hhmm(lesson.end_time),
            'instructor_name': _instructor_name(lesson),
            'room_name': lesson.room.name if lesson.room else '',
            'students': count,
            'capacity': capacity,
            'missing': max(capacity - count, 0) if capacity else None,
            'fill': round(count / capacity * 100, 1) if capacity else None,
        }
        if include_branch:
            group['branch_name'] = lesson.course.branch.name
        groups.append(group)

        day = days.setdefault(lesson.day_of_week, {
            'day': lesson.day_of_week,
            'label': DAY_LABELS.get(lesson.day_of_week, ''),
            'children': set(), 'groups': [],
        })
        day['children'] |= children
        day['groups'].append(group)

        key = str(lesson.instructor_id or '')
        teacher = instructors.setdefault(key, {
            'instructor_id': key or None,
            'name': group['instructor_name'],
            'children': set(), 'groups': [],
        })
        teacher['children'] |= children
        teacher['groups'].append(group)

    def roll(entry, extra):
        occ = _occupancy(entry['groups'])
        return {
            **extra,
            'students': len(entry['children']),
            'groups': occ['groups'],
            'seats': occ['seats'],
            'capacity': occ['capacity'],
            'fill': occ['fill'],
        }

    by_day = [roll(d, {'day': d['day'], 'label': d['label']}) for _, d in sorted(days.items())]
    by_instructor = sorted(
        (roll(t, {'instructor_id': t['instructor_id'], 'name': t['name']}) for t in instructors.values()),
        key=lambda row: (-row['students'], row['name']),
    )
    # By field (owner, 14.10.2026: "הקבוצות בדוח יהיו מסודרות לפי תחום"), and
    # within a field the weakest first: the emptiest share of its capacity, then
    # the most places missing; a group with no capacity set cannot be weighed
    # and goes last in its field.
    groups.sort(key=lambda g: (
        g['field_name'] == NO_FIELD,
        g['field_name'],
        g['capacity'] is None,
        (g['students'] / g['capacity']) if g['capacity'] else 0,
        -(g['missing'] or 0),
        g['day'], g['start_time'],
    ))
    return {
        'occupancy': _occupancy(groups),
        'by_weekday': by_day,
        'by_instructor': by_instructor,
        'groups': groups,
    }


def build_students_report(branches: list, date_from: date, date_to: date, *, today: Optional[date] = None) -> dict:
    """
    The whole report in one answer: the chosen branches together, then each one.

    `branches` are Branch rows the caller has already limited to what the user
    may see. `date_to` after today is read as today — the future has no students.
    """
    today = today or timezone.localdate()
    hi = min(date_to, today)
    lo = date_from
    hi_excl = hi + timedelta(days=1)
    branch_ids = [b.id for b in branches]

    # Every regular place in these branches that may have been held in the period.
    rows = list(
        LessonEnrollment.objects
        .filter(lesson__course__branch_id__in=branch_ids, child__isnull=False, trial_lesson_date__isnull=True)
        .values(
            'child_id', 'lesson_id', 'lesson__course__branch_id', 'status',
            'start_date', 'end_date', 'enrolled_at', 'updated_at', 'trial_held_on',
        )
    )
    places: list[_Place] = []
    for row in rows:
        opened, closed = place_span(row, today)
        if opened >= hi_excl or (closed is not None and closed <= lo):
            continue
        places.append(_Place(
            child_id=str(row['child_id']),
            lesson_id=str(row['lesson_id']),
            branch_id=str(row['lesson__course__branch_id']),
            opened=opened,
            closed=closed,
        ))

    child_ids = {p.child_id for p in places}
    current = dict(Child.objects.filter(id__in=child_ids).values_list('id', 'status'))
    current = {str(k): v for k, v in current.items()}
    changes: dict[str, list[tuple[date, str]]] = defaultdict(list)
    lo_start = timezone.make_aware(datetime.combine(lo, time.min))
    history = (
        ChildStatusHistory.objects
        .filter(child_id__in=child_ids, changed_at__gte=lo_start)
        .order_by('child_id', 'changed_at')
        .values_list('child_id', 'changed_at', 'previous_status')
    )
    for child_id, changed_at, previous in history:
        changes[str(child_id)].append((_local_day(changed_at), previous))

    status_spans = {
        child_id: student_spans(current.get(child_id, ''), changes.get(child_id, []), lo, hi_excl)
        for child_id in child_ids
    }

    everyone = _Scope()
    per_branch = {str(b.id): _Scope() for b in branches}
    students_in_lesson: dict[str, set] = defaultdict(set)
    for place in places:
        held = _intersect(status_spans[place.child_id], place.opened, place.closed or hi_excl)
        if not held:
            continue
        everyone.add(place.child_id, held)
        per_branch[place.branch_id].add(place.child_id, held)
        if any(s <= hi < e for s, e in held):
            students_in_lesson[place.lesson_id].add(place.child_id)

    lessons = list(
        Lesson.objects
        .filter(course__branch_id__in=branch_ids)
        .select_related('course', 'course__branch', 'course__course_type', 'room', 'instructor')
    )
    listed = [l for l in lessons if students_in_lesson.get(str(l.id)) or _on_offer(l, hi)]

    days = sample_days(lo, hi)
    summary = everyone.figures(lo, hi, days)
    summary.update(_breakdowns(listed, students_in_lesson, include_branch=len(branches) > 1))

    branch_pages = []
    for branch in sorted(branches, key=lambda b: b.name):
        mine = [l for l in listed if l.course.branch_id == branch.id]
        page = per_branch[str(branch.id)].figures(lo, hi, days)
        page.update(_breakdowns(mine, students_in_lesson, include_branch=False))
        page.update({
            'branch_id': str(branch.id),
            'branch_name': branch.name,
            'city_name': branch.city.name if branch.city_id else '',
        })
        branch_pages.append(page)

    return {
        'date_from': lo.isoformat(),
        'date_to': hi.isoformat(),
        'requested_date_to': date_to.isoformat(),
        'generated_at': timezone.localtime().isoformat(),
        'summary': summary,
        'branches': branch_pages,
    }
