"""
What a person does with a conversation once they have dealt with it
(docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג): the ✓ that closes it, and the clock
with its two answers — "the bot carries on" and "a future lead of mine" — and
the notes a person writes about the customer while talking to them.

The owner, 13.10.2026: "she may have understood that the child will only be
three in two months. I want her to have a way to close a conversation — there
is a V — and there is a clock. If she presses the clock, two options: either
hand it to the bot to deal with later, and the bot understands by itself what
the conversation was about… or she marks it a future lead of HERS, and picks a
date and a reason. When the date comes the chat jumps up for her that day as an
open conversation to deal with, and it says 'future lead'."

Nothing new is kept for any of it. A future lead is the office's own "בזמן אחר"
mark with its day (`followup_*`: the day, the reason, whose it is) on a
conversation a person holds, so no bot answers the customer meanwhile. Closing
is the mark "ענה" — a person dealt with whatever was written until now — with
the conversation handed back to the bot. What the lists and the lead stage make
of those marks is in queries.py and lead_stage.py.

Who answers is changed through handoff.py only, which writes the mute in
ManyChat first. Handing a conversation BACK to the bot must succeed there, or
nothing changes here — a muted conversation that says "the bot answers" is a
customer nobody answers. Taking it for a future lead is tried, and a refusal
does not undo the lead: the date and the reason are the office's own, and the
answer says plainly that the bot was not muted.

Nothing here sends anything to anybody.
"""
from __future__ import annotations

import copy
import logging
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.wahub import analysis, handoff, lead_followups, lead_stage, leads, queries, sending, state
from apps.wahub.models import (
    EVENT_CLOSED,
    EVENT_FOLLOWUP,
    EVENT_LEAD_ACTION,
    EVENT_NOTE,
    EVENT_NOTE_CHANGED,
    FOLLOWUP_ANSWERED,
    FOLLOWUP_LATER,
    FOLLOWUP_NOT_RELEVANT,
    FOLLOWUP_REGISTERED,
    HANDLED_BOT,
    HANDLED_HUMAN,
    LEAD_GROUP_CLOSED,
    LEAD_OWNER_BOT,
    LEAD_OWNER_OFFICE,
    LEAD_WAITING_LIST,
    NUDGE_DATE_ARRIVED,
    NUDGE_OPEN_STATUSES,
    Contact,
    ContactEvent,
    LeadFollowUp,
)
from apps.wahub.serializers import note_payload

logger = logging.getLogger(__name__)

REASON_CHARS = 300
NOTE_CHARS = ContactEvent._meta.get_field('text').max_length
NOTES_IN_CONVERSATION = 50
NOTES_LISTED = 100
# How long the reading of the conversation may take when a person hands it to the bot.
UNDERSTAND_SECONDS = 20
# The marks that close a lead; closing the conversation does not write over them.
CLOSING_MARKS = (FOLLOWUP_REGISTERED, FOLLOWUP_NOT_RELEVANT)

CLOSED_LINE = 'השיחה נסגרה'
NO_STEP = (
    'הבוט לא מצא בשיחה מתי ועל מה לחזור. אפשר לכתוב פתק קצר (למשל "הילד יהיה בן 3 בדצמבר") ולנסות שוב, '
    'או לבחור "ליד עתידי שלי" עם תאריך.'
)
NOT_A_LEAD = 'השיחה הזאת אינה ליד לחוגים — אין לבוט על מה לחזור.'
NO_WORDS = 'לבוט אין נוסח פעיל לחזרה מהסוג הזה — אין מה לתזמן.'
OFFICE_ONLY = 'זה ליד שרק אדם יכול לקדם'
MUTE_FAILED = 'תקלה לא צפויה בהשתקת הבוט. לא ידוע אם הבוט הושתק — אפשר לנסות "לקחת שיחה".'
ON_A_WAITING_LIST = 'הלקוח ברשימת המתנה — המערכת תתריע כשיתפנה מקום. אין לבוט מה לתזמן מעבר לזה.'
NOTE_EDITED = 'פתק נערך'
NOTE_DELETED = 'פתק נמחק'


class FutureLeadError(ValueError):
    """An action refused, with the reason in Hebrew."""


class NoteNotFound(LookupError):
    """No such note on this contact — also for a line of the journal that is no note."""


def _person(user):
    return user if getattr(user, 'is_authenticated', False) else None


def _day_words(day: date) -> str:
    return f'{day.day}.{day.month}.{day.year}'


def _held_marks(contact: Contact, user, now) -> dict:
    """
    The fields that say "a person has dealt with what was written until now":
    nobody is asked for any more, and the mark is "ענה" — unless a mark that
    closes the lead already stands. A future lead ends with it.
    """
    fields = dict(needs_human=False, needs_human_reason='', needs_human_at=None)
    if contact.followup_status not in CLOSING_MARKS:
        fields.update(followup_status=FOLLOWUP_ANSWERED, followup_due=None, followup_by=_person(user), followup_at=now)
    return fields


def _without_pin(contact: Contact) -> dict | None:
    """`lead_manual` without "קח אליי"; None when there was no pin to take off."""
    manual = dict(contact.lead_manual) if isinstance(contact.lead_manual, dict) else {}
    return manual if manual.pop('owner', None) is not None else None


# --- ✓ ---------------------------------------------------------------------------------------------------

def close(contact: Contact, user) -> Contact:
    """
    "השיחה נסגרה": out of the list of open conversations, and back to the bot.
    Raises handoff.HandoffError — with nothing changed — when a person holds the
    conversation and ManyChat did not take it back.
    """
    if contact.handled_by == HANDLED_HUMAN:
        handoff.release(contact, user)
    now = timezone.now()
    with transaction.atomic():
        row = Contact.objects.select_for_update().get(pk=contact.pk)
        was_future = bool(queries.future_state(row, timezone.localtime(now).date()))
        fields = {**_held_marks(row, user, now), 'unread_count': 0}
        manual = _without_pin(row)
        if manual is not None:
            fields['lead_manual'] = manual
        state.touch(row.pk, **fields)
        state.log_event(row.pk, EVENT_CLOSED, f'{CLOSED_LINE} · הליד העתידי נסגר' if was_future else CLOSED_LINE, actor=user)
    lead_stage.refresh_quietly(contact.pk)
    return Contact.objects.get(pk=contact.pk)


# --- the clock: a future lead of mine --------------------------------------------------------------------

def set_future(contact: Contact, due, reason, user) -> tuple[Contact, dict]:
    """
    "ליד עתידי שלי": a day and a reason, and nothing more. Kept as the office's
    "בזמן אחר" with its day; the conversation is taken by a person so that no
    bot answers meanwhile. Returns (the contact, {muted, detail}) — the lead is
    kept also when the mute was refused, and `detail` then says why.
    """
    try:
        day = due if isinstance(due, date) else date.fromisoformat(str(due or '').strip())
    except ValueError:
        raise FutureLeadError('נדרש תאריך (due) בצורה YYYY-MM-DD.')
    today = state.now_israel_date()
    if day <= today:
        raise FutureLeadError('המועד צריך להיות אחרי היום.')
    reason = ' '.join(str(reason or '').split())
    if len(reason) > REASON_CHARS:
        raise FutureLeadError(f'הסיבה ארוכה מדי (עד {REASON_CHARS} תווים).')
    already = bool(queries.future_state(contact, today))
    if not reason and not already:
        raise FutureLeadError('נדרשת סיבה קצרה (reason) — שיהיה קל לזכור למה חוזרים.')

    now = timezone.now()
    with transaction.atomic():
        row = Contact.objects.select_for_update().get(pk=contact.pk)
        fields = dict(
            followup_status=FOLLOWUP_LATER, followup_due=day, followup_by=_person(user), followup_at=now,
            needs_human=False, needs_human_reason='', needs_human_at=None,
        )
        if reason:
            fields['followup_note'] = reason
        manual = dict(row.lead_manual) if isinstance(row.lead_manual, dict) else {}
        # A lead a person had closed is a lead again: somebody set a day to come back on. And "קח אליי" comes
        # off — the lead is this person's by the mark itself, and until its day it is on nobody's list of work.
        if [manual.pop(name, None) for name in ('closed', 'owner')] != [None, None]:
            fields['lead_manual'] = manual
        state.touch(row.pk, **fields)
        why = reason or row.followup_note
        whose = state.user_display_name(_person(user))
        state.log_event(
            row.pk, EVENT_FOLLOWUP,
            f'ליד עתידי{" של " + whose if whose else ""}: לחזור ב-{_day_words(day)}' + (f' — {why}' if why else ''),
            actor=user,
        )

    mute = {'muted': True, 'detail': ''}
    contact = Contact.objects.get(pk=contact.pk)
    if contact.handled_by != HANDLED_HUMAN:
        try:
            handoff.takeover(contact, user)
        except handoff.HandoffError as exc:
            mute = {'muted': False, 'detail': str(exc)}
        except Exception:
            # The lead is kept whatever happened to the mute; the answer still has to say the bot was not muted.
            logger.exception('wahub future leads: muting the bot for contact %s failed unexpectedly', contact.pk)
            mute = {'muted': False, 'detail': MUTE_FAILED}
    lead_stage.refresh_quietly(contact.pk)
    return Contact.objects.get(pk=contact.pk), mute


# --- the clock: the bot carries on -----------------------------------------------------------------------

def _when_words(moment, now) -> str:
    """ "היום ב-10:30", "מחר ב-10:30", "ב-1.12.2026"."""
    local, today = timezone.localtime(moment), timezone.localtime(now).date()
    days = (local.date() - today).days
    if days <= 0:
        return f'היום ב-{local:%H:%M}'
    if days == 1:
        return f'מחר ב-{local:%H:%M}'
    return f'ב-{_day_words(local.date())}'


def _if_handed_over(contact: Contact, user, now) -> Contact:
    """The contact as it would be once handed to the bot — a copy; nothing is written."""
    would = copy.copy(contact)
    for name, value in _held_marks(contact, user, now).items():
        setattr(would, name, value)
    would.handled_by = HANDLED_BOT
    would.lead_bot = {}
    manual = _without_pin(contact)
    if manual is not None:
        would.lead_manual = manual
    return would


def _not_understood(contact: Contact, result: dict, text: str) -> dict:
    return {
        'ok': False, 'text': text, 'why': '', 'stage': result['stage'], 'stage_label': result['label'],
        'come_back_on': None, 'next': None, 'source': contact.analysis_source or '',
        'sending_enabled': sending.sending_enabled(contact),
    }


def _why_no_step(result: dict) -> str:
    if not result['stage']:
        return NOT_A_LEAD
    if result['group'] == LEAD_GROUP_CLOSED:
        return f'{result["label"]} — אין מה לתזמן.'
    if result['owner'] == LEAD_OWNER_OFFICE:
        return f'{OFFICE_ONLY}: {result["reason"] or result["label"]}'
    if result['stage'] == LEAD_WAITING_LIST:
        return ON_A_WAITING_LIST
    return NO_STEP


def hand_to_bot(contact: Contact, user, *, timeout: float = UNDERSTAND_SECONDS) -> tuple[Contact, dict]:
    """
    "הבוט ימשיך לטפל": the conversation is read NOW as a lead — with the notes
    the office wrote, and the word that a person passed it on — and, when the
    rules then give the bot a step with a time, it is handed back to the bot
    with that step planned. Returns (the contact, what the bot understood);
    `ok` False means nothing was changed, and the sentence says why.

    Raises handoff.HandoffError when a person holds the conversation and
    ManyChat did not take it back; nothing is changed then either (the reading
    itself is kept — it is only a summary).
    """
    try:
        analysis.analyze_contact(contact, timeout=timeout, handed_over=True)
    except Exception:
        # The stage is worked out from what the last summary kept.
        logger.exception('wahub future leads: contact %s could not be read before it was handed to the bot', contact.pk)
    contact = Contact.objects.get(pk=contact.pk)
    now = timezone.now()
    result = lead_stage.compute(_if_handed_over(contact, user, now), now)
    follow = result['follow']
    if result['owner'] != LEAD_OWNER_BOT or follow is None:
        return contact, _not_understood(contact, result, _why_no_step(result))
    if not lead_followups.phrasing(follow['kind'], follow['from_step'], follow.get('variant') or '')[0]:
        return contact, _not_understood(contact, result, NO_WORDS)

    if contact.handled_by == HANDLED_HUMAN:
        handoff.release(contact, user)
    with transaction.atomic():
        row = Contact.objects.select_for_update().get(pk=contact.pk)
        fields = {**_held_marks(row, user, now), 'lead_bot': {}}
        manual = _without_pin(row)
        if manual is not None:
            fields['lead_manual'] = manual
        state.touch(row.pk, **fields)
    # The stage, and with it the bot's step, planned at once (a fault there is the cron's to put right).
    lead_stage.refresh_quietly(contact.pk)
    contact = Contact.objects.get(pk=contact.pk)
    planned = LeadFollowUp.objects.filter(contact=contact, status__in=NUDGE_OPEN_STATUSES).order_by('-id').first()
    understood = _understood(contact, planned, now)
    state.log_event(contact.pk, EVENT_LEAD_ACTION, f'הועבר לבוט לטיפול בהמשך: {understood["text"]}', actor=user)
    return contact, understood


def _understood(contact: Contact, planned: LeadFollowUp | None, now) -> dict:
    """What the bot made of the conversation, as one sentence and as its parts."""
    said = contact.lead_ai if isinstance(contact.lead_ai, dict) else {}
    stage = contact.lead_stage
    label = lead_stage.STAGE_LABELS.get(stage, '')
    by_date = planned is not None and planned.kind == NUDGE_DATE_ARRIVED
    why = (said.get('come_back_why') or '').strip() if by_date else ''
    move = leads.next_move(contact, planned)
    if planned is None:
        # The rules gave the bot a step and nothing was planned for it (its words were turned off meanwhile).
        text, following = f'{why or label} — {leads.no_step_because(contact, stage)}', None
    else:
        following = {
            'what': move['what'], 'when': move['when'], 'state': move['state'], 'kind': planned.kind,
            'kind_label': lead_followups.KIND_LABELS.get(planned.kind, planned.kind), 'template_key': planned.template_key,
        }
        when = _when_words(planned.due_at, now)
        text = f'{why} — הבוט יחזור אליהם {when}' if why else f'{label} — {move["what"]} {when}'
    return {
        'ok': True,
        'text': text,
        'why': why,
        'stage': stage,
        'stage_label': label,
        'come_back_on': contact.lead_come_back_on.isoformat() if contact.lead_come_back_on else None,
        'next': following,
        'source': contact.analysis_source or '',
        'sending_enabled': sending.sending_enabled(contact),
    }


# --- notes ------------------------------------------------------------------------------------------------

def _note_text(text) -> str:
    text = str(text or '').strip()
    if not text:
        raise FutureLeadError('נדרש טקסט לפתק.')
    if len(text) > NOTE_CHARS:
        raise FutureLeadError(f'הפתק ארוך מדי (עד {NOTE_CHARS} תווים).')
    return text


def add_note(contact: Contact, text, user) -> ContactEvent:
    """
    A note about the customer, written by a person while talking to them. A
    line of the journal: it is no message and is sent to nobody. The summary
    reads it the next time it reads the conversation (analysis.office_notes).
    """
    event = state.log_event(contact.pk, EVENT_NOTE, _note_text(text), actor=user)
    # The screens show it; and what the office now knows may change what the lead is.
    state.touch(contact.pk, needs_analysis=True)
    return event


def _note_of(contact: Contact, note_id):
    """The note itself, held for the change — a line of this contact's journal that is a note, or NoteNotFound."""
    try:
        wanted = int(note_id)
    except (TypeError, ValueError):
        raise NoteNotFound
    event = ContactEvent.objects.select_for_update().filter(pk=wanted, contact=contact, kind=EVENT_NOTE).first()
    if event is None:
        raise NoteNotFound
    return event


def edit_note(contact: Contact, note_id, text, user) -> ContactEvent:
    """
    A note rewritten (the contract, ז.5). It stays where it was in the
    conversation — who wrote it and when do not move — and says when it was
    rewritten and by whom. The journal gets a line of its own, without the
    words. The same text again changes nothing.
    """
    text = _note_text(text)
    with transaction.atomic():
        event = _note_of(contact, note_id)
        if text != event.text:
            event.text, event.edited_at, event.edited_by = text, timezone.now(), _person(user)
            event.save(update_fields=['text', 'edited_at', 'edited_by'])
            state.log_event(contact.pk, EVENT_NOTE_CHANGED, NOTE_EDITED, actor=user)
            # What the office knows has changed: the lead is read again with the note as it now is.
            state.touch(contact.pk, needs_analysis=True)
    return ContactEvent.objects.select_related('actor', 'edited_by').get(pk=event.pk)


def delete_note(contact: Contact, note_id, user) -> int:
    """
    A note deleted — for good, not marked: every reader of the notes (the
    screens, the lead's details, the summary that reads them beside the
    conversation) stops seeing it at once. The journal keeps that a note was
    deleted, by whom and when, and not what it said: a deletion that kept the
    words would be no deletion. The lead is read again without it, so the bot
    stops going by what the note said.
    """
    with transaction.atomic():
        event = _note_of(contact, note_id)
        deleted = event.pk
        event.delete()
        state.log_event(contact.pk, EVENT_NOTE_CHANGED, NOTE_DELETED, actor=user)
        state.touch(contact.pk, needs_analysis=True)
    return deleted


def notes(contact: Contact, limit: int = NOTES_IN_CONVERSATION) -> list:
    """The notes of one contact, the newest first."""
    rows = (
        ContactEvent.objects.filter(contact=contact, kind=EVENT_NOTE).select_related('actor', 'edited_by').order_by('-id')[:limit]
    )
    return [note_payload(event) for event in rows]
