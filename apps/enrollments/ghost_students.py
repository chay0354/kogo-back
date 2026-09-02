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


def _mark_present(*, lesson, child, occurrence_date, child_is_new=False):
    """
    Put the walk-in's present mark on the register.

    The same model and the same per-occurrence key mark_attendance writes, so
    the two can never disagree about what a mark is or which occurrence it
    belongs to. Nothing else mark_attendance does applies here: a walk-in has no
    previous mark to undo, no absence to clear, and no active enrolment for the
    messaging path to find.

    A child that came into being moments ago inside this same transaction cannot
    already carry a mark, so that path writes the row outright. Looking first
    would be a lookup and two savepoints spent on a question with one possible
    answer — and on a phone in a hall, every one of those is a round trip.
    """
    from apps.enrollments.models import LessonAttendance

    if child_is_new:
        LessonAttendance.objects.create(
            lesson=lesson,
            child=child,
            occurrence_date=occurrence_date,
            status='present',
        )
        return

    LessonAttendance.objects.update_or_create(
        lesson=lesson,
        child=child,
        occurrence_date=occurrence_date,
        defaults={'status': 'present'},
    )


def is_ghost_enrollment(enrollment) -> bool:
    """
    Whether this enrolment is a walk-in and nothing else.

    All three marks together, because each carries a different half of the
    guarantee: 'ghost' keeps the child out of billing and messaging,
    'inactive' keeps the enrolment out of capacity and pay, and the window is
    what the attendance screen created it with. A row missing any of them has
    been turned into something else, and something else is not ours to delete.
    """
    child = getattr(enrollment, 'child', None)
    return bool(
        child is not None
        and child.status == GHOST_STATUS
        and enrollment.status == 'inactive'
        and enrollment.ghost_visible_until is not None
    )


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

    They are marked present here rather than by a second call from the browser.
    A walk-in is by definition someone standing in the room, and a second call
    can fail on its own and leave them added but unmarked. It rides inside this
    function's transaction, so the row and its mark arrive together or not at
    all. The same is done for a repeat tap: they are in the room today whatever
    happened on an earlier occurrence.
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
            _mark_present(lesson=lesson, child=child, occurrence_date=occurrence_date)
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
    _mark_present(
        lesson=lesson, child=child, occurrence_date=occurrence_date, child_is_new=True
    )
    return enrollment, True


# Rows the walk-in flow creates itself, and so is entitled to take back with it.
# Anything else pointing at the child was put there by something outside this
# screen, and the child stays for its sake.
GHOST_OWNED_CHILD_RELATIONS = frozenset({
    'lesson_enrollments',
    'attendance_records',
    'absences',
})


def _has_foreign_references(obj, owned: frozenset) -> bool:
    """
    Whether anything outside `owned` still points at this row.

    Walked off the model's own relations rather than a written-down list, so an
    FK added later is a reason to keep the row rather than something this
    function silently cascades through. Deleting a Child reaches Payment,
    RecurringPayment and InvoiceChild; deleting a Family reaches Payment and
    Invoice. None of those may ever go because an instructor undid a tap.
    """
    from django.core.exceptions import ObjectDoesNotExist

    for rel in obj._meta.related_objects:
        accessor = rel.get_accessor_name()
        if accessor in owned:
            continue
        try:
            related = getattr(obj, accessor)
        except ObjectDoesNotExist:
            # A one-to-one with nothing on the other side.
            continue
        if rel.one_to_one:
            return True
        if related.exists():
            return True
    return False


@transaction.atomic
def delete_ghost_enrollment(*, enrollment) -> dict:
    """
    Undo an instructor's walk-in tap.

    Takes back exactly what the tap left behind: the present mark it wrote, any
    absence recorded on it since, and the enrolment itself. The instructor's
    list is what they asked to fix, and the enrolment is what puts them on it.

    The Child and its Family follow only when nothing outside this flow points
    at them. An orphan family costs something, so it is worth collecting; a
    family a payment or an invoice still names costs far more, and CASCADE
    would take it without asking. So the reference check runs first and a
    referenced row is kept rather than the deletion being refused — the
    instructor still gets the row off their register either way.

    Caller must have established this is a ghost. is_ghost_enrollment says so.
    """
    from apps.enrollments.models import ChildAbsence, LessonAttendance

    lesson = enrollment.lesson
    child = enrollment.child
    family = child.family

    removed = {
        'attendance': LessonAttendance.objects.filter(lesson=lesson, child=child).delete()[0],
        'absences': ChildAbsence.objects.filter(lesson=lesson, child=child).delete()[0],
        'child': False,
        'family': False,
    }
    enrollment.delete()

    child.refresh_from_db()
    if child.status != GHOST_STATUS or _has_foreign_references(child, GHOST_OWNED_CHILD_RELATIONS):
        return removed
    if child.lesson_enrollments.exists() or child.attendance_records.exists() or child.absences.exists():
        # Put on another lesson since, or marked on one. Still somebody's record.
        return removed

    child.delete()
    removed['child'] = True

    if family is not None and not _has_foreign_references(family, frozenset()):
        family.delete()
        removed['family'] = True

    return removed


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
