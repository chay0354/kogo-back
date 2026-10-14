"""The JSON of docs/WAHUB-CONTRACT.md, built from the rows. Every Hebrew label comes from here."""
from __future__ import annotations

import re

from django.db.models import F
from django.utils import timezone
from rest_framework import serializers

from apps.wahub import office_clock, optout, queries, search, state, waiting_list
from apps.wahub.models import (
    CUSTOMER_OUTCOMES,
    EVENT_KIND_CHOICES,
    FLAG_LABELS,
    FOLLOWUP_CHOICES,
    HANDLED_BY_CHOICES,
    HANDLED_HUMAN,
    HIDDEN_OUTCOMES,
    INTEREST_CHOICES,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    LEGACY_LEAD_STAGES,
    OUTCOME_CHOICES,
    SENDER_CHOICES,
    SENDER_OFFICE,
    SOURCE_CHOICES,
    TOPIC_CHOICES,
    Contact,
    ContactEvent,
    Message,
    QuickReply,
    Tag,
)
from apps.wahub.phones import phone_display
from apps.wahub.queries import days_since, is_due, is_hot, temperature
from apps.wahub.sending import can_free_text, window_closes_at

SOURCE_LABELS = dict(SOURCE_CHOICES)
HANDLED_BY_LABELS = dict(HANDLED_BY_CHOICES)
TOPIC_LABELS = dict(TOPIC_CHOICES)
INTEREST_LABELS = dict(INTEREST_CHOICES)
OUTCOME_LABELS = dict(OUTCOME_CHOICES)
FOLLOWUP_LABELS = dict(FOLLOWUP_CHOICES)
SENDER_LABELS = dict(SENDER_CHOICES)
EVENT_LABELS = dict(EVENT_KIND_CHOICES)
LEAD_STAGE_LABELS = dict(LEAD_STAGE_CHOICES)
UNCHECKED_LABEL = 'עוד לא נבדק'
# "What they asked" on the list of those who did not register: a line, not a conversation.
ASKED_CHARS = 160

_COLOR = re.compile(r'#[0-9a-fA-F]{6}')


def _iso(moment):
    return moment.isoformat() if moment else None


def tag_payload(tag: Tag) -> dict:
    return {'id': tag.id, 'name': tag.name, 'color': tag.color}


def sorted_tags(tags) -> list:
    """By name, sorted here: the database's order for Hebrew depends on its collation."""
    return sorted(tags, key=lambda tag: (tag.name, tag.id))


def lead_payload(contact: Contact, *, today) -> dict:
    """
    The lead stage as it is kept on the contact (apps/wahub/lead_stage.py) — the
    very values the board counts and the lists filter by. Until the first look
    the stage is '' and the sentences are empty; '' with a sentence is a
    conversation that is not about a class at all (no lead). `days` counts from
    `since`. `owner` is who makes the next move (docs/WAHUB-CONTRACT-LEADS-V2.md).
    """
    # A row the cron has not looked at since the stage got its present name reads like its heir.
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    return {
        'stage': stage,
        'stage_label': LEAD_STAGE_LABELS.get(stage, ''),
        'group': LEAD_STAGE_GROUPS.get(stage, ''),
        'since': _iso(contact.lead_stage_since),
        'days': days_since(contact.lead_stage_since, today),
        'reason': contact.lead_reason,
        'next_step': contact.lead_next_step,
        'human_touched': contact.lead_human_touched,
        'temperature': temperature(contact),
        'owner': contact.lead_owner,
    }


def future_payload(contact: Contact, *, today) -> dict | None:
    """
    The future lead of docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג: the office's "בזמן
    אחר" with its day — the reason, whose it is, and where it stands today
    (parked / due / wrote). None for a contact that is not one.
    """
    where = queries.future_state(contact, today)
    if not where:
        return None
    return {
        'due': contact.followup_due.isoformat(),
        'reason': contact.followup_note,
        'by_name': state.user_display_name(contact.followup_by) if contact.followup_by_id else None,
        'at': _iso(contact.followup_at),
        'state': where,
        'label': queries.FUTURE_LABEL,
        'days_left': (contact.followup_due - today).days,
        # Whether the bot keeps quiet for this customer now: a person holds the conversation.
        'muted': contact.handled_by == HANDLED_HUMAN,
    }


def note_payload(event: ContactEvent) -> dict:
    """A note a person wrote about the customer — a line of the journal, never a message."""
    return {
        'id': event.id,
        'text': event.text,
        'by_name': state.user_display_name(event.actor) if event.actor_id else None,
        'created_at': _iso(event.created_at),
        # A note a person rewrote (the same contract, ז.5); both empty for a note as it was first written.
        'edited_at': _iso(event.edited_at),
        'edited_by_name': state.user_display_name(event.edited_by) if event.edited_by_id else None,
    }


# A key that is there only when the list was searched: None is an answer ("found by what the row shows").
NOT_SEARCHED = object()


def history_note(contact: Contact, now=None) -> dict | None:
    """
    A conversation that began before the switch from the old bot (14.10.2026): what the old bot answered, and
    what the office wrote in ManyChat's inbox, never reached Kogo — only the customer's messages did. Said at
    the head of the conversation, with the way to read all of it in ManyChat, so that nobody answers it blind
    (the owner, that day: "שלא יהיה מצב… שאתחיל לענות לדברים בלי שאני יודע את ההיסטוריה"). None for a
    conversation that began after it, a demo, or the live test (the new bot answered that one all along).
    """
    from django.conf import settings

    from apps.wahub import lead_stage

    if contact.is_demo or contact.live_test:
        return None
    switched = lead_stage.switched_at(now)
    began = contact.first_inbound_at or contact.created_at
    if switched is None or began is None or began >= switched:
        return None
    subscriber = (contact.manychat_subscriber_id or '').strip()
    url = settings.WAHUB_MANYCHAT_CHAT_URL.format(subscriber_id=subscriber) if subscriber.isdigit() else ''
    return {'switched_at': _iso(switched), 'manychat_url': url}


def contact_payload(contact: Contact, *, now=None, today=None, found_by=NOT_SEARCHED, clock=None) -> dict:
    """
    One contact as every list, the live update and every action answer it.
    `clock` is the office's hours laid out for this request (office_clock.Clock);
    a caller that answers many rows builds one and passes it.
    """
    now = now or timezone.now()
    today = today or timezone.localtime(now).date()
    clock = clock or office_clock.Clock(now)
    open_reason = queries.open_reason(contact, today)
    box, box_label = queries.box_of(contact, today)
    flags = [flag for flag in (contact.known_flags or []) if flag in FLAG_LABELS]
    last_message = None
    if contact.last_message_at and contact.last_message_direction:
        last_message = {
            'text': contact.last_message_text,
            'direction': contact.last_message_direction,
            'sender': contact.last_message_sender,
            'sent_at': _iso(contact.last_message_at),
        }
    payload = {
        'id': contact.id,
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'name': contact.name,
        'source': contact.source,
        'source_label': SOURCE_LABELS.get(contact.source, contact.source),
        'is_demo': contact.is_demo,
        # The owner's own number under the live test: the NEW bot answers it for real (docs/WAHUB-CONTRACT-LIVE-TEST.md).
        'live_test': contact.live_test,
        # Began before the switch: the old bot's answers are not here, and where to read them (history_note).
        'history': history_note(contact, now),
        'first_inbound_at': _iso(contact.first_inbound_at),
        'last_inbound_at': _iso(contact.last_inbound_at),
        'last_message_at': _iso(contact.last_message_at),
        'last_message': last_message,
        'messages_count': contact.messages_count,
        'chat': {
            'unread_count': contact.unread_count,
            'waiting_since': _iso(contact.waiting_since),
            'handled_by': contact.handled_by,
            'handled_by_label': HANDLED_BY_LABELS.get(contact.handled_by, contact.handled_by),
            'needs_human': contact.needs_human,
            'needs_human_reason': contact.needs_human_reason,
            'needs_human_at': _iso(contact.needs_human_at),
            'can_free_text': can_free_text(contact, now),
            'window_closes_at': _iso(window_closes_at(contact)),
            # Whether a person has to deal with the conversation now, and why (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג.1).
            'open': bool(open_reason),
            'open_reason': open_reason,
            'open_label': queries.OPEN_LABELS[open_reason],
            # The box the conversation is shown under when a search crosses the boxes (the same contract, ח.1.2).
            'box': box,
            'box_label': box_label,
            # How long it has waited for a person, in the office's own hours, and whether that is three hours
            # or more — pinned to the head of the list, its mark blinking (the same contract, ט.2).
            **queries.waiting_block(contact, today, clock),
        },
        'future': future_payload(contact, today=today),
        # The customer asked not to get broadcast messages — "הסר" (the same contract, ט.1); null for everybody else.
        'opted_out': optout.payload(contact),
        'waiting_list': waiting_list.contact_block(contact),
        # The customer's card behind the contact — the family, the child, the address (the same contract, ח.2).
        'customer': search.customer_block(contact),
        'known': {
            'topic': contact.known_topic,
            'topic_label': TOPIC_LABELS.get(contact.known_topic, ''),
            'course_type': contact.known_course_type,
            'city': contact.known_city,
            'branch_id': str(contact.known_branch_id) if contact.known_branch_id else None,
            'branch_name': contact.known_branch_name,
            'child_age': contact.known_child_age,
            'interest': contact.known_interest,
            'interest_label': INTEREST_LABELS.get(contact.known_interest, ''),
            'callback_on': contact.known_callback_on.isoformat() if contact.known_callback_on else None,
            'flags': flags,
            'flag_labels': [FLAG_LABELS[flag] for flag in flags],
            'summary': contact.known_summary,
            'analyzed_at': _iso(contact.analyzed_at),
            'analysis_source': contact.analysis_source,
        },
        'kogo': {
            'outcome': contact.kogo_outcome,
            'outcome_label': OUTCOME_LABELS.get(contact.kogo_outcome, UNCHECKED_LABEL),
            'family_id': str(contact.kogo_family_id) if contact.kogo_family_id else None,
            'child_ids': [str(child_id) for child_id in (contact.kogo_child_ids or [])],
            'detail': contact.kogo_detail,
            'checked_at': _iso(contact.kogo_checked_at),
            'is_customer': contact.kogo_outcome in CUSTOMER_OUTCOMES,
            'hidden_by_default': contact.kogo_outcome in HIDDEN_OUTCOMES,
        },
        'followup': {
            'status': contact.followup_status,
            'status_label': FOLLOWUP_LABELS.get(contact.followup_status, ''),
            'due': contact.followup_due.isoformat() if contact.followup_due else None,
            'note': contact.followup_note,
            'by_name': state.user_display_name(contact.followup_by) if contact.followup_by_id else None,
            'at': _iso(contact.followup_at),
            'is_due': is_due(contact, today),
        },
        'lead': lead_payload(contact, today=today),
        'tags': [tag_payload(tag) for tag in sorted_tags(contact.tags.all())],
    }
    if found_by is not NOT_SEARCHED:
        # Why a search found the row, when it was not by what the row shows (the same contract, ח.1.1).
        payload['found_by'] = found_by
    return payload


def found_elsewhere(found, contacts, *, today=None) -> list:
    """
    `contacts` of a searched list that is not the list of conversations (the
    same contract, ח.1.4): whoever the search finds by name or by phone among
    `contacts` — the ones that are not on that list — a few of them, the best
    match first. Reads only.
    """
    today = today or state.now_israel_date()
    rows = list(
        found.apply(contacts, identity_only=True)
        .order_by('_search_rank', F('last_message_at').desc(nulls_last=True), '-id')[:search.OTHERS_SHOWN]
    )
    why = found.found_by(rows)
    shown = []
    for contact in rows:
        box, box_label = queries.box_of(contact, today)
        shown.append({
            'id': contact.id,
            'name': contact.name,
            'phone_display': phone_display(contact.phone),
            'is_demo': contact.is_demo,
            'box': box,
            'box_label': box_label,
            'found_by': why.get(contact.pk),
            'customer': search.customer_block(contact),
            'link': f'/wahub?tab=chats&contact={contact.id}',
        })
    return shown


def message_payload(message: Message) -> dict:
    return {
        'id': message.id,
        'direction': message.direction,
        'sender': message.sender,
        'sender_label': SENDER_LABELS.get(message.sender, message.sender),
        'sender_name': (message.sender_name or None) if message.sender == SENDER_OFFICE else None,
        'text': message.text,
        'message_type': message.message_type,
        # The object's path in the private bucket, not an address: the screen asks messages/{id}/media/ for one.
        'media_url': message.media_url or None,
        'has_media': bool(message.media_url),
        'transcript': message.transcript or '',
        # The NEW bot's answer: written into a demo conversation (never sent), or sent for real under the live test.
        'new_bot': message.new_bot,
        'status': message.status,
        'error': message.error,
        'sent_at': _iso(message.sent_at),
    }


def _short(text: str, limit: int) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()[:limit]


def unregistered_lead_payload(contact: Contact, *, today) -> dict:
    """One row of "שאלו ולא נרשמו" (docs/WAHUB-CONTRACT-STAGE3.md, א); `contact` comes from queries.unregistered_leads."""
    return {
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'is_demo': contact.is_demo,
        'first_inbound_at': _iso(contact.first_inbound_at),
        'last_inbound_at': _iso(contact.last_inbound_at),
        'days_since_first': days_since(contact.first_inbound_at, today),
        'days_since_last': days_since(contact.last_inbound_at, today),
        # The summary when there is one; until then, their last message.
        'asked': _short(contact.known_summary or getattr(contact, 'last_inbound_text', '') or '', ASKED_CHARS),
        'known_interest': contact.known_interest,
        'known_interest_label': INTEREST_LABELS.get(contact.known_interest, ''),
        'known_city': contact.known_city,
        'known_branch_name': contact.known_branch_name,
        'known_course_type': contact.known_course_type,
        'kogo_outcome': contact.kogo_outcome,
        'kogo_outcome_label': OUTCOME_LABELS.get(contact.kogo_outcome, UNCHECKED_LABEL),
        'kogo_detail': contact.kogo_detail,
        'followup_status': contact.followup_status,
        'followup_status_label': FOLLOWUP_LABELS.get(contact.followup_status, ''),
        'followup_due': contact.followup_due.isoformat() if contact.followup_due else None,
        'hot': is_hot(contact),
        'handled_by': contact.handled_by,
        'needs_human': contact.needs_human,
    }


def for_customer_payload(family, phones: list, contacts: list) -> dict:
    """The WhatsApp block of a customer's card (docs/WAHUB-CONTRACT-STAGE3.md, ב)."""
    checked = [contact.kogo_checked_at for contact in contacts if contact.kogo_checked_at]
    return {
        'family': str(family.id),
        'phones': phones,
        'contacts': [_for_customer_contact(contact, family.id) for contact in contacts],
        # When the registrations were last read for these contacts; "בדוק שוב" moves it to now.
        'checked_at': _iso(max(checked) if checked else None),
    }


def _for_customer_contact(contact: Contact, family_id) -> dict:
    return {
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'is_demo': contact.is_demo,
        'last_message_at': _iso(contact.last_message_at),
        'last_message_text': contact.last_message_text,
        'last_message_direction': contact.last_message_direction,
        'last_message_sender': contact.last_message_sender,
        'handled_by': contact.handled_by,
        'needs_human': contact.needs_human,
        'known_summary': contact.known_summary,
        'known_interest_label': INTEREST_LABELS.get(contact.known_interest, ''),
        'followup_status': contact.followup_status,
        'followup_status_label': FOLLOWUP_LABELS.get(contact.followup_status, ''),
        'followup_due': contact.followup_due.isoformat() if contact.followup_due else None,
        'kogo_outcome': contact.kogo_outcome,
        'kogo_outcome_label': OUTCOME_LABELS.get(contact.kogo_outcome, UNCHECKED_LABEL),
        # The matching put THIS family on the contact (two families may share a phone).
        'linked': contact.kogo_family_id == family_id,
        'link': f'/wahub?tab=chats&contact={contact.id}',
    }


def event_payload(event: ContactEvent) -> dict:
    return {
        'id': event.id,
        'kind': event.kind,
        'kind_label': EVENT_LABELS.get(event.kind, event.kind),
        'text': event.text,
        'actor_name': state.user_display_name(event.actor) if event.actor_id else None,
        'created_at': _iso(event.created_at),
    }


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ['id', 'name', 'color']

    def validate_name(self, value):
        value = ' '.join((value or '').split())
        if not value:
            raise serializers.ValidationError('נדרש שם לתגית')
        clash = Tag.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('כבר קיימת תגית בשם הזה')
        return value

    def validate_color(self, value):
        if not _COLOR.fullmatch(value or ''):
            raise serializers.ValidationError('צבע בצורה #RRGGBB')
        return value.lower()


class QuickReplySerializer(serializers.ModelSerializer):
    class Meta:
        model = QuickReply
        fields = ['id', 'title', 'text']

    def validate_title(self, value):
        value = ' '.join((value or '').split())
        if not value:
            raise serializers.ValidationError('נדרשת כותרת')
        return value

    def validate_text(self, value):
        if not (value or '').strip():
            raise serializers.ValidationError('נדרש נוסח')
        if len(value) > 4096:
            raise serializers.ValidationError('הנוסח ארוך מדי (עד 4,096 תווים)')
        return value
