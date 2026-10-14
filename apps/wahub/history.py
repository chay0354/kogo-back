"""
The conversation from before the switch, copied once from ManyChat (the owner, 14.10.2026: "תתחבר ותעלה את
השיחות האלה שיהיו פה במערכת, שנדע אחורה").

Until the switch (bot_mode.first_live_at) the old bot answered inside ManyChat, and the office wrote there too.
Kogo kept every customer's message, and for a while the old bot's answers as ManyChat copied them — never the
rest, and nothing at all from before it began keeping them. What Kogo did not keep is brought here once, from the
office's own session in ManyChat (a file the owner makes there and hands to the front's import page; nothing here
asks ManyChat), and kept apart (HistoryLine). It is read in its place among Kogo's own messages: on the screen, by
the bot that answers now, and by the summary that reads the lead. "כל השיחות שהיו" (the owner, that day): a
conversation of somebody Kogo never saw becomes a contact of its own (SOURCE_IMPORT), found by the phone.

Nothing copied is sent, waits for an answer, counts as unread or moves a lead's clock. The summary reads each
conversation again with its past a few at a time (cron, step 1c), behind every new message.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.wahub import bot_mode, state
from apps.wahub.models import (
    DIRECTION_IN, DIRECTION_OUT, EVENT_CREATED, MESSAGE_TYPE_CHOICES, SENDER_BOT, SENDER_CUSTOMER, SENDER_OFFICE,
    SENDER_SYSTEM, SOURCE_IMPORT, STATUS_FAILED, TYPE_TEXT, Contact, HistoryLine, Message,
)
from apps.wahub.phones import normalize_phone

# A call brings at most this much: the import page sends the conversations in small batches.
MAX_CONVERSATIONS = 25
MAX_LINES = 600
MAX_TEXT = 4000
# The same message, as Kogo kept it and as ManyChat shows it: same side, same words, this close in time.
SAME_LINE_WITHIN = timedelta(minutes=3)
# The copied lines the screen gets with a conversation — the latest of them.
LINES_SHOWN = 400
CREATED_NOTE = 'נוסף מהשיחות שהיו ב-ManyChat לפני המעבר'

SENDERS = {SENDER_CUSTOMER, SENDER_BOT, SENDER_OFFICE, SENDER_SYSTEM}
TYPES = {value for value, _label in MESSAGE_TYPE_CHOICES}


class NoSwitch(Exception):
    """The new bot never answered everybody: the old bot still does, and there is no "before" to copy yet."""


def _moment(raw):
    """An ISO time, or seconds / milliseconds since 1970 — what ManyChat's pages carry."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.isdigit():
            return _moment(int(text))
        moment = parse_datetime(text)
        if moment is not None and timezone.is_naive(moment):
            moment = timezone.make_aware(moment, dt_timezone.utc)
        return moment
    return None


def _words(text: str) -> str:
    return ' '.join((text or '').split())


def _line(raw) -> dict | None:
    """One line as the import page sends it — {id, at, from, text, type} — checked; None when it is not one."""
    if not isinstance(raw, dict):
        return None
    sender = raw.get('from')
    moment = _moment(raw.get('at'))
    if sender not in SENDERS or moment is None:
        return None
    kind = raw.get('type') if raw.get('type') in TYPES else TYPE_TEXT
    text = str(raw.get('text') or '').strip()[:MAX_TEXT]
    if not text and kind == TYPE_TEXT:
        return None
    ident = str(raw.get('id') or '').strip()
    if not ident:
        # No id on the event: the same moment, side and words are the same line.
        ident = 'h:' + hashlib.sha1(f'{moment.isoformat()}|{sender}|{text}'.encode()).hexdigest()[:32]
    return {
        'external_id': ident[:120], 'sent_at': moment, 'sender': sender, 'text': text, 'message_type': kind,
        'direction': DIRECTION_IN if sender == SENDER_CUSTOMER else DIRECTION_OUT,
    }


def _kept_by_kogo(contact: Contact, until) -> list[dict]:
    rows = Message.objects.filter(contact=contact, sent_at__lt=until + SAME_LINE_WITHIN).exclude(status=STATUS_FAILED)
    return [
        {'direction': direction, 'sent_at': sent_at, 'words': _words(text), 'message_type': kind}
        for direction, sent_at, text, kind in rows.values_list('direction', 'sent_at', 'text', 'message_type')
    ]


def _already_there(line: dict, kept: list[dict]) -> bool:
    """Kogo has this message itself; the copy it matched is used up, so two equal messages stay two."""
    words = _words(line['text'])
    for index, row in enumerate(kept):
        if row['direction'] != line['direction'] or abs(row['sent_at'] - line['sent_at']) > SAME_LINE_WITHIN:
            continue
        same = (words and row['words'] == words) or (
            line['message_type'] != TYPE_TEXT and row['message_type'] == line['message_type']
        )
        if same:
            del kept[index]
            return True
    return False


def _contacts_for(batch: list[dict]) -> tuple[dict, dict]:
    """The contacts Kogo has for a batch: by ManyChat's number for them, and by phone."""
    ids = {str(row.get('subscriber_id') or '').strip() for row in batch} - {''}
    phones = {normalize_phone(row.get('phone')) for row in batch} - {''}
    by_id, by_phone = {}, {}
    for contact in Contact.objects.filter(Q(manychat_subscriber_id__in=ids) | Q(phone__in=phones)):
        if contact.manychat_subscriber_id:
            by_id.setdefault(contact.manychat_subscriber_id, contact)
        by_phone[contact.phone] = contact
    return by_id, by_phone


def _new_contact(phone: str, subscriber: str, name: str) -> tuple[Contact, bool]:
    """Somebody Kogo never saw: a contact of its own, as quiet as the conversation was. Another door may have made it a moment ago."""
    try:
        with transaction.atomic():
            contact = Contact.objects.create(phone=phone, name=name, source=SOURCE_IMPORT, manychat_subscriber_id=subscriber)
            state.log_event(contact.pk, EVENT_CREATED, CREATED_NOTE)
            return contact, True
    except IntegrityError:
        return Contact.objects.get(phone=phone), False


def _moments(contact: Contact, lines: list[dict], own_messages: bool) -> dict:
    """
    What the lists read of a conversation, from the copied lines — only where Kogo has nothing of its own: the
    first and last time the customer wrote, and, for a conversation with no message in Kogo at all, its last line.
    """
    fields = {}
    theirs = [line['sent_at'] for line in lines if line['sender'] == SENDER_CUSTOMER]
    if theirs and (contact.first_inbound_at is None or min(theirs) < contact.first_inbound_at):
        fields['first_inbound_at'] = min(theirs)
    if theirs and contact.last_inbound_at is None:
        fields['last_inbound_at'] = max(theirs)
    if lines and not own_messages and contact.last_message_at is None:
        last = max(lines, key=lambda line: line['sent_at'])
        fields.update(
            last_message_at=last['sent_at'], last_message_text=state.preview(last['text']),
            last_message_direction=last['direction'], last_message_sender=last['sender'],
        )
    return fields


def import_conversations(conversations, *, now=None) -> dict:
    """
    [{subscriber_id, phone, name, messages: [{id, at, from: customer | bot | office | system, text, type}]}] → the
    counts.

    Only what came before the switch, and only what Kogo did not keep itself; brought twice, a line is kept once.
    The contact is the one Kogo has by ManyChat's number, else by phone; somebody Kogo never saw becomes a contact
    (an Israeli mobile only, like every contact), quiet: nothing waits, nothing is unread.
    """
    now = now or timezone.now()
    switched = bot_mode.first_live_at()
    if switched is None:
        raise NoSwitch()
    counts = {
        'conversations': 0, 'contacts': 0, 'created': 0, 'lines': 0, 'already_kept': 0, 'after_switch': 0,
        'no_phone': 0, 'empty': 0, 'bad': 0,
    }
    batch = [row for row in (conversations or [])[:MAX_CONVERSATIONS] if isinstance(row, dict)]
    by_id, by_phone = _contacts_for(batch)
    for row in batch:
        counts['conversations'] += 1
        subscriber = str(row.get('subscriber_id') or '').strip()[:40]
        phone = normalize_phone(row.get('phone'))
        lines = []
        for raw in (row.get('messages') or [])[:MAX_LINES]:
            line = _line(raw)
            if line is None:
                counts['bad'] += 1
            elif line['sent_at'] >= switched:
                counts['after_switch'] += 1
            else:
                lines.append(line)
        if not lines:
            counts['empty'] += 1
            continue
        contact = by_id.get(subscriber) or by_phone.get(phone)
        if contact is None:
            if not phone:
                counts['no_phone'] += 1
                continue
            contact, created = _new_contact(phone, subscriber, ' '.join(str(row.get('name') or '').split())[:200])
            counts['created'] += int(created)
            by_phone[phone] = contact
        if contact.is_demo:
            continue
        kept = _kept_by_kogo(contact, switched)
        own_messages = Message.objects.filter(contact=contact).exists()
        fresh = []
        for line in sorted(lines, key=lambda line: line['sent_at']):
            if _already_there(line, kept):
                counts['already_kept'] += 1
            else:
                fresh.append(line)
        with transaction.atomic():
            before = HistoryLine.objects.filter(contact=contact).count()
            HistoryLine.objects.bulk_create([HistoryLine(contact=contact, **line) for line in fresh], ignore_conflicts=True)
            added = HistoryLine.objects.filter(contact=contact).count() - before
            fields = {'history_copied_at': now, **_moments(contact, fresh, own_messages)}
            if subscriber.isdigit() and not contact.manychat_subscriber_id:
                fields['manychat_subscriber_id'] = subscriber
            if added:
                # Its past is here: the summary reads the conversation again with it, a few a call (cron 1c).
                fields['history_read_at'] = None
            state.touch(contact.pk, **fields)
        counts['contacts'] += 1
        counts['lines'] += added
    return counts


def still_to_copy(*, again: bool = False) -> dict:
    """
    The conversations to bring from ManyChat: every contact of a real customer that began before the switch and
    has ManyChat's number on record — the latest first — and none copied already, unless `again`.
    """
    switched = bot_mode.first_live_at()
    if switched is None:
        # Before the switch there is nothing "before" yet: nothing to bring, nothing brought.
        return {'switched_at': None, 'subscribers': [], 'copied': 0}
    rows = (
        Contact.objects.filter(is_demo=False, live_test=False, first_inbound_at__lt=switched)
        .exclude(manychat_subscriber_id='')
    )
    copied = Contact.objects.filter(history_copied_at__isnull=False).count()
    if not again:
        rows = rows.filter(history_copied_at__isnull=True)
    ids = [
        subscriber for subscriber in rows.order_by(F('last_inbound_at').desc(nulls_last=True), 'id')
        .values_list('manychat_subscriber_id', flat=True)
        if subscriber.strip().isdigit()
    ]
    return {'switched_at': switched.isoformat(), 'subscribers': ids, 'copied': copied}


def line_payload(line: HistoryLine) -> dict:
    return {
        'id': line.pk, 'sent_at': line.sent_at.isoformat(), 'direction': line.direction, 'sender': line.sender,
        'text': line.text, 'message_type': line.message_type,
    }


def lines_for(contact: Contact) -> list[dict]:
    """The copied lines the screen shows with the conversation, oldest first; none asked for before a copy."""
    if contact.history_copied_at is None:
        return []
    rows = list(HistoryLine.objects.filter(contact=contact).order_by('-sent_at', '-id')[:LINES_SHOWN])
    rows.reverse()
    return [line_payload(line) for line in rows]


def interleave(own: list, copied: list, at) -> list:
    """
    Kogo's own rows (in their order) with the copied ones (oldest first) put where they were written: each copied
    line just before the first row of Kogo's that came after it. `at` reads the moment of either.
    """
    merged, waiting = [], list(copied)
    for row in own:
        moment = at(row)
        while waiting and moment is not None and at(waiting[0]) < moment:
            merged.append(waiting.pop(0))
        merged.append(row)
    return merged + waiting


def read_beside(contact: Contact, own: list, *, shape, at, limit: int, full: bool, until=None) -> list:
    """
    The last `limit` lines of a conversation for a reader (the bot, the summary): Kogo's own rows, oldest first, with
    the copied lines in their place, each made a row of the reader's own by `shape`; `at` reads a row's moment.
    `full` says `own` was cut at `limit` — the copied lines from before its first row would then stand where Kogo's
    own earlier rows were left out. `until`: nothing written after it (an earlier message answered again).
    """
    if contact is None or contact.history_copied_at is None:
        return own
    rows = HistoryLine.objects.filter(contact=contact)
    if full and own and at(own[0]) is not None:
        rows = rows.filter(sent_at__gte=at(own[0]))
    if until is not None:
        rows = rows.filter(sent_at__lte=until)
    copied = list(rows.order_by('-sent_at', '-id')[:limit])
    copied.reverse()
    return interleave(own, [shape(line) for line in copied], at)[-limit:]
