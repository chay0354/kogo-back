"""
"מחכים לברכה ליום הולדת" (the owner, 15.10.2026): "אופציה להבין מי מבקש ברכה ליום הולדת — רק ברכה או סרטון
מקוגומלו זה הכל אותו דבר — זה מתייג אותו ישר למבקש ברכה, ושבנציג יהיה עוד סטטוס 'מחכים לברכה ליום הולדת'".

A customer who asked for Kogo's birthday greeting (or its video — one and the same) waits for a person to send it.
That wait is `Contact.birthday_wait_since`; the lists read it as one more reason a conversation is open, and as a box
of its own in the person's group (queries.py). It is set — only when it is not set yet — in two places:

1. The bot's tag is kept for real (`apply_tag_marks`). The bot's tool `tag_conversation` only marks its draft
   (shadow_tools.py), like `request_human`; the tag is written on the contact where an answer really left for the
   customer — the live answer (live_test.py), beside the waiting list's own marks — and where the new bot's answer
   is written into a demo conversation. Any tag a topic ends with is kept; "ברכת יום הולדת" also starts the wait.
2. The customer's own message (`from_message`, called by inbound.py): a greeting or a video together with a birthday,
   read by rules, whichever bot answers. It asks nobody for a person — the box is the call on the office.

✓ (future_leads.close) ends the wait. Nothing here sends anything to anybody.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import (
    DIRECTION_OUT,
    EVENT_TAGS,
    KIND_TOPIC,
    SENDER_OFFICE,
    Contact,
    KnowledgeItem,
    Message,
    Tag,
)

logger = logging.getLogger(__name__)

TOOL_NAME = 'tag_conversation'
GREETING_TAG = 'ברכת יום הולדת'
WAIT_LINE = 'מחכה לברכה ליום הולדת'
DEFAULT_TAG_COLOR = Tag._meta.get_field('color').default
DETAILS_CHARS = 200

# "ברכה", "ברכת", "ברכות", a video — and a birthday in any of the ways it is written: "יום הולדת", "יום ההולדת",
# "יום-הולדת", "יומולדת". Searched anywhere in the message, so the Hebrew prefixes (ל, ש, ה, ו…) need nothing.
_GREETING = re.compile(r'ברכה|ברכת|ברכות|סרטון|סירטון')
_BIRTHDAY = re.compile(r'יום[\s\-־]*ה?הולדת|יומולדת|יומהולדת')
# "בברכה, דנה" closes a letter; it is no greeting asked for.
_SIGN_OFF = re.compile(r'(?<![א-ת])בברכה(?![א-ת])')
# Once the office has sent the greeting, a word about it from the customer is a thank-you, not another request.
ANSWERED_WITHIN = timedelta(days=7)


def asks_for_greeting(text: str) -> bool:
    """A greeting or a video, and a birthday, in the same message."""
    plain = _SIGN_OFF.sub(' ', text or '')
    return bool(_GREETING.search(plain) and _BIRTHDAY.search(plain))


def _norm(text) -> str:
    # The bot's tool matches a tag by the same rule (shadow_tools._norm): spaces, dashes and quotes do not count.
    from apps.wahub.shadow_tools import _norm as tool_norm

    return tool_norm(text)


def _seed_colors() -> dict:
    from apps.wahub import knowledge_seed

    return {name: color for name, color in knowledge_seed.TAGS}


def _allowed_tags() -> dict:
    """
    {normalized name: name} — what the bot's tool accepts: the tags the topics of the knowledge end with, the
    section's own tags, and the two of the seed. A call the tool refused (a name it does not know) stays on the
    answer's list of calls, and must not become a tag of its own here.
    """
    names = list(Tag.objects.values_list('name', flat=True))
    names += [
        str((data or {}).get('tag') or '').strip()
        for data in KnowledgeItem.objects.filter(kind=KIND_TOPIC, is_active=True).values_list('data', flat=True)
    ]
    names += list(_seed_colors())
    allowed = {}
    for name in names:
        if name and _norm(name) not in allowed:
            allowed[_norm(name)] = name
    return allowed


def _tag_named(name: str) -> Tag:
    """The section's tag of this name — the one there is, however it is spaced, or a new one in the seed's colour."""
    for tag in Tag.objects.all():
        if _norm(tag.name) == _norm(name):
            return tag
    tag, _created = Tag.objects.get_or_create(name=name[:60], defaults={'color': _seed_colors().get(name, DEFAULT_TAG_COLOR)})
    return tag


def _start_wait(contact_id, now) -> bool:
    """The wait begins now, unless it already did. Whether it began."""
    return bool(Contact.objects.filter(pk=contact_id, birthday_wait_since__isnull=True).update(
        birthday_wait_since=now, touched_at=now, updated_at=now,
    ))


def _greeting_sent_lately(contact_id, now) -> bool:
    """The office wrote about the birthday greeting this past week — most likely it sent it."""
    texts = Message.objects.filter(
        contact_id=contact_id, direction=DIRECTION_OUT, sender=SENDER_OFFICE, sent_at__gte=now - ANSWERED_WITHIN,
    ).values_list('text', flat=True)
    return any(_BIRTHDAY.search(text or '') for text in texts)


def from_message(contact: Contact, text: str, now) -> bool:
    """
    A customer's message that asks for the greeting: the tag on the contact, a line in its journal, and the wait —
    when it is not on already. Called by inbound.py inside its transaction, with the contact held. Whether the
    wait began.
    """
    if contact.birthday_wait_since is not None or not asks_for_greeting(text):
        return False
    if _greeting_sent_lately(contact.pk, now):
        return False
    tag = _tag_named(GREETING_TAG)
    tagged = not contact.tags.filter(pk=tag.pk).exists()
    if tagged:
        contact.tags.add(tag)
    started = _start_wait(contact.pk, now)
    if tagged or started:
        state.log_event(
            contact.pk, EVENT_TAGS,
            (f'נוספה התגית {tag.name} · ' if tagged else '') + f'{WAIT_LINE} — הלקוח כתב: "{state.preview(text)[:200]}"',
        )
    return started


def apply_tag_marks(contact: Contact, tools_used) -> int:
    """
    The tags behind an answer that really left for the customer: one for every call of the bot's tool in it that
    names a tag the tool accepts. "ברכת יום הולדת" also starts the wait. How many tags were added. Never raises:
    the answer has already left.
    """
    if contact is None:
        return 0
    calls = [
        call for call in (tools_used or []) if isinstance(call, dict) and call.get('name') == TOOL_NAME and isinstance(call.get('input'), dict)
    ]
    if not calls:
        return 0
    added = 0
    try:
        allowed = _allowed_tags()
        now = timezone.now()
        with transaction.atomic():
            for call in calls:
                name = allowed.get(_norm(' '.join(str(call['input'].get('tag') or '').split())))
                if not name:
                    continue
                tag = _tag_named(name)
                details = ' '.join(str(call['input'].get('details') or '').split())[:DETAILS_CHARS]
                tagged = not contact.tags.filter(pk=tag.pk).exists()
                if tagged:
                    contact.tags.add(tag)
                    added += 1
                started = _norm(tag.name) == _norm(GREETING_TAG) and _start_wait(contact.pk, now)
                if tagged or started:
                    parts = ([f'הבוט תייג: {tag.name}'] if tagged else []) + ([WAIT_LINE] if started else [])
                    state.log_event(contact.pk, EVENT_TAGS, ' · '.join(parts) + (f' — {details}' if details else ''))
            state.touch(contact.pk)
    except Exception:
        logger.exception('wahub birthday: the bot\'s tags for contact %s could not be kept', getattr(contact, 'pk', None))
        return 0
    return added
