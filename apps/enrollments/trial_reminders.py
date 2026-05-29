"""
Trial-reminder scheduling for children in trial_signed status.

  • test-lesson-10am — 10:00 Israel time on the trial lesson date (cron)
  • after-test — 2 hours after the trial lesson ends (cron, configurable)

Instant signup uses test-lesson-register (see LessonEnrollmentViewSet.create).

Invoke cron every 30–60 minutes:
  POST /api/v1/enrollments/cron/trial-reminders/?token=CRON_TOKEN
  python manage.py send_trial_reminders
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment

logger = logging.getLogger(__name__)


def lesson_weekday_to_python(day_of_week: int) -> int:
    return (day_of_week - 1) % 7


def next_lesson_occurrence(
    lesson_day_of_week: int,
    lesson_end_time: time,
    *,
    now: Optional[datetime] = None,
) -> date:
    now = now or timezone.localtime()
    today = now.date()
    target_py_weekday = lesson_weekday_to_python(lesson_day_of_week)

    today_matches = today.weekday() == target_py_weekday
    not_yet_ended = now.time() < lesson_end_time
    if today_matches and not_yet_ended:
        return today

    days_ahead = (target_py_weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def compute_trial_lesson_date(lesson: Lesson, *, now: Optional[datetime] = None) -> date:
    return next_lesson_occurrence(lesson.day_of_week, lesson.end_time, now=now)


def _trial_day_10am_send_at(trial_date: date) -> datetime:
    """10:00 (configurable) on the calendar day of the trial lesson."""
    hour = int(getattr(settings, 'TRIAL_10AM_REMINDER_HOUR', 10) or 10)
    naive = datetime.combine(trial_date, time(hour=hour, minute=0))
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _after_test_send_at(trial_date: date, lesson_end_time: time) -> datetime:
    """Configurable hours after trial lesson end (after-test automation)."""
    hours = int(getattr(settings, 'TRIAL_AFTER_TEST_HOURS', 2) or 2)
    naive = datetime.combine(trial_date, lesson_end_time) + timedelta(hours=hours)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _build_send_kwargs(enrollment: LessonEnrollment) -> Optional[dict]:
    from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context

    lesson = enrollment.lesson
    child = enrollment.child
    if not (lesson and child):
        return None

    ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
    if not ctx:
        return None

    if enrollment.trial_lesson_date:
        ctx['trial_date'] = enrollment.trial_lesson_date.strftime('%d/%m/%Y')

    return ctx


def _send_trial_whatsapp(svc, kind: str, ctx: dict, *, dry_run: bool, enrollment_id) -> tuple[bool, dict]:
    if dry_run:
        logger.info("DRY-RUN %s reminder for enrollment %s", kind, enrollment_id)
        return True, {'sent': True, 'dry_run': True}

    lookup_names = ctx.pop('lookup_names', None)
    trial_date = ctx.pop('trial_date', '')
    result = svc.notify_registration(
        kind=kind,
        lookup_names=lookup_names,
        trial_date=trial_date,
        **ctx,
    )
    return bool(result.get('sent')), result


def send_due_trial_reminders(*, dry_run: bool = False) -> dict:
    from apps.core.manychat_service import ManyChatService

    now = timezone.localtime()
    qs = (
        LessonEnrollment.objects
        .select_related(
            'lesson', 'lesson__course', 'lesson__branch',
            'child', 'child__family',
        )
        .prefetch_related('child__family__parents')
        .filter(trial_lesson_date__isnull=False)
        .filter(child__status='trial_signed')
    )

    svc = ManyChatService()
    summary = {'ten_am_sent': 0, 'after_test_sent': 0, 'skipped': 0, 'errors': 0}

    for enr in qs:
        lesson = enr.lesson
        if not lesson or not enr.trial_lesson_date:
            summary['skipped'] += 1
            continue

        ten_am_due = _trial_day_10am_send_at(enr.trial_lesson_date)
        after_test_due = _after_test_send_at(enr.trial_lesson_date, lesson.end_time)

        if not enr.trial_10am_reminder_sent_at and now >= ten_am_due:
            ctx = _build_send_kwargs(enr)
            if not ctx:
                summary['skipped'] += 1
            else:
                sent, result = _send_trial_whatsapp(
                    svc,
                    ManyChatService.REGISTRATION_KIND_TRIAL_10AM,
                    ctx,
                    dry_run=dry_run,
                    enrollment_id=enr.id,
                )
                if sent and not dry_run:
                    enr.trial_10am_reminder_sent_at = timezone.now()
                    enr.save(update_fields=['trial_10am_reminder_sent_at', 'updated_at'])
                    summary['ten_am_sent'] += 1
                elif sent:
                    summary['ten_am_sent'] += 1
                else:
                    logger.warning("10am trial reminder NOT sent for %s: %s", enr.id, result)
                    summary['errors'] += 1

        if not enr.trial_followup_reminder_sent_at and now >= after_test_due:
            ctx = _build_send_kwargs(enr)
            if not ctx:
                summary['skipped'] += 1
                continue
            sent, result = _send_trial_whatsapp(
                svc,
                ManyChatService.REGISTRATION_KIND_TRIAL_AFTER_TEST,
                ctx,
                dry_run=dry_run,
                enrollment_id=enr.id,
            )
            if sent and not dry_run:
                enr.trial_followup_reminder_sent_at = timezone.now()
                enr.save(update_fields=['trial_followup_reminder_sent_at', 'updated_at'])
                summary['after_test_sent'] += 1
            elif sent:
                summary['after_test_sent'] += 1
            else:
                logger.warning("after-test WhatsApp NOT sent for %s: %s", enr.id, result)
                summary['errors'] += 1

    return summary
