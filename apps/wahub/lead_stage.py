"""
"מצב הליד" — one stage per contact, picked by rules (docs/WAHUB-CONTRACT-LEAD-STAGES.md).

No model reads anything here. The stage is worked out from facts the rest of
the app already keeps on the contact, and the first rule that fits decides:

    the conversation      last_message_*, waiting_since, last_inbound_at, needs_human   (inbound.py, sending.py)
    the registrations     kogo_outcome, kogo_detail, kogo_trial                         (matching.py)
    what was said         known_*                                                       (analysis.py)
    what a person marked  followup_*                                                    (the office)

Three groups: `act` — somebody has to do something; `wait` — in motion, nothing
to do now; `closed`. With the stage come one sentence of why, built from the
same facts, and one of what to do.

The rules, in order:

     1. not_relevant     the office marked "לא רלוונטי"
     2. registered       registered after writing (kogo), or the office marked "נרשם"
     3. customer         was a paying customer before writing: service, not a lead
     4. needs_reply      asks for a person, or wrote and got no answer for REPLY_AFTER
     5. signup_failed    tried to register and the charge failed
     6. signup_started   filled in details and never paid
     7. trial_booked     a trial lesson is still ahead
     8. trial_no_show    the trial was marked "לא הגיע"
     9. trial_done       a trial took place (came, or not marked) and nobody registered
    10. callback_due     the day to come back has come, or the office marked "מחכה לנו"
    11. waiting_date     a day to come back that is still ahead
    12. not_relevant     the summary says so: no branch nearby, or not interested
    13. in_conversation  the last message (or mark) is younger than IN_CONVERSATION_FOR
    14. answered_silent  answered, quiet for SILENT_AFTER or more
    15. cold             quiet for COLD_AFTER or more

What the contract leaves open is settled here, and said in the sentence the
screen shows:

  * A mark of "ענה" or "בזמן אחר" answers the customer's messages written before
    it: the office spoke to them on the phone, and WhatsApp will never show it.
  * A mark is activity like a message is, so a lead somebody dealt with
    yesterday is not "silent" this morning.
  * The day to come back is the office's own when it set one; otherwise the day
    the conversation named, until a person marks the contact once it has come.
  * A contact with no message at all follows the same clock from the day it was
    added: nobody is waiting for an answer, and nobody registered.

Only the lead_* fields are written, by name, and a change of stage is a line in
the contact's journal. Nothing here sends anything to anybody.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as day_time, timedelta

from django.db import connection, transaction
from django.db.models import Count, F, Max, Min, Q
from django.utils import timezone

from apps.wahub import matching, queries, state
from apps.wahub.models import (
    CALLBACK_COUNTS_UNDER,
    Contact,
    DELIVERED_STATUSES,
    DIRECTION_IN,
    DIRECTION_OUT,
    EVENT_LEAD_STAGE,
    FOLLOWUP_ANSWERED,
    FOLLOWUP_CHOICES,
    FOLLOWUP_LATER,
    FOLLOWUP_NOT_RELEVANT,
    FOLLOWUP_NO_ANSWER,
    FOLLOWUP_REGISTERED,
    FOLLOWUP_WAITING_US,
    HANDLED_HUMAN,
    LEAD_ANSWERED_SILENT,
    LEAD_CALLBACK_DUE,
    LEAD_COLD,
    LEAD_CUSTOMER,
    LEAD_GROUP_CHOICES,
    LEAD_GROUP_CLOSED,
    LEAD_IN_CONVERSATION,
    LEAD_NEEDS_REPLY,
    LEAD_NOT_RELEVANT,
    LEAD_REGISTERED,
    LEAD_SIGNUP_FAILED,
    LEAD_SIGNUP_STARTED,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    LEAD_TRIAL_BOOKED,
    LEAD_TRIAL_DONE,
    LEAD_TRIAL_NO_SHOW,
    LEAD_WAITING_DATE,
    MESSAGE_SOURCE_MANYCHAT,
    Message,
    OUTCOME_CUSTOMER_BEFORE,
    OUTCOME_PENDING,
    OUTCOME_REGISTERED_AFTER,
    OUTCOME_SIGNUP_DECLINED,
    OUTCOME_TRIAL_ONLY,
    OUTCOME_TRIAL_UPCOMING,
    SENDER_BOT,
    SENDER_OFFICE,
)

logger = logging.getLogger(__name__)

# --- the thresholds ---------------------------------------------------------------

# A customer's message with no answer, from the bot or from the office, for this long.
REPLY_AFTER = timedelta(minutes=30)
# Answered, and the customer has said nothing since.
SILENT_AFTER = timedelta(days=2)
# The last message is this fresh: the conversation is still going.
IN_CONVERSATION_FOR = timedelta(hours=48)
# Quiet this long: an old lead.
COLD_AFTER = timedelta(days=30)

# One cron call: how long the look may take, and how many contacts of each kind it reads.
SCAN_SECONDS = 2.0
SCAN_BATCH = 500
# A contact whose look failed is tried again after this long, not on every call.
RETRY_AFTER = timedelta(hours=1)

# The marks that say a person spoke to the customer (on the phone).
SPOKEN_MARKS = (FOLLOWUP_ANSWERED, FOLLOWUP_LATER)

STAGE_LABELS = dict(LEAD_STAGE_CHOICES)
FOLLOWUP_LABELS = dict(FOLLOWUP_CHOICES)
STAGES = [stage for stage, _label in LEAD_STAGE_CHOICES]

_REPLY_MINUTES = int(REPLY_AFTER.total_seconds() // 60)
_CONVERSATION_HOURS = int(IN_CONVERSATION_FOR.total_seconds() // 3600)


def _days_in_words(days: int) -> str:
    """יומיים, 5 ימים, 30 יום."""
    if days == 2:
        return 'יומיים'
    return f'{days} ימים' if 2 < days <= 10 else f'{days} יום'


# What each stage asks of the office. `compute` sharpens it for one contact where it can.
NEXT_STEPS = {
    LEAD_NEEDS_REPLY: 'לענות לו',
    LEAD_SIGNUP_FAILED: 'לברר מה קרה ולשלוח קישור חדש',
    LEAD_SIGNUP_STARTED: 'להתקשר ולעזור לסיים',
    LEAD_TRIAL_NO_SHOW: 'לתאם מועד חדש',
    LEAD_TRIAL_DONE: 'להתקשר: איך היה, ולהציע הרשמה',
    LEAD_CALLBACK_DUE: 'לחזור אליו היום',
    LEAD_ANSWERED_SILENT: 'להתקשר ולהציע שיעור ניסיון',
    LEAD_TRIAL_BOOKED: 'אין מה לעשות עכשיו — מחכים לשיעור הניסיון',
    LEAD_WAITING_DATE: 'אין מה לעשות עכשיו — מחכים לתאריך',
    LEAD_IN_CONVERSATION: 'אין מה לעשות עכשיו',
    LEAD_REGISTERED: '',
    LEAD_CUSTOMER: '',
    LEAD_NOT_RELEVANT: '',
    LEAD_COLD: '',
}
NOBODY_SPOKE_YET = 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון'
TRY_AGAIN = 'ניסו להשיג אותו ולא ענה — לנסות שוב'

# When a contact is in the stage, in the board's words.
HINTS = {
    LEAD_NEEDS_REPLY: f'ביקש נציג, או כתב ולא נענה יותר מ-{_REPLY_MINUTES} דקות',
    LEAD_SIGNUP_FAILED: 'מילא הרשמה והחיוב לא עבר',
    LEAD_SIGNUP_STARTED: 'מילא פרטים ולא שילם',
    LEAD_TRIAL_NO_SHOW: 'היה רשום לשיעור ניסיון וסומן שלא הגיע',
    LEAD_TRIAL_DONE: 'שיעור הניסיון עבר ועדיין לא נרשם',
    LEAD_CALLBACK_DUE: 'הגיע היום שנקבע לחזור אליו, או שסומן "מחכה לנו"',
    LEAD_ANSWERED_SILENT: f'נענה, שותק {_days_in_words(SILENT_AFTER.days)} ויותר, ולא נרשם',
    LEAD_TRIAL_BOOKED: 'שיעור הניסיון עוד לפניו',
    LEAD_WAITING_DATE: 'נקבע יום לחזור אליו, והוא עוד לא הגיע',
    LEAD_IN_CONVERSATION: f'ההודעה האחרונה ב-{_CONVERSATION_HOURS} השעות האחרונות',
    LEAD_REGISTERED: 'נרשם אחרי שפנה, או שסומן "נרשם"',
    LEAD_CUSTOMER: 'היה לקוח רשום לפני שפנה — השיחה היא שירות, לא ליד',
    LEAD_NOT_RELEVANT: 'סומן "לא רלוונטי", או אמר שלא מעוניין או שאין סניף באזור שלו',
    LEAD_COLD: f'שותק {_days_in_words(COLD_AFTER.days)} ויותר',
}


# --- the words of the sentence ----------------------------------------------------

def _day(value: date, today: date) -> str:
    """2.10, and 2.10.2025 when it is not this year."""
    return f'{value.day}.{value.month}' + ('' if value.year == today.year else f'.{value.year}')


def _when(moment, now, *, absolute: bool = False) -> str:
    """
    "היום ב-10:42", "אתמול", "לפני 4 ימים" — by the Israeli calendar, so the
    words change at midnight only and the stored sentence is right all day.
    `absolute` gives the date itself, for a stage nobody looks at again.
    """
    local = timezone.localtime(moment)
    today = timezone.localtime(now).date()
    if absolute:
        return f'ב-{_day(local.date(), today)}'
    days = (today - local.date()).days
    if days <= 0:
        return f'היום ב-{local:%H:%M}'
    if days == 1:
        return 'אתמול'
    if days == 2:
        return 'לפני יומיים'
    return f'לפני {days} ימים'


def _noon(day: date):
    """A calendar day as a moment (noon in Israel: the same day whatever the clock change)."""
    return timezone.make_aware(datetime.combine(day, day_time(12)), timezone.get_current_timezone())


def _midnight(day: date):
    return timezone.make_aware(datetime.combine(day, day_time.min), timezone.get_current_timezone())


def _sentences(*parts: str) -> str:
    return ' '.join(part.strip().rstrip('.') + '.' for part in parts if part and part.strip())


def _asked(contact: Contact) -> str:
    """What they asked about, from what the summary knows: "שאל על קפוארה בראש העין (גיל 5)"."""
    course = (contact.known_course_type or '').strip()
    place = (contact.known_branch_name or contact.known_city or '').strip()
    if contact.known_topic == 'trial':
        text = 'שאל על שיעור ניסיון' + (f' ב{course}' if course else '')
    elif contact.known_topic == 'registration':
        text = 'שאל על הרשמה' + (f' ל{course}' if course else '')
    elif course:
        text = f'שאל על {course}'
    elif place or contact.known_topic == 'info':
        text = 'שאל על חוגים'
    else:
        return 'פנה בוואטסאפ'
    if place:
        text += f' ב{place}'
    age = (contact.known_child_age or '').strip()
    if age:
        text += f' (גיל {age})' if age[0].isdigit() else f' ({age})'
    return text


def _trial_line(contact: Contact, fallback: str) -> str:
    """The matching's own sentence about the trial, with the class beside it."""
    line = (contact.kogo_detail or '').strip() or fallback
    course = str((contact.kogo_trial or {}).get('course') or '').strip()
    return f'{line} · {course}' if course else line


def _did_not_come(contact: Contact) -> bool:
    trial = contact.kogo_trial or {}
    if trial:
        return trial.get('outcome') == 'no_show'
    # A contact matched before the facts were kept beside the sentence (until its next
    # match, twelve hours at most): the matching's own words for "did not come".
    return f', {matching.TRIAL_OUTCOME_LABELS["no_show"]} ·' in (contact.kogo_detail or '')


def _trial_day(contact: Contact):
    raw = str((contact.kogo_trial or {}).get('on') or '')
    try:
        return _noon(date.fromisoformat(raw)) if raw else None
    except ValueError:
        return None


# --- does Kogo see who answered? ---------------------------------------------------

# The old bot answers in ManyChat, and its answer reaches Kogo only when ManyChat
# copies it (docs/WAHUB-MANYCHAT-SETUP.md, §3). Until it does, a customer's
# message with no answer on record says nothing: the bot answered, and we never
# saw it. Calling every such lead "asked and got no answer" would be a false
# alarm on every conversation.
BOT_REPLIES_SEEN_FOR = timedelta(days=7)
_SEEN_TTL = timedelta(seconds=60)
_seen = {'at': None, 'value': False}


def old_bot_replies_seen(now=None) -> bool:
    """ManyChat copied a reply of the old bot in the last week. One query a minute, whoever asks."""
    now = now or timezone.now()
    if _seen['at'] is None or abs(now - _seen['at']) >= _SEEN_TTL:
        _seen['value'] = Message.objects.filter(
            sender=SENDER_BOT, source=MESSAGE_SOURCE_MANYCHAT, created_at__gte=now - BOT_REPLIES_SEEN_FOR,
        ).exists()
        _seen['at'] = now
    return _seen['value']


def replies_visible(contact: Contact, now=None) -> bool:
    """
    Kogo sees the answers this contact gets: they are its own (a demo, a live
    test, a conversation a person took), or ManyChat copies the old bot's.
    """
    if contact.is_demo or contact.live_test or contact.handled_by == HANDLED_HUMAN:
        return True
    return old_bot_replies_seen(now)


class _Facts:
    """What the rules ask about a contact, read once from the row in hand."""

    def __init__(self, contact: Contact, now):
        self.contact = contact
        self.now = now
        self.today = timezone.localtime(now).date()
        self.mark = contact.followup_status
        # A mark that was cleared is no mark; its time says nothing.
        self.marked_at = contact.followup_at if self.mark else None

        # The customer's messages nobody has answered in WhatsApp, bot or office: since the oldest of them.
        waiting = None
        if contact.waiting_since is not None or contact.last_message_direction == DIRECTION_IN:
            newest = contact.last_inbound_at or contact.last_message_at or contact.waiting_since
            waiting = contact.waiting_since or newest
            # A person who marked that they spoke answered, on the phone, what had been written by then —
            # and nothing written after it. (`waiting_since` is not moved by a mark, so it cannot tell.)
            spoke_at = self.marked_at if self.mark in SPOKEN_MARKS else None
            if spoke_at is not None and newest is not None:
                if spoke_at >= newest:
                    waiting = None
                elif spoke_at >= waiting:
                    waiting = newest
        # Without the answers on record, "nobody answered" cannot be said of this contact.
        self.replies_visible = replies_visible(contact, now)
        if not self.replies_visible:
            waiting = None
        self.waiting_from = waiting
        self.reply_overdue = waiting is not None and now - waiting >= REPLY_AFTER

        moments = [moment for moment in (contact.last_message_at, self.marked_at) if moment is not None]
        self.last_activity = max(moments) if moments else (contact.created_at or now)
        self.quiet = now - self.last_activity
        # A person from the office has dealt with this customer: wrote, or marked that they spoke.
        self.person_spoke = bool(contact.lead_human_touched) or self.mark in SPOKEN_MARKS
        # The mark is the last thing that happened: no message since.
        self.mark_is_newest = self.marked_at is not None and (
            contact.last_message_at is None or self.marked_at >= contact.last_message_at
        )

    def opening(self, *, absolute: bool = False) -> str:
        """The first sentence: what they asked and when."""
        contact = self.contact
        if contact.last_inbound_at is not None:
            return f'{_asked(contact)} {_when(contact.last_inbound_at, self.now, absolute=absolute)}'
        if contact.last_message_at is not None:
            return f'נשלחה אליו הודעה {_when(contact.last_message_at, self.now, absolute=absolute)}, והוא לא כתב'
        if contact.created_at is not None:
            return f'אין שיחה בוואטסאפ; נוסף {_when(contact.created_at, self.now, absolute=absolute)}'
        return 'אין שיחה בוואטסאפ'

    def who_answered(self) -> str:
        """Who dealt with them last: a person's mark when it is the newest thing, else who answered in WhatsApp."""
        contact = self.contact
        if self.waiting_from is not None:
            return 'עוד לא קיבל תשובה'
        if self.mark_is_newest:
            return self.marked()
        if contact.lead_human_touched or contact.last_message_sender == SENDER_OFFICE:
            return 'נציג ענה לו'
        if contact.last_message_direction == DIRECTION_OUT:
            return 'רק הבוט ענה' if contact.last_message_sender == SENDER_BOT else 'נשלחה אליו הודעה'
        return ''

    def day(self, value: date) -> str:
        return _day(value, self.today)

    def marked(self) -> str:
        """ "הצוות סימן "לא ענה" ב-3.10" — a person's mark, with its day."""
        label = FOLLOWUP_LABELS.get(self.mark, self.mark)
        when = f' ב-{self.day(timezone.localtime(self.marked_at).date())}' if self.marked_at is not None else ''
        return f'הצוות סימן "{label}"{when}'


# --- the rules --------------------------------------------------------------------

def compute(contact: Contact, now=None) -> dict:
    """
    The stage of one contact at `now`, from the row in hand: no query, nothing
    written. Returns {stage, group, label, reason, next_step, since, recheck_at}.

    `since` is the moment of the fact behind the stage when one dates it, else
    None (and the caller keeps the moment the stage was first seen).
    `recheck_at` is when the clock alone may change the answer or its words;
    None for a stage only a new fact can move.
    """
    now = now or timezone.now()
    facts = _Facts(contact, now)
    today = facts.today
    outcome = contact.kogo_outcome

    # Set once rule 4 has passed: from there on, a message still waiting for its
    # answer turns whatever stage was picked into "needs a reply" at this moment.
    reply_due_at = None

    def done(stage: str, reason: str, *, next_step: str | None = None, since=None, recheck_at=None) -> dict:
        group = LEAD_STAGE_GROUPS[stage]
        moments = [recheck_at, reply_due_at]
        if group != LEAD_GROUP_CLOSED:
            # "אתמול" becomes "לפני יומיים" at midnight, and a day to come back arrives.
            moments.append(_midnight(today + timedelta(days=1)))
        moments = [moment for moment in moments if moment is not None]
        return {
            'stage': stage,
            'group': group,
            'label': STAGE_LABELS[stage],
            'reason': reason[:300],
            'next_step': (NEXT_STEPS[stage] if next_step is None else next_step)[:200],
            'since': since,
            'recheck_at': min(moments) if moments else None,
        }

    # 1. a person closed it
    if facts.mark == FOLLOWUP_NOT_RELEVANT:
        return done(LEAD_NOT_RELEVANT, _sentences(facts.marked()), since=facts.marked_at)

    # 2. registered: the registrations say so, or a person does
    if outcome == OUTCOME_REGISTERED_AFTER:
        return done(LEAD_REGISTERED, _sentences(contact.kogo_detail or 'נרשם אחרי הפנייה'))
    if facts.mark == FOLLOWUP_REGISTERED:
        return done(LEAD_REGISTERED, _sentences(facts.marked()), since=facts.marked_at)

    # 3. a customer from before: the conversation is service
    if outcome == OUTCOME_CUSTOMER_BEFORE:
        return done(LEAD_CUSTOMER, _sentences(contact.kogo_detail or 'לקוח רשום'))

    # 4. somebody is waiting for us
    if contact.needs_human or facts.reply_overdue:
        waiting_for_person = ''
        if contact.needs_human:
            why = (contact.needs_human_reason or '').strip()
            waiting_for_person = f'מחכה לנציג ({why})' if why else 'מחכה לנציג'
        moments = [moment for moment in (
            facts.waiting_from, contact.needs_human_at if contact.needs_human else None,
        ) if moment is not None]
        return done(
            LEAD_NEEDS_REPLY,
            _sentences(
                facts.opening(), 'עוד לא קיבל תשובה' if facts.waiting_from is not None else '', waiting_for_person,
            ),
            since=min(moments) if moments else None,
        )
    if facts.waiting_from is not None:
        reply_due_at = facts.waiting_from + REPLY_AFTER

    # 5–9. what the registrations show
    if outcome == OUTCOME_SIGNUP_DECLINED:
        return done(LEAD_SIGNUP_FAILED, _sentences(contact.kogo_detail or 'ניסה להירשם והחיוב נכשל', 'לא נרשם'))
    if outcome == OUTCOME_PENDING:
        return done(LEAD_SIGNUP_STARTED, _sentences(contact.kogo_detail or 'התחיל רישום', 'לא שילם'))
    if outcome == OUTCOME_TRIAL_UPCOMING:
        return done(LEAD_TRIAL_BOOKED, _sentences(_trial_line(contact, 'רשום לשיעור ניסיון')))
    if outcome == OUTCOME_TRIAL_ONLY:
        if _did_not_come(contact):
            return done(
                LEAD_TRIAL_NO_SHOW, _sentences(_trial_line(contact, 'נרשם לניסיון ולא הגיע'), 'לא נרשם'),
                since=_trial_day(contact),
            )
        return done(
            LEAD_TRIAL_DONE, _sentences(_trial_line(contact, 'עשה שיעור ניסיון'), 'לא נרשם אחרי הניסיון'),
            since=_trial_day(contact),
        )

    # 10–11. a day to come back. The office's own day ("בזמן אחר" with a date) is a person's decision and
    #     stands alone; without one, the day the conversation named counts under the marks that leave it
    #     open (the rule of the "לחזור אליהם" queue) — until a person marks the contact once that day has
    #     come: the office has gone back to them, and from there the conversation's clock decides.
    office_day = contact.followup_due if facts.mark == FOLLOWUP_LATER else None
    said_day = contact.known_callback_on if facts.mark in CALLBACK_COUNTS_UNDER and office_day is None else None
    if said_day is not None and facts.marked_at is not None and timezone.localtime(facts.marked_at).date() >= said_day:
        said_day = None
    day, why = (office_day, 'הצוות קבע לחזור אליו') if office_day is not None else (said_day, 'לפי השיחה, לחזור אליו')

    if facts.mark == FOLLOWUP_WAITING_US:
        return done(LEAD_CALLBACK_DUE, _sentences(facts.opening(), facts.marked()), since=facts.marked_at)
    if day is not None and day <= today:
        return done(LEAD_CALLBACK_DUE, _sentences(facts.opening(), f'{why} ב-{facts.day(day)}'), since=_noon(day))
    if day is not None:
        return done(
            LEAD_WAITING_DATE, _sentences(facts.opening(), f'{why} ב-{facts.day(day)}'),
            next_step=f'לחזור אליו ב-{facts.day(day)}', recheck_at=_midnight(day),
        )

    # 12. the conversation itself said it is not for them — only what the summary already marked
    if 'no_branch_nearby' in (contact.known_flags or []):
        return done(LEAD_NOT_RELEVANT, _sentences(facts.opening(absolute=True), 'אין לנו סניף באזור שלו'))
    if contact.known_interest == 'cold':
        return done(LEAD_NOT_RELEVANT, _sentences(facts.opening(absolute=True), 'אמר שלא מעוניין כרגע'))

    # 13. still talking
    if facts.quiet < IN_CONVERSATION_FOR:
        return done(
            LEAD_IN_CONVERSATION, _sentences(facts.opening(), facts.who_answered()),
            since=facts.last_activity, recheck_at=facts.last_activity + IN_CONVERSATION_FOR,
        )

    # 14. answered, and silent since
    if SILENT_AFTER <= facts.quiet < COLD_AFTER:
        if facts.mark == FOLLOWUP_NO_ANSWER and facts.mark_is_newest:
            next_step = TRY_AGAIN
        elif not facts.person_spoke:
            next_step = NOBODY_SPOKE_YET
        else:
            next_step = None
        return done(
            LEAD_ANSWERED_SILENT, _sentences(facts.opening(), facts.who_answered(), 'לא נרשם'),
            next_step=next_step, since=facts.last_activity, recheck_at=facts.last_activity + COLD_AFTER,
        )

    # 15. an old lead
    if facts.quiet >= COLD_AFTER:
        return done(
            LEAD_COLD, _sentences(facts.opening(absolute=True), facts.who_answered(), 'לא נרשם'),
            since=facts.last_activity,
        )

    # Only when the thresholds above are set apart: not fresh, not yet "silent".
    return done(
        LEAD_IN_CONVERSATION, _sentences(facts.opening(), facts.who_answered()),
        since=facts.last_activity, recheck_at=facts.last_activity + SILENT_AFTER,
    )


# --- keeping it -------------------------------------------------------------------

def _office_wrote(contact_ids) -> set:
    """The contacts somebody from the office has written to, of those given. One query."""
    contact_ids = list(contact_ids)
    if not contact_ids:
        return set()
    return set(
        Message.objects.filter(
            contact_id__in=contact_ids, direction=DIRECTION_OUT, sender=SENDER_OFFICE, status__in=DELIVERED_STATUSES,
        ).values_list('contact_id', flat=True).distinct()
    )


def refresh(contact, *, now=None, fresh: bool = False, human_touched: bool | None = None) -> bool:
    """
    Work one contact's stage out again and keep it. True when what the screen
    shows changed (stage, since, why, what to do, the "a person wrote" mark).

    `contact` is a Contact or its id; the row is read again unless `fresh` says
    the one in hand was just read (the scan). Only the lead_* fields are
    written, and only when they changed: a look that finds nothing new moves
    `lead_checked_at` alone and wakes no screen. A change of stage is a line in
    the journal; the first stage a contact gets is not.

    The write holds only if nobody touched the contact since it was read. If
    somebody did, nothing is written and the contact stays marked for the next
    look — a stage worked out from an older row never covers a newer fact.
    """
    now = now or timezone.now()
    if not fresh or not isinstance(contact, Contact):
        contact = Contact.objects.filter(pk=contact.pk if isinstance(contact, Contact) else contact).first()
        if contact is None:
            return False
    was_touched = contact.lead_human_touched
    if not was_touched:
        # Once true it stays true; until then it is looked up.
        contact.lead_human_touched = bool(_office_wrote([contact.pk])) if human_touched is None else human_touched
    result = compute(contact, now)

    stage = result['stage']
    since = result['since']
    if since is None:
        # No fact dates this stage: it began when it was first seen.
        since = contact.lead_stage_since if stage == contact.lead_stage and contact.lead_stage_since else now
    before = (contact.lead_stage, contact.lead_stage_since, contact.lead_reason, contact.lead_next_step, was_touched)
    after = (stage, since, result['reason'], result['next_step'], contact.lead_human_touched)

    unchanged_since_read = Contact.objects.filter(pk=contact.pk, touched_at=contact.touched_at)
    moment = timezone.now()
    if before == after:
        unchanged_since_read.update(lead_checked_at=moment, lead_recheck_at=result['recheck_at'])
        return False
    kept = unchanged_since_read.update(
        lead_stage=stage,
        lead_stage_since=since,
        lead_reason=result['reason'],
        lead_next_step=result['next_step'],
        lead_human_touched=contact.lead_human_touched,
        lead_checked_at=moment,
        lead_recheck_at=result['recheck_at'],
        # The screen has to see it; and checked at the same instant, so the scan does not come back for it.
        touched_at=moment,
        updated_at=moment,
    )
    if not kept:
        return False
    if contact.lead_stage and contact.lead_stage != stage:
        state.log_event(
            contact.pk, EVENT_LEAD_STAGE,
            f'{STAGE_LABELS.get(contact.lead_stage, contact.lead_stage)} ← {result["label"]}'
            + (f' ({result["reason"]})' if result['reason'] else ''),
        )
    return True


def refresh_quietly(contact_id) -> bool:
    """
    `refresh` for a caller whose own work the stage must never fail: the door
    ManyChat is waiting on, a mark a person has just saved. A fault is logged
    and the cron's look puts the stage right. Inside a caller's transaction the
    look runs in a savepoint, so a database error here does not take the
    caller's work down with it.
    """
    try:
        if connection.in_atomic_block:
            with transaction.atomic():
                return refresh(contact_id)
        return refresh(contact_id)
    except Exception:
        logger.exception('wahub lead stage: contact %s was not refreshed', contact_id)
        return False


def pending_q(now) -> Q:
    """Contacts the scan still has to look at: never looked at, touched since, or their time has come."""
    return Q(lead_checked_at__isnull=True) | Q(touched_at__gt=F('lead_checked_at')) | Q(lead_recheck_at__lte=now)


def pending_count(now=None) -> int:
    return Contact.objects.filter(pending_q(now or timezone.now())).count()


def scan(budget_seconds: float = SCAN_SECONDS, *, now=None, batch: int = SCAN_BATCH) -> dict:
    """
    The cron's look, inside its time: first the contacts something happened to
    since their stage was worked out (and those that never had one — the first
    runs fill the table a batch at a time), then the contacts whose time has
    come (`lead_recheck_at`: thirty minutes with no reply, two days of quiet, a
    day to come back, and every open lead once a day, after midnight).

    Every contact is finished and saved on its own, so a call that runs out of
    time loses nothing and the next one carries on.
    """
    started = time.monotonic()
    now = now or timezone.now()
    counts = {'lead_checked': 0, 'lead_changed': 0}

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    touched = Contact.objects.filter(
        Q(lead_checked_at__isnull=True) | Q(touched_at__gt=F('lead_checked_at'))
    ).order_by(F('lead_checked_at').asc(nulls_first=True), 'id')
    # After midnight every open lead is due at once; a customer waiting for an answer goes before that wave.
    timed = Contact.objects.filter(lead_recheck_at__lte=now).order_by(
        F('waiting_since').asc(nulls_last=True), 'lead_recheck_at', 'id',
    )

    seen: set = set()
    for queryset in (touched, timed):
        if left() <= 0:
            break
        rows = [row for row in queryset[:batch] if row.pk not in seen]
        seen.update(row.pk for row in rows)
        wrote = _office_wrote(row.pk for row in rows if not row.lead_human_touched)
        for row in rows:
            if left() <= 0:
                break
            try:
                changed = refresh(row, now=now, fresh=True, human_touched=row.pk in wrote)
            except Exception:
                logger.exception('wahub lead stage: contact %s failed', row.pk)
                # Left as it is, it would be first in line again every five minutes.
                moment = timezone.now()
                Contact.objects.filter(pk=row.pk).update(lead_checked_at=moment, lead_recheck_at=moment + RETRY_AFTER)
                continue
            counts['lead_checked'] += 1
            counts['lead_changed'] += int(changed)
    return counts


# --- the board --------------------------------------------------------------------

def board(now=None) -> dict:
    """
    GET leads/board/ — the three groups with their stages, each with how many
    contacts are in it and how long the longest has been there. One aggregate,
    whatever the number of contacts; a stage with nobody stays on the list.
    """
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    wanted = {'checked_at': Max('lead_checked_at'), 'pending_scan': Count('id', filter=pending_q(now))}
    for stage in STAGES:
        wanted[f'count_{stage}'] = Count('id', filter=Q(lead_stage=stage))
        wanted[f'oldest_{stage}'] = Min('lead_stage_since', filter=Q(lead_stage=stage))
    row = Contact.objects.aggregate(**wanted)

    groups = []
    for group, label in LEAD_GROUP_CHOICES:
        stages = [
            {
                'key': stage,
                'label': STAGE_LABELS[stage],
                'hint': HINTS[stage],
                'next_step': NEXT_STEPS[stage],
                'count': row[f'count_{stage}'],
                'oldest_days': queries.days_since(row[f'oldest_{stage}'], today),
            }
            for stage in STAGES if LEAD_STAGE_GROUPS[stage] == group
        ]
        groups.append({'key': group, 'label': label, 'total': sum(stage['count'] for stage in stages), 'stages': stages})
    return {
        'groups': groups,
        'checked_at': row['checked_at'].isoformat() if row['checked_at'] else None,
        'pending_scan': row['pending_scan'],
    }
