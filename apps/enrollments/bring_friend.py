"""
"חבר מביא חבר" with the trial-day message.

Owner, 15.10.2026: when the 10:00 "are you coming" message goes out for a trial,
the system looks at the class the child is coming to, and only if fewer than 15
are registered in it does the parent also get the "חבר מביא חבר" automation —
the one the office already sends by hand from ManyChat.

The decision is taken next to the 10:00 message (trial_reminders.py), never on
its own: a trial whose 10:00 message did not go out gets no invitation either.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from apps.enrollments.models import LessonEnrollment

logger = logging.getLogger(__name__)

# The invitation goes with the 10:00 message, so only shortly after it: the
# next run picks up a run that was cut halfway, and a deploy in the afternoon
# does not send it hours after the morning message.
BRING_FRIEND_AFTER_10AM_WINDOW = timedelta(hours=1)

SENT = 'sent'
FULL = 'full'
FAMILY_HAS_IT = 'family'
NO_FLOW = 'no_flow'
NO_PHONE = 'no_phone'
OPTED_OUT = 'opted_out'
FAILED = 'failed'


def bring_friend_below() -> int:
    """Fewer registered than this, and the class gets the invitation (default 15; 0 = off)."""
    value = getattr(settings, 'TRIAL_BRING_FRIEND_BELOW', 15)
    try:
        return 15 if value is None or value == '' else int(value)
    except (TypeError, ValueError):
        return 15


def registered_on_lesson(lesson, occurrence_date: date) -> int:
    """
    How many are registered to this lesson on that date — the number the
    schedule shows as "רשומים" (scheduling/views.py, active_student_count):
    register_kind's students, one row per person, whoever cancelled and still
    comes this month, and the municipality's children. Trials are not counted.
    """
    from apps.enrollments.duplicate_students import collapse_duplicate_people
    from apps.enrollments.enrollment_counts import ON_REGISTER_STUDENT, register_kind
    from apps.enrollments.leaving import leaving_enrollments, leaving_on
    from apps.external_students.models import ExternalStudent
    from apps.external_students.roster import external_visible_on_date
    from apps.scheduling.views import enrollment_visible_on_date

    rows = collapse_duplicate_people(list(
        LessonEnrollment.objects
        .filter(lesson_id=lesson.id, status='active')
        .select_related('child', 'child__family')
    ))
    registered = 0
    for enr in rows:
        if not enrollment_visible_on_date(enr, occurrence_date):
            continue
        if register_kind(enr, occurrence_date) == ON_REGISTER_STUDENT:
            registered += 1

    seen = {enr.child_id for enr in rows}
    leaving = leaving_enrollments([lesson.id], occurrence_date, occurrence_date)
    for enr in leaving_on(leaving.get(lesson.id), occurrence_date):
        if enr.child_id not in seen:
            registered += 1

    for student in ExternalStudent.objects.filter(lesson_id=lesson.id, is_active=True):
        if external_visible_on_date(student, occurrence_date):
            registered += 1
    return registered


def class_has_room_for_friends(lesson, occurrence_date: date, *, cache: dict | None = None) -> tuple[bool, int]:
    """
    (invite?, registered). Fewer than bring_friend_below() registered, and —
    when the lesson has a limit of its own — a place still free under it: a
    class of six that holds six has no room for a friend.
    """
    from apps.enrollments.enrollment_counts import resolve_lesson_capacity

    key = (lesson.id, occurrence_date)
    if cache is not None and key in cache:
        return cache[key]
    registered = registered_on_lesson(lesson, occurrence_date)
    capacity = resolve_lesson_capacity(lesson)
    answer = (
        registered < bring_friend_below() and (capacity is None or registered < capacity),
        registered,
    )
    if cache is not None:
        cache[key] = answer
    return answer


def bring_friend_is_due(enrollment, *, ten_am_sent_now: bool = False) -> bool:
    """The 10:00 message went out for this trial a moment ago, and the invitation did not."""
    if enrollment.trial_bring_friend_sent_at:
        return False
    if ten_am_sent_now:
        return True
    sent_at = enrollment.trial_10am_reminder_sent_at
    return bool(sent_at) and sent_at >= timezone.now() - BRING_FRIEND_AFTER_10AM_WINDOW


def resolve_bring_friend_flow(svc) -> str:
    """The ManyChat automation, by MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS or by its name."""
    return svc.resolve_flow_ns('MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS')


def send_bring_friend(enrollment, ctx: dict, *, svc, flow_ns: str, dry_run: bool, cache: dict) -> tuple[str, dict]:
    """
    One invitation for one trial, if its class is small enough. Returns
    (outcome, detail). Stamps trial_bring_friend_sent_at only for a send
    ManyChat accepted.
    """
    from apps.core.manychat_service import ManyChatError, manychat_error_detail

    lesson = enrollment.lesson
    invite, registered = class_has_room_for_friends(lesson, enrollment.trial_lesson_date, cache=cache)
    if not invite:
        return FULL, {'registered': registered}

    # Two children of one family on a trial the same day: one invitation.
    family_id = getattr(enrollment.child, 'family_id', None)
    if family_id and (
        LessonEnrollment.objects
        .filter(
            child__family_id=family_id,
            trial_lesson_date=enrollment.trial_lesson_date,
            trial_bring_friend_sent_at__isnull=False,
        )
        .exclude(pk=enrollment.pk)
        .exists()
    ):
        return FAMILY_HAS_IT, {'registered': registered}

    if not flow_ns:
        return NO_FLOW, {'registered': registered}
    phone = (ctx.get('phone') or '').strip()
    if not phone:
        return NO_PHONE, {'registered': registered}

    # An invitation is a broadcast, not a message about the child's own trial:
    # whoever said "הסר" does not get it (core/whatsapp_optout.py). A list that
    # cannot be read sends nothing — the next run asks again.
    from apps.core import whatsapp_optout

    try:
        if whatsapp_optout.is_blocked(phone):
            return OPTED_OUT, {'registered': registered}
    except whatsapp_optout.OptOutListUnavailable:
        return FAILED, {'registered': registered, 'reason': whatsapp_optout.UNAVAILABLE_CODE}

    if dry_run:
        logger.info('DRY-RUN bring-a-friend for enrollment %s (%s registered)', enrollment.id, registered)
        return SENT, {'registered': registered, 'dry_run': True}

    # Claimed before the send, so two overlapping runs cannot both send it.
    claimed = LessonEnrollment.objects.filter(
        pk=enrollment.pk, trial_bring_friend_sent_at__isnull=True,
    ).update(trial_bring_friend_sent_at=timezone.now())
    if claimed != 1:
        return FAMILY_HAS_IT, {'registered': registered}

    try:
        result = svc.send_automation_to_contact(
            automation_type='flow',
            automation_id=flow_ns,
            phone=phone,
            name=ctx.get('parent_name') or '',
        )
    except ManyChatError as exc:
        result = {'sent': False, 'reason': 'manychat_error', 'error': manychat_error_detail(exc)}
    except Exception as exc:  # the trial-day loop must go on to the next parent
        logger.exception('bring-a-friend failed for enrollment %s', enrollment.id)
        result = {'sent': False, 'reason': 'error', 'error': str(exc)}

    if not result.get('sent'):
        LessonEnrollment.objects.filter(pk=enrollment.pk).update(trial_bring_friend_sent_at=None)
        logger.warning('bring-a-friend NOT sent for %s: %s', enrollment.id, result)
        return FAILED, {'registered': registered, **result}

    logger.info('bring-a-friend sent for %s (%s registered): %s', enrollment.id, registered, result)
    return SENT, {'registered': registered, **result}
