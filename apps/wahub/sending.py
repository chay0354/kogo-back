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
from apps.wahub import lead_ai, optout, state
from apps.wahub.models import (
    DELIVERED_STATUSES,
    DIRECTION_OUT,
    MESSAGE_SOURCE_KOGO,
    SENDER_BOT,
    SENDER_OFFICE,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SIMULATED,
    TYPE_IMAGE,
    TYPE_OTHER,
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


SENDING_OFF = 'השליחה ללקוחות כבויה. מפעילים אותה בהגדרות המערכת (WAHUB_SENDING_ENABLED) כשמוכנים.'
DEMO_CONTACT = 'איש קשר דמו — לא שולחים אליו הודעות, גם כשהשליחה דולקת.'
# "הסר" (optout.py): a message the bot starts by itself never leaves for somebody who asked not to get any.
OPTED_OUT = 'הלקוח ביקש הסר — הודעה שהבוט יוזם לא נשלחת אליו.'


def sending_enabled(contact: Contact | None = None) -> bool:
    """
    May a message leave for this contact? The owner's switch, or simulation on
    a developer's machine. With a contact in hand two things come first
    (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב): a demo contact never gets anything,
    whatever the switch says; a live-test contact (the owner's own number, the
    NEW bot answering it for real) does, even while the switch is off. Without
    a contact this is the general switch, as the status screen shows it.
    """
    if contact is not None:
        if contact.is_demo:
            return False
        if contact.live_test:
            return True
    return bool(getattr(settings, 'WAHUB_SENDING_ENABLED', False)) or simulate_send()


def simulate_send() -> bool:
    """Whether sends are only recorded. Looked at on a developer's machine alone."""
    if not settings.DEBUG:
        return False
    return bool(getattr(settings, 'WAHUB_SIMULATE_SEND', False))


def manychat_reachable() -> bool:
    """
    Is there a ManyChat to send through at all: the server holds its key — or
    sends are only recorded here, and none is needed. Without one every send is
    a certain "not sent" (NOT_CONFIGURED), whatever the switch says.
    """
    return simulate_send() or ManyChatService().is_configured


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


def send_flow(contact: Contact, flow_ns: str, name: str, user, *,
              sender: str = SENDER_OFFICE, new_bot: bool = False) -> Message:
    """
    A template (ManyChat automation). Allowed outside the 24-hour window.
    "שלח תבנית" on the screen is this, as the office; the bot's own follow-up to
    a lead after the 24 hours (send_lead_followup_flow) is this very function
    too, kept as the bot's line — one way out for every template.
    """
    return _deliver(
        contact, user, text=name or flow_ns, message_type=TYPE_TEMPLATE,
        call=lambda service, subscriber_id: service.send_flow(subscriber_id, flow_ns),
        sender=sender, new_bot=new_bot,
    )


# What the list shows for a file the office sent without words (the inbound side says it the same way).
MEDIA_WORDS = {'image': '🖼️ תמונה', 'video': '🎬 סרטון', 'file': '📎 קובץ'}


def _refused(exc: ManyChatError) -> bool:
    """
    ManyChat answered, and said no (4xx): nothing was sent, so another shape
    of the same message cannot make a second copy. A timeout, no connection, a
    5xx and a contact ManyChat no longer has are not this.
    """
    status = getattr(exc, 'status_code', None)
    return isinstance(status, int) and 400 <= status < 500 and not _contact_gone(exc)


def media_link_text(kind: str, url: str, name: str = '') -> str:
    """The file as a link, for what WhatsApp takes no other way through ManyChat."""
    head = MEDIA_WORDS.get(kind, MEDIA_WORDS['file'])
    return f'{head}: {name}\n{url}' if name else f'{head}\n{url}'


def send_media(contact: Contact, *, path: str, kind: str, url: str, caption: str, user, name: str = '') -> Message:
    """
    A picture, a video or a document the office attached (docs/WAHUB-CONTRACT.md,
    "צירוף קבצים"): the file is already in the bucket at `path` (media.office_kind),
    and `url` is a signed address of it that ManyChat — and the customer — can open.

    ManyChat's reference says WhatsApp takes no video and no file through
    sendContent, and documents no caption on a picture. So: a picture leaves as
    a picture, and the words as a text message right after it, in the same call;
    a video or a document leaves as its link, with the words after it. A picture
    ManyChat refuses outright (4xx — nothing went out) leaves once more as its
    link. Same 24-hour rule as free text, same doors as every send (_deliver).
    Kept as one line: the caption as its text, the file as its media.
    """
    if not can_free_text(contact):
        raise WindowClosed()
    caption = (caption or '').strip()[:MAX_TEXT_CHARS]
    words = [{'type': 'text', 'text': caption}] if caption else []

    def call(service, subscriber_id):
        if kind == 'image':
            try:
                return service.send_whatsapp_messages(subscriber_id, [{'type': 'image', 'url': url}, *words])
            except ManyChatError as exc:
                if not _refused(exc):
                    raise
                logger.warning('wahub media to contact %s: ManyChat refused the picture (%s); sending its link', contact.pk, exc)
        return service.send_whatsapp_messages(subscriber_id, [{'type': 'text', 'text': media_link_text(kind, url, name)}, *words])

    return _deliver(
        contact, user, text=caption, message_type=TYPE_IMAGE if kind == 'image' else TYPE_OTHER, call=call,
        media_url=path, preview=caption or MEDIA_WORDS.get(kind, MEDIA_WORDS['file']),
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


def _deliver(contact: Contact, user, *, text: str, message_type: str, call,
             sender: str = SENDER_OFFICE, new_bot: bool = False, media_url: str = '', preview: str | None = None) -> Message:
    status, error = STATUS_SENT, ''
    if contact.is_demo:
        # An invented contact: the message is kept so the screen shows it, nothing leaves.
        status, error = STATUS_FAILED, DEMO_CONTACT
    elif simulate_send():
        status = STATUS_SIMULATED
    elif not sending_enabled(contact):
        # Kept, so the office sees the attempt and the reason; nothing leaves.
        status, error = STATUS_FAILED, SENDING_OFF
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
    return _keep_outbound(
        contact, user, text, message_type, status, error, sender=sender, new_bot=new_bot, media_url=media_url, preview=preview,
    )


def new_bot_answers(contact: Contact) -> bool:
    """
    Is this a contact the NEW bot may write to: a live-test contact, or — once
    the owner has chosen the mode "all" and sending is on — any contact but a
    demo (bot_mode.answered_live). The last look before a send; live_test.py
    asks the same question before it computes anything.
    """
    from apps.wahub import bot_mode

    return bot_mode.answered_live(contact)[0]


def send_new_bot_reply(contact: Contact, text: str) -> Message:
    """
    The NEW bot's answer, sent for real through ManyChat to a contact it
    answers (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב; docs/WAHUB-CONTRACT-GO-LIVE.md):
    the same path as the office's free text, kept with sender "bot" and
    `new_bot=True` and the status the send got (sent, or failed with the
    reason). Refuses every other contact — a demo never gets anything, and a
    real customer is answered by the old bot until the owner says otherwise
    (`new_bot_answers`).
    """
    if not new_bot_answers(contact):
        raise ValueError('the new bot sends only to a contact it answers')
    text = (text or '').strip()[:MAX_TEXT_CHARS]
    if not can_free_text(contact):
        return _keep_outbound(contact, None, text, TYPE_TEXT, STATUS_FAILED, WINDOW_CLOSED_DETAIL, sender=SENDER_BOT, new_bot=True)
    return _deliver(
        contact, None, text=text, message_type=TYPE_TEXT,
        call=lambda service, subscriber_id: service.send_whatsapp_text(subscriber_id, text),
        sender=SENDER_BOT, new_bot=True,
    )


def send_new_bot_flow(contact: Contact, flow_ns: str, name: str) -> Message:
    """
    An automation the NEW bot's answer sends after its text — the holidays
    board of a branch (holiday_flows.py) — to a contact it answers, through
    `send_flow`: the same way out as every template, with the same doors (a
    demo contact gets nothing, the switch or the contact's own live test
    decides, a developer's machine only records). Refuses every other contact,
    as `send_new_bot_reply` does. Kept as the bot's line, `new_bot=True`, with
    the status the send got; `name` is what the conversation shows.
    """
    if not new_bot_answers(contact):
        raise ValueError('the new bot sends only to a contact it answers')
    return send_flow(contact, flow_ns, (name or flow_ns).strip()[:MAX_TEXT_CHARS], None, sender=SENDER_BOT, new_bot=True)


def send_lead_followup(contact: Contact, text: str) -> Message:
    """
    The bot coming back to a lead by itself (lead_followups.py), as free text —
    inside the 24 hours WhatsApp allows after the customer's last message. The
    same path as every other send, so the same three doors: a demo contact gets
    nothing, the owner's switch (or the contact's own live test) decides, and a
    developer's machine only records. Kept with sender "bot" and `new_bot=True`:
    it is Kogo's own bot that wrote, not the vendor's.

    And a fourth door, before them: somebody who asked "הסר" gets nothing the
    bot starts. lead_followups.py never brings such a follow-up here (it reads
    the system's list right before); this is the same rule said once more at
    the way out, so that no other caller can pass it by.
    """
    text = (text or '').strip()[:MAX_TEXT_CHARS]
    if optout.is_marked(contact):
        return _keep_outbound(contact, None, text, TYPE_TEXT, STATUS_FAILED, OPTED_OUT, sender=SENDER_BOT, new_bot=True)
    if not can_free_text(contact):
        return _keep_outbound(contact, None, text, TYPE_TEXT, STATUS_FAILED, WINDOW_CLOSED_DETAIL, sender=SENDER_BOT, new_bot=True)
    return _deliver(
        contact, None, text=text, message_type=TYPE_TEXT,
        call=lambda service, subscriber_id: service.send_whatsapp_text(subscriber_id, text),
        sender=SENDER_BOT, new_bot=True,
    )


def send_lead_followup_flow(contact: Contact, flow_ns: str, text: str) -> Message:
    """
    The same, after the 24 hours: only an approved template may leave, and it
    leaves as the ManyChat flow the owner connected to this follow-up
    (settings_store.lead_templates) — through `send_flow`, the same function
    the screen's "שלח תבנית" goes through, with the same three doors. `text` is
    what the conversation shows — the wording that actually reaches the
    customer is the template's own. Never to somebody who asked "הסר", as above.
    """
    text = (text or '').strip()[:MAX_TEXT_CHARS]
    if optout.is_marked(contact):
        return _keep_outbound(contact, None, text, TYPE_TEMPLATE, STATUS_FAILED, OPTED_OUT, sender=SENDER_BOT, new_bot=True)
    return send_flow(contact, flow_ns, text, None, sender=SENDER_BOT, new_bot=True)


def keep_new_bot_reply(contact: Contact, text: str) -> Message:
    """
    The NEW bot's answer inside a DEMO conversation (docs/WAHUB-CONTRACT-REAL-DEMO.md, ב).

    The same simulation the screen already has — a message kept as `simulated`,
    nothing leaves — with sender "bot" and `new_bot=True` so the screen and the
    comparisons (shadow.old_bot_reply_for) tell it from the old bot's real
    replies. Refuses any contact that is not a demo: a real customer's
    conversation never gets an unsent line that looks like an answer.
    """
    if not contact.is_demo:
        raise ValueError('new bot replies are written into demo conversations only')
    return _keep_outbound(contact, None, text, TYPE_TEXT, STATUS_SIMULATED, '', sender=SENDER_BOT, new_bot=True)


def _without_quote(text: str) -> str:
    """
    A reply's own words, without the "> " lines it starts with (the screen's "השב"
    quotes the message it answers that way, as WhatsApp shows a quote): what the
    list's one-line preview shows. The text as it is when it is all quote.
    """
    lines = (text or '').split('\n')
    count = 0
    while count < len(lines) and (lines[count] == '>' or lines[count].startswith('> ')):
        count += 1
    rest = '\n'.join(lines[count:]).strip()
    return rest if count and rest else text


def _keep_outbound(contact: Contact, user, text: str, message_type: str, status: str, error: str,
                   *, sender: str = SENDER_OFFICE, new_bot: bool = False, media_url: str = '',
                   preview: str | None = None) -> Message:
    """
    The line in the conversation. `media_url` is the path of a file the office
    attached (media.office_kind); `preview` is what the list shows for it when
    it carries no words of its own.
    """
    now = timezone.now()
    with transaction.atomic():
        message = Message.objects.create(
            contact=contact,
            direction=DIRECTION_OUT,
            sender=sender,
            sender_name=state.user_display_name(user)[:150],
            sent_by=user if getattr(user, 'is_authenticated', False) else None,
            text=text,
            message_type=message_type,
            media_url=media_url,
            new_bot=new_bot,
            status=status,
            error=error[:300],
            sent_at=now,
            source=MESSAGE_SOURCE_KOGO,
        )
        changes = {'messages_count': F('messages_count') + 1}
        if status in DELIVERED_STATUSES:
            # Somebody answered: nobody is waiting.
            changes.update(
                last_message_at=now,
                last_message_text=state.preview(_without_quote(text) if preview is None else preview),
                last_message_direction=DIRECTION_OUT,
                last_message_sender=sender,
                waiting_since=None,
            )
            if sender == SENDER_OFFICE:
                # The office answered, so it has read the conversation; a bot's answer reads nothing.
                changes['unread_count'] = 0
            if message_type == TYPE_TEXT and lead_ai.has_registration_link(text):
                # "קיבל קישור ועוד לא נרשם" starts here (lead_stage.py).
                changes['lead_link_sent_at'] = now
        state.touch(contact.pk, **changes)
    return message
