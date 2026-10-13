"""
Telling a customer on a waiting list that a seat came free
(docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ז.4) — the one thing about the list that
sends.

The owner asked, in the morning of 13.10.2026, that a freed lesson "alert US" —
that is waiting_list.check, and it sends nothing. At noon he asked for the
message to the customer as well. Two families may wait for one seat, and who
gets it is the office's to decide, so nothing here runs by itself: a person
presses for one row (`tell`), after the screen showed what would leave and how
(`preview`).

Nothing leaves except through sending.py, with every door it has: a demo
contact never gets anything; the owner's switch (or the contact's own live
test) decides; without a ManyChat to send through nothing leaves; inside
WhatsApp's 24 hours the message is free text, after them only the approved
template connected to this message — the ManyChat automation `kogo_lead_seat_1`,
found by its name or connected on the settings card like every other
(lead_templates.py). It leaves as the office's own message, with the name of
whoever pressed, and takes the conversation from nobody.

The words are a phrasing the owner edits with the others
(lead_followup_seed.SEAT_FREED), put together with the row's own lesson: the
class, the day, the hour and the branch that were waited for. No model is
asked anything.

A press is refused, with nothing sent, when the row is not one to tell about:
it left the list, no seat is free, or the seat was taken again since the cron
last looked (the form's own count is read this minute). A customer who was
already told of this very seat is told again only when the press says so
(`again`) — a request that was sent twice by mistake never becomes two
messages. And two presses in the same moment: the first marks the row as being
sent (`notify_claimed_at`) before ManyChat is asked, and the second is refused.

What is kept: the message in the conversation (sent, or failed with the reason,
as every send is), and on the row when the customer was told, by whom and how
(`notified_*`) — only for a message that really left. A press that could not
leave for a reason known beforehand (the switch, a demo, no template) writes
nothing at all.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.wahub import lead_followup_seed, lead_followups, lead_stage, lead_templates, sending, state, waiting_list
from apps.wahub.models import (
    DELIVERED_STATUSES,
    EVENT_WAITING_LIST,
    STATUS_SIMULATED,
    WAITING_ACTIVE_STATUSES,
    WAITING_FREED,
    WAITING_TOLD_TEMPLATE,
    WAITING_TOLD_TEXT,
    WaitingListEntry,
)

logger = logging.getLogger(__name__)

KIND = lead_followup_seed.SEAT_FREED
TEMPLATE_KEY = f'{KIND}_1'
FLOW_NAME = lead_templates.FLOW_NAMES[TEMPLATE_KEY]
# A press marked as being sent this long ago was cut in the middle: the row may be pressed again.
CLAIM_STALE = timedelta(minutes=2)

# Why a row is not one to tell about (409: the screen reads the row again).
NOT_ACTIVE = 'not_active'
NOT_FREED = 'not_freed'
TAKEN = 'taken'
NO_LESSON = 'no_lesson'
ALREADY = 'already_notified'
IN_PROGRESS = 'in_progress'
# Why a message cannot leave for this customer now (200, `sent` False: nothing was written).
DEMO = 'demo'
SENDING_OFF = 'sending_off'
NO_MANYCHAT = 'no_manychat'
NO_TEMPLATE = 'no_template'
NO_WORDS = 'no_words'
# What became of a message that was handed to ManyChat.
SENT_TEXT = 'sent_text'
SENT_TEMPLATE = 'sent_template'
FAILED = 'failed'

DETAILS = {
    NOT_ACTIVE: 'הרשומה כבר לא ברשימת ההמתנה.',
    NOT_FREED: 'עוד לא התפנה מקום בשיעור הזה — אין על מה להודיע.',
    TAKEN: 'המקום נתפס בינתיים — השיעור שוב מלא. ההודעה לא נשלחת.',
    NO_LESSON: 'השיעור כבר לא מתקיים — הרשומה תיסגר לבד.',
    IN_PROGRESS: 'ההודעה לרשומה הזאת נשלחת ממש עכשיו. נסו שוב בעוד רגע.',
    DEMO: sending.DEMO_CONTACT,
    SENDING_OFF: sending.SENDING_OFF,
    NO_MANYCHAT: 'השרת לא מחובר ל-ManyChat — ההודעה לא יכולה לצאת.',
    NO_TEMPLATE: (
        'חסרה תבנית: עברו 24 שעות מההודעה האחרונה של הלקוח, ואז וואטסאפ מרשה רק תבנית מאושרת. '
        f'יוצרים ב-ManyChat אוטומציה בשם {FLOW_NAME}.'
    ),
    NO_WORDS: 'הנוסח של ההודעה כבוי בידע של הבוט — אין מה לשלוח.',
}
HOW_LABELS = {
    WAITING_TOLD_TEXT: 'תצא כהודעה רגילה (הלקוח כתב ב-24 השעות האחרונות)',
    WAITING_TOLD_TEMPLATE: (
        'תצא כתבנית מאושרת (עברו 24 שעות מההודעה האחרונה של הלקוח) — הלקוח מקבל את הנוסח הקבוע של התבנית'
    ),
}
SENT_DETAILS = {SENT_TEXT: 'נשלח כהודעה רגילה', SENT_TEMPLATE: f'נשלח כתבנית ({FLOW_NAME})'}
SIMULATED_NOTE = ' (הדמיה — לא יצא לאף אחד)'
SEND_FAILED = 'השליחה נכשלה.'


class Refused(Exception):
    """A press on a row that is not one to tell about: the code the screen reads, the reason in Hebrew, the row."""

    def __init__(self, code: str, detail: str, entry: WaitingListEntry):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.entry = entry


def _iso(moment):
    return moment.isoformat() if moment else None


def _day_and_hour(moment) -> str:
    """ "14.10 11:02" — as the office reads a moment."""
    local = timezone.localtime(moment)
    return f'{local.day}.{local.month} {local:%H:%M}'


def _row(entry_id) -> WaitingListEntry:
    entry = WaitingListEntry.objects.select_related(*waiting_list.ROW_RELATED).filter(pk=entry_id).first()
    if entry is None:
        raise WaitingListEntry.DoesNotExist
    return entry


# --- the words ------------------------------------------------------------------------------------------

def words(entry: WaitingListEntry) -> str:
    """
    What the conversation shows, and what leaves inside the 24 hours: the
    phrasing as the owner has it now, with the lesson of this very row. '' when
    he turned the phrasing off.
    """
    body, _template_key = lead_followups.phrasing(KIND, 1)
    if not body:
        return ''
    lesson = waiting_list.lesson_block(entry)
    return lead_followups.render(
        body, {'חוג': lesson['course'], 'יום': lesson['day'], 'שעה': lesson['start'], 'סניף': lesson['branch']},
    )


# --- may it leave ---------------------------------------------------------------------------------------

def _standing(entry: WaitingListEntry) -> str:
    """Why this row is not one to tell about now; '' when it is. The seats are the form's own, read this minute."""
    if entry.status not in WAITING_ACTIVE_STATUSES:
        return NOT_ACTIVE
    if entry.status != WAITING_FREED:
        return NOT_FREED
    seen = waiting_list.look(entry.lesson_id)
    if not seen['found']:
        return NO_LESSON
    if seen['cap']['is_full']:
        return TAKEN
    return ''


def _pipe(entry: WaitingListEntry, now) -> dict:
    """
    How a message would leave for this row's customer now — {how, flow, text,
    blocked} — through the doors of sending.py, in their order. `blocked` is ''
    when it may leave.
    """
    contact = entry.contact
    how = WAITING_TOLD_TEXT if sending.can_free_text(contact, now) else WAITING_TOLD_TEMPLATE
    text = words(entry)
    flow, blocked = '', ''
    if contact.is_demo:
        blocked = DEMO
    elif not sending.sending_enabled(contact):
        blocked = SENDING_OFF
    elif not sending.manychat_reachable():
        blocked = NO_MANYCHAT
    else:
        if how == WAITING_TOLD_TEMPLATE:
            flow = lead_templates.flow_for(TEMPLATE_KEY, now)
            if not flow:
                blocked = NO_TEMPLATE
        if not blocked and not text:
            blocked = NO_WORDS
    return {'how': how, 'flow': flow, 'text': text, 'blocked': blocked}


def _told_before(entry: WaitingListEntry) -> dict:
    """The last telling the row keeps — whichever seat it was about."""
    return {
        'already_notified_at': _iso(entry.notified_at),
        'already_notified_by_name': state.user_display_name(entry.notified_by) if entry.notified_by_id else None,
    }


def _already_detail(entry: WaitingListEntry) -> str:
    who = state.user_display_name(entry.notified_by) if entry.notified_by_id else ''
    return f'כבר נשלחה ללקוח הודעה על המקום הזה ב-{_day_and_hour(entry.notified_at)}' + (f' ({who})' if who else '') + '. לשלוח שוב?'


def preview(entry_id, *, now=None) -> dict:
    """
    GET waiting-list/{id}/notify/ — what a press would do now, for the screen
    that asks before it sends: the words, how they would leave, and what stops
    them. Reads only (ManyChat may be asked for its automations, as the
    templates card asks). Raises WaitingListEntry.DoesNotExist.
    """
    now = now or timezone.now()
    entry = _row(entry_id)
    standing = _standing(entry)
    pipe = _pipe(entry, now)
    blocked = standing or pipe['blocked']
    return {
        'entry': waiting_list.payloads([entry])[0],
        'notify': {
            'can': not blocked,
            'how': pipe['how'],
            'how_label': HOW_LABELS[pipe['how']],
            'text': pipe['text'],
            'blocked': blocked,
            'detail': DETAILS.get(blocked, ''),
            # A customer already told of the seat that is free now: the press has to say "again".
            'needs_again': waiting_list.told_of_this_seat(entry) is not None,
            **_told_before(entry),
        },
    }


# --- the press ------------------------------------------------------------------------------------------

def _claim(entry_id, again: bool, now) -> WaitingListEntry:
    """
    The row, marked as being sent — or Refused. Everything that may have
    changed since the screen showed the row is looked at again here, under the
    row's lock: its place on the list, the seat, an earlier telling, a press
    that is on its way this very moment.
    """
    with transaction.atomic():
        entry = (
            WaitingListEntry.objects.select_for_update(of=('self',)).select_related(*waiting_list.ROW_RELATED)
            .filter(pk=entry_id).first()
        )
        if entry is None:
            raise WaitingListEntry.DoesNotExist
        standing = _standing(entry)
        if standing:
            raise Refused(standing, DETAILS[standing], entry)
        if waiting_list.told_of_this_seat(entry) is not None and not again:
            raise Refused(ALREADY, _already_detail(entry), entry)
        if entry.notify_claimed_at is not None and now - entry.notify_claimed_at < CLAIM_STALE:
            raise Refused(IN_PROGRESS, DETAILS[IN_PROGRESS], entry)
        entry.notify_claimed_at = now
        entry.save(update_fields=['notify_claimed_at'])
    return entry


def _deliver(entry: WaitingListEntry, pipe: dict, user):
    """Hand the message to sending.py — free text inside the 24 hours, else the template's flow. The message kept."""
    contact = entry.contact
    if pipe['how'] == WAITING_TOLD_TEXT:
        try:
            return sending.send_text(contact, pipe['text'], user), WAITING_TOLD_TEXT
        except sending.WindowClosed:
            # The 24 hours ended between the look and the send: only the template may leave now.
            flow = lead_templates.flow_for(TEMPLATE_KEY)
            if not flow:
                return None, WAITING_TOLD_TEMPLATE
            return sending.send_flow(contact, flow, pipe['text'], user), WAITING_TOLD_TEMPLATE
    return sending.send_flow(contact, pipe['flow'], pipe['text'], user), WAITING_TOLD_TEMPLATE


def tell(entry_id, user, *, again: bool = False, now=None) -> dict:
    """
    POST waiting-list/{id}/notify/ — tell this row's customer that a seat came
    free. Returns {entry, notify}: `notify.sent` says whether a message left,
    `code` and `detail` say what happened. Raises Refused for a row that is not
    one to tell about (nothing sent, nothing written), and
    WaitingListEntry.DoesNotExist.
    """
    now = now or timezone.now()
    entry = _row(entry_id)
    before = _told_before(entry)
    standing = _standing(entry)
    if standing:
        raise Refused(standing, DETAILS[standing], entry)
    if waiting_list.told_of_this_seat(entry) is not None and not again:
        raise Refused(ALREADY, _already_detail(entry), entry)

    def answer(sent: bool, code: str, detail: str, how: str, message=None) -> dict:
        return {
            'entry': _row(entry_id),
            'notify': {'sent': sent, 'how': how, 'code': code, 'detail': detail, 'text': pipe['text'], 'message': message, **before},
        }

    pipe = _pipe(entry, now)
    if pipe['blocked']:
        # Known beforehand that nothing would leave: nothing is written, anywhere.
        return answer(False, pipe['blocked'], DETAILS[pipe['blocked']], '')

    entry = _claim(entry_id, again, now)
    person = user if getattr(user, 'is_authenticated', False) else None
    message, how, left = None, pipe['how'], False
    try:
        message, how = _deliver(entry, pipe, user)
        left = message is not None and message.status in DELIVERED_STATUSES
    finally:
        # The mark is given back whatever happened; what was told is kept only for a message that left.
        told = dict(notified_at=timezone.now(), notified_by=person, notified_how=how) if left else {}
        WaitingListEntry.objects.filter(pk=entry.pk).update(notify_claimed_at=None, **told)
    if message is None:
        return answer(False, NO_TEMPLATE, DETAILS[NO_TEMPLATE], '')
    if not left:
        logger.warning('wahub waiting list: telling contact %s of a free seat failed: %s', entry.contact_id, message.error)
        lead_stage.refresh_quietly(entry.contact_id)
        return answer(False, FAILED, message.error or SEND_FAILED, how, message)

    code = SENT_TEXT if how == WAITING_TOLD_TEXT else SENT_TEMPLATE
    try:
        state.log_event(
            entry.contact_id, EVENT_WAITING_LIST,
            f'נשלחה ללקוח הודעה שהתפנה מקום ({waiting_list.TOLD_LABELS[how]}): {waiting_list.lesson_block(entry)["label"]}',
            actor=user,
        )
        # The lead says the customer was told, and — once they write — that they answered.
        if not waiting_list.sync_contact(entry.contact_id):
            lead_stage.refresh_quietly(entry.contact_id)
    except Exception:
        # The message has left and the row says so; the journal and the lead's sentence are the cron's to catch up on.
        logger.exception('wahub waiting list: the telling of contact %s was sent but not written in full', entry.contact_id)
    detail = SENT_DETAILS[code] + (SIMULATED_NOTE if message.status == STATUS_SIMULATED else '')
    return answer(True, code, detail, how, message)
