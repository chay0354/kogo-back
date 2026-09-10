"""
The same child twice on one lesson.

A parent who registers again with a slightly different record — a nickname, a
second family row, a missing ID — leaves two Child rows, and the register then
shows the same person twice. The instructor marks one of them, the other stays
open, and the lesson looks fuller than it is.

Reading and writing go through the same key (person_match), so a duplicate the
report finds is exactly one the guard would have refused.
"""
from __future__ import annotations

from collections import defaultdict

from apps.enrollments.models import LessonEnrollment
from apps.enrollments.person_match import child_person_key, person_key


def _roster_enrollments(lesson_ids=None):
    """Registered children — walk-ins carry their own supersede rule."""
    qs = (
        LessonEnrollment.objects
        .filter(status='active')
        .select_related('child', 'child__family', 'lesson', 'lesson__course')
    )
    if lesson_ids is not None:
        qs = qs.filter(lesson_id__in=lesson_ids)
    return qs


def collapse_duplicate_people(enrollments):
    """
    One row per person on a lesson.

    A parent who registered twice leaves two Child rows for one child, and both
    get an enrolment on the same lesson. The register then shows the child
    twice, the class reads as fuller than it is, and a real seat is held by a
    person who is not there. Folding them here means every screen counts and
    shows the person once, without touching the rows themselves — merging the
    cards is a decision for the office, not a side effect of drawing a list.

    A row that cannot be keyed (no name or no phone) is never folded: half a key
    is not evidence, and folding on it would hide unrelated children.
    """
    kept = {}
    order = []
    loose = []
    for enrollment in enrollments:
        key = child_person_key(getattr(enrollment, 'child', None))
        if key is None:
            loose.append(enrollment)
            continue
        current = kept.get(key)
        if current is None:
            kept[key] = enrollment
            order.append(key)
        elif _outranks(enrollment, current):
            kept[key] = enrollment
    return [kept[key] for key in order] + loose


def _outranks(candidate, current) -> bool:
    """A paying row beats a trial one; between equals the older card wins."""
    if bool(current.trial_lesson_date) != bool(candidate.trial_lesson_date):
        return not candidate.trial_lesson_date
    current_at = getattr(current, 'created_at', None)
    candidate_at = getattr(candidate, 'created_at', None)
    if current_at and candidate_at:
        return candidate_at < current_at
    return False


def duplicate_person_on_lesson(lesson, *, first_name, last_name, phone, exclude_child_id=None):
    """
    The enrolment of the same person already on this lesson, or None.

    Only ever called with both halves of the key present; a signup with no
    phone is not evidence of anything and is let through.
    """
    key = person_key(first_name=first_name, last_name=last_name, phone=phone)
    if key is None:
        return None
    for enrollment in _roster_enrollments([lesson.id if hasattr(lesson, 'id') else lesson]):
        if exclude_child_id and str(enrollment.child_id) == str(exclude_child_id):
            continue
        if child_person_key(enrollment.child) == key:
            return enrollment
    return None


def duplicate_roster_rows(lesson_ids=None) -> list[dict]:
    """Every lesson where one person sits on the register more than once."""
    by_lesson_person: dict = defaultdict(list)
    for enrollment in _roster_enrollments(lesson_ids):
        key = child_person_key(enrollment.child)
        if key is None:
            continue
        by_lesson_person[(enrollment.lesson_id, key)].append(enrollment)

    rows = []
    for (lesson_id, key), enrollments in by_lesson_person.items():
        if len(enrollments) < 2:
            continue
        lesson = enrollments[0].lesson
        rows.append({
            'lesson_id': str(lesson_id),
            'lesson_display': f'{lesson.course.name} — {lesson.start_time}',
            'course_id': str(lesson.course_id),
            'child_name': f'{enrollments[0].child.first_name} {enrollments[0].child.last_name}'.strip(),
            'phone': key[1],
            'children': [
                {
                    'child_id': str(enrollment.child_id),
                    'enrollment_id': str(enrollment.id),
                    'status': enrollment.child.status,
                    'enrolled_at': enrollment.enrolled_at.isoformat() if enrollment.enrolled_at else None,
                }
                for enrollment in enrollments
            ],
        })
    rows.sort(key=lambda row: (row['lesson_display'], row['child_name']))
    return rows
