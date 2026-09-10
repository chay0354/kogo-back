"""
Reading external students onto a register, and counting them.

Everything that needs to know "who is in this class today" comes through here,
so the visibility rule has one definition. The lesson-detail payload, the
week-list "did anyone mark this" badge, the instructor's reminder cron and the
instructor's own dashboard all call the same two functions.

The one rule worth stating out loud: ``is_external`` on the branch gates
*creation*, never *reading*. If someone flips that flag off mid-term the
register keeps working and only new additions are refused. A mis-toggle in the
branch settings must never blank out a class's roster.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from apps.enrollments.person_match import normalise_name, normalise_phone
from apps.external_students.models import ExternalStudent, ExternalStudentAttendance

# The frontend keys attendance state on one identifier per row. Real children
# keep theirs; these two values tell it which table an id came from.
ATTENDEE_KIND_CHILD = 'child'
ATTENDEE_KIND_EXTERNAL = 'external'

# The status an external row reports in the roster payload. It is not a
# Child.status value and no Child ever carries it — it exists so the register
# can render a tag without the frontend having to infer anything.
EXTERNAL_STUDENT_STATUS = 'external'


def external_visible_on_date(student: ExternalStudent, occ_date: Optional[date]) -> bool:
    """Whether this student belongs on the register for that occurrence."""
    if not student.is_active:
        return False
    if occ_date is None:
        return True
    if student.start_date and student.start_date > occ_date:
        return False
    if student.end_date and student.end_date < occ_date:
        return False
    return True


def visible_external_students(lesson, occ_date: Optional[date]) -> list[ExternalStudent]:
    return [
        s for s in ExternalStudent.objects.filter(lesson=lesson, is_active=True)
        if external_visible_on_date(s, occ_date)
    ]


def external_roster_rows(students: Iterable[ExternalStudent]) -> list[dict]:
    """
    Roster rows shaped exactly like the enrollment rows beside them.

    ``child_id`` is None and ``child_phone`` carries the student's own number —
    the register already renders that field as a contact card with WhatsApp and
    dial buttons, so an instructor gets the number without a line of new UI.
    """
    return [
        {
            'id': str(s.id),
            'child_id': None,
            'child_name': s.full_name,
            'child_status': EXTERNAL_STUDENT_STATUS,
            'child_phone': (s.phone or '').strip(),
            'trial_lesson_date': None,
            'is_trial': False,
            'trial_outcome': None,
            'trial_number': None,
            'attendee_kind': ATTENDEE_KIND_EXTERNAL,
            'attendee_id': str(s.id),
        }
        for s in students
    ]


def external_attendance_rows(students: Iterable[ExternalStudent], occ_date: Optional[date]) -> list[dict]:
    """
    Marks for those students on that date, in the same shape as AttendanceSerializer.

    Same key set as the real rows so the frontend never branches on shape.
    """
    ids = [s.id for s in students]
    if not ids or occ_date is None:
        return []
    by_id = {s.id: s for s in students}
    return [
        {
            'id': str(row.id),
            'lesson_id': str(by_id[row.student_id].lesson_id),
            'occurrence_date': row.occurrence_date.isoformat(),
            'child_id': None,
            'child_name': by_id[row.student_id].full_name,
            'child_status': EXTERNAL_STUDENT_STATUS,
            'status': row.status,
            'notes': row.notes,
            'created_at': row.created_at.isoformat(),
            'attendee_kind': ATTENDEE_KIND_EXTERNAL,
            'attendee_id': str(row.student_id),
        }
        for row in ExternalStudentAttendance.objects.filter(
            student_id__in=ids, occurrence_date=occ_date,
        )
    ]


def count_external_students_for_period(lesson, start_d: date, end_d: date) -> int:
    """
    Heads on this lesson whose window overlaps the period.

    Written for the salary tier and **deliberately not called by it yet**. The
    function that feeds the tier also feeds ``base_revenue`` from the same
    number, so wiring this in today would invent revenue from children who pay
    nothing. Turning it on is a three-line change on the salary lines only.
    """
    return sum(
        1 for s in ExternalStudent.objects.filter(lesson=lesson, is_active=True)
        if (s.start_date is None or s.start_date <= end_d)
        and (s.end_date is None or s.end_date >= start_d)
    )


def external_counts_by_lesson(lesson_ids) -> dict:
    """{lesson_id: active external head count} — one query for a page of lessons."""
    from django.db.models import Count

    return dict(
        ExternalStudent.objects
        .filter(lesson_id__in=lesson_ids, is_active=True)
        .values_list('lesson_id')
        .annotate(c=Count('id'))
        .values_list('lesson_id', 'c')
    )


def _distinct_heads(rows) -> int:
    """
    How many children those rows are, rather than how many rows they are.

    A child in a twice-weekly municipality group holds one row per lesson, the
    same way a paying child in a combined track holds one enrolment per lesson.
    The paying side answers this question with distinct children
    (``count_distinct_paying_children``), so a number sitting beside that one
    has to answer it the same way, or the two drift apart the day the first
    twice-weekly group is imported.

    Identity is the same normalised name + phone the walk-in matcher uses, so a
    child is never judged to be two people by two different definitions. A row
    with no phone folds on its name alone, which is safe here: a duplicate
    active name on one lesson is already refused, so two rows sharing a name
    across a course really are the one child attending twice.
    """
    return len({
        (normalise_name(f'{first} {last}'), normalise_phone(phone))
        for first, last, phone in rows
    })


def _distinct_counts(group_field: str, ids) -> dict:
    """{id: distinct external children} for a page of courses or branches."""
    ids = list(ids)
    if not ids:
        return {}
    buckets: dict = {}
    for key, first, last, phone in (
        ExternalStudent.objects
        .filter(is_active=True, **{f'{group_field}__in': ids})
        .values_list(group_field, 'first_name', 'last_name', 'phone')
    ):
        buckets.setdefault(key, []).append((first, last, phone))
    return {key: _distinct_heads(rows) for key, rows in buckets.items()}


def external_counts_by_course(course_ids) -> dict:
    """{course_id: distinct external children} — one query for a dashboard tab."""
    return _distinct_counts('lesson__course_id', course_ids)


def external_counts_by_branch(branch_ids) -> dict:
    """{branch_id: distinct external children} — one query for a dashboard tab."""
    return _distinct_counts('lesson__course__branch_id', branch_ids)
