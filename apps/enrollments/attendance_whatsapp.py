"""
WhatsApp when a child is not marked present 3 lesson occurrences in a row.

Triggered from scheduling mark_attendance after the manager saves attendance.
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.enrollments.models import LessonAttendance, LessonEnrollment

logger = logging.getLogger(__name__)

NON_PRESENT_STATUSES = frozenset({'absent', 'not_marked'})


def consecutive_non_present_count(*, child_id, lesson_id) -> int:
    """
    Count the current streak of non-present marks for this child on this lesson,
    starting from the most recent occurrence_date.
    """
    records = (
        LessonAttendance.objects
        .filter(
            lesson_id=lesson_id,
            child_id=child_id,
            occurrence_date__isnull=False,
        )
        .order_by('-occurrence_date')
    )
    streak = 0
    for rec in records:
        if rec.status == 'present':
            break
        if rec.status in NON_PRESENT_STATUSES:
            streak += 1
    return streak


def _threshold() -> int:
    return int(getattr(settings, 'CONSECUTIVE_ABSENCE_WHATSAPP_THRESHOLD', 3) or 3)


def maybe_send_didnt_arrive_whatsapp(*, child, lesson, attendance_status: str) -> dict | None:
    """
    After attendance is saved:
      • present → clear send flag so a future 3-streak can notify again
      • absent / not_marked → if streak >= threshold and not yet sent, fire didnt_arrive
    """
    enrollment = (
        LessonEnrollment.objects
        .filter(lesson=lesson, child=child, status='active')
        .first()
    )
    if not enrollment:
        return None

    if attendance_status == 'present':
        if enrollment.didnt_arrive_whatsapp_sent_at:
            enrollment.didnt_arrive_whatsapp_sent_at = None
            enrollment.save(update_fields=['didnt_arrive_whatsapp_sent_at', 'updated_at'])
        return None

    if attendance_status not in NON_PRESENT_STATUSES:
        return None

    streak = consecutive_non_present_count(child_id=child.id, lesson_id=lesson.id)
    threshold = _threshold()
    if streak < threshold:
        return None

    if enrollment.didnt_arrive_whatsapp_sent_at:
        logger.info(
            'didnt_arrive already sent for enrollment %s (streak=%s)',
            enrollment.id,
            streak,
        )
        return {'sent': False, 'reason': 'already_sent_for_streak'}

    from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
    from apps.core.manychat_service import ManyChatService

    lesson = (
        type(lesson).objects
        .select_related('course', 'course__branch')
        .filter(pk=lesson.pk)
        .first()
    ) or lesson

    ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
    if not ctx:
        logger.info('Skipping didnt_arrive WhatsApp: no parent phone for child %s', child.id)
        return {'sent': False, 'reason': 'no_parent_phone'}

    lookup_names = ctx.pop('lookup_names', None)
    result = ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_DIDNT_ARRIVE,
        lookup_names=lookup_names,
        **ctx,
    )

    if result.get('sent'):
        from django.utils import timezone

        enrollment.didnt_arrive_whatsapp_sent_at = timezone.now()
        enrollment.save(update_fields=['didnt_arrive_whatsapp_sent_at', 'updated_at'])
        logger.info(
            'didnt_arrive WhatsApp sent for child %s lesson %s (streak=%s)',
            child.id,
            lesson.id,
            streak,
        )
    else:
        logger.warning(
            'didnt_arrive WhatsApp NOT sent for child %s: %s',
            child.id,
            result.get('reason'),
        )

    return result
