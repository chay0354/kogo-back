"""The dimensions every ledger row carries, so one filter bar can slice all of them.

The invoices page filters by business → (for the branch network) city and
branch → course type → age → instructor. Documents, charges and standing orders
come from different models, so each builds its rows here and they all answer
with the same keys. A row with no lesson behind it (a store sale, a manual
document) still carries every key, empty — the client never branches on shape.
"""
from __future__ import annotations

EMPTY_DIMENSIONS = {
    'business_id': None,
    'business_name': '',
    'city_id': None,
    'city_name': '',
    'course_id': None,
    'course_name': '',
    'course_type_id': None,
    'course_type_name': '',
    'age_key': '',
    'age_label': '',
    'instructor_id': None,
    'instructor_name': '',
}

# select_related paths below a lesson, for callers that fetch rows in bulk.
LESSON_PATHS = ('course', 'course__course_type', 'course__business', 'course__branch__city', 'instructor')


def lesson_paths(prefix: str) -> list[str]:
    """`lesson_paths('payment__lesson')` → every path this module reads, prefixed."""
    return [prefix] + [f'{prefix}__{path}' for path in LESSON_PATHS]


def age_key(min_age, max_age) -> str:
    """A stable filter value for a course's age range: '6-9', '6-', '-9', or ''."""
    if not (min_age or max_age):
        return ''
    return f'{min_age or ""}-{max_age or ""}'


def age_label(min_age, max_age) -> str:
    if min_age and max_age:
        return f'גילאי {min_age}–{max_age}'
    if min_age:
        return f'מגיל {min_age}'
    if max_age:
        return f'עד גיל {max_age}'
    return ''


def parse_age_key(value: str) -> tuple[int | None, int | None]:
    low, _, high = (value or '').partition('-')
    return (int(low) if low.isdigit() else None, int(high) if high.isdigit() else None)


def branch_dimensions(branch) -> dict:
    if branch is None:
        return {'city_id': None, 'city_name': ''}
    city = branch.city if branch.city_id else None
    return {'city_id': str(branch.city_id) if branch.city_id else None, 'city_name': city.name if city else ''}


def lesson_dimensions(lesson) -> dict:
    out = dict(EMPTY_DIMENSIONS)
    if lesson is None or not lesson.course_id:
        return out
    course = lesson.course
    out.update({
        'course_id': str(course.id),
        'course_name': course.name,
        'course_type_id': str(course.course_type_id) if course.course_type_id else None,
        'course_type_name': course.course_type.name if course.course_type_id else '',
        'age_key': age_key(course.min_age, course.max_age),
        'age_label': age_label(course.min_age, course.max_age),
        'instructor_id': str(lesson.instructor_id) if lesson.instructor_id else None,
        'instructor_name': lesson.instructor.full_name if lesson.instructor_id else '',
        'business_id': str(course.business_id) if course.business_id else None,
        'business_name': course.business.name if course.business_id else '',
    })
    out.update(branch_dimensions(course.branch if course.branch_id else None))
    return out


def row_dimensions(*, lesson=None, branch=None, business=None) -> dict:
    """The lesson knows the most; a branch or a business fills in what it does not."""
    out = lesson_dimensions(lesson)
    if out['city_id'] is None and branch is not None:
        out.update(branch_dimensions(branch))
    if out['business_id'] is None and business is not None:
        out.update({'business_id': str(business.id), 'business_name': business.name})
    return out
