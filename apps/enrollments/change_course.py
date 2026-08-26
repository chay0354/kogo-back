"""Move a child from one course-unit to another without changing billed amounts.

A twice/thrice-a-week חוג is one unit: every member day is replaced together.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.courses.models import Lesson, LessonBundle
from apps.customers.models import Payment
from apps.enrollments.enrollment_counts import count_capacity_enrollments
from apps.enrollments.models import LessonEnrollment


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


def replace_course_unit(*, enrollment: LessonEnrollment, new_course) -> dict:
    """Swap every slot of the current חוג for every lesson of the target חוג.

    Standing-order amounts are left as-is; only lesson/bundle pointers move.
    """
    target_lessons = course_unit_lessons(new_course)
    if not target_lessons:
        raise ValueError('לא נמצאו שיעורים בחוג שנבחר')

    old_rows = sibling_unit_enrollments(enrollment)
    old_course_id = enrollment.lesson.course_id
    if str(new_course.id) == str(old_course_id):
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

    target_bundle = matching_bundle(new_course, target_lessons)
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

        Payment.objects.filter(child=child, lesson_id__in=old_lesson_ids).update(
            lesson=kept[0].lesson,
            bundle=target_bundle,
        )

    return {
        'unchanged': False,
        'kept': kept,
        'removed_ids': removed_ids,
    }
