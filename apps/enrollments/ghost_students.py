"""
Walk-ins added by an instructor from the attendance screen.

A child turns up who is not on the roster. The instructor adds them so the
lesson can be marked and the child can be followed up, without that becoming a
real registration: the Child is created with status 'ghost', which every
financial and messaging path already ignores.

Two rules keep a ghost from lingering:

* it is shown on the lesson it came to and on the next one only — there with
  "הגיע בשיעור שעבר", the last time unless the instructor marks it present
  again, which keeps it one lesson more (owner, 14.10.2026: "בפעם השלישית הילד
  הזה כבר לא יופיע"); and
* it disappears the moment a real child exists with the same phone, or the same
  full name on that same lesson.

A walk-in with no phone is nobody the office can reach, so once its last
lesson has passed it is removed altogether (remove_expired_phoneless_ghosts).
One with a phone stays: it is a lead.

The second is resolved when the roster is read rather than by a background job,
so a ghost can never outlive the real record that replaced it. Matching is
deliberately strict — never on first name alone, which would collide constantly
in a class of children.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from django.db import transaction

from apps.enrollments.person_match import normalise_name, normalise_phone

GHOST_STATUS = 'ghost'

# After the lesson a walk-in came to, how many more of that lesson it is shown
# on: the next one, and only that (owner, 14.10.2026). Coming again — the
# instructor marks it present — counts from that lesson instead.
GHOST_NEXT_LESSONS = 1

# How far ahead a cancelled week is stepped over to find "the next lesson".
_NEXT_LESSON_LOOKAHEAD_WEEKS = 4


# The same rules the duplicate check uses, so a walk-in and a registration are
# never judged to be the same person by two different definitions.
_normalise_phone = normalise_phone
_normalise_name = normalise_name


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


def next_lesson_after(lesson, day: date) -> date:
    """
    The next date this weekly lesson meets after `day`, stepping over a week the
    office cancelled — otherwise a holiday would swallow the walk-in's last
    appearance and it would vanish without the instructor being told.
    """
    from apps.scheduling.models import LessonCancellation

    candidates = [day + timedelta(weeks=n) for n in range(1, _NEXT_LESSON_LOOKAHEAD_WEEKS + 1)]
    cancelled = set(
        LessonCancellation.objects
        .filter(lesson=lesson, occurrence_date__in=candidates)
        .values_list('occurrence_date', flat=True)
    )
    for candidate in candidates:
        if candidate not in cancelled:
            return candidate
    return candidates[0]


def ghost_window_end(lesson, last_seen: date) -> date:
    """
    Last date a walk-in on this lesson stays visible: the next lesson after the
    one it last came to. A one-off lesson has only the occurrence it was added on.
    """
    if not getattr(lesson, 'is_recurring', False):
        return last_seen
    day = last_seen
    for _ in range(GHOST_NEXT_LESSONS):
        day = next_lesson_after(lesson, day)
    return day


def last_seen_on_lesson(lesson, child_ids) -> dict:
    """{child_id: the last date each was marked present on this lesson}."""
    from django.db.models import Max

    from apps.enrollments.models import LessonAttendance

    if not child_ids:
        return {}
    return dict(
        LessonAttendance.objects
        .filter(lesson=lesson, child_id__in=list(child_ids), status='present', occurrence_date__isnull=False)
        .values('child_id')
        .annotate(last=Max('occurrence_date'))
        .values_list('child_id', 'last')
    )


def refresh_ghost_window(lesson, child) -> None:
    """
    Recount how long a walk-in stays on this lesson's register after a mark.

    Marked present again → shown on the lesson after that one too. The mark
    taken back → back to the lesson after the visit before it. Never before the
    lesson it was added on.
    """
    from apps.enrollments.models import LessonEnrollment

    if getattr(child, 'status', None) != GHOST_STATUS:
        return
    rows = list(
        LessonEnrollment.objects
        .filter(lesson=lesson, child=child, ghost_visible_until__isnull=False)
        .only('id', 'start_date', 'ghost_visible_until')
    )
    if not rows:
        return
    last = last_seen_on_lesson(lesson, [child.id]).get(child.id)
    for row in rows:
        anchor = max(d for d in (last, row.start_date) if d is not None) if (last or row.start_date) else None
        if anchor is None:
            continue
        window_end = ghost_window_end(lesson, anchor)
        if row.ghost_visible_until != window_end:
            LessonEnrollment.objects.filter(pk=row.pk).update(ghost_visible_until=window_end)


def ghost_phone(child) -> str:
    """The walk-in's phone, digits only — its own, or the family's. '' when none."""
    return _normalise_phone(
        (getattr(child, 'phone_number', '') or '')
        or (getattr(getattr(child, 'family', None), 'phone', '') or '')
    )


def valid_phone(raw: str) -> str:
    """
    A phone an instructor typed, as stored: digits only, starting 0. '' for an
    empty box. Raises ValueError for something that cannot be an Israeli number.
    """
    digits = _normalise_phone(raw)
    if not digits:
        return ''
    if not digits.startswith('0') or not 9 <= len(digits) <= 10:
        raise ValueError('מספר טלפון לא תקין')
    return digits


def register_facts(enrollment, occurrence_date: date, last_seen: date | None) -> dict:
    """
    What the instructor's register says about a walk-in on this date.

    came_before   it came to an earlier lesson and has not been marked here yet
                  ("הגיע בשיעור שעבר");
    last_time     unless it is marked present on this date, this is the last
                  lesson it is shown on;
    phone_missing nobody has its phone yet — it is no lead without one.
    """
    child = enrollment.child
    seen = last_seen or enrollment.start_date
    window_end = enrollment.ghost_visible_until
    return {
        'added_on': enrollment.start_date.isoformat() if enrollment.start_date else None,
        'last_seen': seen.isoformat() if seen else None,
        'came_before': bool(seen and occurrence_date and seen < occurrence_date),
        'last_time': bool(
            seen and occurrence_date and window_end
            and seen < occurrence_date and occurrence_date >= window_end
        ),
        'phone_missing': not ghost_phone(child),
    }


@transaction.atomic
def set_ghost_contact(*, enrollment, phone=None, first_name=None, last_name=None) -> None:
    """
    Fill in what the instructor did not have when the walk-in was added: the
    phone (owner, 14.10.2026: "יהיה לו אופציה להוסיף מספר טלפון"), or a name
    spelt better. Only the fields given change. The phone is written to the
    child and to the family the walk-in flow opened for it — the two places a
    registration later looks for it.
    """
    child = enrollment.child
    child_fields = []
    if first_name is not None:
        first_name = first_name.strip()
        if not first_name:
            raise ValueError('נדרש שם פרטי')
        child.first_name = first_name
        child_fields.append('first_name')
    if last_name is not None:
        last_name = last_name.strip()
        if not last_name:
            raise ValueError('נדרש שם משפחה')
        child.last_name = last_name
        child_fields.append('last_name')
    if phone is not None:
        digits = valid_phone(phone)
        child.phone_number = digits
        child_fields.append('phone_number')
        family = child.family
        if family is not None and family.name.endswith('(נוסף בשיעור)'):
            family.phone = digits
            family.save(update_fields=['phone'])
    if child_fields:
        child.save(update_fields=child_fields)


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
    phone = valid_phone(phone or '')

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
            refresh_ghost_window(lesson, child)
            if phone and not ghost_phone(child):
                set_ghost_contact(enrollment=enrollment, phone=phone)
            return enrollment, False

    family = Family.objects.create(
        name=f'{last_name} (נוסף בשיעור)',
        phone=phone,
    )
    child = Child.objects.create(
        family=family,
        first_name=first_name,
        last_name=last_name,
        birth_date=occurrence_date,  # unknown; the real record will carry the truth
        gender='male',               # unknown; not used for a ghost
        status=GHOST_STATUS,
        phone_number=phone,
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


def _phones_of_registered_children(phones: set) -> set:
    """
    Which of these phone numbers already belong to a registered child, anywhere.

    A ghost is a stand-in for a child the system does not hold. The moment the
    office registers that child — for any lesson, as a trial or as a subscriber
    — the stand-in is a second row for one person, and the register stops
    describing the room. So the phone is matched across the whole system and not
    only against this lesson's roster.

    The name is not, and that asymmetry is deliberate: a phone identifies a
    family, while two different children can share a full name across branches,
    and a global name match would quietly delete one of them from a register.
    """
    from apps.customers.models import Child

    if not phones:
        return set()

    candidates = (
        Child.objects
        .exclude(status=GHOST_STATUS)
        .values_list('phone_number', 'family__phone')
    )
    found = set()
    for own, family_phone in candidates.iterator(chunk_size=2000):
        for raw in (own, family_phone):
            digits = _normalise_phone(raw)
            if digits and digits in phones:
                found.add(digits)
    return found


def _names_registered_once_in_branch(names: set, branch_id) -> set:
    """
    Which of these full names belong to exactly one registered child in the branch.

    For a ghost typed in without a phone, the name is all there is. Matched
    across the whole system it would be unsafe — two children share a full name
    across branches, and one of them would vanish from a register somebody is
    standing in front of. So it is narrowed twice: to the branch the ghost was
    added in, and to a name that one registered child holds there, not two.

    A walk-in is almost always a local child, which is what makes the branch the
    right fence. When the name is ambiguous the ghost stays, and runs out with
    its window as before: leaving a ghost up a little longer is a small cost,
    removing a real child from the register is not.
    """
    from django.db.models import Q

    from apps.customers.models import Child

    if not names or branch_id is None:
        return set()

    in_branch = (
        Child.objects
        .exclude(status=GHOST_STATUS)
        .filter(
            Q(family__branch_id=branch_id)
            | Q(lesson_enrollments__lesson__course__branch_id=branch_id)
        )
        .values_list('id', 'first_name', 'last_name')
        .distinct()
    )
    holders: dict[str, set] = {}
    for child_id, first, last in in_branch.iterator(chunk_size=2000):
        name = _normalise_name(f'{first} {last}')
        if name in names:
            holders.setdefault(name, set()).add(child_id)
    return {name for name, ids in holders.items() if len(ids) == 1}


def visible_ghost_enrollments(lesson, occurrence_date: date, real_enrollments: Iterable):
    """
    Ghosts that should still appear on this lesson's roster for this date.

    Drops anything past its window, and anything a real child has since
    replaced — the same phone anywhere in the system, or the same full name on
    this lesson.
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

    # Ghosts still inside their window; the rest are gone whatever else is true.
    in_window = [
        g for g in ghosts
        if not (g.ghost_visible_until and occurrence_date > g.ghost_visible_until)
    ]

    # One query for the lot rather than one per ghost: a lesson rarely holds
    # more than a couple, but the roster is read on every register open.
    ghost_phones = set()
    for ghost in in_window:
        digits = _normalise_phone(
            getattr(ghost.child, 'phone_number', '')
            or getattr(getattr(ghost.child, 'family', None), 'phone', '')
        )
        if digits:
            ghost_phones.add(digits)
    registered_phones = real_phones | _phones_of_registered_children(ghost_phones - real_phones)

    # A ghost with no phone has only a name to go on. It still clears on its own
    # lesson by that name, as it always did; beyond the lesson it needs the name
    # to be unmistakable in the branch.
    phoneless_names = set()
    for ghost in in_window:
        if not _normalise_phone(
            getattr(ghost.child, 'phone_number', '')
            or getattr(getattr(ghost.child, 'family', None), 'phone', '')
        ):
            phoneless_names.add(_normalise_name(f'{ghost.child.first_name} {ghost.child.last_name}'))
    branch_id = getattr(getattr(lesson, 'course', None), 'branch_id', None)
    registered_names = real_names | _names_registered_once_in_branch(
        phoneless_names - real_names, branch_id,
    )

    visible = []
    for ghost in in_window:
        child = ghost.child
        name = _normalise_name(f'{child.first_name} {child.last_name}')
        digits = _normalise_phone(
            getattr(child, 'phone_number', '')
            or getattr(getattr(child, 'family', None), 'phone', '')
        )
        if name in real_names:
            continue
        if digits and digits in registered_phones:
            continue
        # The branch-wide name match is only for a ghost that has no phone. One
        # that does is identified by it, and a namesake with another number is
        # somebody else.
        if not digits and name in registered_names:
            continue
        visible.append(ghost)
    return visible


def phoneless_walk_ins_on(lesson, day: date) -> list:
    """
    Walk-ins marked present on this lesson on this day with no phone yet — what
    the instructor is asked to fill in before the lesson ends.
    """
    from apps.enrollments.models import LessonAttendance

    children = [
        row.child for row in (
            LessonAttendance.objects
            .filter(lesson=lesson, occurrence_date=day, status='present', child__status=GHOST_STATUS)
            .select_related('child', 'child__family')
            .order_by('child__first_name', 'child__last_name')
        )
    ]
    return [child for child in children if not ghost_phone(child)]


def remove_expired_phoneless_ghosts(today: date, *, limit: int = 50) -> int:
    """
    Take away the walk-ins nobody can reach once their last lesson has passed.

    Owner, 14.10.2026: "אם אין מספר טלפון על הילד הזה, הוא נמחק... ולא קיים יותר
    כי אין לנו פרטים עליו בכלל." Only a ghost with no phone, only after the
    last lesson it was shown on, and only when no lesson still shows it. The
    rows go the way an instructor's undo takes them (delete_ghost_enrollment):
    the child and family follow only when nothing else points at them.
    A ghost with a phone is never removed here — it is a lead.
    """
    from django.db.models import Q

    from apps.enrollments.models import LessonEnrollment

    no_own_phone = Q(child__phone_number__isnull=True) | Q(child__phone_number='')
    no_family_phone = Q(child__family__isnull=True) | Q(child__family__phone__isnull=True) | Q(child__family__phone='')
    rows = (
        LessonEnrollment.objects
        .filter(child__status=GHOST_STATUS, status='inactive', ghost_visible_until__lt=today)
        .filter(no_own_phone & no_family_phone)
        .select_related('child', 'child__family', 'lesson')
        .order_by('ghost_visible_until')[:limit]
    )
    removed = 0
    for enrollment in rows:
        child = enrollment.child
        if ghost_phone(child) or not is_ghost_enrollment(enrollment):
            continue
        if LessonEnrollment.objects.filter(child=child, ghost_visible_until__gte=today).exists():
            continue
        delete_ghost_enrollment(enrollment=enrollment)
        removed += 1
    return removed


def walk_in_summary(child) -> dict | None:
    """
    What the office sees about a walk-in (owner, 14.10.2026: "שיהיה רשום באיזה
    שיעור, איזה תאריך הוא הגיע"): every lesson it was added to, when it first and
    last came, and until when the instructor still sees it.
    """
    from apps.enrollments.models import LessonAttendance, LessonEnrollment

    if getattr(child, 'status', None) != GHOST_STATUS:
        return None
    rows = list(
        LessonEnrollment.objects
        .filter(child=child, ghost_visible_until__isnull=False)
        .select_related('lesson', 'lesson__course', 'lesson__course__branch', 'lesson__instructor')
        .order_by('start_date')
    )
    visits: dict = {}
    for lesson_id, day in (
        LessonAttendance.objects
        .filter(child=child, status='present', occurrence_date__isnull=False)
        .values_list('lesson_id', 'occurrence_date')
    ):
        visits.setdefault(lesson_id, []).append(day)
    lessons = []
    for row in rows:
        lesson = row.lesson
        days = sorted(visits.get(lesson.id, [])) or ([row.start_date] if row.start_date else [])
        course = lesson.course
        lessons.append({
            'lesson_id': str(lesson.id),
            'course_name': course.name if course else '',
            'branch_name': course.branch.name if course and course.branch_id else '',
            'instructor_name': lesson.instructor.full_name if lesson.instructor_id else '',
            'day_of_week': lesson.day_of_week,
            'start_time': lesson.start_time.strftime('%H:%M') if lesson.start_time else '',
            'first_seen': days[0].isoformat() if days else None,
            'last_seen': days[-1].isoformat() if days else None,
            'visits': len(days),
            'shown_until': row.ghost_visible_until.isoformat() if row.ghost_visible_until else None,
        })
    return {
        'lessons': lessons,
        'phone_missing': not ghost_phone(child),
    }
