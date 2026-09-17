"""
One child, one record.

Two things produce a second record for a child the system already holds.

* **Walk-ins.** An instructor adds a child from the attendance screen, the child
  is saved as `ghost`, and later the parent registers — or already had, and the
  child simply was not on that day's list. The walk-in flow hides such a ghost
  on the register, but never took the record away, so it stayed in the
  customers list and in every ghost count for good. In production, on the day
  this was written, 53 of 98 ghosts had a registered twin.
* **Repeat registrations.** A parent books a trial, then another, then pays, and
  each pass can leave its own Child row. One girl held five.

This module decides which records are the same child and folds the extras into
the one that stays. Deciding is kept apart from doing: `plan_merges` works on
plain rows and touches nothing, so the same plan can be read against a copy of
production before a single row is written.

Which record stays
------------------
The one money points at, if exactly one does. Otherwise the best status —
active before payment_problem before the trial states before pending — and the
oldest on a tie. That is "unify by the active one", with one refinement: a row
that carries a payment, a standing order, an invoice, a signature or a card link
is never deleted, whatever its status, because those rows are the business's
records and CASCADE would take them without asking. Two such rows for one
person are reported and left alone.

When two records are the same child
-----------------------------------
Stricter than the rule that hides a ghost on the register, because hiding is
undone by the next read and merging is not.

* Two registered records: full name **and** phone, together (`person_key`).
  Siblings share a phone; classmates share a name.
* A ghost with a phone: the same phone **and** the same first name. A family's
  phone plus a first name is one child — which is what catches a surname the
  instructor typed wrong — while a sibling on the same phone keeps a different
  first name and is left alone.
* A ghost typed without a phone: the same full name, held by exactly one
  registered child in a branch the ghost belongs to. Ambiguous names stay a
  ghost and run out with their window.
* A ghost whose phone disagrees with the registered child's: not merged. A
  different number is evidence of a different child, and a typo is not worth
  deleting somebody over.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.enrollments.person_match import normalise_name, normalise_phone

logger = logging.getLogger(__name__)

# Set while a merge is writing, so the post_save hook does not start another
# sweep for the record the merge itself just saved.
merging: ContextVar[bool] = ContextVar('child_merge_in_progress', default=False)

GHOST = 'ghost'

# How long a new registration is left alone before it may be folded away.
REGISTRATION_SETTLE = timedelta(hours=24)

# Best first. A status not listed sorts last.
STATUS_PRECEDENCE = (
    'active',
    'payment_problem',
    'trial_signed',
    'trial_completed',
    'pending',
    'inactive',
    GHOST,
)

# Relations whose rows are history of the same child and follow it to the record
# that stays.
MOVABLE_CHILD_RELATIONS = frozenset({
    'lesson_enrollments',
    'attendance_records',
    'absences',
    'status_history',
    'enrollments',
})

# Anything else pointing at a Child is money or a document. A record holding any
# of it is never the one deleted. Read off the model rather than listed, so a
# relation added later blocks a merge instead of being cascaded through.


def _status_rank(status: str) -> int:
    try:
        return STATUS_PRECEDENCE.index(status)
    except ValueError:
        return len(STATUS_PRECEDENCE)


@dataclass
class ChildRow:
    """What planning needs to know about one Child, and nothing it could write."""

    id: object
    first_name: str
    last_name: str
    phone: str                      # the child's own, else the family's
    status: str
    created_at: object
    branch_ids: frozenset = frozenset()
    blocking: bool = False          # money or a document points at it

    @property
    def full_name(self) -> str:
        return normalise_name(f'{self.first_name} {self.last_name}')

    @property
    def first(self) -> str:
        return normalise_name(self.first_name)

    @property
    def digits(self) -> str:
        return normalise_phone(self.phone)


@dataclass
class MergeStep:
    source_id: object
    target_id: object
    reason: str


@dataclass
class MergePlan:
    steps: list[MergeStep] = field(default_factory=list)
    # (child ids, why) — found to be one person but not safe to fold.
    skipped: list[tuple[tuple, str]] = field(default_factory=list)


def _best(rows: Iterable[ChildRow]) -> ChildRow:
    return min(rows, key=lambda r: (_status_rank(r.status), r.created_at))


def plan_merges(rows: Iterable[ChildRow]) -> MergePlan:
    """Which records fold into which. Reads nothing, writes nothing."""
    rows = list(rows)
    plan = MergePlan()
    real = [r for r in rows if r.status != GHOST]
    ghosts = [r for r in rows if r.status == GHOST]

    # --- registered records of one person -----------------------------------
    people: dict[tuple, list[ChildRow]] = {}
    for row in real:
        if row.full_name and row.digits:
            people.setdefault((row.full_name, row.digits), []).append(row)

    # For each person, the record everything folds into.
    target_of: dict[tuple, ChildRow] = {}
    for key, group in people.items():
        blockers = [r for r in group if r.blocking]
        if len(blockers) >= 2:
            target = _best(blockers)
            others = [r for r in blockers if r is not target]
            plan.skipped.append((
                tuple(r.id for r in [target, *others]),
                'אותו ילד מופיע בכמה רשומות שיש לכל אחת תשלום או מסמך — דורש איחוד ידני',
            ))
        elif blockers:
            target = blockers[0]
        else:
            target = _best(group)
        target_of[key] = target
        for row in group:
            if row is target or row.blocking:
                continue
            plan.steps.append(MergeStep(row.id, target.id, 'אותו שם מלא ואותו טלפון'))

    def person_target(row: ChildRow) -> ChildRow:
        key = (row.full_name, row.digits)
        return target_of.get(key, row)

    # --- ghosts -------------------------------------------------------------
    by_phone: dict[str, list[ChildRow]] = {}
    by_name: dict[str, list[ChildRow]] = {}
    for row in real:
        if row.digits:
            by_phone.setdefault(row.digits, []).append(row)
        if row.full_name:
            by_name.setdefault(row.full_name, []).append(row)

    for ghost in ghosts:
        if ghost.blocking:
            plan.skipped.append(((ghost.id,), 'תלמיד רפאים שיש לו תשלום או מסמך'))
            continue

        if ghost.digits:
            same_first = [r for r in by_phone.get(ghost.digits, []) if r.first == ghost.first]
            if not same_first:
                # Nobody on that phone, or only a sibling. Either way not this child.
                continue
            targets = {person_target(r).id: person_target(r) for r in same_first}
            if len(targets) > 1:
                plan.skipped.append((
                    (ghost.id, *targets), 'כמה ילדים רשומים עם אותו טלפון ושם פרטי',
                ))
                continue
            target = next(iter(targets.values()))
            plan.steps.append(MergeStep(ghost.id, target.id, 'תלמיד רפאים — אותו טלפון ושם פרטי'))
            continue

        # No phone: a full name, fenced to the ghost's branch.
        in_branch = [
            r for r in by_name.get(ghost.full_name, [])
            if not ghost.branch_ids or (r.branch_ids & ghost.branch_ids)
        ]
        if not in_branch:
            continue
        targets = {person_target(r).id: person_target(r) for r in in_branch}
        if len(targets) > 1:
            plan.skipped.append(((ghost.id, *targets), 'שם זהה לכמה ילדים שונים בסניף'))
            continue
        target = next(iter(targets.values()))
        plan.steps.append(MergeStep(ghost.id, target.id, 'תלמיד רפאים — אותו שם מלא בסניף'))

    return plan


# --------------------------------------------------------------------------
# Reading rows from the database
# --------------------------------------------------------------------------

def _blocking_accessors(model, movable: frozenset) -> list[str]:
    return [
        rel.get_accessor_name()
        for rel in model._meta.related_objects
        if rel.get_accessor_name() not in movable
    ]


def load_rows(queryset=None) -> list[ChildRow]:
    """ChildRows for every child in `queryset` (all children by default)."""
    from django.db.models import Exists, OuterRef

    from apps.customers.models import Child

    qs = queryset if queryset is not None else Child.objects.all()
    annotations = {}
    for rel in Child._meta.related_objects:
        accessor = rel.get_accessor_name()
        if accessor in MOVABLE_CHILD_RELATIONS:
            continue
        related = rel.related_model
        annotations[f'_b_{accessor}'] = Exists(
            related.objects.filter(**{rel.field.name: OuterRef('pk')})
        )
    qs = (
        qs.select_related('family')
        .annotate(**annotations)
        .prefetch_related('lesson_enrollments__lesson__course')
    )

    rows = []
    for child in qs:
        branches = set()
        if child.family_id and child.family.branch_id:
            branches.add(child.family.branch_id)
        for enrollment in child.lesson_enrollments.all():
            course = enrollment.lesson.course if enrollment.lesson_id else None
            if course is not None and course.branch_id:
                branches.add(course.branch_id)
        rows.append(ChildRow(
            id=child.pk,
            first_name=child.first_name or '',
            last_name=child.last_name or '',
            phone=(child.phone_number or '').strip() or (getattr(child.family, 'phone', '') or ''),
            status=child.status or '',
            created_at=child.created_at,
            branch_ids=frozenset(branches),
            blocking=any(getattr(child, key) for key in annotations),
        ))
    return rows


# --------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------

def _has_blocking_refs(obj, movable: frozenset) -> bool:
    from django.core.exceptions import ObjectDoesNotExist

    for accessor in _blocking_accessors(type(obj), movable):
        try:
            related = getattr(obj, accessor)
        except ObjectDoesNotExist:
            continue
        if hasattr(related, 'exists'):
            if related.exists():
                return True
        elif related is not None:
            return True
    return False


def _move_lesson_enrollments(source, target):
    from apps.enrollments.models import LessonEnrollment

    for row in LessonEnrollment.objects.filter(child=source):
        is_walk_in = row.ghost_visible_until is not None and row.status == 'inactive'
        existing = LessonEnrollment.objects.filter(child=target, lesson_id=row.lesson_id).first()
        if existing is None and not is_walk_in:
            row.child = target
            row.save(update_fields=['child', 'updated_at'])
            continue
        if existing is not None:
            # Keep the record that stays, but not at the price of the trial's
            # history: a date or an outcome only the duplicate knew is copied.
            changed = []
            if not existing.trial_held_on and row.trial_held_on:
                existing.trial_held_on = row.trial_held_on
                changed.append('trial_held_on')
            if not existing.trial_outcome and row.trial_outcome:
                existing.trial_outcome = row.trial_outcome
                changed.append('trial_outcome')
            if changed:
                LessonEnrollment.objects.filter(pk=existing.pk).update(
                    **{name: getattr(existing, name) for name in changed}
                )
        # A walk-in's own row is the ghost's placeholder on the register, not a
        # registration; the attendance it earned moves separately.
        row.delete()


def _move_attendance(source, target):
    from apps.enrollments.models import LessonAttendance

    for mark in LessonAttendance.objects.filter(child=source):
        existing = LessonAttendance.objects.filter(
            child=target, lesson_id=mark.lesson_id, occurrence_date=mark.occurrence_date,
        ).first()
        if existing is None:
            mark.child = target
            mark.save(update_fields=['child'])
            continue
        # Two marks for one child on one day: somebody saw them in the room.
        if mark.status == 'present' and existing.status != 'present':
            LessonAttendance.objects.filter(pk=existing.pk).update(status='present')
        mark.delete()


def _move_simple(source, target, model, unique_fields: tuple):
    for row in model.objects.filter(child=source):
        clash = model.objects.filter(
            child=target, **{f: getattr(row, f) for f in unique_fields},
        ).exists() if unique_fields else False
        if clash:
            row.delete()
        else:
            row.child = target
            row.save(update_fields=['child'])


@transaction.atomic
def merge_child(source_id, target_id) -> bool:
    """
    Fold one record into another. Returns False, changing nothing, when the
    source turns out to hold money or a document after all.
    """
    from apps.customers.child_status import resolve_child_status
    from apps.customers.models import Child
    from apps.customers.status_history_models import ChildStatusHistory
    from apps.enrollments.models import ChildAbsence, Enrollment, LessonAttendance

    ids = sorted([source_id, target_id], key=str)
    locked = {c.pk: c for c in Child.objects.select_for_update().filter(pk__in=ids)}
    source, target = locked.get(source_id), locked.get(target_id)
    if source is None or target is None or source.pk == target.pk:
        return False
    if _has_blocking_refs(source, MOVABLE_CHILD_RELATIONS):
        return False
    if source.status != GHOST and source.created_at and (
        timezone.now() - source.created_at < REGISTRATION_SETTLE
    ):
        # A registration saved moments ago may be mid-checkout: the widget
        # creates the child before it charges the card. Folding it away now
        # would pull the record out from under the payment about to be written.
        # A day later it has either paid — and holds money, so it stays — or
        # was abandoned, and folds on that run.
        return False
    if source.status == GHOST and LessonAttendance.objects.filter(
        child=source, occurrence_date=date.today(),
    ).exists():
        # Added on today's register. The instructor may still be tapping that
        # row, and a row that vanishes under their thumb is a failed tap. It
        # folds on the next run, once the day is over.
        return False

    _move_lesson_enrollments(source, target)
    _move_attendance(source, target)
    _move_simple(source, target, ChildAbsence, ('lesson_id', 'occurrence_date'))
    _move_simple(source, target, Enrollment, ('course_id',))
    _move_simple(source, target, ChildStatusHistory, ())

    if source.trial_classes_attended:
        Child.objects.filter(pk=target.pk).update(
            trial_classes_attended=target.trial_classes_attended + source.trial_classes_attended,
        )

    family = source.family
    source.delete()

    if (
        family is not None
        and family.pk != target.family_id
        and not family.children.exists()
        and not _family_is_referenced(family)
    ):
        family.delete()

    target.refresh_from_db()
    if target.status != GHOST:
        resolved = resolve_child_status(target)
        if resolved != target.status:
            target.status = resolved
            target.save(update_fields=['status', 'updated_at'])
    return True


def _family_is_referenced(family) -> bool:
    """A family whose parents or own rows are named by money or a document stays."""
    if _has_blocking_refs(family, frozenset({'parents', 'children'})):
        return True
    return any(
        _has_blocking_refs(parent, frozenset())
        for parent in family.parents.all()
    )


def resolve_duplicates(*, dry_run: bool = False, queryset=None) -> dict:
    """
    Plan over `queryset` (every child by default) and, unless dry_run, carry it out.

    Returns counts and the steps, so the cron and the command report the same
    thing a dry run shows.
    """
    plan = plan_merges(load_rows(queryset))
    done = 0
    refused = 0
    if not dry_run:
        # Folding A into B and then B into C is fine; folding into a record
        # already folded away is not. Follow each target to where it ended.
        moved_to: dict = {}

        def final(pk):
            while pk in moved_to:
                pk = moved_to[pk]
            return pk

        for step in plan.steps:
            target = final(step.target_id)
            token = merging.set(True)
            try:
                ok = merge_child(step.source_id, target)
            except Exception:  # never let one bad pair stop the rest
                logger.exception('merge %s -> %s failed', step.source_id, target)
                ok = False
            finally:
                merging.reset(token)
            if ok:
                moved_to[step.source_id] = target
                done += 1
            else:
                refused += 1

    return {
        'dry_run': dry_run,
        'planned': len(plan.steps),
        'merged': done,
        'refused': refused,
        'skipped': len(plan.skipped),
        'ghosts_planned': sum(1 for s in plan.steps if 'רפאים' in s.reason),
        'steps': [
            {'source': str(s.source_id), 'target': str(s.target_id), 'reason': s.reason}
            for s in plan.steps
        ],
        'skipped_detail': [
            {'children': [str(i) for i in ids], 'reason': why} for ids, why in plan.skipped
        ],
    }


def resolve_around(child) -> dict:
    """
    Resolve only the records that could be this child — for the moment a real
    child is saved, so a ghost it replaces goes at once rather than overnight.

    Narrowed by surname or by the last seven digits of the phone, which covers
    every rule `plan_merges` applies while reading a handful of rows instead of
    every child in the system.
    """
    from django.db.models import Q

    from apps.customers.models import Child

    last = (child.last_name or '').strip()
    first = (child.first_name or '').strip()
    digits = normalise_phone(
        (child.phone_number or '').strip() or getattr(getattr(child, 'family', None), 'phone', '')
    )
    scope = Q(pk=child.pk)
    if last:
        scope |= Q(last_name__iexact=last)
    if first:
        scope |= Q(first_name__iexact=first)
    if len(digits) >= 7:
        tail = digits[-7:]
        scope |= Q(phone_number__contains=tail) | Q(family__phone__contains=tail)
    return resolve_duplicates(queryset=Child.objects.filter(scope))
