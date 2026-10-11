"""
What ManyChat sends in: a copy of every message a customer writes and of every
answer the bot gives.

`handle_payload` reads either body ManyChat can post (docs/WAHUB-CONTRACT.md)
and `store_event` keeps it: the contact by phone, the message, and the state of
the conversation. The endpoint and the local simulator both go through here.

Nothing in this module calls another company, and nothing is matched against
the registrations: the answer has to come back to ManyChat fast. The summary
and the matching happen later, in the cron (apps/wahub/cron.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.wahub import media, state
from apps.wahub.models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    EVENT_CREATED,
    EVENT_NEEDS_HUMAN,
    MESSAGE_SOURCE_MANYCHAT,
    SENDER_BOT,
    SENDER_CUSTOMER,
    SOURCE_WHATSAPP,
    STATUS_FAILED,
    STATUS_RECEIVED,
    STATUS_SENT,
    TYPE_IMAGE,
    TYPE_OTHER,
    TYPE_TEXT,
    TYPE_VOICE,
    Contact,
    Message,
)
from apps.wahub.phones import normalize_phone, phone_display

logger = logging.getLogger(__name__)

EVENT_CUSTOMER_MESSAGE = 'customer_message'
EVENT_BOT_REPLY = 'bot_reply'
EVENTS = (EVENT_CUSTOMER_MESSAGE, EVENT_BOT_REPLY)

# ManyChat repeats a request it did not get an answer to.
DUPLICATE_WINDOW = timedelta(seconds=30)

# WhatsApp's own limit is 4,096 characters; the rest is not a message.
MAX_TEXT_CHARS = 8000

# A recording or a picture — a demo contact's upload (docs/WAHUB-CONTRACT-REAL-DEMO.md, א)
# or the address ManyChat passes for a real customer's file (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב) —
# and the line the list shows for one that came with no text.
MEDIA_TYPES = (TYPE_VOICE, TYPE_IMAGE, TYPE_OTHER)
MEDIA_PREVIEW = {TYPE_VOICE: '🎤 הודעה קולית', TYPE_IMAGE: '🖼️ תמונה', TYPE_OTHER: '📎 קובץ'}
# A live-test contact's message with no text and no address: media ManyChat did not pass on.
MEDIA_NOT_FORWARDED = '(מדיה — ManyChat לא העביר)'

NEEDS_HUMAN_REASON = 'ביקש נציג'

# "I want a person". Plain phrases on purpose: a false light costs the office a
# glance, a missed one costs a customer.
_ASKS_FOR_HUMAN = re.compile(
    r'נציג|בן אדם|בנאדם|בן-אדם|אנושי|שיחזרו אל|תחזרו אל|לדבר עם מישה'
)

# ManyChat keeps a duplicate of an imported User Field under the same name plus
# a timestamp ("Client_Phone (2026-07-26 07:28:15)").
_TIMESTAMP_SUFFIX = re.compile(r' \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\)$')


@dataclass
class StoreResult:
    stored: bool
    reason: str = ''
    contact_id: int | None = None
    message_id: int | None = None
    # A customer message kept for a live-test contact: the NEW bot answers it now (live_test.py).
    live_test: bool = False


def asks_for_human(text: str) -> bool:
    return bool(_ASKS_FOR_HUMAN.search(text or ''))


# --- reading the two bodies -----------------------------------------------------

def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ''
    return str(value).strip()


def _custom_fields(raw) -> dict:
    """ManyChat sends them as {name: value} or as a list of {name, value}."""
    fields: dict = {}
    if isinstance(raw, dict):
        fields = dict(raw)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get('name') is not None:
                fields[str(item['name'])] = item.get('value')
    # The timestamped copy answers for a clean name that is empty or missing.
    for name, value in list(fields.items()):
        clean = _TIMESTAMP_SUFFIX.sub('', str(name))
        if clean != name and not _text(fields.get(clean)) and _text(value):
            fields[clean] = value
    return fields


def _first(*values) -> str:
    return next((text for text in (_text(value) for value in values) if text), '')


def parse_payload(data, query_event: str = '') -> dict:
    """The fields `store_event` takes, out of either body."""
    data = data if isinstance(data, dict) else {}
    fields = _custom_fields(data.get('custom_fields'))
    event = _first(data.get('event'), query_event) or EVENT_CUSTOMER_MESSAGE

    name = _text(data.get('name'))
    if not name:
        name = f"{_text(data.get('first_name'))} {_text(data.get('last_name'))}".strip()

    if event == EVENT_BOT_REPLY:
        text = _first(data.get('text'), fields.get('Last_AI_Respone'))
    else:
        text = _first(data.get('text'), data.get('last_input_text'), fields.get('Client_Last_Input'))

    return {
        'event': event,
        # The first one that is there decides. A WhatsApp number from abroad is
        # not replaced by whatever the parent typed into the bot's phone field.
        'phone': _first(
            data.get('whatsapp_phone'), fields.get('kogo_whatsapp_phone'), fields.get('Client_Phone'), data.get('phone'),
        ),
        'name': name,
        'subscriber_id': _first(data.get('subscriber_id'), data.get('id')),
        'text': text,
        'ts': data.get('ts'),
    }


def _moment(ts, now):
    """
    When the message was written, if ManyChat said so believably; otherwise now.

    A time with no zone is Israel's. One that lands in the future or more than
    a day back is a formatting accident, and the 24-hour window is counted from
    this value — so it is not trusted.
    """
    moment = None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        seconds = ts / 1000 if ts > 10_000_000_000 else ts
        try:
            moment = datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            moment = None
    elif isinstance(ts, str) and ts.strip():
        raw = ts.strip()
        if raw.isdigit():
            return _moment(int(raw), now)
        try:
            moment = parse_datetime(raw)
        except ValueError:
            moment = None
        if moment is not None and timezone.is_naive(moment):
            moment = timezone.make_aware(moment)
    if moment is None or moment > now + timedelta(minutes=2) or moment < now - timedelta(hours=24):
        return now
    return moment


# --- keeping it -----------------------------------------------------------------

def handle_payload(data, query_event: str = '') -> StoreResult:
    # The body itself travels along: it is kept on a live-test contact's message (store_event).
    return store_event(**parse_payload(data, query_event), raw=data if isinstance(data, dict) else None)


def media_in_text(text: str) -> tuple[str, str]:
    """
    ManyChat passes a customer's recording or picture as a message whose whole
    text is the file's address (seen 11.10.2026: `last_input_text` =
    https://manybot-files.s3.eu-central-1.amazonaws.com/…/original_….ogg).
    Returns (message_type, media_url): ('voice'|'image'|'other', the address)
    for such a text, ('text', '') for anything else — a sentence with a link
    in it, or a link to a site, stays the text it is.
    """
    kind = media.kind_of_url(text)
    return (kind, text.strip()) if kind else (TYPE_TEXT, '')


def store_event(*, event: str, phone, text, name: str = '', subscriber_id='', ts=None,
                message_type: str = TYPE_TEXT, media_url: str = '', raw=None) -> StoreResult:
    """
    `message_type` and `media_url` come from the demo door (a recording or a
    picture the manager uploaded, apps/wahub/media.py); for ManyChat's copy of
    a real message — of any contact — they are read from the text when it is a
    bare address of a file (`media_in_text`). The address is kept as it came:
    ManyChat is waiting on this door, so the file is fetched into the bucket a
    moment later, when the message is answered (shadow.py — the live test
    within seconds, the cron's shadow for everybody else), and stays at its
    address if that fails. A media message may have no text. The duplicate
    window is for ManyChat's repeats: of one text, or of one file's address — a
    second picture has an address of its own, and a demo contact's second
    upload is a second message.

    `raw` is ManyChat's body; it is kept on the message for a live-test
    contact only (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב). Such a contact's
    message with neither text nor address is media ManyChat did not pass on,
    and is kept as `other` so the bot answers it; for everybody else an empty
    message is nothing, as before.
    """
    if event not in EVENTS:
        return StoreResult(False, 'unknown_event')
    phone = normalize_phone(phone)
    if not phone:
        return StoreResult(False, 'invalid_phone')
    text = _text(text)[:MAX_TEXT_CHARS]
    incoming = event == EVENT_CUSTOMER_MESSAGE
    if incoming and not media_url and message_type == TYPE_TEXT:
        message_type, media_url = media_in_text(text)
        if media_url:
            text = ''
    is_media = message_type in MEDIA_TYPES and bool(media_url)
    if not is_media:
        message_type = TYPE_TEXT
        media_url = ''
    if not text and not is_media:
        if not (incoming and raw is not None and Contact.objects.filter(phone=phone, live_test=True, is_demo=False).exists()):
            return StoreResult(False, 'empty_text')
        message_type, text = TYPE_OTHER, MEDIA_NOT_FORWARDED

    now = timezone.now()
    sent_at = _moment(ts, now)
    name = _text(name)[:200]
    subscriber_id = _text(subscriber_id)[:40]
    direction = DIRECTION_IN if incoming else DIRECTION_OUT

    with transaction.atomic():
        contact, created = Contact.objects.get_or_create(
            phone=phone,
            defaults={'name': name, 'manychat_subscriber_id': subscriber_id, 'source': SOURCE_WHATSAPP},
        )
        # One event at a time per contact: two copies of one message arriving
        # together would both pass the duplicate check, and the counters below
        # are read before they are written.
        contact = Contact.objects.select_for_update().get(pk=contact.pk)

        recent = Message.objects.filter(contact=contact, direction=direction, created_at__gte=now - DUPLICATE_WINDOW)
        if message_type == TYPE_TEXT:
            same = recent.filter(text=text)
        elif media.is_external(media_url):
            # ManyChat names every file once, so the same address again is the same message again —
            # also when the first copy's file has been fetched into the bucket meanwhile.
            kept = media.archived_stem(contact.id, media_url) + '.'
            same = recent.filter(Q(media_url=media_url) | Q(media_url__startswith=kept))
        else:
            same = recent.none()
        earlier = same.order_by('-id').values_list('id', flat=True).first()
        # A repeat is ManyChat saying one thing twice, a moment apart. Once the other side has
        # spoken in between it is a new message: "כן", an answer within seconds, and "כן" again.
        if earlier and not Message.objects.filter(contact=contact, id__gt=earlier).exclude(direction=direction).exclude(status=STATUS_FAILED).exists():
            return StoreResult(False, 'duplicate', contact_id=contact.id)

        live_test = bool(contact.live_test and incoming and not contact.is_demo)
        message = Message.objects.create(
            contact=contact,
            direction=direction,
            sender=SENDER_CUSTOMER if incoming else SENDER_BOT,
            text=text,
            message_type=message_type,
            media_url=media_url,
            raw=raw if live_test and raw is not None else None,
            status=STATUS_RECEIVED if incoming else STATUS_SENT,
            sent_at=sent_at,
            source=MESSAGE_SOURCE_MANYCHAT,
        )

        changes = {
            'messages_count': F('messages_count') + 1,
            'last_message_at': sent_at,
            'last_message_text': state.preview(text) or MEDIA_PREVIEW.get(message_type, ''),
            'last_message_direction': direction,
            'last_message_sender': message.sender,
        }
        if name and not contact.name:
            changes['name'] = name
        if subscriber_id and subscriber_id != contact.manychat_subscriber_id:
            changes['manychat_subscriber_id'] = subscriber_id

        if incoming:
            changes['last_inbound_at'] = sent_at
            changes['unread_count'] = F('unread_count') + 1
            changes['needs_analysis'] = True
            # The shadow bot answers it in the cron (apps/wahub/shadow.py); never sent.
            changes['needs_shadow'] = True
            if contact.first_inbound_at is None:
                changes['first_inbound_at'] = sent_at
            if contact.waiting_since is None:
                changes['waiting_since'] = sent_at
            if asks_for_human(text) and not contact.needs_human:
                changes.update(needs_human=True, needs_human_reason=NEEDS_HUMAN_REASON, needs_human_at=now)
                state.log_event(contact.id, EVENT_NEEDS_HUMAN, f'{NEEDS_HUMAN_REASON}: "{state.preview(text)[:200]}"')
                if not contact.is_demo:
                    _alert_office(contact, name or contact.name, text)
        else:
            # The bot answered: nobody is waiting any more.
            changes['waiting_since'] = None

        state.touch(contact.id, **changes)
        if created:
            state.log_event(contact.id, EVENT_CREATED, 'נוצר מהודעת וואטסאפ')

    return StoreResult(True, contact_id=contact.id, message_id=message.id, live_test=live_test)


def _alert_office(contact, name: str, text: str) -> None:
    """
    Tell the office, once per contact per day (apps/core/office_alerts.py).

    The alert is kept when this transaction commits and sent only if the
    office's WhatsApp template is set up. It never raises.
    """
    from apps.core.office_alerts import raise_office_alert

    frontend = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
    raise_office_alert(
        kind='wahub_needs_human',
        dedup_key=f'wahub_needs_human:{contact.id}:{state.now_israel_date().isoformat()}',
        title='לקוח מבקש נציג בוואטסאפ',
        where='וואטסאפ ולידים — הודעה נכנסת',
        what=f'הלקוח כתב: "{state.preview(text)[:300]}"',
        why='בהודעה יש בקשה לדבר עם נציג',
        customer=f'{name or "ללא שם"} {phone_display(contact.phone)}',
        action='לפתוח את השיחה במדור "וואטסאפ ולידים" ולענות.',
        link=f'{frontend}/wahub?contact={contact.id}' if frontend else '',
        details={'contact_id': contact.id},
    )
