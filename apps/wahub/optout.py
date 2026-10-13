"""
"הסר" in the WhatsApp section (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.1).

The list itself is the whole system's and lives in core
(apps/core/whatsapp_optout.py). Here is what this section does with it:

  * reads a customer's incoming message and decides, by rules alone, whether
    it is a request to be taken off the broadcast list (`is_request`) — at the
    door every message comes through (inbound.py), so it works whichever bot
    answers the customer, or none;
  * puts the phone on the list and says so on the contact — `opted_out`, a
    copy of the list's row that the lists, the lead stage and the screens read
    with no other query (as `lead_waiting` is a copy of the waiting list) —
    and takes both off again when a person says the customer asked to come back;
  * answers, for whatever in this section starts a message by itself, whether
    it may leave for this contact (`blocked`).

The words that count as a request are all in this file. The reading is careful
on purpose: a request has to be the whole message, or a whole sentence of a
short one. "אפשר להסיר את הילד מהחוג?" is a cancellation and stays with the
bot and the office; a long message the word merely appears in is not caught.
A message the rules miss costs a broadcast to somebody who did not want it —
and the office can always mark the contact by hand.

A demo contact is marked on the contact alone: its invented number is never
put on the list the real broadcasts read.

Nothing here talks to ManyChat, and nothing here sends.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from django.db import transaction
from django.utils import timezone

from apps.core import whatsapp_optout as optout_list
from apps.core.whatsapp_optout import OptOutListUnavailable
from apps.wahub import state
from apps.wahub.models import EVENT_OPT_OUT, Contact

logger = logging.getLogger(__name__)

SOURCE_MESSAGE = 'message'
SOURCE_OFFICE = 'office'
SOURCE_LABELS = {SOURCE_MESSAGE: 'ביקש בהודעה', SOURCE_OFFICE: 'סומן על ידי נציג'}
LABEL = 'ביקש הסר'
# What the lead and the journal say of a follow-up that did not leave because of it.
NOT_SENT = 'ביקש הסר'
HOLD = 'ביקש הסר — הבוט לא חוזר אליו'
TEXT_SHOWN = 160

# --- the words --------------------------------------------------------------------------------------------

# Longer than this, a message is not read for a sentence that asks; and a bare word is a sentence of a shorter one.
MAX_WORDS = 12
MAX_WORDS_BARE_SENTENCE = 6

# Politeness around the request, dropped before it is read.
_FILLERS = frozenset({
    'בבקשה', 'תודה', 'רבה', 'מראש', 'שלום', 'היי', 'הי', 'נא', 'אנא', 'please', 'pls', 'plz', 'thanks', 'thank', 'you', 'hi', 'hello',
})

# The whole message is this, and nothing else.
_BARE = re.compile(r'(?:הסר|הסירו|הסרה|להסיר|תסירו|תסיר)(?: אותי| אותנו)?|(?:stop|unsubscribe|remove)(?: me)?')
# One sentence of a short message is this.
_BARE_SENTENCE = re.compile(r'(?:הסר|הסירו)(?: אותי| אותנו)?|(?:stop|unsubscribe)(?: me)?')

_WANT = r'(?:(?:אני |אנחנו )?(?:מבקש|מבקשת|מבקשים|רוצה|רוצים|אבקש|אפשר|ניתן|תוכלו) )?'
_TAKE_OFF = r'(?:להסיר|הסירו|תסירו|תסיר|הסר|הסרה|להוריד|תורידו|תוריד|הורידו|למחוק|תמחקו|מחקו)'
_WHOM = r'(?: אותי| אותנו| את המספר(?: שלי| הזה)?| את הטלפון(?: שלי)?)?'
_FROM_THE_LIST = r' מ?ה?(?:רשימת |רשימה של |קבוצת )?ה?(?:תפוצה|דיוור|רשימה|הודעות|פרסומות|פרסום|ספאם)(?: שלכם| הזאת| הזו)?'
_MESSAGES = r'(?:הודעות|פרסומות|פרסום|דיוור|ספאם|סמסים)'
_THESE = r'(?: האלה| האלו| הללו)?'
# A whole sentence that asks, in so many words, not to be written to any more.
_EXPLICIT = re.compile('|'.join((
    # "הסירו אותי מרשימת התפוצה", "להסיר אותי מהרשימה", "הסרה מהתפוצה"
    _WANT + _TAKE_OFF + _WHOM + _FROM_THE_LIST,
    # "מבקשת הסרה", "אבקש להסיר אותי"
    r'(?:אני |אנחנו )?(?:מבקש|מבקשת|מבקשים|אבקש) (?:הסרה|להסיר אותי|להסיר אותנו|שתסירו אותי|שתסירו אותנו)',
    # "תפסיקו לשלוח לי הודעות", "להפסיק לשלוח לי"
    r'(?:תפסיקו|תפסיק|הפסיקו|להפסיק|די|מספיק)(?: כבר)? (?:לשלוח|לשלח)(?: לי| לנו)?(?: כבר| יותר| עוד)?(?: (?:את )?ה?' + _MESSAGES + _THESE + ')?',
    # "די עם ההודעות"
    r'(?:די|מספיק|תפסיקו|תפסיק)(?: כבר)? עם ה?' + _MESSAGES + _THESE,
    # "אל תשלחו לי יותר", "לא לשלוח לי יותר הודעות"
    r'(?:אל|לא|בלי) (?:תשלחו|תשלח|לשלוח)(?: לי| לנו)(?: יותר| עוד)?(?: (?:שום )?(?:' + _MESSAGES + r'|הודעה|כלום|דבר))?(?: יותר| עוד)?',
    # "לא מעוניין לקבל הודעות"
    r'(?:אני |אנחנו )?לא (?:מעוניין|מעוניינת|מעוניינים|רוצה|רוצים) (?:לקבל|בקבלת)(?: יותר| עוד)?(?: מכם)? (?:' + _MESSAGES + r'|תפוצה)'
    + r'(?: מכם| נוספות| יותר| עוד)*',
    r'(?:stop|unsubscribe|remove)(?: me)? from (?:this |the |your )?(?:mailing )?(?:list|group|broadcast)',
    r'stop (?:sending|messaging|texting)(?: me)?(?: (?:these |any )?messages)?',
    r"(?:do not|don't|dont) (?:send|message|text) me(?: anymore| again| any more| messages| any messages)*",
    r'take me off(?: (?:this|the|your) list)?',
)))

_MARKS = re.compile(r'[֑-ׇ​-‏‪-‮⁦-⁩﻿]')
_SENTENCE_END = re.compile(r'[.!?,;:\n\r…|()\[\]"“”״]+|\s[-–—]+\s')
_NOT_A_WORD = re.compile(r"[^0-9a-zא-ת' ]+")


def _sentences(text: str) -> list[list[str]]:
    """The message as sentences of plain words: no marks, no emoji, lower case, the apostrophe of "don't" kept."""
    text = _MARKS.sub('', unicodedata.normalize('NFKC', str(text or ''))).lower().replace('’', "'").replace('׳', "'")
    found = []
    for part in _SENTENCE_END.split(text):
        words = _NOT_A_WORD.sub(' ', part).split()
        if words:
            found.append(words)
    return found


def _said(words: list[str]) -> str:
    return ' '.join(word for word in words if word not in _FILLERS)


def is_request(text: str) -> bool:
    """
    Is this incoming message a request to be taken off the broadcast list?
    Rules alone, the same answer every time. See the module's opening for what
    is caught and what is deliberately not.
    """
    sentences = _sentences(text)
    total = sum(len(words) for words in sentences)
    if not total or total > MAX_WORDS * 3:
        return False
    whole = _said([word for words in sentences for word in words])
    if _BARE.fullmatch(whole):
        return True
    if total > MAX_WORDS:
        return False
    for words in sentences:
        said = _said(words)
        if not said:
            continue
        if _EXPLICIT.fullmatch(said):
            return True
        if total <= MAX_WORDS_BARE_SENTENCE and _BARE_SENTENCE.fullmatch(said):
            return True
    return False


# --- the mark on the contact ------------------------------------------------------------------------------

def payload(contact: Contact) -> dict | None:
    """`opted_out` as the screens get it; None for a contact that is not on the list."""
    kept = contact.opted_out if isinstance(contact.opted_out, dict) else {}
    if not kept.get('at'):
        return None
    source = kept.get('source') if kept.get('source') in SOURCE_LABELS else SOURCE_MESSAGE
    return {
        'at': kept['at'],
        'source': source,
        'source_label': SOURCE_LABELS[source],
        'by_name': (kept.get('by_name') or None) if source == SOURCE_OFFICE else None,
        'text': str(kept.get('text') or '')[:TEXT_SHOWN] if source == SOURCE_MESSAGE else '',
        'label': LABEL,
    }


def is_marked(contact: Contact) -> bool:
    return bool(isinstance(contact.opted_out, dict) and contact.opted_out.get('at'))


def _copy(*, source: str, at, text: str = '', user=None) -> dict:
    return {
        'at': at.isoformat(),
        'source': source,
        'text': state.preview(text)[:TEXT_SHOWN] if source == SOURCE_MESSAGE else '',
        'by': getattr(user, 'pk', None) if source == SOURCE_OFFICE else None,
        'by_name': state.user_display_name(user) if source == SOURCE_OFFICE and user is not None else '',
    }


def _onto_the_list(contact: Contact, *, source: str, text: str, user, message_id, now):
    """The system's list — never for a demo contact, whose number somebody made up."""
    if contact.is_demo:
        return
    # In a savepoint of its own: a fault of the list must not undo what the caller is in the middle of.
    with transaction.atomic():
        optout_list.add(
            contact.phone, source=source, text=state.preview(text), by=user,
            wahub_contact_id=contact.pk, wahub_message_id=message_id, now=now,
        )


def from_message(contact: Contact, text: str, message_id, now) -> dict | None:
    """
    The customer wrote "הסר" (inbound.py found that `is_request`): the phone
    goes on the list, the journal says so, and the copy for the contact comes
    back for the caller to write with the rest of the message's changes. None —
    and nothing written — for a contact already marked, or when the list could
    not be written (said in the log; the message itself is kept as any other).
    Never raises.
    """
    if is_marked(contact):
        return None
    try:
        _onto_the_list(contact, source=SOURCE_MESSAGE, text=text, user=None, message_id=message_id, now=now)
        state.log_event(contact.pk, EVENT_OPT_OUT, f'ביקש הסר: "{state.preview(text)[:200]}"')
    except Exception:
        logger.exception('wahub opt-out: the request of contact %s was not kept', contact.pk)
        return None
    return _copy(source=SOURCE_MESSAGE, at=now, text=text)


def opt_out(contact: Contact, user) -> bool:
    """
    POST contacts/{id}/opt-out/ — a person marks that the customer asked not to
    get broadcasts. True when this call marked it; a contact already marked is
    left as it is. Raises OptOutListUnavailable with nothing changed.
    """
    now = timezone.now()
    with transaction.atomic():
        held = Contact.objects.select_for_update().get(pk=contact.pk)
        if is_marked(held):
            return False
        _onto_the_list(held, source=SOURCE_OFFICE, text='', user=user, message_id=None, now=now)
        state.touch(held.pk, opted_out=_copy(source=SOURCE_OFFICE, at=now, user=user))
        state.log_event(held.pk, EVENT_OPT_OUT, 'סומן "הסר" על ידי נציג — לא יקבל הודעות תפוצה', actor=user)
    return True


def opt_in(contact: Contact, user) -> bool:
    """
    POST contacts/{id}/opt-in/ — "ביקש לחזור לתפוצה". The phone comes off the
    list (its row stays, with who gave it back and when) and the mark comes off
    the contact. True when something was taken off. Raises OptOutListUnavailable
    with nothing changed.
    """
    now = timezone.now()
    with transaction.atomic():
        held = Contact.objects.select_for_update().get(pk=contact.pk)
        listed = False
        if not held.is_demo:
            with transaction.atomic():
                listed = optout_list.restore(held.phone, by=user, now=now)
        if not is_marked(held) and not listed:
            return False
        state.touch(held.pk, opted_out={})
        state.log_event(held.pk, EVENT_OPT_OUT, 'הוחזר לתפוצה — ביקש לחזור לקבל הודעות', actor=user)
    return True


def blocked(contact: Contact) -> bool:
    """
    May nothing that starts by itself leave for this contact — the bot's own
    follow-up to a lead, the telling that a seat came free? The mark on the
    contact, and the list itself, read this minute: a phone put on the list by
    another part of the system counts too. Raises OptOutListUnavailable when
    the list cannot be read; the caller then sends nothing.
    """
    if is_marked(contact):
        return True
    if contact.is_demo or not optout_list.is_blocked(contact.phone):
        return False
    _adopt(contact)
    return True


def _adopt(contact: Contact) -> None:
    """
    The phone is on the list and the contact does not say so — another part of
    the system put it there. The contact takes the list's row as its mark, so
    the lead rules and the screens see it from here on. Never raises: the
    answer of `blocked` does not depend on it.
    """
    try:
        row = optout_list.standing(contact.phone)
        if row is None:
            return
        source = SOURCE_OFFICE if row.source == SOURCE_OFFICE else SOURCE_MESSAGE
        copy = _copy(source=source, at=row.opted_out_at, text=row.text, user=row.opted_out_by)
        state.touch(contact.pk, opted_out=copy)
        contact.opted_out = copy
    except Exception:
        logger.exception('wahub opt-out: the mark of contact %s was not brought from the list', contact.pk)
