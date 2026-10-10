"""
Answering a customer from the CRM. ManyChat is the only pipe to the number, so
every send goes through the client Kogo already has (apps/core/manychat_service.py):
free text by `sendContent`, a template by `sendFlow`.

What the office sees is always a message in the conversation: sent, or failed
with the reason in Hebrew. A send never raises to the screen and is never
repeated by itself — after a timeout nobody knows whether the customer got it,
and a second copy is worse than a line that says so.

On a developer's machine (DEBUG) with WAHUB_SIMULATE_SEND=1 nothing leaves:
the message is kept as `simulated`. In production the setting is not read.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.manychat_service import (
    ManyChatContactUnfindable,
    ManyChatError,
    ManyChatService,
    _manychat_error_text,
    manychat_error_detail,
)
from apps.wahub import state
from apps.wahub.models import (
    DELIVERED_STATUSES,
    DIRECTION_OUT,
    MESSAGE_SOURCE_KOGO,
    SENDER_OFFICE,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SIMULATED,
    TYPE_TEMPLATE,
    TYPE_TEXT,
    Contact,
    Message,
)

logger = logging.getLogger(__name__)

# WhatsApp lets a business write freely for 24 hours after the customer's last message.
FREE_TEXT_WINDOW = timedelta(hours=24)
MAX_TEXT_CHARS = 4096

WINDOW_CLOSED_DETAIL = (
    'עברו יותר מ-24 שעות מההודעה האחרונה של הלקוח. וואטסאפ מאפשר עכשיו לשלוח רק תבנית מאושרת.'
)
UNKNOWN_OUTCOME = 'ManyChat לא ענה בזמן. לא ידוע אם ההודעה נשלחה — בדקו ב-ManyChat לפני שליחה חוזרת.'
NOT_CONFIGURED = 'ManyChat לא מוגדר בשרת (MANYCHAT_KEY). ההודעה לא נשלחה.'

_HEBREW = re.compile(r'[א-ת]')


class WindowClosed(Exception):
    """Free text outside the 24-hour window. Nothing was sent and nothing was kept."""


def simulate_send() -> bool:
    """Whether sends are only recorded. Looked at on a developer's machine alone."""
    if not settings.DEBUG:
        return False
    return bool(getattr(settings, 'WAHUB_SIMULATE_SEND', False))


def window_closes_at(contact: Contact):
    return contact.last_inbound_at + FREE_TEXT_WINDOW if contact.last_inbound_at else None


def can_free_text(contact: Contact, now=None) -> bool:
    closes = window_closes_at(contact)
    return closes is not None and (now or timezone.now()) < closes


def failure_reason(exc: ManyChatError, *, unknown: str = UNKNOWN_OUTCOME) -> str:
    """Why ManyChat did not do it, for the office to read. `unknown` is the line for a request that got no answer."""
    cause = exc.__cause__
    if isinstance(cause, requests.ReadTimeout):
        return unknown
    if isinstance(cause, requests.RequestException):
        return 'אין חיבור ל-ManyChat. הפעולה לא בוצעה.'
    if isinstance(exc, ManyChatContactUnfindable):
        return str(exc)
    detail = manychat_error_detail(exc)
    # The client's own messages are already in Hebrew; ManyChat's are not.
    return detail if _HEBREW.search(detail) else f'ManyChat דחה את הפעולה: {detail}'


def subscriber_id_for(service: ManyChatService, contact: Contact) -> str:
    """The ManyChat contact behind this phone: the one that wrote to us, else found (or opened) by phone."""
    if contact.manychat_subscriber_id:
        return contact.manychat_subscriber_id
    resolved = service.lookup_or_create(contact.phone, contact.name)
    subscriber_id = resolved.get('subscriber_id')
    if not subscriber_id:
        raise ManyChatError('לא נמצא איש קשר ב-ManyChat למספר הזה.')
    contact.manychat_subscriber_id = str(subscriber_id)
    Contact.objects.filter(pk=contact.pk).update(manychat_subscriber_id=contact.manychat_subscriber_id)
    return contact.manychat_subscriber_id


def _contact_gone(exc: ManyChatError) -> bool:
    """ManyChat no longer has the contact we remembered (its own rule deletes idle contacts)."""
    text = _manychat_error_text(exc)
    return 'subscriber does not exist' in text or 'subscriber not found' in text


def with_subscriber(service: ManyChatService, contact: Contact, call):
    """
    Run `call(subscriber_id)`. A remembered contact ManyChat has since deleted
    is looked up again by phone, once — that refusal is a certain "not sent",
    so trying again cannot produce a second copy.
    """
    remembered = bool(contact.manychat_subscriber_id)
    try:
        return call(subscriber_id_for(service, contact))
    except ManyChatError as exc:
        if not (remembered and _contact_gone(exc)):
            raise
    contact.manychat_subscriber_id = ''
    Contact.objects.filter(pk=contact.pk).update(manychat_subscriber_id='')
    return call(subscriber_id_for(service, contact))


def send_text(contact: Contact, text: str, user) -> Message:
    if not can_free_text(contact):
        raise WindowClosed()
    return _deliver(
        contact, user, text=text, message_type=TYPE_TEXT,
        call=lambda service, subscriber_id: service.send_whatsapp_text(subscriber_id, text),
    )


def send_flow(contact: Contact, flow_ns: str, name: str, user) -> Message:
    """A template (ManyChat automation). Allowed outside the 24-hour window."""
    return _deliver(
        contact, user, text=name or flow_ns, message_type=TYPE_TEMPLATE,
        call=lambda service, subscriber_id: service.send_flow(subscriber_id, flow_ns),
    )


def flow_for(automation_id: str) -> str:
    """The flow behind what the screen sent: a flow id as it is, or the flow of one of Kogo's own kinds."""
    automation_id = (automation_id or '').strip()
    entry = ManyChatService._REGISTRATION_KINDS.get(automation_id)
    if entry is None:
        return automation_id
    if simulate_send():
        return automation_id
    return ManyChatService().resolve_flow_for(entry)


def flow_name(flow_ns: str) -> str:
    """What ManyChat calls the automation, for the line in the conversation. '' when it cannot be asked."""
    if simulate_send():
        return ''
    try:
        flows = ManyChatService().get_flows()
    except ManyChatError:
        return ''
    for flow in flows:
        if (flow.get('ns') or flow.get('flow_ns') or '').strip() == flow_ns:
            return (flow.get('name') or flow.get('title') or '').strip()
    return ''


def _deliver(contact: Contact, user, *, text: str, message_type: str, call) -> Message:
    status, error = STATUS_SENT, ''
    if simulate_send():
        status = STATUS_SIMULATED
    else:
        service = ManyChatService()
        try:
            if not service.is_configured:
                raise ManyChatError(NOT_CONFIGURED)
            with_subscriber(service, contact, lambda subscriber_id: call(service, subscriber_id))
        except ManyChatError as exc:
            status, error = STATUS_FAILED, failure_reason(exc)
            logger.warning('wahub send to contact %s failed: %s', contact.pk, error)
        except Exception:  # a send is reported under the message, never thrown at the screen
            logger.exception('wahub send to contact %s failed unexpectedly', contact.pk)
            status, error = STATUS_FAILED, 'תקלה לא צפויה בשליחה. לא ידוע אם ההודעה נשלחה.'
    return _keep_outbound(contact, user, text, message_type, status, error)


def _keep_outbound(contact: Contact, user, text: str, message_type: str, status: str, error: str) -> Message:
    now = timezone.now()
    with transaction.atomic():
        message = Message.objects.create(
            contact=contact,
            direction=DIRECTION_OUT,
            sender=SENDER_OFFICE,
            sender_name=state.user_display_name(user)[:150],
            sent_by=user if getattr(user, 'is_authenticated', False) else None,
            text=text,
            message_type=message_type,
            status=status,
            error=error[:300],
            sent_at=now,
            source=MESSAGE_SOURCE_KOGO,
        )
        changes = {'messages_count': F('messages_count') + 1}
        if status in DELIVERED_STATUSES:
            # The office answered: nobody is waiting, and nothing is unread.
            changes.update(
                last_message_at=now,
                last_message_text=state.preview(text),
                last_message_direction=DIRECTION_OUT,
                last_message_sender=SENDER_OFFICE,
                waiting_since=None,
                unread_count=0,
            )
        state.touch(contact.pk, **changes)
    return message
