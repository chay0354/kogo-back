"""The JSON of docs/WAHUB-CONTRACT.md, built from the rows. Every Hebrew label comes from here."""
from __future__ import annotations

import re

from django.utils import timezone
from rest_framework import serializers

from apps.wahub import state
from apps.wahub.models import (
    CUSTOMER_OUTCOMES,
    EVENT_KIND_CHOICES,
    FLAG_LABELS,
    FOLLOWUP_CHOICES,
    HANDLED_BY_CHOICES,
    HIDDEN_OUTCOMES,
    INTEREST_CHOICES,
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
from apps.wahub.queries import is_due
from apps.wahub.sending import can_free_text, window_closes_at

SOURCE_LABELS = dict(SOURCE_CHOICES)
HANDLED_BY_LABELS = dict(HANDLED_BY_CHOICES)
TOPIC_LABELS = dict(TOPIC_CHOICES)
INTEREST_LABELS = dict(INTEREST_CHOICES)
OUTCOME_LABELS = dict(OUTCOME_CHOICES)
FOLLOWUP_LABELS = dict(FOLLOWUP_CHOICES)
SENDER_LABELS = dict(SENDER_CHOICES)
EVENT_LABELS = dict(EVENT_KIND_CHOICES)
UNCHECKED_LABEL = 'עוד לא נבדק'

_COLOR = re.compile(r'#[0-9a-fA-F]{6}')


def _iso(moment):
    return moment.isoformat() if moment else None


def tag_payload(tag: Tag) -> dict:
    return {'id': tag.id, 'name': tag.name, 'color': tag.color}


def sorted_tags(tags) -> list:
    """By name, sorted here: the database's order for Hebrew depends on its collation."""
    return sorted(tags, key=lambda tag: (tag.name, tag.id))


def contact_payload(contact: Contact, *, now=None, today=None) -> dict:
    now = now or timezone.now()
    today = today or timezone.localtime(now).date()
    flags = [flag for flag in (contact.known_flags or []) if flag in FLAG_LABELS]
    last_message = None
    if contact.last_message_at and contact.last_message_direction:
        last_message = {
            'text': contact.last_message_text,
            'direction': contact.last_message_direction,
            'sender': contact.last_message_sender,
            'sent_at': _iso(contact.last_message_at),
        }
    return {
        'id': contact.id,
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'name': contact.name,
        'source': contact.source,
        'source_label': SOURCE_LABELS.get(contact.source, contact.source),
        'is_demo': contact.is_demo,
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
        },
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
        'tags': [tag_payload(tag) for tag in sorted_tags(contact.tags.all())],
    }


def message_payload(message: Message) -> dict:
    return {
        'id': message.id,
        'direction': message.direction,
        'sender': message.sender,
        'sender_label': SENDER_LABELS.get(message.sender, message.sender),
        'sender_name': (message.sender_name or None) if message.sender == SENDER_OFFICE else None,
        'text': message.text,
        'message_type': message.message_type,
        'media_url': message.media_url or None,
        'status': message.status,
        'error': message.error,
        'sent_at': _iso(message.sent_at),
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
