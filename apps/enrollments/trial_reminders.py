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

# Nearest N lesson dates offered in the registration widget for trial signup.
TRIAL_LESSON_OCCURRENCE_LIMIT = 3

# Rosh Hashanah / Yom Kippur 2026 — not offered as trial lesson dates.
DEFAULT_BLOCKED_TRIAL_LESSON_DATES = '2026-09-13,2026-09-20,2026-09-21'


def blocked_trial_lesson_dates() -> frozenset[date]:
    """Calendar dates that must not appear as trial-lesson options."""
    raw = getattr(settings, 'BLOCKED_TRIAL_LESSON_DATES', None)
    if raw is None:
        raw = DEFAULT_BLOCKED_TRIAL_LESSON_DATES
    if isinstance(raw, str) and not raw.strip():
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(item).strip() for item in raw]
    else:
        parts = [p.strip() for p in str(raw).split(',')]
    dates: set[date] = set()
    for part in parts:
        if not part:
            continue
        dates.add(date.fromisoformat(part[:10]))
    return frozenset(dates)


def trial_lesson_min_date(*, now: Optional[datetime] = None) -> date:
    """
    Earliest calendar date offered in the trial-lesson date picker.

    While SUBSCRIPTION_FIRST_CHARGE_DATE is still in the future, trial signups
    cannot be scheduled before that date (aligned with deferred monthly billing).
    """
    now = now or timezone.localtime()
    today = now.date()
    from apps.core.payment_service import deferred_first_charge_date

    deferred = deferred_first_charge_date(today)
    return deferred if deferred else today


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


def iter_upcoming_lesson_occurrences(
    lesson: Lesson,
    *,
    count: int = 8,
    now: Optional[datetime] = None,
) -> list[date]:
    """Next N calendar dates when this lesson occurs (excluding cancellations)."""
    from apps.scheduling.models import LessonCancellation

    now = now or timezone.localtime()
    count = max(1, min(int(count or 8), 16))
    min_date = trial_lesson_min_date(now=now)

    if not lesson.is_recurring:
        if lesson.lesson_date and lesson.lesson_date >= min_date:
            if lesson.lesson_date in blocked_trial_lesson_dates():
                return []
            if not LessonCancellation.objects.filter(lesson=lesson, occurrence_date=lesson.lesson_date).exists():
                return [lesson.lesson_date]
        return []

    cancelled = set(
        LessonCancellation.objects.filter(lesson=lesson).values_list('occurrence_date', flat=True)
    )
    blocked = blocked_trial_lesson_dates()

    results: list[date] = []
    cursor_now = now
    while len(results) < count:
        candidate = next_lesson_occurrence(lesson.day_of_week, lesson.end_time, now=cursor_now)
        if candidate < min_date:
            cursor_now = timezone.make_aware(
                datetime.combine(min_date, time.min),
                timezone.get_current_timezone(),
            )
            continue
        if candidate not in cancelled and candidate not in blocked and candidate not in results:
            results.append(candidate)

        next_week = candidate + timedelta(days=7)
        cursor_now = timezone.make_aware(
            datetime.combine(next_week, time.min),
            timezone.get_current_timezone(),
        )
        if next_week > now.date() + timedelta(days=366):
            break

    return results


def iter_merged_upcoming_lesson_occurrences(
    lessons: list[Lesson],
    *,
    count: int = TRIAL_LESSON_OCCURRENCE_LIMIT,
    now: Optional[datetime] = None,
) -> list[tuple[Lesson, date]]:
    """Earliest N lesson occurrences across one or more weekly lessons (e.g. bundle)."""
    count = max(1, min(int(count or TRIAL_LESSON_OCCURRENCE_LIMIT), TRIAL_LESSON_OCCURRENCE_LIMIT))
    candidates: list[tuple[Lesson, date]] = []
    for lesson in lessons:
        for occurrence in iter_upcoming_lesson_occurrences(
            lesson, count=TRIAL_LESSON_OCCURRENCE_LIMIT, now=now,
        ):
            candidates.append((lesson, occurrence))
    candidates.sort(key=lambda item: item[1])
    seen: set[tuple[str, date]] = set()
    merged: list[tuple[Lesson, date]] = []
    for lesson, occurrence in candidates:
        key = (str(lesson.id), occurrence)
        if key in seen:
            continue
        seen.add(key)
        merged.append((lesson, occurrence))
        if len(merged) >= count:
            break
    return merged


def validate_trial_lesson_date(lesson: Lesson, trial_date: date, *, now: Optional[datetime] = None) -> None:
    if trial_date in blocked_trial_lesson_dates():
        raise ValueError('תאריך שיעור הניסיון אינו זמין')
    allowed = iter_upcoming_lesson_occurrences(
        lesson, count=TRIAL_LESSON_OCCURRENCE_LIMIT, now=now,
    )
    if trial_date not in allowed:
        raise ValueError('תאריך שיעור הניסיון אינו זמין')


def next_trial_date_after(lesson: Lesson, after: date, *, now: Optional[datetime] = None) -> Optional[date]:
    """Next offered trial date for this lesson strictly after `after`."""
    for occurrence in iter_upcoming_lesson_occurrences(lesson, count=16, now=now):
        if occurrence > after:
            return occurrence
    return None


def _parent_contact_for_child(child) -> tuple[str, str, str]:
    family = getattr(child, 'family', None)
    if not family:
        return '', '', ''
    parents = list(family.parents.all())
    primary = next((p for p in parents if p.is_primary), None) or (parents[0] if parents else None)
    parent_name = ''
    parent_phone = ''
    if primary:
        parent_name = f"{primary.first_name} {primary.last_name}".strip()
        parent_phone = (primary.phone or '').strip()
    if not parent_phone:
        parent_phone = (family.phone or '').strip()
    if not parent_name:
        parent_name = family.name or ''
    return parent_name, parent_phone, (family.email or '').strip()


def reschedule_blocked_trial_enrollments(*, dry_run: bool = False) -> list[dict]:
    """
    Move active trial enrollments off blocked dates to the next date of the same חוג.
    Also updates matching payment.trial_lesson_date and start_date when it matched.
    """
    blocked = blocked_trial_lesson_dates()
    if not blocked:
        return []

    qs = (
        LessonEnrollment.objects
        .select_related(
            'lesson',
            'lesson__course',
            'lesson__course__branch',
            'child',
            'child__family',
        )
        .prefetch_related('child__family__parents')
        .filter(
            status='active',
            trial_lesson_date__in=sorted(blocked),
        )
        .order_by('trial_lesson_date', 'child__last_name', 'child__first_name')
    )

    moved: list[dict] = []
    for enrollment in qs:
        lesson = enrollment.lesson
        child = enrollment.child
        old_date = enrollment.trial_lesson_date
        if not lesson or not child or not old_date:
            continue

        new_date = next_trial_date_after(lesson, old_date)
        parent_name, parent_phone, email = _parent_contact_for_child(child)
        family = child.family
        row = {
            'enrollment_id': str(enrollment.id),
            'child_id': str(child.id),
            'child_first_name': child.first_name,
            'child_last_name': child.last_name,
            'child_status': child.status,
            'family_name': family.name if family else '',
            'parent_name': parent_name,
            'parent_phone': parent_phone,
            'email': email,
            'course': lesson.course.name if lesson.course_id else '',
            'branch': (
                lesson.course.branch.name
                if lesson.course_id and lesson.course.branch_id
                else ''
            ),
            'old_trial_date': old_date.isoformat(),
            'new_trial_date': new_date.isoformat() if new_date else '',
            'moved': bool(new_date),
        }

        if new_date and not dry_run:
            update_fields = ['trial_lesson_date', 'updated_at']
            if enrollment.start_date == old_date:
                enrollment.start_date = new_date
                update_fields.append('start_date')
            enrollment.trial_lesson_date = new_date
            enrollment.trial_10am_reminder_sent_at = None
            enrollment.trial_followup_reminder_sent_at = None
            enrollment.trial_evening_reminder_sent_at = None
            update_fields.extend([
                'trial_10am_reminder_sent_at',
                'trial_followup_reminder_sent_at',
                'trial_evening_reminder_sent_at',
            ])
            enrollment.save(update_fields=update_fields)

            from apps.customers.models import Payment
            Payment.objects.filter(
                child_id=child.id,
                lesson_id=lesson.id,
                trial_lesson_date=old_date,
            ).update(trial_lesson_date=new_date)

        moved.append(row)

    return moved


def remove_expired_trial_enrollments(*, dry_run: bool = False) -> dict:
    """
    After the trial lesson day ends, remove the child from the lesson roster
    (first cron run on the day after trial_lesson_date).
    """
    from apps.customers.models import Child

    now = timezone.localtime()
    today = now.date()

    qs = (
        LessonEnrollment.objects
        .select_related('lesson', 'child')
        .filter(
            trial_lesson_date__isnull=False,
            child__status='trial_signed',
            status='active',
        )
    )

    removed = 0
    skipped = 0

    for enrollment in qs:
        lesson = enrollment.lesson
        trial_date = enrollment.trial_lesson_date
        if not lesson or not trial_date:
            skipped += 1
            continue

        if trial_date >= today:
            skipped += 1
            continue

        if not dry_run:
            enrollment.status = 'inactive'
            enrollment.end_date = trial_date
            enrollment.save(update_fields=['status', 'end_date', 'updated_at'])
            Child.objects.filter(pk=enrollment.child_id, status='trial_signed').update(status='trial_completed')

        removed += 1

    return {'removed': removed, 'skipped': skipped}


def stamp_and_notify_trial_enrollment(enrollment_id: str) -> dict:
    """
    Immediately after trial signup (הרשם לניסיון):
      • store trial_lesson_date for later reminder cron
      • send ManyChat test-lesson-register flow to parent WhatsApp
    """
    from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
    from apps.core.manychat_service import ManyChatService

    enrollment = (
        LessonEnrollment.objects
        .select_related(
            'lesson',
            'lesson__course',
            'lesson__course__branch',
            'child',
            'child__family',
        )
        .prefetch_related('child__family__parents')
        .filter(id=enrollment_id)
        .first()
    )
    if not enrollment or not enrollment.lesson_id:
        return {'sent': False, 'reason': 'enrollment_not_found'}

    lesson = enrollment.lesson

    try:
        if not enrollment.trial_lesson_date:
            trial_date = compute_trial_lesson_date(lesson)
            enrollment.trial_lesson_date = trial_date
            enrollment.save(update_fields=['trial_lesson_date', 'updated_at'])
    except Exception:
        logger.exception("Failed to compute trial_lesson_date for enrollment %s", enrollment_id)

    child = enrollment.child
    ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
    if not ctx:
        return {'sent': False, 'reason': 'no_parent_phone'}

    if enrollment.trial_lesson_date:
        ctx['trial_date'] = enrollment.trial_lesson_date.strftime('%d/%m/%Y')

    lookup_names = ctx.pop('lookup_names', None)
    trial_date = ctx.pop('trial_date', '')
    result = ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_TRIAL,
        lookup_names=lookup_names,
        trial_date=trial_date,
        **ctx,
    )
    logger.info("Trial registration WhatsApp (instant): %s", result)
    return result


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
            'lesson', 'lesson__course', 'lesson__course__branch',
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

    cleanup = remove_expired_trial_enrollments(dry_run=dry_run)
    summary['trial_cleanup'] = cleanup

    return summary
