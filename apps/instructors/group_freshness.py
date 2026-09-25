"""
Keeping each group's stored month honest between the morning recounts.

The instructor page reads each group's month — students, money in, pay — from
a stored row, because counting a group again takes about half a second. The
morning recount refreshes every row; in between, a registration, a payment or
a child's new status left the stored row wrong until the next morning, and
refreshing the page in the browser did not help (owner, 25.9.2026: "למה רענון
נתונים הוא לא פשוט לרענן את הדף").

So whatever changes a group's numbers marks that group's row as out of date,
and the next read of the page counts that group again and stores the answer.
The mark is the row's `updated_at` set to STALE_MARK: older than any freshness
window, so every reader — the page, the morning recount — treats the row as not
done. Nothing else about the row changes, and only the running month (and any
month ahead) is marked; a closed month is the record.

What changes a group's numbers, and the hook that marks it:
  an enrolment added, changed or removed .......... the group
  a payment on the group .......................... the group
  a child's status (פעיל / בעיית תשלום / ...) ...... every group the child is in
  an occurrence cancelled or restored ............. the group
  the group itself (day, time, instructor, pay) ... every group of its course,
                                                    since a course paid by the
                                                    month splits it across them
  the course (price, monthly pay, active) ......... every group of the course
  the instructor's pay (model, fixed, tiers) ...... every group of the instructor

A write that goes around the model (`QuerySet.update`) sends no signal; the few
of those that change a child's status call mark_child_groups_stale themselves.
Anything still missed is put right by the next morning's recount.
"""
import logging
import threading
from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Older than any freshness window the readers use.
STALE_MARK = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)

# What this thread has been asked to mark and not yet marked. Written by the
# hooks, emptied by _flush once the change is committed. A change that is rolled
# back leaves its entries here for the next flush — a few groups recounted for
# nothing, which is harmless; a mark lost would not be.
_pending = threading.local()


def _queue(**targets) -> None:
    """
    Remember what to mark, and mark it once the change is committed.

    After the commit, not inside it: marking is one more statement, and inside a
    payment's transaction a failure of that statement would roll the payment
    back with it. Everything one transaction asks for is marked in a single
    UPDATE. Outside a transaction the commit is now, so this marks at once.
    """
    state = getattr(_pending, 'targets', None)
    if state is None:
        state = _pending.targets = {'lessons': set(), 'children': set(), 'courses': set(), 'instructors': set()}
    for kind, ids in targets.items():
        state[kind].update(i for i in ids if i)
    transaction.on_commit(_flush)


def _flush() -> int:
    """Mark everything queued, in one statement. Never raises."""
    from apps.core.models import LessonMonthlySnapshot
    from apps.enrollments.models import LessonEnrollment

    state = getattr(_pending, 'targets', None)
    _pending.targets = None
    if not state or not any(state.values()):
        return 0
    which = Q()
    if state['lessons']:
        which |= Q(lesson_id__in=state['lessons'])
    if state['courses']:
        which |= Q(lesson__course_id__in=state['courses'])
    if state['instructors']:
        which |= Q(lesson__instructor_id__in=state['instructors'])
    if state['children']:
        which |= Q(lesson_id__in=LessonEnrollment.objects.filter(
            child_id__in=state['children'],
        ).values('lesson_id'))
    try:
        return LessonMonthlySnapshot.objects.filter(
            which, month__gte=timezone.now().strftime('%Y-%m'),
        ).update(updated_at=STALE_MARK)
    except Exception:  # noqa: BLE001 — a count out of date is not worth an error page
        logger.exception('could not mark groups for a recount')
        return 0


def mark_groups_stale(lesson_ids) -> None:
    _queue(lessons=list(lesson_ids))


def mark_child_groups_stale(child_id) -> None:
    _queue(children=[child_id])


def mark_course_groups_stale(course_id) -> None:
    _queue(courses=[course_id])


def mark_instructor_groups_stale(instructor_id) -> None:
    _queue(instructors=[instructor_id])


# --- the hooks -----------------------------------------------------------------


def _enrollment_changed(sender, instance, **kwargs):
    mark_groups_stale([instance.lesson_id])


def _payment_changed(sender, instance, **kwargs):
    mark_groups_stale([getattr(instance, 'lesson_id', None)])


def _child_changed(sender, instance, created=False, update_fields=None, **kwargs):
    # A new child is in no group yet; a save that names its fields and leaves
    # out the status cannot have moved the child in or out of a count.
    if created or (update_fields is not None and 'status' not in update_fields):
        return
    mark_child_groups_stale(instance.pk)


def _cancellation_changed(sender, instance, **kwargs):
    mark_groups_stale([instance.lesson_id])


def _lesson_changed(sender, instance, **kwargs):
    mark_course_groups_stale(instance.course_id)


def _course_changed(sender, instance, **kwargs):
    mark_course_groups_stale(instance.pk)


def _instructor_changed(sender, instance, created=False, **kwargs):
    if not created:
        mark_instructor_groups_stale(instance.pk)


def _salary_tier_changed(sender, instance, **kwargs):
    mark_instructor_groups_stale(instance.instructor_id)


def connect() -> None:
    from django.db.models.signals import post_delete, post_save

    from apps.courses.models import Course, Lesson
    from apps.customers.models import Child, Payment
    from apps.enrollments.models import LessonEnrollment
    from apps.instructors.models import Instructor, InstructorSalaryTier
    from apps.scheduling.models import LessonCancellation

    hooks = (
        (LessonEnrollment, _enrollment_changed, True),
        (Payment, _payment_changed, True),
        (Child, _child_changed, False),
        (LessonCancellation, _cancellation_changed, True),
        (Lesson, _lesson_changed, True),
        (Course, _course_changed, False),
        (Instructor, _instructor_changed, False),
        (InstructorSalaryTier, _salary_tier_changed, True),
    )
    for model, receiver, on_delete in hooks:
        uid = f'group_freshness:{model.__name__}'
        post_save.connect(receiver, sender=model, dispatch_uid=f'{uid}:save')
        if on_delete:
            post_delete.connect(receiver, sender=model, dispatch_uid=f'{uid}:delete')
