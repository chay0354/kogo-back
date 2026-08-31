"""
Walk-ins added by an instructor from the attendance screen.

A child turns up who is not on the roster. The instructor adds them so the
lesson can be marked and the child can be followed up, without that becoming a
real registration: the Child is created with status 'ghost', which every
financial and messaging path already ignores.

Two rules keep a ghost from lingering:

* it is shown only for the next few occurrences of that lesson, and
* it disappears the moment a real child exists with the same phone, or the same
  full name on that same lesson.

The second is resolved when the roster is read rather than by a background job,
so a ghost can never outlive the real record that replaced it. Matching is
deliberately strict — never on first name alone, which would collide constantly
in a class of children.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from django.db import transaction

GHOST_STATUS = 'ghost'

# How many future occurrences of the lesson a walk-in stays visible for.
GHOST_OCCURRENCES = 3


def _normalise_phone(raw: str | None) -> str:
    """Digits only, so 050-123-4567 and 0501234567 compare equal."""
    if not raw:
        return ''
    return ''.join(ch for ch in str(raw) if ch.isdigit())


def _normalise_name(raw: str | None) -> str:
    return ' '.join(str(raw or '').split()).strip().casefold()


def ghost_window_end(lesson, from_date: date) -> date:
    """
    Last date a walk-in on this lesson stays visible.

    A recurring lesson meets weekly, so three more occurrences is three weeks.
    A one-off has only the occurrence it was added on.
    """
    if not getattr(lesson, 'is_recurring', False):
        return from_date
    return from_date + timedelta(weeks=GHOST_OCCURRENCES)


@transaction.atomic
def create_ghost_enrollment(*, lesson, first_name: str, last_name: str, phone: str = '', occurrence_date: date):
    """
    Create the walk-in and put them on this lesson.

    Returns (enrollment, created). If an unexpired ghost with the same name is
    already on the lesson, that one is returned instead of a duplicate — an
    instructor tapping twice should not produce two rows.
    """
    from apps.customers.models import Child, Family
    from apps.enrollments.models import LessonEnrollment

    first_name = (first_name or '').strip()
    last_name = (last_name or '').strip()
    if not first_name or not last_name:
        raise ValueError('נדרשים שם פרטי ושם משפחה')

    full = _normalise_name(f'{first_name} {last_name}')

    existing = (
        LessonEnrollment.objects
        .filter(lesson=lesson, ghost_visible_until__isnull=False, child__status=GHOST_STATUS)
        .select_related('child')
    )
    for enrollment in existing:
        child = enrollment.child
        if _normalise_name(f'{child.first_name} {child.last_name}') == full:
            return enrollment, False

    family = Family.objects.create(
        name=f'{last_name} (נוסף בשיעור)',
        phone=(phone or '').strip(),
    )
    child = Child.objects.create(
        family=family,
        first_name=first_name,
        last_name=last_name,
        birth_date=occurrence_date,  # unknown; the real record will carry the truth
        gender='male',               # unknown; not used for a ghost
        status=GHOST_STATUS,
        phone_number=(phone or '').strip(),
        notes='נוסף על ידי המדריך ממסך הנוכחות',
    )
    enrollment = LessonEnrollment.objects.create(
        lesson=lesson,
        child=child,
        status='inactive',  # never 'active': that is what capacity and pay count
        start_date=occurrence_date,
        ghost_visible_until=ghost_window_end(lesson, occurrence_date),
        notes='תלמיד שהגיע ואינו רשום',
    )
    return enrollment, True


def visible_ghost_enrollments(lesson, occurrence_date: date, real_enrollments: Iterable):
    """
    Ghosts that should still appear on this lesson's roster for this date.

    Drops anything past its window, and anything a real child has since
    replaced — same phone anywhere, or same full name on this lesson.
    """
    from apps.enrollments.models import LessonEnrollment

    ghosts = list(
        LessonEnrollment.objects
        .filter(lesson=lesson, ghost_visible_until__isnull=False, child__status=GHOST_STATUS)
        .select_related('child', 'child__family')
    )
    if not ghosts:
        return []

    real_names = set()
    real_phones = set()
    for enrollment in real_enrollments:
        child = getattr(enrollment, 'child', None)
        if child is None or child.status == GHOST_STATUS:
            continue
        real_names.add(_normalise_name(f'{child.first_name} {child.last_name}'))
        for candidate in (
            getattr(child, 'phone_number', ''),
            getattr(getattr(child, 'family', None), 'phone', ''),
        ):
            digits = _normalise_phone(candidate)
            if digits:
                real_phones.add(digits)

    visible = []
    for ghost in ghosts:
        if ghost.ghost_visible_until and occurrence_date > ghost.ghost_visible_until:
            continue
        child = ghost.child
        if _normalise_name(f'{child.first_name} {child.last_name}') in real_names:
            continue
        digits = _normalise_phone(getattr(child, 'phone_number', '') or getattr(child.family, 'phone', ''))
        if digits and digits in real_phones:
            continue
        visible.append(ghost)
    return visible
