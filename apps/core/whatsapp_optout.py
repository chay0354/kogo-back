"""
"הסר" — the phones that asked not to get broadcast messages on WhatsApp.

One list for the whole system (the owner, 13.10.2026: "אם מישהו אמר הסר —
תוסיף אותו לרשימה שהמערכת יודעת לא לשלוח הודעות תפוצה"). It lives here, in
core, because whoever sends a broadcast reads it — the customers list, the
municipality students, the WhatsApp page of the settings, the bot's own
follow-ups — and core is the one place they all already lean on. The WhatsApp
section (apps/wahub/optout.py) is what writes to it: a customer's message, or
a person's mark on a contact.

What the list stops is a message the business starts by itself to many people.
It does not stop a service message about the customer's own registration,
payment or trial lesson, nor an answer inside a conversation the customer
opened (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.1.5 has the table).

A broadcast that cannot read the list does not go out at all
(`OptOutListUnavailable`): a message that reached somebody who asked "הסר"
cannot be taken back, a broadcast that did not leave is simply sent again.

Nothing here talks to ManyChat.
"""
from __future__ import annotations

import logging

from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from apps.core.manychat_service import ManyChatService

logger = logging.getLogger(__name__)

# What a row of a broadcast says when its phone is on the list.
REASON = 'opted_out'
UNAVAILABLE = 'לא ניתן לקרוא עכשיו את רשימת "הסר", ולכן שום הודעה לא נשלחה. נסו שוב בעוד רגע.'
UNAVAILABLE_CODE = 'opt_out_list_unavailable'


class OptOutListUnavailable(Exception):
    """The list could not be read or written. Nothing was sent and nothing was changed."""

    def __init__(self, detail: str = UNAVAILABLE):
        super().__init__(detail)
        self.detail = detail
        self.code = UNAVAILABLE_CODE


def phone_key(raw) -> str:
    """One spelling for a phone, the one every broadcast already compares by: 9725XXXXXXXX. '' for no phone."""
    return ManyChatService.normalize_phone_e164(str(raw or '').strip())


def _rows():
    from apps.core.models import WhatsAppOptOut

    return WhatsAppOptOut.objects.filter(restored_at__isnull=True)


def blocked_phones() -> frozenset:
    """
    Every phone on the list right now, in one query — what a broadcast reads
    once, before its first message. Raises OptOutListUnavailable when the list
    cannot be read; the caller then sends nothing.
    """
    try:
        return frozenset(_rows().values_list('phone', flat=True))
    except DatabaseError as exc:
        logger.exception('whatsapp opt-out: the list could not be read')
        raise OptOutListUnavailable() from exc


def is_blocked(phone) -> bool:
    """Is this one phone on the list. Raises OptOutListUnavailable when the list cannot be read."""
    key = phone_key(phone)
    if not key:
        return False
    try:
        return _rows().filter(phone=key).exists()
    except DatabaseError as exc:
        logger.exception('whatsapp opt-out: the list could not be read')
        raise OptOutListUnavailable() from exc


def standing(phone):
    """The row that keeps this phone on the list, or None."""
    key = phone_key(phone)
    return _rows().filter(phone=key).first() if key else None


def add(phone, *, source: str, text: str = '', by=None, wahub_contact_id=None, wahub_message_id=None, now=None):
    """
    Put a phone on the list: (the row, whether this call made it). A phone that
    is already there keeps the row it has — who asked first, and when. Raises
    OptOutListUnavailable when the list cannot be written, and ValueError for
    something that is no phone.
    """
    from apps.core.models import WhatsAppOptOut

    key = phone_key(phone)
    if not key:
        raise ValueError('a phone is needed to put on the opt-out list')
    person = by if getattr(by, 'is_authenticated', False) else None
    try:
        row = _rows().filter(phone=key).first()
        if row is not None:
            return row, False
        try:
            # A savepoint: two messages of one customer arriving together must not break the caller's transaction.
            with transaction.atomic():
                row = WhatsAppOptOut.objects.create(
                    phone=key, source=source, text=(text or '')[:WhatsAppOptOut._meta.get_field('text').max_length],
                    opted_out_at=now or timezone.now(), opted_out_by=person,
                    wahub_contact_id=wahub_contact_id, wahub_message_id=wahub_message_id,
                )
            return row, True
        except IntegrityError:
            row = _rows().filter(phone=key).first()
            if row is None:
                raise
            return row, False
    except DatabaseError as exc:
        logger.exception('whatsapp opt-out: the list could not be written')
        raise OptOutListUnavailable() from exc


def restore(phone, *, by=None, now=None) -> bool:
    """
    "ביקש לחזור לתפוצה": the phone is off the list from now on. The row stays,
    with when it was given back and by whom. True when this call took it off.
    Raises OptOutListUnavailable when the list cannot be written.
    """
    key = phone_key(phone)
    if not key:
        return False
    person = by if getattr(by, 'is_authenticated', False) else None
    try:
        return bool(_rows().filter(phone=key).update(restored_at=now or timezone.now(), restored_by=person))
    except DatabaseError as exc:
        logger.exception('whatsapp opt-out: the list could not be written')
        raise OptOutListUnavailable() from exc
