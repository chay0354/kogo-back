"""
Registers nobody read.

A lesson is taught, the roster stays blank, and by the end of the week nobody
remembers who was there. Three things happen here, in this order:

* five minutes after the lesson ends the instructor is asked to finish today's
  register, and asked again every hour while it is still open (up to ten times
  in the day);
* at 08:00 the next morning, one message lists whatever is still open from
  yesterday, with the link that takes them straight into the app;
* from two days on it stops being a message and becomes a line the office sees
  on the instructors screen.

Nothing is sent until a ManyChat flow is configured for these two messages
(MANYCHAT_REGISTER_LESSON_FLOW_NS / MANYCHAT_REGISTER_MORNING_FLOW_NS): the
gate is deliberate, so the automation can be built and deployed before the
WhatsApp side of it exists.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from apps.courses.models import Lesson
from apps.enrollments.models import LessonAttendance, LessonEnrollment, RegisterReminder

logger = logging.getLogger(__name__)

# A mark the instructor actually made. 'not_marked' is a row the system wrote.
MARKED_STATUSES = ('present', 'absent')

# How long after the lesson ends the first reminder waits.
LESSON_REMINDER_DELAY_MINUTES = 5

# And how long before it is said again, while the register is still open. The
# owner's rule: keep asking through the day rather than say it once and hope.
LESSON_REMINDER_REPEAT_MINUTES = 60

# Where the asking stops for that lesson. The morning summary carries whatever
# is still open after that.
LESSON_REMINDER_MAX_PER_DAY = 10

# The morning summary goes out at this local hour, whatever hour the cron ran.
MORNING_SUMMARY_HOUR = 8

# How far back the office view looks, and from which day a gap is "old".
GAP_WINDOW_DAYS = 14
GAP_ALERT_AFTER_DAYS = 2


@dataclass(frozen=True)
class MissingRegister:
    """One lesson occurrence whose register was never finished."""
    lesson: Lesson
    occurrence_date: date_cls
    roster: int
    marked: int

    @property
    def instructor(self):
        return self.lesson.instructor

    @property
    def title(self) -> str:
        course = getattr(self.lesson, 'course', None)
        name = getattr(course, 'name', '') or 'שיעור'
        return f'{name} {self.lesson.start_time.strftime("%H:%M")}'


def _weekday_index(day: date_cls) -> int:
    """Sunday is 0 here; Python counts from Monday."""
    return (day.weekday() + 1) % 7


def lessons_meeting_on(day: date_cls, *, instructor_id=None):
    """Lessons whose occurrence falls on this date, cancellations removed."""
    from django.db.models import Q

    from apps.scheduling.models import LessonCancellation

    qs = (
        Lesson.objects
        .filter(
            Q(is_recurring=True, day_of_week=_weekday_index(day))
            & (Q(lesson_date__isnull=True) | Q(lesson_date__lte=day))
            | Q(is_recurring=False, lesson_date=day)
        )
        .exclude(status='cancelled')
        .exclude(instructor__isnull=True)
        .select_related('course', 'course__branch', 'instructor')
    )
    if instructor_id:
        qs = qs.filter(instructor_id=instructor_id)

    lessons = list(qs)
    if not lessons:
        return []
    cancelled = set(
        LessonCancellation.objects
        .filter(lesson_id__in=[lesson.id for lesson in lessons], occurrence_date=day)
        .values_list('lesson_id', flat=True)
    )
    return [lesson for lesson in lessons if lesson.id not in cancelled]


def missing_registers(day: date_cls, *, instructor_id=None) -> list[MissingRegister]:
    """
    Every lesson that met on this date and still has someone unmarked.

    The roster is read exactly as the attendance screen reads it, so a lesson
    the instructor sees as finished is never reported as open.
    """
    from apps.scheduling.views import enrollment_visible_on_date

    lessons = lessons_meeting_on(day, instructor_id=instructor_id)
    if not lessons:
        return []

    lesson_ids = [lesson.id for lesson in lessons]
    roster: dict = {}
    for enrollment in (
        LessonEnrollment.objects
        .filter(lesson_id__in=lesson_ids, status='active')
        .select_related('child')
        .only('lesson', 'child', 'status', 'trial_lesson_date', 'ghost_visible_until',
              'child__status', 'child__created_at')
    ):
        if enrollment_visible_on_date(enrollment, day):
            roster.setdefault(enrollment.lesson_id, set()).add(enrollment.child_id)

    marked: dict = {}
    for lesson_id, child_id in (
        LessonAttendance.objects
        .filter(lesson_id__in=lesson_ids, occurrence_date=day, status__in=MARKED_STATUSES)
        .values_list('lesson_id', 'child_id')
    ):
        marked.setdefault(lesson_id, set()).add(child_id)

    open_registers = []
    for lesson in lessons:
        children = roster.get(lesson.id, set())
        if not children:
            continue  # an empty lesson has no register to read
        done = marked.get(lesson.id, set())
        if children <= done:
            continue
        open_registers.append(
            MissingRegister(
                lesson=lesson,
                occurrence_date=day,
                roster=len(children),
                marked=len(children & done),
            )
        )
    open_registers.sort(key=lambda row: (row.lesson.start_time, row.title))
    return open_registers


def instructor_register_gaps(*, days: int = GAP_WINDOW_DAYS, instructor_id=None) -> list[dict]:
    """
    Open registers of the last days, grouped by instructor — the office view.

    Today is left out: a lesson taught an hour ago is not yet a gap.
    """
    today = timezone.localdate()
    by_instructor: dict = {}
    for offset in range(1, max(days, 1) + 1):
        day = today - timedelta(days=offset)
        for row in missing_registers(day, instructor_id=instructor_id):
            instructor = row.instructor
            entry = by_instructor.setdefault(str(instructor.id), {
                'instructor_id': str(instructor.id),
                'instructor_name': f'{instructor.first_name} {instructor.last_name}'.strip(),
                'instructor_phone': instructor.phone,
                'branch_name': getattr(getattr(instructor, 'primary_branch', None), 'name', '') or '',
                'lessons': [],
            })
            entry['lessons'].append({
                'lesson_id': str(row.lesson.id),
                'course_name': getattr(row.lesson.course, 'name', ''),
                'course_display_id': getattr(row.lesson.course, 'display_id', None),
                'branch_name': getattr(getattr(row.lesson.course, 'branch', None), 'name', '') or '',
                'occurrence_date': day.isoformat(),
                'start_time': row.lesson.start_time.strftime('%H:%M'),
                'days_open': offset,
                'roster': row.roster,
                'marked': row.marked,
            })

    rows = []
    for entry in by_instructor.values():
        entry['lessons'].sort(key=lambda item: (item['occurrence_date'], item['start_time']), reverse=True)
        entry['open_count'] = len(entry['lessons'])
        entry['oldest_days_open'] = max(item['days_open'] for item in entry['lessons'])
        entry['needs_attention'] = entry['oldest_days_open'] >= GAP_ALERT_AFTER_DAYS
        rows.append(entry)
    rows.sort(key=lambda entry: (-entry['oldest_days_open'], -entry['open_count'], entry['instructor_name']))
    return rows


def _login_url() -> str:
    base = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    return f'{base}/schedule/instructor' if base else ''


def _send(kind_setting: str, *, instructor, fields: dict) -> dict:
    """
    Hand one message to ManyChat, or say why it did not go.

    Unlike the parent-facing messages there is no free-text fallback: an
    instructor reminder with no flow behind it should be silence, not a text
    the office never wrote.
    """
    from apps.core.manychat_service import ManyChatError, ManyChatService

    service = ManyChatService()
    if not service.is_configured:
        return {'sent': False, 'reason': 'manychat_not_configured'}

    flow_ns = service.resolve_flow_ns(kind_setting)
    if not flow_ns:
        return {'sent': False, 'reason': 'flow_not_configured'}

    phone = (getattr(instructor, 'phone', '') or '').strip()
    if not phone:
        return {'sent': False, 'reason': 'no_instructor_phone'}

    name = f'{instructor.first_name} {instructor.last_name}'.strip()
    try:
        resolved = service.lookup_or_create(phone, name)
    except ManyChatError as exc:
        return {'sent': False, 'reason': 'lookup_failed', 'error': str(exc)}

    subscriber_id = resolved.get('subscriber_id')
    if not subscriber_id:
        return {'sent': False, 'reason': 'no_subscriber_id'}

    try:
        written = service.set_custom_fields(subscriber_id, fields)
    except ManyChatError as exc:
        return {'sent': False, 'reason': 'fields_failed', 'error': str(exc)}
    if written.get('skipped'):
        return {'sent': False, 'reason': 'fields_not_set'}

    # The same pause the parent messages take: ManyChat indexes fields
    # asynchronously, and a template fired too early arrives with blanks.
    from apps.core.manychat_service import FIELD_SETTLE_SECONDS

    if FIELD_SETTLE_SECONDS > 0:
        time.sleep(FIELD_SETTLE_SECONDS)

    try:
        service.send_flow(subscriber_id, flow_ns)
    except ManyChatError as exc:
        return {'sent': False, 'reason': 'flow_failed', 'error': str(exc)}
    return {'sent': True, 'flow_ns': flow_ns, 'subscriber_id': subscriber_id}


def _record(*, instructor, lesson, day: date_cls, kind: str, now) -> bool:
    """Write the row that says this was said. False when it already was."""
    try:
        RegisterReminder.objects.create(
            instructor=instructor, lesson=lesson, occurrence_date=day, kind=kind, sent_at=now,
        )
    except IntegrityError:
        return False
    return True


def _already_sent(*, instructor, lesson, day: date_cls, kind: str) -> bool:
    return RegisterReminder.objects.filter(
        instructor=instructor, lesson=lesson, occurrence_date=day, kind=kind,
    ).exists()


def _lesson_reminder_due(*, instructor, lesson, day: date_cls, now) -> bool:
    """
    Whether this lesson's register is due to be asked about again.

    Not due while the last ask is still recent, and not due at all once the
    day's quota is spent — the cron runs every few minutes, and without both
    an instructor would get one message per run.
    """
    sent = list(
        RegisterReminder.objects
        .filter(
            instructor=instructor, lesson=lesson, occurrence_date=day,
            kind=RegisterReminder.KIND_LESSON,
        )
        .order_by('-sent_at')
        .values_list('sent_at', flat=True)[:LESSON_REMINDER_MAX_PER_DAY]
    )
    if len(sent) >= LESSON_REMINDER_MAX_PER_DAY:
        return False
    if sent and now - sent[0] < timedelta(minutes=LESSON_REMINDER_REPEAT_MINUTES):
        return False
    return True


def _lesson_ended_at(lesson, day: date_cls) -> datetime:
    naive = datetime.combine(day, lesson.end_time)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def send_due_register_reminders(*, dry_run: bool = False, now=None) -> dict:
    """
    What the cron calls, every hour of the teaching day.

    Both messages are decided from the clock rather than from the schedule the
    cron happens to run on, so an extra run sends nothing twice and a missed run
    still catches up on the next one.
    """
    now = now or timezone.localtime()
    today = now.date()
    yesterday = today - timedelta(days=1)
    summary = {'lesson_sent': 0, 'morning_sent': 0, 'skipped': 0, 'errors': 0}

    for row in missing_registers(today):
        instructor = row.instructor
        if now < _lesson_ended_at(row.lesson, today) + timedelta(minutes=LESSON_REMINDER_DELAY_MINUTES):
            continue
        if not _lesson_reminder_due(instructor=instructor, lesson=row.lesson, day=today, now=now):
            continue
        if dry_run:
            summary['lesson_sent'] += 1
            continue
        result = _send(
            'MANYCHAT_REGISTER_LESSON_FLOW_NS',
            instructor=instructor,
            fields={
                'kogo_instructor_name': f'{instructor.first_name} {instructor.last_name}'.strip(),
                'kogo_lesson_name': row.title,
                'kogo_lesson_branch': getattr(getattr(row.lesson.course, 'branch', None), 'name', '') or '',
                'kogo_login_url': _login_url(),
            },
        )
        if not result.get('sent'):
            summary['skipped' if result.get('reason') in ('manychat_not_configured', 'flow_not_configured') else 'errors'] += 1
            continue
        if _record(instructor=instructor, lesson=row.lesson, day=today, kind=RegisterReminder.KIND_LESSON, now=now):
            summary['lesson_sent'] += 1

    if now.hour >= MORNING_SUMMARY_HOUR:
        open_yesterday: dict = {}
        for row in missing_registers(yesterday):
            open_yesterday.setdefault(row.instructor, []).append(row)

        for instructor, rows in open_yesterday.items():
            if _already_sent(instructor=instructor, lesson=None, day=yesterday, kind=RegisterReminder.KIND_MORNING):
                continue
            if dry_run:
                summary['morning_sent'] += 1
                continue
            result = _send(
                'MANYCHAT_REGISTER_MORNING_FLOW_NS',
                instructor=instructor,
                fields={
                    'kogo_instructor_name': f'{instructor.first_name} {instructor.last_name}'.strip(),
                    'kogo_missing_lessons': ', '.join(row.title for row in rows),
                    'kogo_missing_count': str(len(rows)),
                    'kogo_login_url': _login_url(),
                },
            )
            if not result.get('sent'):
                summary['skipped' if result.get('reason') in ('manychat_not_configured', 'flow_not_configured') else 'errors'] += 1
                continue
            if _record(instructor=instructor, lesson=None, day=yesterday, kind=RegisterReminder.KIND_MORNING, now=now):
                summary['morning_sent'] += 1

    return summary
