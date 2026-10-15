"""Move a child from one course-unit to another without changing billed amounts.

A twice/thrice-a-week חוג is one unit: every member day is replaced together.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.customers.child_status import STATUS_GHOST, resolve_child_status
from django.db.models import Q

from apps.courses.models import Lesson, LessonBundle
from apps.customers.models import Payment, RecurringPayment
from apps.enrollments.enrollment_counts import TRIAL_CHILD_STATUSES, count_capacity_enrollments
from apps.enrollments.models import Enrollment, LessonEnrollment


def sibling_unit_enrollments(enrollment: LessonEnrollment) -> list[LessonEnrollment]:
    """Active regular slots that belong with this enrollment (same bundle or same course)."""
    child = enrollment.child
    qs = (
        LessonEnrollment.objects
        .filter(child=child, status='active', trial_lesson_date__isnull=True)
        .select_related('lesson', 'lesson__course', 'bundle')
        .order_by('lesson__day_of_week', 'lesson__start_time', 'id')
    )
    if enrollment.trial_lesson_date:
        return [enrollment]
    if enrollment.bundle_id:
        rows = list(qs.filter(bundle_id=enrollment.bundle_id))
        return rows or [enrollment]
    course_id = enrollment.lesson.course_id
    rows = list(qs.filter(lesson__course_id=course_id))
    return rows or [enrollment]


def course_unit_lessons(course) -> list[Lesson]:
    return list(
        course.lessons
        .exclude(status='cancelled')
        .select_related('course', 'room')
        .order_by('day_of_week', 'start_time', 'id')
    )


def matching_bundle(course, lessons: list[Lesson]) -> LessonBundle | None:
    lesson_ids = {lesson.id for lesson in lessons}
    if len(lesson_ids) < 2:
        return None
    for bundle in course.lesson_bundles.all().prefetch_related('lessons'):
        member_ids = {member.id for member in bundle.lessons.all()}
        if member_ids == lesson_ids:
            return bundle
    return None


def _lesson_has_room(lesson: Lesson, child_id) -> str | None:
    caps = []
    if lesson.capacity:
        caps.append(int(lesson.capacity))
    if lesson.course.capacity:
        caps.append(int(lesson.course.capacity))
    if lesson.room_id and lesson.room and lesson.room.capacity:
        caps.append(int(lesson.room.capacity))
    if not caps:
        return None
    already = LessonEnrollment.objects.filter(
        child_id=child_id, lesson=lesson, status='active',
    ).exists()
    if already:
        return None
    current = count_capacity_enrollments(lesson=lesson)
    capacity = min(caps)
    if current >= capacity:
        return f'השיעור מלא — קיבולת מקסימלית: {capacity} תלמידים'
    return None


def swap_day_targets(enrollment: LessonEnrollment, new_lesson: Lesson) -> tuple[list[Lesson], LessonBundle]:
    """The unit after one day of a twice/thrice-a-week track is replaced by `new_lesson`.

    Owner, 15.10.2026: one day of a family's track was cancelled, and they go to
    another class at the same branch instead — another group, another instructor —
    for the same money, without signing up again. `enrollment` is the day that
    goes; the other days stay as they are.

    The replacement keeps the track's bundle, so everything that reads a
    registration by its bundle — a later move, "remove from the class", the
    standing order's match, the unpaid check — still sees one registration at
    one price. Moving the child back is an ordinary move to the track.
    """
    if enrollment.trial_lesson_date:
        raise ValueError('שיעור ניסיון — אין בו ימים להחליף')
    rows = sibling_unit_enrollments(enrollment)
    bundle = enrollment.bundle
    if (
        bundle is None
        or len(rows) < 2
        or all(row.id != enrollment.id for row in rows)
        or any(row.bundle_id != bundle.id for row in rows)
    ):
        raise ValueError('החלפת יום אחד אפשרית רק ברישום פעיל למסלול של פעמיים או שלוש בשבוע')
    if new_lesson.status == 'cancelled':
        raise ValueError('השיעור שנבחר מבוטל')
    if any(row.lesson_id == new_lesson.id for row in rows):
        raise ValueError('הילד כבר רשום לשיעור הזה במסלול')
    if new_lesson.course.branch_id != enrollment.lesson.course.branch_id:
        raise ValueError('אפשר להחליף יום רק לשיעור באותו סניף')
    # A row of the child's own on that lesson that is live — a class paid for
    # apart, or a trial still ahead — must not be swallowed into the track.
    live = LessonEnrollment.objects.filter(
        child=enrollment.child, lesson=new_lesson, status__in=('active', 'payments_problem'),
    ).first()
    if live is not None:
        if live.trial_lesson_date:
            raise ValueError('לילד יש שיעור ניסיון בשיעור הזה — יש לבטל אותו קודם')
        raise ValueError('הילד כבר רשום לשיעור הזה')
    kept = [row.lesson for row in rows if row.id != enrollment.id]
    # The days that stay come first: the move points the payments at the first
    # lesson of the unit, and that must remain the track's own lesson.
    return kept + [new_lesson], bundle


def swap_note(*, old_lesson: Lesson, new_lesson: Lesson, who: str = '', reason: str = '', today: date | None = None) -> str:
    """'15.10.2026 · יום מוחלף: רביעי 16:00 (…) ← חמישי 17:00 (…) · המחיר לא השתנה · …'."""
    def label(lesson: Lesson) -> str:
        when = f"{lesson.get_day_of_week_display()} {lesson.start_time.strftime('%H:%M') if lesson.start_time else ''}".strip()
        about = ' · '.join(part for part in (
            lesson.course.name,
            lesson.instructor.full_name if lesson.instructor_id else '',
        ) if part)
        return f'{when} ({about})' if about else when

    day = today or date.today()
    parts = [
        day.strftime('%d.%m.%Y'),
        f'יום מוחלף: {label(old_lesson)} ← {label(new_lesson)}',
        'המחיר לא השתנה',
    ]
    if who:
        parts.append(who)
    if reason:
        parts.append(f'סיבה: {reason}')
    return ' · '.join(parts)


def _same_unit(old_rows: list[LessonEnrollment], target_lessons: list[Lesson], target_bundle: LessonBundle | None) -> bool:
    old_ids = {row.lesson_id for row in old_rows}
    new_ids = {lesson.id for lesson in target_lessons}
    old_bundle_id = next((row.bundle_id for row in old_rows if row.bundle_id), None)
    new_bundle_id = target_bundle.id if target_bundle else None
    return old_ids == new_ids and old_bundle_id == new_bundle_id


def replace_unit(
    *,
    enrollment: LessonEnrollment,
    target_lessons: list[Lesson],
    target_bundle: LessonBundle | None = None,
) -> dict:
    """Swap the current unit for the given lessons (one day or a bundle).

    Standing-order amounts are left as-is; only lesson/bundle pointers move.
    """
    if not target_lessons:
        raise ValueError('לא נמצאו שיעורים בחוג שנבחר')

    old_rows = sibling_unit_enrollments(enrollment)
    if _same_unit(old_rows, target_lessons, target_bundle):
        return {
            'unchanged': True,
            'kept': old_rows,
            'removed_ids': [],
        }

    child = enrollment.child
    for lesson in target_lessons:
        error = _lesson_has_room(lesson, child.id)
        if error:
            raise ValueError(error)

    old_lesson_ids = [row.lesson_id for row in old_rows]
    today = date.today()

    with transaction.atomic():
        target_ids = [lesson.id for lesson in target_lessons]
        existing = {
            row.lesson_id: row
            for row in LessonEnrollment.objects.filter(child=child, lesson_id__in=target_ids)
        }
        kept: list[LessonEnrollment] = []
        claimed: set = set()

        for lesson in target_lessons:
            row = existing.get(lesson.id)
            if row is None:
                continue
            row.status = 'active'
            if not row.start_date:
                row.start_date = today
            row.bundle = target_bundle
            row.end_date = None
            fields = ['status', 'start_date', 'bundle', 'end_date', 'updated_at']
            if row.trial_lesson_date and not enrollment.trial_lesson_date and row.id not in {old.id for old in old_rows}:
                # An old trial row on the target lesson is taken as the regular
                # place. Left with its trial date it reads as a trial — off the
                # register after that date, outside the unit — the same thing
                # enroll_child_in_paid_lessons clears when a trial turns paid.
                from apps.core.payment_service import _record_trial_outcome_before_conversion

                _record_trial_outcome_before_conversion(row, today)
                row.trial_lesson_date = None
                fields += ['trial_lesson_date', 'trial_outcome']
            row.save(update_fields=fields)
            kept.append(row)
            claimed.add(row.id)

        reusable = [row for row in old_rows if row.id not in claimed]
        for lesson in target_lessons:
            if lesson.id in existing:
                continue
            if reusable:
                row = reusable.pop(0)
                row.lesson = lesson
                row.status = 'active'
                if not row.start_date:
                    row.start_date = today
                row.bundle = target_bundle
                row.end_date = None
                row.save(update_fields=['lesson', 'status', 'start_date', 'bundle', 'end_date', 'updated_at'])
                kept.append(row)
                claimed.add(row.id)
                continue
            kept.append(
                LessonEnrollment.objects.create(
                    child=child,
                    lesson=lesson,
                    status='active',
                    start_date=today,
                    bundle=target_bundle,
                )
            )

        order = {lesson.id: index for index, lesson in enumerate(target_lessons)}
        kept.sort(key=lambda row: order.get(row.lesson_id, 99))

        removed_ids = []
        for row in reusable:
            row.status = 'inactive'
            row.bundle = None
            row.end_date = today
            row.save(update_fields=['status', 'bundle', 'end_date', 'updated_at'])
            removed_ids.append(row.id)

        if not enrollment.trial_lesson_date:
            Payment.objects.filter(child=child, lesson_id__in=old_lesson_ids).update(
                lesson=kept[0].lesson,
                bundle=target_bundle,
            )

    return {
        'unchanged': False,
        'kept': kept,
        'removed_ids': removed_ids,
    }


def move_trial_enrollment(*, enrollment: LessonEnrollment, new_lesson: Lesson, trial_date=None) -> dict:
    """Move a trial signup to another single lesson and keep it a trial."""
    from apps.enrollments.trial_reminders import compute_trial_lesson_date, validate_trial_lesson_date

    if not enrollment.trial_lesson_date:
        raise ValueError('זה אינו שיעור ניסיון')
    if isinstance(trial_date, str):
        trial_date = trial_date.strip()
        if trial_date:
            try:
                trial_date = date.fromisoformat(trial_date)
            except ValueError:
                raise ValueError('תאריך שיעור הניסיון אינו זמין')
        else:
            trial_date = None

    old_date = enrollment.trial_lesson_date
    old_lesson_id = enrollment.lesson_id

    if str(new_lesson.id) != str(enrollment.lesson_id):
        result = replace_unit(
            enrollment=enrollment,
            target_lessons=[new_lesson],
            target_bundle=None,
        )
    else:
        result = {
            'unchanged': True,
            'kept': [enrollment],
            'removed_ids': [],
        }

    row = result['kept'][0]
    if trial_date is None:
        trial_date = old_date if str(new_lesson.id) == str(old_lesson_id) else compute_trial_lesson_date(new_lesson)
    validate_trial_lesson_date(new_lesson, trial_date)

    lesson_changed = str(old_lesson_id) != str(new_lesson.id)
    date_changed = old_date != trial_date
    if lesson_changed or date_changed:
        row.trial_lesson_date = trial_date
        row.trial_outcome = ''
        row.start_date = trial_date
        row.trial_10am_reminder_sent_at = None
        row.trial_followup_reminder_sent_at = None
        row.trial_evening_reminder_sent_at = None
        row.save(update_fields=[
            'trial_lesson_date', 'trial_outcome', 'start_date',
            'trial_10am_reminder_sent_at', 'trial_followup_reminder_sent_at',
            'trial_evening_reminder_sent_at', 'updated_at',
        ])
        Payment.objects.filter(
            child=row.child,
            trial_lesson_date=old_date,
            lesson_id__in=[old_lesson_id, new_lesson.id],
        ).update(trial_lesson_date=trial_date, lesson=new_lesson)

    result['unchanged'] = not lesson_changed and not date_changed
    result['kept'] = [row]
    return result


def replace_course_unit(*, enrollment: LessonEnrollment, new_course) -> dict:
    """Swap every slot of the current חוג for every lesson of the target חוג."""
    old_rows = sibling_unit_enrollments(enrollment)
    if str(new_course.id) == str(enrollment.lesson.course_id):
        return {
            'unchanged': True,
            'kept': old_rows,
            'removed_ids': [],
        }
    target_lessons = course_unit_lessons(new_course)
    target_bundle = matching_bundle(new_course, target_lessons)
    return replace_unit(
        enrollment=enrollment,
        target_lessons=target_lessons,
        target_bundle=target_bundle,
    )


def recurring_payments_for_unit(child, enrollments: list[LessonEnrollment]):
    """Active standing orders billed for this חוג (lesson, bundle, or course)."""
    regular = [row for row in enrollments if not row.trial_lesson_date]
    if not regular:
        return RecurringPayment.objects.none()
    lesson_ids = [row.lesson_id for row in regular]
    bundle_ids = [row.bundle_id for row in regular if row.bundle_id]
    course_ids = [row.lesson.course_id for row in regular]
    query = Q(initial_payment__lesson_id__in=lesson_ids)
    query |= Q(initial_payment__lesson__course_id__in=course_ids)
    if bundle_ids:
        query |= Q(initial_payment__bundle_id__in=bundle_ids)
    return (
        RecurringPayment.objects
        .filter(child=child, status='active')
        .filter(query)
        .distinct()
    )


def drop_course_unit(*, enrollment: LessonEnrollment, cancellation_reason: str = '') -> dict:
    """Remove the child from this חוג and stop matching standing orders."""
    from apps.core.payment_service import PaymentService

    rows = sibling_unit_enrollments(enrollment)
    child = enrollment.child
    course_ids = {row.lesson.course_id for row in rows}
    today = date.today()
    reason = cancellation_reason or 'הוסר מהחוג'

    stos = list(recurring_payments_for_unit(child, rows))
    dropping_regular = any(not row.trial_lesson_date for row in rows)
    remaining_regular = (
        LessonEnrollment.objects
        .filter(child=child, status__in=['active', 'payments_problem'], trial_lesson_date__isnull=True)
        .exclude(id__in=[row.id for row in rows])
        .exists()
    )
    if dropping_regular and not remaining_regular:
        extra = RecurringPayment.objects.filter(child=child, status='active').exclude(
            id__in=[sto.id for sto in stos]
        )
        stos.extend(list(extra))

    cancelled_ids = []
    payment_service = PaymentService()
    for sto in stos:
        result = payment_service.cancel_subscription(
            recurring_payment_id=str(sto.id),
            cancellation_reason=reason,
            # Worked out below, once the rows are gone too.
            recheck_status=False,
        )
        if result.get('success'):
            cancelled_ids.append(str(sto.id))

    removed_ids = []
    with transaction.atomic():
        for row in rows:
            row.status = 'inactive'
            row.end_date = today
            row.bundle = None
            row.save(update_fields=['status', 'end_date', 'bundle', 'updated_at'])
            removed_ids.append(row.id)
        if course_ids:
            still_on_course = set(
                LessonEnrollment.objects
                .filter(
                    child=child,
                    status__in=['active', 'payments_problem'],
                    lesson__course_id__in=course_ids,
                )
                .exclude(id__in=removed_ids)
                .values_list('lesson__course_id', flat=True)
            )
            drop_course_ids = course_ids - still_on_course
            if drop_course_ids:
                Enrollment.objects.filter(
                    child=child, course_id__in=drop_course_ids, is_active=True,
                ).update(is_active=False)

        still_active = LessonEnrollment.objects.filter(
            child=child, status__in=['active', 'payments_problem'],
        ).exclude(id__in=removed_ids).exists()
        # Losing the last active enrolment is not a status of its own — there
        # is no "לא פעיל" any more. What the child is now comes from what is
        # recorded about them: money in, a trial ahead, a trial behind, or
        # nothing yet.
        #
        # A cancelled trial is worked out again even when other rows remain:
        # a child on נרשם לניסיון whose trial was the thing just cancelled is
        # not on one any more, and leaving the status alone kept seven of them
        # there. Only a trial status is re-read that way — the drop is about
        # the trial, and it must not reopen a student's פעיל.
        dropped_trial = any(row.trial_lesson_date for row in rows)
        recheck = not still_active or (dropped_trial and child.status in TRIAL_CHILD_STATUSES)
        if recheck and child.status != STATUS_GHOST:
            resolved = resolve_child_status(child)
            if resolved != child.status:
                child.status = resolved
                child.save(update_fields=['status', 'updated_at'])

    return {
        'removed_ids': removed_ids,
        'cancelled_recurring_ids': cancelled_ids,
        'child_status': child.status,
    }
