"""
Alerts to the office, at once, about a customer whose payment or registration
went wrong — so the office knows before the customer calls.

Every alert has the same sections, one per field of the office's WhatsApp
template (WhatsApp does not allow a line break inside a template value, so the
lines are the template's own and each section fills one):

    title     — what happened, in one line
    where     — which part of the system, and which step in it
    what      — what exactly happened
    why       — the cause, as far as the system knows it
    customer  — who: parent, phone, child, course, sum
    action    — what the office should do now
    link      — the child's card in the CRM

Setup (no code): create the ManyChat user fields in ALERT_FIELDS, a flow that
sends the office template with them, and set in Vercel
MANYCHAT_OFFICE_ALERT_FLOW_NS (the flow) and OFFICE_ALERT_PHONES (comma
separated). Until then every alert is still kept (OfficeAlert,
'not_configured') and listed in the morning brief.

An alert never breaks what raised it: it is written after the caller's
transaction commits, and nothing here raises.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# ManyChat user fields the office template reads, one per section.
ALERT_FIELDS = {
    'title': 'kogo_alert_title',
    'where': 'kogo_alert_where',
    'what': 'kogo_alert_what',
    'why': 'kogo_alert_why',
    'customer': 'kogo_alert_customer',
    'action': 'kogo_alert_action',
    'link': 'kogo_alert_link',
}
# A WhatsApp template value: one line, and short.
_MAX_VALUE = 900

# Why an alert kept with deliver=False never reached the office's WhatsApp.
HELD_NOTE = 'במערכת בלבד, בלי וואטסאפ למשרד'


def _one_line(value) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:_MAX_VALUE]


def office_alert_phones() -> list[str]:
    return [p.strip() for p in (getattr(settings, 'OFFICE_ALERT_PHONES', '') or '').split(',') if p.strip()]


def office_alert_flow_ns() -> str:
    return (getattr(settings, 'MANYCHAT_OFFICE_ALERT_FLOW_NS', '') or '').strip()


def crm_child_link(child_id) -> str:
    base = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    if not base or not child_id:
        return ''
    return f'{base}/customers?child={child_id}'


def raise_office_alert(
    *,
    kind: str,
    dedup_key: str,
    title: str,
    where: str,
    what: str,
    why: str = '',
    customer: str = '',
    action: str = '',
    link: str = '',
    details: Optional[dict] = None,
    deliver: bool = True,
) -> None:
    """
    Keep the alert and send it to the office once the caller's transaction commits. Never raises.

    ``deliver=False`` keeps it in the system only — the morning brief — with
    no WhatsApp to the office (the WhatsApp-delivery alerts, by the owner's
    choice on 30.9.2026).
    """
    fields = dict(title=title, where=where, what=what, why=why, customer=customer, action=action, link=link)

    def _record_and_send():
        from apps.core.models import OfficeAlert

        try:
            with transaction.atomic():
                alert = OfficeAlert.objects.create(
                    kind=kind[:60],
                    dedup_key=dedup_key[:255],
                    details=details or {},
                    **{name: _one_line(value)[:OfficeAlert._meta.get_field(name).max_length] for name, value in fields.items()},
                )
        except IntegrityError:
            return  # this event was already reported
        except Exception:
            logger.exception('Office alert %s could not be recorded', dedup_key)
            return
        logger.warning('Office alert [%s] %s — %s', kind, alert.title, alert.what)
        if deliver:
            deliver_office_alert(alert)
        else:
            OfficeAlert.objects.filter(id=alert.id).update(error=HELD_NOTE)

    try:
        transaction.on_commit(_record_and_send)
    except Exception:
        logger.exception('Office alert %s could not be scheduled', dedup_key)


def deliver_office_alert(alert) -> None:
    """Send one kept alert to every office phone on WhatsApp. Records the outcome on the alert."""
    from apps.core.manychat_service import FIELD_SETTLE_SECONDS, ManyChatError, ManyChatService, manychat_error_detail
    from apps.core.models import OfficeAlert

    phones = office_alert_phones()
    flow_ns = office_alert_flow_ns()
    service = ManyChatService()
    if not phones or not flow_ns or not service.is_configured:
        OfficeAlert.objects.filter(id=alert.id).update(status=OfficeAlert.STATUS_NOT_CONFIGURED)
        return
    # Every section, every time: ManyChat keeps a field it is not sent, and the
    # template would show the previous alert's reason or customer.
    values = {field: (getattr(alert, name) or '—') for name, field in ALERT_FIELDS.items()}
    errors = []
    for phone in phones:
        try:
            resolved = service.lookup_or_create(phone, 'משרד קוגומלו')
            subscriber_id = resolved.get('subscriber_id')
            if not subscriber_id:
                errors.append(f'{phone}: no subscriber')
                continue
            service.set_custom_fields(subscriber_id, values)
            if FIELD_SETTLE_SECONDS > 0:
                time.sleep(FIELD_SETTLE_SECONDS)
            service.send_flow(subscriber_id, flow_ns)
        except ManyChatError as exc:
            errors.append(f'{phone}: {manychat_error_detail(exc)}')
        except Exception as exc:  # an alert must never break its caller
            errors.append(f'{phone}: {exc}')
    if errors and len(errors) == len(phones):
        OfficeAlert.objects.filter(id=alert.id).update(status=OfficeAlert.STATUS_FAILED, error='; '.join(errors)[:500])
        logger.error('Office alert %s not delivered: %s', alert.id, errors)
        return
    OfficeAlert.objects.filter(id=alert.id).update(
        status=OfficeAlert.STATUS_SENT, sent_at=timezone.now(), error='; '.join(errors)[:500],
    )


# ---------------------------------------------------------------------------
# Who the customer is, from what the flows hold
# ---------------------------------------------------------------------------

# lesson.day_of_week: 0 is Sunday (apps/enrollments/serializers.py).
_DAYS = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']


def _lesson_label(lesson) -> str:
    if lesson is None:
        return ''
    course = getattr(getattr(lesson, 'course', None), 'name', '') or ''
    day = _DAYS[lesson.day_of_week] if isinstance(getattr(lesson, 'day_of_week', None), int) and 0 <= lesson.day_of_week < 7 else ''
    start = lesson.start_time.strftime('%H:%M') if getattr(lesson, 'start_time', None) else ''
    return ' '.join(part for part in (course, day, start) if part)


def describe_family(family, *, children: Iterable = (), lessons: Iterable = (), amount=None) -> str:
    """One line: parent, phone, children, courses, sum."""
    parts = []
    if family is not None:
        parent = family.parents.filter(is_primary=True).first() if hasattr(family, 'parents') else None
        name = parent.full_name if parent else family.name
        phone = (parent.phone if parent and parent.phone else '') or family.phone or ''
        parts.append(f'הורה: {name}' + (f' {phone}' if phone else ''))
    names = [c.full_name for c in children if c is not None]
    if names:
        parts.append('ילד: ' + ', '.join(dict.fromkeys(names)))
    labels = [label for label in (_lesson_label(lesson) for lesson in lessons) if label]
    if labels:
        parts.append('חוג: ' + ', '.join(dict.fromkeys(labels)))
    if amount is not None:
        parts.append(f'סכום: ₪{amount}')
    return ' · '.join(parts)


def describe_payments(payments) -> tuple[str, str]:
    """(customer line, link to the first child's card) for a set of widget payments."""
    payments = list(payments)
    if not payments:
        return '', ''
    family = payments[0].family
    lessons = []
    for payment in payments:
        if payment.bundle_id:
            lessons.extend(payment.bundle.lessons.all())
        elif payment.lesson_id:
            lessons.append(payment.lesson)
    customer = describe_family(
        family,
        children=[p.child for p in payments],
        lessons=lessons,
        amount=sum((p.final_amount for p in payments), start=0),
    )
    return customer, crm_child_link(payments[0].child_id)


def send_test_alert(*, requested_by: str = '') -> None:
    """A test alert, for checking the template once it is set up (managers, from the CRM)."""
    now = timezone.localtime(timezone.now())
    raise_office_alert(
        kind='test',
        dedup_key=f'test:{now.isoformat()}',
        title='התראת בדיקה',
        where='הגדרות — בדיקת התראות למשרד',
        what='זו הודעת בדיקה. אם היא הגיעה, ההתראות למשרד עובדות.',
        why='נשלחה ידנית' + (f' על ידי {requested_by}' if requested_by else ''),
        customer='—',
        action='אין צורך לעשות דבר.',
        link=(getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip(),
    )
