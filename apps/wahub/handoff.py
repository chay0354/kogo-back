"""
Who answers a conversation: the bot, or a person.

The bot that answers today belongs to an outside vendor and sits behind
ManyChat's default reply. It skips a contact only when three things are true
together (docs/WHATSAPP-BOT-MAP-2026-10-10.md): the contact has the tag
"התחיל טיפול: נציג אנושי", does not have "סיים טיפול: נציג אנושי", and the
field "סטטוס ט.אנושי" holds a value.

So taking a conversation over writes those three in ManyChat, and handing it
back removes the first tag. The names are settings, since they are the
vendor's and may change.

If ManyChat refuses, nothing changes here either: a screen that says "a person
is answering" while the bot still answers is worse than an error. On a
developer's machine in simulation mode only the local state changes.
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.wahub import state
from apps.wahub.models import EVENT_HANDLED_BY, EVENT_LIVE_TEST, HANDLED_BOT, HANDLED_HUMAN, Contact
from apps.wahub.sending import SENDING_OFF, failure_reason, sending_enabled, simulate_send, subscriber_id_for

logger = logging.getLogger(__name__)

DEFAULT_START_TAG = 'התחיל טיפול: נציג אנושי'
DEFAULT_END_TAG = 'סיים טיפול: נציג אנושי'
DEFAULT_STATUS_FIELD = 'סטטוס ט.אנושי'
DEFAULT_STATUS_VALUE = 'בטיפול נציג (Kogo)'
# The live test (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב): the same three things mute the
# old bot, with this value in the status field so ManyChat's screen says why.
DEFAULT_LIVE_TEST_STATUS_VALUE = 'בדיקה חיה (Kogo)'
LIVE_TEST_DEMO = 'איש קשר דמו לא נכנס לבדיקה חיה — לא שולחים אליו הודעות.'

NOT_CONFIGURED_HERE = 'ManyChat לא מוגדר בשרת (MANYCHAT_KEY), ולכן אי אפשר להשתיק או להחזיר את הבוט.'


class HandoffError(Exception):
    """ManyChat did not take the change. The local state was left as it was."""


def _setting(name: str, default: str) -> str:
    return (getattr(settings, name, '') or '').strip() or default


def start_tag() -> str:
    return _setting('WAHUB_HUMAN_START_TAG', DEFAULT_START_TAG)


def end_tag() -> str:
    return _setting('WAHUB_HUMAN_END_TAG', DEFAULT_END_TAG)


def status_field() -> str:
    return _setting('WAHUB_HUMAN_STATUS_FIELD', DEFAULT_STATUS_FIELD)


def status_value() -> str:
    return _setting('WAHUB_HUMAN_STATUS_VALUE', DEFAULT_STATUS_VALUE)


def live_test_status_value() -> str:
    return _setting('WAHUB_LIVE_TEST_STATUS_VALUE', DEFAULT_LIVE_TEST_STATUS_VALUE)


def _tags_of(service: ManyChatService, subscriber_id) -> set | None:
    """The contact's tags by name; None when ManyChat's answer does not list them."""
    info = service.get_subscriber(subscriber_id)
    tags = info.get('tags') if isinstance(info, dict) else None
    if not isinstance(tags, list):
        return None
    return {str(tag.get('name') or '').strip() for tag in tags if isinstance(tag, dict)}


def _remove_tag(service: ManyChatService, subscriber_id, tag: str, tags: set | None) -> None:
    """
    Take a tag off, when it is on. With the tags unknown the removal is tried,
    and ManyChat saying "nothing to remove" (a 400) is not a failure — a
    network fault or a refused key still is.
    """
    if tags is not None and tag not in tags:
        return
    try:
        service.remove_tag_by_name(subscriber_id, tag)
    except ManyChatError as exc:
        if tags is not None or exc.status_code != 400:
            raise


def _in_manychat(contact: Contact, change, *, allowed: bool | None = None) -> None:
    """
    Write `change` in ManyChat. Allowed for this contact by `sending_enabled`
    (the switch, or the contact's own live test), or by `allowed` when the
    caller knows better — turning a live test on is what makes the contact one.
    """
    if contact.is_demo or simulate_send():
        # An invented contact has nothing in ManyChat to mute; only the local state moves.
        return
    if not (sending_enabled(contact) if allowed is None else allowed):
        # The bot would keep answering while the screen said a person took over.
        raise HandoffError(SENDING_OFF)
    service = ManyChatService()
    try:
        if not service.is_configured:
            raise ManyChatError(NOT_CONFIGURED_HERE)
        subscriber_id = subscriber_id_for(service, contact)
        change(service, subscriber_id, _tags_of(service, subscriber_id))
    except ManyChatError as exc:
        logger.warning('wahub handoff for contact %s refused by ManyChat: %s', contact.pk, exc)
        raise HandoffError(failure_reason(
            exc, unknown='ManyChat לא ענה בזמן. לא ידוע אם השינוי נרשם שם — נסו שוב.',
        )) from exc


def takeover(contact: Contact, user) -> None:
    """A person answers from now on, and the bot keeps quiet."""
    def mute(service, subscriber_id, tags):
        _remove_tag(service, subscriber_id, end_tag(), tags)
        service.set_custom_field_by_name(subscriber_id, status_field(), status_value())
        # Last: the bot checks all three, and a rule in ManyChat fires on this tag.
        service.add_tag_by_name(subscriber_id, start_tag())

    _in_manychat(contact, mute)
    _set_handled_by(contact, HANDLED_HUMAN, user, 'נציג לקח את השיחה, הבוט הושתק')


OFFICE_ANSWERED = 'נציג ענה מתוך Kogo — השיחה אצל נציג, הבוט לא עונה בה'


def office_answered(contact: Contact, user) -> None:
    """
    Somebody of the office wrote to the customer from Kogo: the conversation is a person's from here on,
    and the bot keeps quiet in it (the owner, 14.10.2026, an hour after the switch: "ברגע שהמשרד התחיל
    לענות… זה עובר לטיפול נציג; לא הגיוני שהוא עדיין יישאר על הבוט"). Only Kogo's own state moves: the new
    bot reads Kogo, and ManyChat's tags only ever silenced the old bot, which no longer answers — so the
    office's message is not held up by three more calls to ManyChat. "החזר לבוט" gives the conversation back.
    """
    if contact.is_demo or contact.handled_by == HANDLED_HUMAN:
        return
    _set_handled_by(contact, HANDLED_HUMAN, user, OFFICE_ANSWERED)


def release(contact: Contact, user) -> None:
    """Back to the bot — the NEW one, for a live-test contact: its mute stays on, only the status field says so again."""
    if contact.live_test:
        _in_manychat(contact, lambda service, subscriber_id, tags: service.set_custom_field_by_name(
            subscriber_id, status_field(), live_test_status_value(),
        ))
    else:
        _in_manychat(contact, lambda service, subscriber_id, tags: _remove_tag(service, subscriber_id, start_tag(), tags))
    _set_handled_by(contact, HANDLED_BOT, user, 'השיחה הוחזרה לבוט')


# --- the live test (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב) ---------------------------------------------

def start_live_test(contact: Contact, user) -> None:
    """
    The old bot is muted for this contact in ManyChat (the same three things a
    takeover writes, with the live-test value in the status field) and the NEW
    bot answers it for real from now on (live_test.py). `handled_by` is not
    touched: a bot answers, not a person. Refused for a demo contact.
    """
    if contact.is_demo:
        raise HandoffError(LIVE_TEST_DEMO)
    if contact.live_test:
        return

    def mute(service, subscriber_id, tags):
        _remove_tag(service, subscriber_id, end_tag(), tags)
        service.set_custom_field_by_name(subscriber_id, status_field(), live_test_status_value())
        service.add_tag_by_name(subscriber_id, start_tag())

    _in_manychat(contact, mute, allowed=True)
    state.touch(contact.pk, live_test=True)
    state.log_event(contact.pk, EVENT_LIVE_TEST, 'בדיקה חיה הופעלה: הבוט הישן הושתק ב-ManyChat, הבוט החדש עונה לו באמת', actor=user)
    contact.live_test = True


def stop_live_test(contact: Contact, user) -> None:
    """
    The old bot answers again: the start tag comes off.

    A person who took the conversation over during the test keeps it only when
    the general switch is on — then the mute stays and the status field goes
    back to the takeover's value. With the switch off (as it is today) the
    office can neither write to this contact nor hand it back once the test is
    over, so leaving it muted would leave it with nobody: the conversation
    goes back to the bot together with the tag.

    The claim of an answer in progress is left alone: that answer looks at
    `live_test` again before it sends, and a test turned back on within the
    same seconds must not start a second answer beside it.
    """
    if not contact.live_test:
        return
    person_keeps_it = contact.handled_by == HANDLED_HUMAN and sending_enabled()
    if person_keeps_it:
        def change(service, subscriber_id, tags):
            service.set_custom_field_by_name(subscriber_id, status_field(), status_value())
        text = 'בדיקה חיה כובתה; נציג עדיין מטפל, הבוט הישן נשאר מושתק'
    else:
        def change(service, subscriber_id, tags):
            _remove_tag(service, subscriber_id, start_tag(), tags)
        text = 'בדיקה חיה כובתה: הבוט הישן עונה שוב'
    _in_manychat(contact, change, allowed=True)
    state.touch(contact.pk, live_test=False)
    state.log_event(contact.pk, EVENT_LIVE_TEST, text, actor=user)
    contact.live_test = False
    if contact.handled_by == HANDLED_HUMAN and not person_keeps_it:
        _set_handled_by(contact, HANDLED_BOT, user, 'השיחה הוחזרה לבוט עם כיבוי הבדיקה החיה')


def _set_handled_by(contact: Contact, value: str, user, text: str) -> None:
    if contact.handled_by == value:
        return
    state.touch(contact.pk, handled_by=value)
    state.log_event(contact.pk, EVENT_HANDLED_BY, text, actor=user)
    contact.handled_by = value
