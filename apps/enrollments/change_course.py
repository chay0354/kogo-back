"""Move a child from one course-unit to another without changing billed amounts.

A twice/thrice-a-week חוג is one unit: every member day is replaced together.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import Q

from apps.courses.models import Lesson, LessonBundle
from apps.customers.models import Payment, RecurringPayment
from apps.enrollments.enrollment_counts import count_capacity_enrollments
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
            row.save(update_fields=['status', 'start_date', 'bundle', 'end_date', 'updated_at'])
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
        row.start_date = trial_date
        row.trial_10am_reminder_sent_at = None
        row.trial_followup_reminder_sent_at = None
        row.trial_evening_reminder_sent_at = None
        row.save(update_fields=[
            'trial_lesson_date', 'start_date',
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
        if not still_active and child.status not in ('inactive', 'ghost'):
            child.status = 'inactive'
            child.save(update_fields=['status', 'updated_at'])

    return {
        'removed_ids': removed_ids,
        'cancelled_recurring_ids': cancelled_ids,
        'child_status': child.status,
    }
