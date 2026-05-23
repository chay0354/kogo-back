"""
Trial-reminder scheduling — finds LessonEnrollment rows that are still in
trial state (child.status == 'trial_signed') and fires WhatsApp reminders:
  • same evening as trial registration (default 19:00 / 7pm Israel time)
  • 72 hours after the trial lesson ends

Idempotent: each reminder type is gated by a `*_sent_at` timestamp on the
enrollment. Invoke via cron every 30–60 minutes:
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


# ── Date helpers ────────────────────────────────────────────────────────────

def lesson_weekday_to_python(day_of_week: int) -> int:
    """
    Lesson.day_of_week uses 0=Sunday … 6=Saturday (Israeli convention).
    Python's date.weekday() uses 0=Monday … 6=Sunday.
    """
    return (day_of_week - 1) % 7


def next_lesson_occurrence(
    lesson_day_of_week: int,
    lesson_end_time: time,
    *,
    now: Optional[datetime] = None,
) -> date:
    """
    Return the date of the next (or today's) occurrence of a weekly lesson.
    If today matches the lesson day AND the lesson hasn't ended yet, today wins;
    otherwise the next matching weekday.
    """
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
    """Public convenience wrapper used when creating a trial enrollment."""
    return next_lesson_occurrence(lesson.day_of_week, lesson.end_time, now=now)


# ── Reminder scheduling ─────────────────────────────────────────────────────

def _evening_send_at(enrollment: LessonEnrollment) -> datetime:
    """
    7pm (configurable) on the same calendar day the parent registered for trial.
    If they registered after that hour, send on the next cron run (same night).
    """
    hour = int(getattr(settings, 'TRIAL_EVENING_REMINDER_HOUR', 19) or 19)
    reg_local = timezone.localtime(enrollment.enrolled_at or enrollment.created_at)
    reg_date = reg_local.date()
    tz = timezone.get_current_timezone()
    due = timezone.make_aware(datetime.combine(reg_date, time(hour=hour, minute=0)), tz)
    if reg_local >= due:
        return reg_local
    return due


def _followup_send_at(trial_date: date, lesson_end_time: time) -> datetime:
    """72 hours after the trial lesson ended."""
    naive = datetime.combine(trial_date, lesson_end_time) + timedelta(hours=72)
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

    return ctx


def send_due_trial_reminders(*, dry_run: bool = False) -> dict:
    """
    Scan trial enrollments and fire any reminders whose scheduled time has passed.
    Skips enrollments where the child has already become 'active' (subscribed).
    """
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
    summary = {'evening_sent': 0, 'followup_sent': 0, 'skipped': 0, 'errors': 0}

    for enr in qs:
        lesson = enr.lesson
        if not lesson:
            summary['skipped'] += 1
            continue

        evening_due = _evening_send_at(enr)
        followup_due = _followup_send_at(enr.trial_lesson_date, lesson.end_time)

        # Same evening after trial registration (7pm Israel by default)
        if not enr.trial_evening_reminder_sent_at and now >= evening_due:
            ctx = _build_send_kwargs(enr)
            if not ctx:
                summary['skipped'] += 1
            elif dry_run:
                logger.info("DRY-RUN evening reminder for enrollment %s", enr.id)
            else:
                lookup_names = ctx.pop('lookup_names', None)
                result = svc.notify_registration(
                    kind=ManyChatService.REGISTRATION_KIND_TRIAL_REMINDER,
                    lookup_names=lookup_names,
                    **ctx,
                )
                if result.get('sent'):
                    enr.trial_evening_reminder_sent_at = timezone.now()
                    enr.save(update_fields=['trial_evening_reminder_sent_at', 'updated_at'])
                    summary['evening_sent'] += 1
                else:
                    logger.warning("Evening reminder NOT sent for %s: %s", enr.id, result)
                    summary['errors'] += 1

        # 72h after the trial lesson (same reminder template)
        if not enr.trial_followup_reminder_sent_at and now >= followup_due:
            ctx = _build_send_kwargs(enr)
            if not ctx:
                summary['skipped'] += 1
                continue
            if dry_run:
                logger.info("DRY-RUN followup reminder for enrollment %s", enr.id)
                continue
            lookup_names = ctx.pop('lookup_names', None)
            result = svc.notify_registration(
                kind=ManyChatService.REGISTRATION_KIND_TRIAL_REMINDER,
                lookup_names=lookup_names,
                **ctx,
            )
            if result.get('sent'):
                enr.trial_followup_reminder_sent_at = timezone.now()
                enr.save(update_fields=['trial_followup_reminder_sent_at', 'updated_at'])
                summary['followup_sent'] += 1
            else:
                logger.warning("Follow-up reminder NOT sent for %s: %s", enr.id, result)
                summary['errors'] += 1

    return summary
