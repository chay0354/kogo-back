"""
"מצב הליד" — one stage per contact, picked by rules, and who makes the next move
(docs/WAHUB-CONTRACT-LEAD-STAGES.md, docs/WAHUB-CONTRACT-LEADS-V2.md).

No model is asked anything here. The stage is worked out from facts the rest of
the app already keeps on the contact, and the first rule that fits decides:

    the conversation      last_message_*, waiting_since, last_inbound_at, needs_human   (inbound.py, sending.py)
    the registrations     kogo_outcome, kogo_detail, kogo_trial                         (matching.py)
    what was said         known_*, lead_ai, lead_link_sent_at                           (analysis.py)
    what a person marked  followup_* (the office), lead_manual (the leads screen)
    the bot's follow-ups  lead_bot                                                      (lead_followups.py)

Three groups, as the first board shows them: `act`, `wait`, `closed`. With the
stage come one sentence of why, one of what a person would do, and — for the
leads screen — the owner: `bot` (it comes back by itself), `office` (somebody
has to do something) or '' (no step now, or closed).

The rules, in order:

     1. deleted           a person deleted the lead, and the customer has not written since
     2. not_interested    a person closed it ("לא מעוניין" on the leads screen, "לא רלוונטי" of the office)
    2w. waiting_list      a seat came free in the lesson they wait for                   office
     3. registered        registered after writing (kogo), the office marked "נרשם", or the customer
                          said they already had ("כבר נרשמנו") — on their word, with no argument
     4. customer          was a paying customer before writing: service, not a lead — unless they
                          ask for one more class (`add_class`), which is a lead like any other
     —  (no stage)        the conversation is not about a class at all: a birthday, a studio to
                          rent, a supplier, the shop, a business's automatic reply
     5. signup_stuck      tried to register and the site stopped them                    office
     6. needs_reply       asks for a person                                              office
     7. not_interested    said so, in words of their own that the summary kept
     8. needs_reply       a question nobody answered, a trial to move, somebody to call;
                          or wrote and got no answer for REPLY_AFTER                     office
     9. callback_due      the office marked "מחכה לנו", or the day it set has come       office
    10. waiting_date      a day the office set, still ahead
    11. signup_failed     tried to register and the charge failed                        office
    12. signup_started    filled in details and never paid                               bot
    13. trial_booked      a trial lesson is still ahead
    14. trial_no_show     the trial was marked "לא הגיע"                                 bot
    15. trial_done        a trial took place (came, or not marked), nobody registered    bot
    16. callback_due      the day the conversation named has come                        bot
    17. waiting_date      a day the conversation named, still ahead                      bot
   17w. waiting_list      on the waiting list of a lesson that is full (waiting_list.py). While it
                          lasts, rules 3, 4 and "no stage" do not close the lead: somebody who
                          waits for a seat is a lead, whatever else they are
    18. nothing_to_offer  no branch nearby, no class that fits, or the class is full
    19. will_check        said they would check and come back                            bot
    20. link_sent         got a registration link and has not registered                 bot
    21. in_conversation   the last message (or mark) is younger than IN_CONVERSATION_FOR
    22. answered_silent   answered, quiet for SILENT_AFTER or more                       bot
        (in 21 and 22 the bot comes back, once, to two kinds of lead: one who wrote nothing
        of their own — two hours later — and one afraid to commit who got no link — a day later)
    23. cold              quiet for COLD_AFTER or more — "רדום"

"bot" is where a round of follow-ups starts (lead_followups.py): how many times
and how far apart is NUDGE_DELAYS. A round ends, and the owner changes, like
this: a person took the conversation or the lead (office, whatever the stage);
the bot came back as many times as the round allows and nobody answered — a hot
lead (a trial, a registration begun) and one who "will check" then wait for the
office, any other goes dormant, GIVE_UP_AFTER later; a hot lead wrote (office:
a person carries on from there); or the fact behind the round is too old to ask
about (START_WITHIN). And there are leads the bot never comes back to by
itself: an open complaint (office), an external branch (nobody can know from
Kogo whether they registered), the price or the days (nothing new to say).

What the contracts leave open is settled here, and said in the sentence the
screen shows:

  * A mark of "ענה" or "בזמן אחר" answers the customer's messages written before
    it: the office spoke to them on the phone, and WhatsApp will never show it.
  * A mark is activity like a message is, so a lead somebody dealt with
    yesterday is not "silent" this morning.
  * The day to come back is the office's own when it set one — it stands above
    whatever the registrations show, so a lead a person put off is not written
    to by the bot meanwhile; otherwise the day the conversation named, until a
    person marks the contact once it has come.
  * A contact with no message at all follows the same clock from the day it was
    added: nobody is waiting for an answer, and nobody registered.
  * A lead that was closed — by a person, or by its own "not interested" — opens
    again by itself when the customer writes after that.
  * What the summary concluded about a conversation is not read once the
    customer has written again: the next summary, minutes later, decides anew.
  * "Not a lead" needs the summary to say so twice — the intent and the topic
    of the card: a conversation one of them calls a class stays a lead.
  * What only a person can settle (stuck on the site, an open question) stops
    being the stage once a person answered: wrote after it, or marked that
    they spoke. From there the lead follows the ordinary rules.

Only the lead_* fields named in `refresh` are written, and a change of stage or
of owner is a line in the contact's journal. Nothing here sends anything.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date, datetime, time as day_time, timedelta

from django.db import connection, transaction
from django.db.models import Count, F, Max, Min, Q
from django.utils import timezone

from apps.wahub import lead_ai, matching, queries, state, waiting_list
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
    LEGACY_LEAD_STAGES,
    LEAD_ANSWERED_SILENT,
    LEAD_CALLBACK_DUE,
    LEAD_CLOSE_DELETED,
    LEAD_COLD,
    LEAD_CUSTOMER,
    LEAD_DELETED,
    LEAD_GROUP_CHOICES,
    LEAD_GROUP_CLOSED,
    LEAD_IN_CONVERSATION,
    LEAD_LINK_SENT,
    LEAD_NEEDS_REPLY,
    LEAD_NOTHING_TO_OFFER,
    LEAD_NOT_INTERESTED,
    LEAD_OWNER_BOT,
    LEAD_OWNER_CHOICES,
    LEAD_OWNER_OFFICE,
    LEAD_REGISTERED,
    LEAD_SIGNUP_FAILED,
    LEAD_SIGNUP_STARTED,
    LEAD_SIGNUP_STUCK,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    LEAD_TRIAL_BOOKED,
    LEAD_TRIAL_DONE,
    LEAD_TRIAL_NO_SHOW,
    LEAD_WAITING_DATE,
    LEAD_WAITING_LIST,
    LEAD_WILL_CHECK,
    MESSAGE_SOURCE_MANYCHAT,
    Message,
    NUDGE_DATE_ARRIVED,
    NUDGE_DORMANT,
    NUDGE_LINK_REMINDER,
    NUDGE_OPENING,
    NUDGE_TRIAL_FEEDBACK,
    NUDGE_TRIAL_NO_SHOW,
    NUDGE_WILL_CHECK,
    OUTCOME_CUSTOMER_BEFORE,
    OUTCOME_PENDING,
    OUTCOME_REGISTERED_AFTER,
    OUTCOME_SIGNUP_DECLINED,
    OUTCOME_TRIAL_ONLY,
    OUTCOME_TRIAL_UPCOMING,
    SENDER_BOT,
    SENDER_OFFICE,
    WAITING_FREED,
    WAITING_WAITING,
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

# The rules as they stand. A contact whose stage an older version worked out is looked at
# again, a batch at a time — which is how the stages and the owners of 12.10.2026 reach the
# contacts that were already there.
RULES_VERSION = 2

# --- the bot's own follow-ups (docs/WAHUB-CONTRACT-LEADS-V2.md, ג and ח.10) ---
DAY = timedelta(days=1)
# When the bot comes back, counted from the fact behind the round. A whole number of days is
# "that many days later, when the office opens"; less than a day is that long after the fact.
NUDGE_DELAYS = {
    NUDGE_LINK_REMINDER: (1 * DAY, 3 * DAY, 7 * DAY),
    NUDGE_WILL_CHECK: (1 * DAY,),                    # once, by the owner's word; then the office calls whoever decides
    NUDGE_TRIAL_FEEDBACK: (1 * DAY, 3 * DAY, 7 * DAY),
    NUDGE_TRIAL_NO_SHOW: (1 * DAY, 3 * DAY),
    NUDGE_DATE_ARRIVED: (timedelta(0), 3 * DAY, 7 * DAY),
    NUDGE_OPENING: (timedelta(hours=2),),            # once, inside WhatsApp's 24 hours
    NUDGE_DORMANT: (timedelta(0),),
}
# The day a child is old enough: that day and a week later, not three times. A child who did
# not want to: once, when the next year opens.
DATE_ARRIVED_AGE = (timedelta(0), 7 * DAY)
DATE_ARRIVED_NOT_READY = (timedelta(0),)
VARIANT_AGE = 'age'
VARIANT_NOT_READY = 'not_ready'
VARIANT_COMMITMENT = 'commitment'
VARIANT_SIGNUP = 'signup'


def delays_for(kind: str, variant: str = '') -> tuple:
    """When the bot comes back in a round of this kind and variant — the one place that says so."""
    if kind == NUDGE_DATE_ARRIVED and variant == VARIANT_AGE:
        return DATE_ARRIVED_AGE
    if kind == NUDGE_DATE_ARRIVED and variant == VARIANT_NOT_READY:
        return DATE_ARRIVED_NOT_READY
    return NUDGE_DELAYS[kind]


# After the last time the bot came back, the customer has this long to answer.
GIVE_UP_AFTER = timedelta(days=3)
# A round begins only this close to the fact behind it: nobody is asked today how last month's trial was.
START_WITHIN = timedelta(days=14)
OPENING_START_WITHIN = timedelta(hours=20)
# What becomes of a lead whose round is over and unanswered.
THEN_OFFICE = 'office'
THEN_DORMANT = 'dormant'
THEN_STAY = 'stay'

EXTERNAL_BRANCH = 'סניף חיצוני — ההרשמה מול העירייה'
CALL_WHO_DECIDES = 'להתקשר למי שמחליט'

# Who makes the next move in each stage (the contract's table). A person who took the
# conversation or the lead, and a round of the bot's that is over, come on top of it (`compute`).
STAGE_OWNERS = {
    LEAD_SIGNUP_STUCK: LEAD_OWNER_OFFICE,
    LEAD_NEEDS_REPLY: LEAD_OWNER_OFFICE,
    LEAD_SIGNUP_FAILED: LEAD_OWNER_OFFICE,
    LEAD_CALLBACK_DUE: LEAD_OWNER_OFFICE,
    LEAD_SIGNUP_STARTED: LEAD_OWNER_BOT,
    LEAD_LINK_SENT: LEAD_OWNER_BOT,
    LEAD_WILL_CHECK: LEAD_OWNER_BOT,
    LEAD_TRIAL_NO_SHOW: LEAD_OWNER_BOT,
    LEAD_TRIAL_DONE: LEAD_OWNER_BOT,
    LEAD_WAITING_DATE: LEAD_OWNER_BOT,
    LEAD_ANSWERED_SILENT: LEAD_OWNER_BOT,
    LEAD_TRIAL_BOOKED: '',
    LEAD_WAITING_LIST: '',
    LEAD_NOTHING_TO_OFFER: '',
    LEAD_IN_CONVERSATION: '',
    LEAD_REGISTERED: '',
    LEAD_CUSTOMER: '',
    LEAD_NOT_INTERESTED: '',
    LEAD_DELETED: '',
    LEAD_COLD: '',
}
# Stages in which a person who holds the conversation has nothing to do right now: the customer's turn,
# a lesson or a date ahead, nothing to offer yet.
NOBODYS_MOVE = (LEAD_IN_CONVERSATION, LEAD_TRIAL_BOOKED, LEAD_WAITING_DATE, LEAD_WAITING_LIST, LEAD_NOTHING_TO_OFFER)
OWNER_LABELS = dict(LEAD_OWNER_CHOICES)

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
    LEAD_SIGNUP_STUCK: 'להתקשר היום ולסיים איתו את ההרשמה',
    LEAD_NEEDS_REPLY: 'לענות לו',
    LEAD_SIGNUP_FAILED: 'לברר מה קרה ולשלוח קישור חדש',
    LEAD_SIGNUP_STARTED: 'להתקשר ולעזור לסיים',
    LEAD_LINK_SENT: 'להתקשר ולעזור להשלים את ההרשמה',
    LEAD_TRIAL_NO_SHOW: 'לתאם מועד חדש',
    LEAD_TRIAL_DONE: 'להתקשר: איך היה, ולהציע הרשמה',
    LEAD_CALLBACK_DUE: 'לחזור אליו היום',
    LEAD_ANSWERED_SILENT: 'להתקשר ולהציע שיעור ניסיון',
    LEAD_WILL_CHECK: 'אין מה לעשות עכשיו — אמר שיחזור',
    LEAD_TRIAL_BOOKED: 'אין מה לעשות עכשיו — מחכים לשיעור הניסיון',
    LEAD_WAITING_DATE: 'אין מה לעשות עכשיו — מחכים לתאריך',
    LEAD_WAITING_LIST: 'אין מה לעשות עכשיו — נתריע כשיתפנה מקום',
    LEAD_NOTHING_TO_OFFER: 'אין מה לעשות עכשיו — לחזור אליו כשייפתח משהו מתאים',
    LEAD_IN_CONVERSATION: 'אין מה לעשות עכשיו',
    LEAD_REGISTERED: '',
    LEAD_CUSTOMER: '',
    LEAD_NOT_INTERESTED: '',
    LEAD_DELETED: '',
    LEAD_COLD: '',
}
OFFER_THE_SEAT = 'להציע ללקוח את המקום שהתפנה'
WROTE_BEFORE_THE_DAY = 'הלקוח כתב לפני המועד — לענות לו'
NOBODY_SPOKE_YET = 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון'
TRY_AGAIN = 'ניסו להשיג אותו ולא ענה — לנסות שוב'

# When a contact is in the stage, in the board's words.
HINTS = {
    LEAD_SIGNUP_STUCK: 'ניסה להירשם והאתר עצר אותו: הקישור לא עובד, החוג לא מופיע, כתוב "מלא"',
    LEAD_NEEDS_REPLY: f'ביקש נציג, שאל ולא קיבל תשובה, או כתב ולא נענה יותר מ-{_REPLY_MINUTES} דקות',
    LEAD_SIGNUP_FAILED: 'מילא הרשמה והחיוב לא עבר',
    LEAD_SIGNUP_STARTED: 'מילא פרטים ולא שילם',
    LEAD_LINK_SENT: 'נשלח אליו קישור להרשמה, ועדיין לא נרשם',
    LEAD_TRIAL_NO_SHOW: 'היה רשום לשיעור ניסיון וסומן שלא הגיע',
    LEAD_TRIAL_DONE: 'שיעור הניסיון עבר ועדיין לא נרשם',
    LEAD_CALLBACK_DUE: 'הגיע היום שנקבע לחזור אליו, או שסומן "מחכה לנו"',
    LEAD_ANSWERED_SILENT: f'נענה, שותק {_days_in_words(SILENT_AFTER.days)} ויותר, ולא נרשם',
    LEAD_WILL_CHECK: 'אמר שיבדוק, יחשוב או ישאל מישהו, ויחזור',
    LEAD_TRIAL_BOOKED: 'שיעור הניסיון עוד לפניו',
    LEAD_WAITING_DATE: 'נקבע יום לחזור אליו, והוא עוד לא הגיע',
    LEAD_WAITING_LIST: 'מחכה למקום בשיעור מסוים שמלא; כשמתפנה מקום הליד עובר למשרד',
    LEAD_NOTHING_TO_OFFER: 'אין סניף באזור שלו, אין חוג שמתאים לו, או שהחוג מלא',
    LEAD_IN_CONVERSATION: f'ההודעה האחרונה ב-{_CONVERSATION_HOURS} השעות האחרונות',
    LEAD_REGISTERED: 'נרשם אחרי שפנה, או שסומן "נרשם"',
    LEAD_CUSTOMER: 'היה לקוח רשום לפני שפנה — השיחה היא שירות, לא ליד',
    LEAD_NOT_INTERESTED: 'אמר במילים שלו שאינו מעוניין, או שסומן "לא רלוונטי"',
    LEAD_DELETED: 'נמחק מרשימת הלידים',
    LEAD_COLD: f'שותק {_days_in_words(COLD_AFTER.days)} ויותר, או שהבוט חזר אליו ולא נענה',
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
    """The parts as sentences, each closed with a full stop — unless it closes itself (a customer's question)."""
    closed = []
    for part in parts:
        text = (part or '').strip()
        if text:
            closed.append(text if text[-1] in '?!' else text.rstrip('.') + '.')
    return ' '.join(closed)


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


_STARTED_ON = re.compile(r'ב-(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?')


def _detail_day(contact: Contact, today: date):
    """The day in the matching's own sentence ("התחיל רישום ב-2.10 · …"), as a moment; None when it names none."""
    found = _STARTED_ON.search(contact.kogo_detail or '')
    if not found:
        return None
    try:
        return _noon(date(int(found.group(3) or today.year), int(found.group(2)), int(found.group(1))))
    except ValueError:
        return None


def _moment(value):
    """A moment kept as text in one of the contact's JSON fields; None when it is not one."""
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


def _stamp(moment) -> str:
    return moment.isoformat() if moment is not None else ''


# --- does Kogo see who answered? ---------------------------------------------------

# The old bot answers in ManyChat, and its answer reaches Kogo only when ManyChat
# copies it (docs/WAHUB-MANYCHAT-SETUP.md, §3). Until it does, a customer's
# message with no answer on record says nothing: the bot answered, and we never
# saw it. Calling every such lead "asked and got no answer" would be a false
# alarm on every conversation.
#
# Two things follow. The old bot's lines in a demo conversation are typed or
# seeded in Kogo and kept with ManyChat's source — they are no copy of anything,
# and must not say "ManyChat copies". And on the day the copying does begin, the
# messages written before it are still unknown: only what is written from then
# on can be called unanswered, or every lead of the weeks before would be.
BOT_REPLIES_SEEN_FOR = timedelta(days=7)
_SEEN_TTL = timedelta(seconds=60)
_seen = {'at': None, 'since': None}


def replies_copied_since(now=None):
    """
    Since when Kogo sees the old bot's answers to real customers: the moment it
    kept the first reply ManyChat copied — as long as one was copied in the last
    week; None while ManyChat copies none. One query a minute, whoever asks.
    """
    now = now or timezone.now()
    if _seen['at'] is None or abs(now - _seen['at']) >= _SEEN_TTL:
        copied = Message.objects.filter(
            sender=SENDER_BOT, source=MESSAGE_SOURCE_MANYCHAT, contact__is_demo=False,
        ).aggregate(first=Min('created_at'), last=Max('created_at'))
        still = copied['last'] is not None and copied['last'] >= now - BOT_REPLIES_SEEN_FOR
        _seen['since'] = copied['first'] if still else None
        _seen['at'] = now
    return _seen['since']


def old_bot_replies_seen(now=None) -> bool:
    """ManyChat copied a reply of the old bot to a real customer in the last week."""
    return replies_copied_since(now) is not None


def answers_itself(contact: Contact) -> bool:
    """The answers this contact gets are Kogo's own: a demo, a live test, a conversation a person took."""
    return bool(contact.is_demo or contact.live_test or contact.handled_by == HANDLED_HUMAN)


def replies_visible(contact: Contact, now=None) -> bool:
    """Kogo sees the answers this contact gets: they are its own, or ManyChat copies the old bot's."""
    return answers_itself(contact) or old_bot_replies_seen(now)


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
        waiting = newest = None
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
        # Without the answers on record, "nobody answered" cannot be said of this contact — nor of a
        # message written before ManyChat began copying them: the wait is counted from what came after.
        copied_since = None if answers_itself(contact) else replies_copied_since(now)
        self.replies_visible = answers_itself(contact) or copied_since is not None
        if not self.replies_visible:
            waiting = None
        elif waiting is not None and copied_since is not None and waiting < copied_since:
            waiting = newest if newest is not None and newest >= copied_since else None
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

        # What a person decided on the leads screen (`lead_manual`). A lead somebody closed opens
        # again by itself when the customer writes after that — and so does the office's "לא רלוונטי".
        manual = contact.lead_manual if isinstance(contact.lead_manual, dict) else {}
        closed = manual.get('closed') if isinstance(manual.get('closed'), dict) else None
        self.closed_at = _moment(closed.get('at')) if closed else None
        self.closed = None if closed is None or self.wrote_after(self.closed_at) else closed
        self.reopened_at = _moment(manual.get('reopened_at'))
        taken = manual.get('owner') if isinstance(manual.get('owner'), dict) else {}
        # A person answers this conversation, or pressed "קח אליי": the next move is theirs, whatever the stage.
        # Somebody pressed "קח אליי" on the lead itself: it stays on her list whatever its stage.
        self.lead_taken = taken.get('value') == LEAD_OWNER_OFFICE
        self.person_holds = contact.handled_by == HANDLED_HUMAN or self.lead_taken
        self.person_holds_why = (
            'נציג לקח את השיחה' if contact.handled_by == HANDLED_HUMAN
            else f'{taken.get("by_name") or "מנהל"} לקח את הליד' if taken.get('value') == LEAD_OWNER_OFFICE else ''
        )

        # A person has dealt with what the customer wrote last: answered it from the CRM, or marked
        # that they spoke. What only a person could settle is then settled, as far as Kogo can tell.
        newest = contact.last_inbound_at
        wrote_back = (
            contact.last_message_sender == SENDER_OFFICE and contact.last_message_direction == DIRECTION_OUT
            and contact.last_message_at is not None and (newest is None or contact.last_message_at >= newest)
        )
        spoke = self.mark in SPOKEN_MARKS and self.marked_at is not None and (newest is None or self.marked_at >= newest)
        self.person_answered = wrote_back or spoke

    def wrote_after(self, moment) -> bool:
        """The customer wrote after `moment`. A moment nobody kept says nothing: False."""
        newest = self.contact.last_inbound_at
        return moment is not None and newest is not None and newest > moment

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


# --- what the conversation says, and where the bot's round stands ------------------

class _Said:
    """
    What the conversation says about the lead: the summary's `lead_ai`
    (apps/wahub/lead_ai.py). Until the summary has reached a contact, the one
    thing the older summary already kept that the stages read: no branch nearby.
    """

    def __init__(self, contact: Contact, facts: _Facts):
        kept = contact.lead_ai if isinstance(contact.lead_ai, dict) else {}
        # The customer wrote after the summary: what it concluded is about a conversation that has moved on.
        stale = bool(kept) and contact.last_inbound_at is not None and (
            contact.analyzed_at is None or contact.last_inbound_at > contact.analyzed_at
        )
        current = kept if not stale else {}
        self.blocker = current.get('blocker') or ''
        if not kept and 'no_branch_nearby' in (contact.known_flags or []):
            self.blocker = lead_ai.BLOCKER_NO_BRANCH
        self.blocker_text = (current.get('blocker_text') or '').strip() or lead_ai.BLOCKER_LABELS.get(self.blocker, '')
        self.empty_opening = bool(current.get('empty_opening'))

        # What the conversation is about. It is not a lead only when the summary says so twice: the
        # intent, and the topic of the card it filled in the same pass.
        self.intent = kept.get('intent') or ''
        self.add_class = self.intent == lead_ai.INTENT_ADD_CLASS
        self.is_lead = not (
            self.intent and self.intent not in lead_ai.LEAD_INTENTS and contact.known_topic in ('', 'other')
        )

        # "Not interested" and "already registered" stand on the customer's own words — and not on
        # words from before a person opened the lead again.
        self.quote = (current.get('quote') or '').strip()
        self.quote_at = _moment(current.get('quote_at'))
        theirs = bool(self.quote) and (
            facts.reopened_at is None or (self.quote_at is not None and self.quote_at > facts.reopened_at)
        )
        self.no = theirs and current.get('interest') == lead_ai.INTEREST_NO
        self.registered = theirs and current.get('interest') == lead_ai.INTEREST_REGISTERED

        # The day to come back: why, on which day it was said, and what kind of day it is.
        come_back = kept.get('come_back') if isinstance(kept.get('come_back'), dict) else {}
        self.come_back_why = (kept.get('come_back_why') or '').strip()
        try:
            self.said_on = date.fromisoformat(str(come_back.get('said_on') or ''))
        except ValueError:
            self.said_on = None
        if come_back.get('kind') == lead_ai.COME_BACK_AGE:
            self.day_variant = VARIANT_AGE
        elif kept.get('blocker') == lead_ai.BLOCKER_CHILD_NOT_READY:
            self.day_variant = VARIANT_NOT_READY
        else:
            self.day_variant = ''
        self.day_delays = delays_for(NUDGE_DATE_ARRIVED, self.day_variant)

        self.external = bool(kept.get('external'))
        branch = kept.get('branch_id') or contact.known_branch_id
        try:
            self.branch_id = uuid.UUID(str(branch)) if branch else None
        except ValueError:
            self.branch_id = None
        self.course_type = (kept.get('course_type') or contact.known_course_type or '')[:100]


class _Round:
    """Where the bot's follow-ups of one round stand, as lead_followups.py keeps it on the contact (`lead_bot`)."""

    def __init__(self, contact: Contact, key: str, steps: int):
        kept = contact.lead_bot if isinstance(contact.lead_bot, dict) else {}
        kept = kept if kept.get('episode') == key else {}
        self.steps = steps
        self.started_at = _moment(kept.get('started_at'))
        self.attempts = int(kept.get('attempts') or 0)
        # Steps the bot never took, because the system's own automations had already said them.
        self.passed = int(kept.get('passed') or 0)
        self.last_at = _moment(kept.get('last_at'))
        # No step of this round is still ahead: every one went out, or could not.
        self.done = bool(kept.get('done'))
        self.note = (kept.get('note') or '').strip()

    @property
    def began(self) -> bool:
        return self.started_at is not None

    @property
    def all_sent(self) -> bool:
        return self.attempts + self.passed >= self.steps and self.last_at is not None

    def said(self) -> str:
        """What the bot did in this round, in a sentence for the screen."""
        if self.attempts:
            times = 'פעם אחת' if self.attempts == 1 else 'פעמיים' if self.attempts == 2 else f'{self.attempts} פעמים'
            return f'הבוט חזר אליו {times} ולא נענה'
        return f'הבוט לא חזר אליו ({self.note})' if self.note else 'הבוט לא חזר אליו'


# --- the rules --------------------------------------------------------------------

def compute(contact: Contact, now=None) -> dict:
    """
    The stage of one contact at `now`, from the row in hand: nothing written,
    and no query but the one, once a minute, that asks whether ManyChat copies
    the old bot's replies (`replies_copied_since`). Returns
    {stage, group, label, reason, next_step, since, recheck_at, owner, follow,
    branch_id, course_type, come_back_on}.

    `since` is the moment of the fact behind the stage when one dates it, else
    None (and the caller keeps the moment the stage was first seen).
    `recheck_at` is when the clock alone may change the answer or its words;
    None for a stage only a new fact can move. `next_step` is what a person
    would do; `owner` says whether it is theirs to do now. `follow` is the
    round of follow-ups the bot should have open for this contact —
    {kind, key, anchor, hot, delays, variant, from_step} — or None
    (lead_followups.py plans and cancels by it). A conversation that is not about a class has no
    stage at all: '' everywhere, and the sentence says what it is about.
    """
    now = now or timezone.now()
    facts = _Facts(contact, now)
    said = _Said(contact, facts)
    today = facts.today
    outcome = contact.kogo_outcome

    # Set once the rule of an unanswered message has passed: from there on, a message still waiting
    # for its answer turns whatever stage was picked into "needs a reply" at this moment.
    reply_due_at = None
    come_back_on = None

    def done(stage: str, reason: str, *, next_step: str | None = None, since=None, recheck_at=None,
             owner: str | None = None, follow: dict | None = None) -> dict:
        group = LEAD_STAGE_GROUPS[stage]
        moments = [recheck_at, reply_due_at]
        if group == LEAD_GROUP_CLOSED:
            owner, follow = '', None
        else:
            # "אתמול" becomes "לפני יומיים" at midnight, and a day to come back arrives.
            moments.append(_midnight(today + timedelta(days=1)))
            owner = STAGE_OWNERS[stage] if owner is None else owner
            if facts.person_holds:
                # A person holds the conversation: the bot never comes back by itself. The lead waits
                # for the office only while there is something for the office to do — one a person has
                # answered, or that waits for a lesson or a date, is nobody's move ("מחכים לך" is a list of
                # work). A lead somebody took by name stays hers.
                waits_for_nobody = stage in NOBODYS_MOVE and owner != LEAD_OWNER_OFFICE and not facts.lead_taken
                owner = '' if waits_for_nobody else LEAD_OWNER_OFFICE
            if owner != LEAD_OWNER_BOT:
                follow = None
        moments = [moment for moment in moments if moment is not None]
        return {
            'stage': stage,
            'group': group,
            'label': STAGE_LABELS[stage],
            'reason': reason[:300],
            'next_step': (NEXT_STEPS[stage] if next_step is None else next_step)[:200],
            'since': since,
            'recheck_at': min(moments) if moments else None,
            'owner': owner,
            'follow': follow,
            'branch_id': said.branch_id,
            'course_type': said.course_type,
            'come_back_on': come_back_on,
        }

    def by_the_bot(stage: str, kind: str, key: str, anchor, *parts: str, then: str, delays=None, variant: str = '',
                   since=None, next_step: str | None = None, office_step: str | None = None, recheck_at=None,
                   in_touch: bool = False, start_within=START_WITHIN, from_step: int = 1):
        """
        A stage the bot follows up by itself, for as long as its round lasts.
        `anchor` is the fact the round counts from; `then` — what becomes of the
        lead when the round is over and nobody answered; `in_touch` — the
        customer has already written since that fact; `from_step` — the first
        step the bot itself takes, when the system's own automations have
        already said what the earlier ones say. None for a lead whose round is
        over and that goes dormant: the caller says so in its words.
        """
        delays = delays or NUDGE_DELAYS[kind]
        round_ = _Round(contact, key, len(delays))
        hot = then == THEN_OFFICE
        common = dict(since=since, recheck_at=recheck_at)

        def plain(*more: str, owner=None, step=next_step, **extra):
            return done(stage, _sentences(*parts, *more), owner=owner, next_step=step, **{**common, **extra})

        def office(*more: str):
            return plain(*more, owner=LEAD_OWNER_OFFICE, step=office_step or next_step)

        # Leads the bot never comes back to by itself.
        if 'complaint' in (contact.known_flags or []):
            return office('יש תלונה פתוחה — הבוט לא חוזר אליו')
        if said.external:
            return plain(EXTERNAL_BRANCH, owner='')
        if said.blocker in lead_ai.NO_NUDGE_BLOCKERS:
            hold = f'{said.blocker_text} — הבוט לא חוזר אליו לבד'
            return office(hold) if hot else plain(hold, owner='')

        if round_.done:
            if then == THEN_STAY:
                return plain()
            if round_.all_sent:
                gives_up = round_.last_at + GIVE_UP_AFTER
                if now < gives_up:
                    # The last word is the bot's, and the customer still has time to answer it.
                    return plain(round_.said().replace(' ולא נענה', ''), owner=LEAD_OWNER_BOT, recheck_at=gives_up)
                return office(round_.said()) if hot else None
            return office(round_.said()) if hot else plain(round_.said())
        if hot and (in_touch or (round_.began and facts.wrote_after(round_.started_at))):
            # A hot lead that is talking: a person carries on from here, not a ready-made line.
            return office()
        if not round_.began and (anchor is None or now - anchor > start_within):
            # Too long ago to ask about: no round is begun. A hot lead is the office's, as it always was.
            return office() if hot else plain()
        follow = {
            'kind': kind, 'key': key[:160], 'anchor': anchor, 'hot': hot, 'delays': tuple(delays), 'variant': variant,
            'from_step': max(1, min(from_step, len(delays))),
        }
        return plain(owner=LEAD_OWNER_BOT, follow=follow)

    def dormant(key: str, steps: int):
        round_ = _Round(contact, key, steps)
        return done(LEAD_COLD, _sentences(facts.opening(absolute=True), round_.said(), 'לא נרשם'), since=round_.last_at)

    def needs_reply(*why: str):
        waiting_for_person = ''
        if contact.needs_human:
            person_why = (contact.needs_human_reason or '').strip()
            waiting_for_person = f'מחכה לנציג ({person_why})' if person_why else 'מחכה לנציג'
        moments = [moment for moment in (
            facts.waiting_from, contact.needs_human_at if contact.needs_human else None,
        ) if moment is not None]
        return done(
            LEAD_NEEDS_REPLY,
            _sentences(
                facts.opening(), *why,
                'עוד לא קיבל תשובה' if facts.waiting_from is not None and not why else '', waiting_for_person,
            ),
            since=min(moments) if moments else None,
        )

    # 1–2. a person closed it — and the customer has not written since
    if facts.closed is not None:
        who = (facts.closed.get('by_name') or '').strip() or 'מנהל'
        on = f' ב-{facts.day(timezone.localtime(facts.closed_at).date())}' if facts.closed_at is not None else ''
        note = (facts.closed.get('note') or '').strip()
        if facts.closed.get('reason') == LEAD_CLOSE_DELETED:
            return done(LEAD_DELETED, _sentences(f'{who} מחק את הליד{on}', note), since=facts.closed_at)
        return done(LEAD_NOT_INTERESTED, _sentences(f'{who} סגר את הליד{on}: לא מעוניין', note), since=facts.closed_at)
    if facts.mark == FOLLOWUP_NOT_RELEVANT and not facts.wrote_after(facts.marked_at):
        return done(LEAD_NOT_INTERESTED, _sentences(facts.marked()), since=facts.marked_at)

    # 2w. a seat came free in the lesson they wait for: the office offers it, before anything else is asked
    waits = contact.lead_waiting if isinstance(contact.lead_waiting, dict) else {}
    if waits.get('status') == WAITING_FREED:
        return done(
            LEAD_WAITING_LIST, _sentences(waiting_list.freed_reason(waits)), next_step=OFFER_THE_SEAT,
            since=_moment(waits.get('freed_at')), owner=LEAD_OWNER_OFFICE,
        )
    # Somebody who waits for a seat is a lead whatever else they are: a customer, registered to
    # another class, a conversation the summary did not read as one about a class.
    on_a_list = waits.get('status') == WAITING_WAITING

    # 3. registered: the registrations say so, a person does — or the customer, whose word is taken
    if outcome == OUTCOME_REGISTERED_AFTER and not on_a_list:
        return done(LEAD_REGISTERED, _sentences(contact.kogo_detail or 'נרשם אחרי הפנייה'))
    if facts.mark == FOLLOWUP_REGISTERED and not on_a_list:
        return done(LEAD_REGISTERED, _sentences(facts.marked()), since=facts.marked_at)
    if said.registered and not on_a_list:
        return done(LEAD_REGISTERED, _sentences(f'הלקוח אמר שכבר רשום: "{said.quote[:160]}"'), since=said.quote_at)

    # 4. a customer from before: the conversation is service — unless they ask for one more class
    if outcome == OUTCOME_CUSTOMER_BEFORE and not said.add_class and not on_a_list:
        return done(LEAD_CUSTOMER, _sentences(contact.kogo_detail or 'לקוח רשום'))

    # Not about a class at all: no stage, no owner, nothing the bot comes back about.
    if not said.is_lead and not on_a_list:
        return {
            'stage': '', 'group': '', 'label': '',
            'reason': _sentences(f'לא ליד לחוגים: {lead_ai.INTENT_LABELS.get(said.intent, said.intent)}'),
            'next_step': '', 'since': None, 'recheck_at': None, 'owner': '', 'follow': None,
            'branch_id': said.branch_id, 'course_type': said.course_type, 'come_back_on': None,
        }

    # 5. tried to register and the site stopped them: hot, and only a person can sort it out
    if said.blocker == lead_ai.BLOCKER_SITE_PROBLEM and not facts.person_answered:
        return done(
            LEAD_SIGNUP_STUCK,
            _sentences(facts.opening(), said.blocker_text, 'מחכה לנציג' if contact.needs_human else ''),
            since=contact.last_inbound_at,
        )

    # 6. asks for a person
    if contact.needs_human:
        return needs_reply()

    # 7. said, in words of their own, that they are not interested — and nobody goes on writing to them
    if said.no:
        return done(
            LEAD_NOT_INTERESTED, _sentences(facts.opening(absolute=True), f'כתב: "{said.quote[:160]}"'), since=said.quote_at,
        )

    # 8. what only a person can settle; or wrote, and nobody answered
    if said.blocker in lead_ai.OFFICE_BLOCKERS and not facts.person_answered:
        if said.blocker == lead_ai.BLOCKER_OPEN_QUESTION:
            return needs_reply(f'שאל ולא קיבל תשובה: {said.blocker_text}')
        return needs_reply(said.blocker_text)
    if facts.reply_overdue:
        return needs_reply()
    if facts.waiting_from is not None:
        reply_due_at = facts.waiting_from + REPLY_AFTER

    # The day to come back. The office's own day ("בזמן אחר" with a date) is a person's decision and
    # stands alone; without one, the day the conversation named counts under the marks that leave it
    # open (the rule of the "לחזור אליהם" queue) — until a person marks the contact once that day has
    # come: the office has gone back to them, and from there the conversation's clock decides.
    office_day = contact.followup_due if facts.mark == FOLLOWUP_LATER else None
    said_day = contact.known_callback_on if facts.mark in CALLBACK_COUNTS_UNDER and office_day is None else None
    if said_day is not None and facts.marked_at is not None and timezone.localtime(facts.marked_at).date() >= said_day:
        said_day = None
    come_back_on = office_day or said_day

    # 9–10. what a person marked holds the lead, above whatever the registrations show: the bot does
    #       not write to somebody the office put off, or owes an answer to.
    if facts.mark == FOLLOWUP_WAITING_US:
        return done(LEAD_CALLBACK_DUE, _sentences(facts.opening(), facts.marked()), since=facts.marked_at)
    if office_day is not None and office_day <= today:
        return done(
            LEAD_CALLBACK_DUE, _sentences(facts.opening(), f'הצוות קבע לחזור אליו ב-{facts.day(office_day)}'),
            since=_noon(office_day),
        )
    if office_day is not None:
        if facts.wrote_after(facts.marked_at):
            # A future lead whose customer did not wait for the day: a person answers, now.
            return done(
                LEAD_WAITING_DATE,
                _sentences(facts.opening(), f'הצוות קבע לחזור אליו ב-{facts.day(office_day)}', 'הלקוח כתב לפני המועד'),
                next_step=WROTE_BEFORE_THE_DAY, since=contact.last_inbound_at, recheck_at=_midnight(office_day),
                owner=LEAD_OWNER_OFFICE,
            )
        return done(
            LEAD_WAITING_DATE, _sentences(facts.opening(), f'הצוות קבע לחזור אליו ב-{facts.day(office_day)}'),
            next_step=f'לחזור אליו ב-{facts.day(office_day)}', recheck_at=_midnight(office_day), owner='',
        )

    def named_since(fact) -> bool:
        """The customer named a day to come back on, and did so after `fact`: that day comes first."""
        return bool(
            said_day is not None and said.said_on is not None
            and (fact is None or said.said_on >= timezone.localtime(fact).date())
        )

    def the_named_day():
        """
        16–17. The day the conversation named: the bot comes back on it by itself.
        None once the day is over — it passed long ago and no round ever began, or
        a round on it ended without every step going out — and the rules below decide.
        """
        why = f'לפי השיחה, לחזור אליו ב-{facts.day(said_day)}'
        if said.come_back_why:
            why += f' ({said.come_back_why})'
        key = f'{NUDGE_DATE_ARRIVED}|{said_day.isoformat()}|{_stamp(contact.last_inbound_at)}'
        the_day = dict(delays=said.day_delays, variant=said.day_variant, then=THEN_DORMANT)
        if said_day > today:
            return by_the_bot(
                LEAD_WAITING_DATE, NUDGE_DATE_ARRIVED, key, _noon(said_day), facts.opening(), why,
                next_step=f'לחזור אליו ב-{facts.day(said_day)}', recheck_at=_midnight(said_day), **the_day,
            )
        round_ = _Round(contact, key, len(said.day_delays))
        if not (round_.began or now - _noon(said_day) <= START_WITHIN) or (round_.done and not round_.all_sent):
            return None
        result = by_the_bot(
            LEAD_CALLBACK_DUE, NUDGE_DATE_ARRIVED, key, _noon(said_day), facts.opening(), why,
            since=_noon(said_day), **the_day,
        )
        return result if result is not None else dormant(key, len(said.day_delays))

    # 11–15. what the registrations show. A day the customer named since then comes before the bot's
    #        own questions about a registration begun or a trial held.
    if outcome == OUTCOME_SIGNUP_DECLINED:
        return done(LEAD_SIGNUP_FAILED, _sentences(contact.kogo_detail or 'ניסה להירשם והחיוב נכשל', 'לא נרשם'))
    if outcome == OUTCOME_PENDING:
        waiting = the_named_day() if named_since(_detail_day(contact, today)) else None
        if waiting is not None:
            return waiting
        detail = contact.kogo_detail or 'התחיל רישום'
        began = _detail_day(contact, today) or (
            contact.lead_stage_since if contact.lead_stage == LEAD_SIGNUP_STARTED and contact.lead_stage_since else now
        )
        return by_the_bot(
            LEAD_SIGNUP_STARTED, NUDGE_LINK_REMINDER, f'{NUDGE_LINK_REMINDER}|signup|{detail[:100]}', began,
            detail, 'לא שילם', then=THEN_OFFICE, variant=VARIANT_SIGNUP,
        )
    if outcome == OUTCOME_TRIAL_UPCOMING:
        return done(LEAD_TRIAL_BOOKED, _sentences(_trial_line(contact, 'רשום לשיעור ניסיון')))
    if outcome == OUTCOME_TRIAL_ONLY:
        held = _trial_day(contact)
        waiting = the_named_day() if named_since(held) else None
        if waiting is not None:
            return waiting
        on = str((contact.kogo_trial or {}).get('on') or '')
        # Wrote on a later day than the trial: they are in touch already, and nobody asks them "how was it".
        in_touch = held is not None and facts.wrote_after(_midnight(timezone.localtime(held).date() + timedelta(days=1)))
        # What the system's own automations have already written to the family about this trial
        # (apps/enrollments; the matching keeps it beside the trial): nobody is asked the same thing
        # twice. The bot's round then begins from its second step, on the second step's own day.
        told = contact.kogo_trial if isinstance(contact.kogo_trial, dict) else {}
        if _did_not_come(contact):
            return by_the_bot(
                LEAD_TRIAL_NO_SHOW, NUDGE_TRIAL_NO_SHOW, f'{NUDGE_TRIAL_NO_SHOW}|{on}', held,
                _trial_line(contact, 'נרשם לניסיון ולא הגיע'), 'לא נרשם', then=THEN_OFFICE, since=held, in_touch=in_touch,
                from_step=2 if told.get('didnt_arrive_sent') else 1,
            )
        return by_the_bot(
            LEAD_TRIAL_DONE, NUDGE_TRIAL_FEEDBACK, f'{NUDGE_TRIAL_FEEDBACK}|{on}', held,
            _trial_line(contact, 'עשה שיעור ניסיון'), 'לא נרשם אחרי הניסיון', then=THEN_OFFICE, since=held, in_touch=in_touch,
            from_step=2 if told.get('after_test_sent') else 1,
        )

    # 16–17. the day the conversation named
    waiting = the_named_day() if said_day is not None else None
    if waiting is not None:
        return waiting

    # 17w. on the waiting list of a lesson that is full: kept, by name of the lesson, until a seat comes free
    if on_a_list:
        return done(
            LEAD_WAITING_LIST, _sentences(f'{waiting_list.ON_THE_LIST}: {waits.get("label") or ""}'.rstrip(': ')),
            since=_moment(waits.get('since')),
        )

    # 18. nothing the business has fits them today — kept, for the day something opens
    if said.blocker in lead_ai.NOTHING_TO_OFFER_BLOCKERS:
        return done(LEAD_NOTHING_TO_OFFER, _sentences(facts.opening(absolute=True), said.blocker_text))

    # 19. said they would check and come back: the bot asks once, and then it is a phone call
    if said.blocker in lead_ai.CHECKING_BLOCKERS and facts.quiet < COLD_AFTER:
        key = f'{NUDGE_WILL_CHECK}|{_stamp(contact.last_inbound_at)}|{_stamp(facts.marked_at)}'
        return by_the_bot(
            LEAD_WILL_CHECK, NUDGE_WILL_CHECK, key, facts.last_activity, facts.opening(), said.blocker_text,
            then=THEN_OFFICE, office_step=CALL_WHO_DECIDES,
        )

    # 20. got a registration link and did not register — once the last word is ours, or the talk has gone quiet
    if contact.lead_link_sent_at is not None and facts.quiet < COLD_AFTER and (
        contact.last_message_direction != DIRECTION_IN or facts.quiet >= IN_CONVERSATION_FOR
    ):
        key = f'{NUDGE_LINK_REMINDER}|{_stamp(contact.last_inbound_at)}|{_stamp(facts.marked_at)}'
        result = by_the_bot(
            LEAD_LINK_SENT, NUDGE_LINK_REMINDER, key, facts.last_activity, facts.opening(),
            f'קיבל קישור להרשמה {_when(contact.lead_link_sent_at, now)}', said.blocker_text, 'לא נרשם',
            then=THEN_DORMANT, variant=VARIANT_COMMITMENT if said.blocker == lead_ai.BLOCKER_COMMITMENT else '',
        )
        return result if result is not None else dormant(key, len(NUDGE_DELAYS[NUDGE_LINK_REMINDER]))

    # 21–22. The two stages the conversation's own clock decides — and the two kinds of lead the bot
    #        comes back to in them, once. Each round is the same one in both stages: a follow-up
    #        planned while the lead was "talking" stands when the clock calls it "silent", so a
    #        weekend between the message and the next opening of the office does not lose it.
    def asked_what_they_look_for(stage: str, *parts: str, **common):
        """Wrote nothing of their own (the ad's words, "היי"): asked what they are looking for, two hours later."""
        return by_the_bot(
            stage, NUDGE_OPENING, f'{NUDGE_OPENING}|{_stamp(contact.last_inbound_at)}', contact.last_inbound_at, *parts,
            then=THEN_STAY, start_within=OPENING_START_WITHIN, **common,
        )

    def hesitant(stage: str, *parts: str, **common):
        """
        Asked what happens if the class does not fit, and no registration link
        has gone out to them: the bot answers exactly that, a day later
        ("מתחילים משיעור ניסיון אחד"). With a link they are in rule 20, and get
        its three reminders instead.
        """
        key = f'{NUDGE_LINK_REMINDER}|{VARIANT_COMMITMENT}|{_stamp(contact.last_inbound_at)}|{_stamp(facts.marked_at)}'
        return by_the_bot(
            stage, NUDGE_LINK_REMINDER, key, facts.last_activity, *parts,
            then=THEN_STAY, variant=VARIANT_COMMITMENT, delays=(DAY,), **common,
        )

    said_nothing = said.empty_opening and contact.last_inbound_at is not None
    # (An external branch is told apart in the sentences of these two stages themselves.)
    afraid_to_commit = said.blocker == lead_ai.BLOCKER_COMMITMENT and not said.external

    # 21. still talking
    if facts.quiet < IN_CONVERSATION_FOR:
        talking = dict(since=facts.last_activity, recheck_at=facts.last_activity + IN_CONVERSATION_FOR)
        if said_nothing:
            return asked_what_they_look_for(LEAD_IN_CONVERSATION, facts.opening(), facts.who_answered(), **talking)
        if afraid_to_commit:
            return hesitant(LEAD_IN_CONVERSATION, facts.opening(), facts.who_answered(), said.blocker_text, **talking)
        return done(LEAD_IN_CONVERSATION, _sentences(facts.opening(), facts.who_answered()), **talking)

    # 22. answered, and silent since
    if SILENT_AFTER <= facts.quiet < COLD_AFTER:
        if facts.mark == FOLLOWUP_NO_ANSWER and facts.mark_is_newest:
            next_step = TRY_AGAIN
        elif not facts.person_spoke:
            next_step = NOBODY_SPOKE_YET
        else:
            next_step = None
        silent = dict(next_step=next_step, since=facts.last_activity, recheck_at=facts.last_activity + COLD_AFTER)
        words = (facts.opening(), facts.who_answered(), said.blocker_text, 'לא נרשם')
        if said_nothing and not said.external:
            return asked_what_they_look_for(LEAD_ANSWERED_SILENT, *words, **silent)
        if afraid_to_commit:
            return hesitant(LEAD_ANSWERED_SILENT, *words, **silent)
        return done(
            LEAD_ANSWERED_SILENT,
            _sentences(
                facts.opening(), facts.who_answered(), said.blocker_text, EXTERNAL_BRANCH if said.external else '', 'לא נרשם',
            ),
            next_step=next_step, since=facts.last_activity, recheck_at=facts.last_activity + COLD_AFTER,
            owner='' if said.external else None,
        )

    # 23. dormant
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


_NOT_ASKED = object()
OWNER_WORDS = {LEAD_OWNER_BOT: 'הבוט', LEAD_OWNER_OFFICE: 'המשרד', '': 'אין צעד כרגע'}


def refresh(contact, *, now=None, fresh: bool = False, human_touched: bool | None = None,
            open_follow=_NOT_ASKED, follow: bool = True) -> bool:
    """
    Work one contact's stage out again and keep it. True when what the screen
    shows changed (stage, since, why, what to do, who makes the next move, the
    "a person wrote" mark, the columns the lists filter by).

    `contact` is a Contact or its id; the row is read again unless `fresh` says
    the one in hand was just read (the scan). Only the lead_* fields named here
    are written, and only when they changed: a look that finds nothing new moves
    `lead_checked_at` alone and wakes no screen. A change of stage, or of owner
    within a stage, is a line in the journal; the first stage a contact gets is not.

    The write holds only if nobody touched the contact since it was read. If
    somebody did, nothing is written and the contact stays marked for the next
    look — a stage worked out from an older row never covers a newer fact.

    Last, the bot's round of follow-ups is brought in line with the stage
    (lead_followups.sync) when the one that is open is not the one the rules
    ask for. `open_follow` is the round open now when the caller already knows
    it (the scan reads them for a whole batch); `follow=False` leaves the
    follow-ups alone — for lead_followups itself, which is in the middle of them.
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
    # A row the cron has not looked at since the stage got its present name.
    was = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    since = result['since']
    if since is None and stage:
        # No fact dates this stage: it began when it was first seen.
        since = contact.lead_stage_since if stage == was and contact.lead_stage_since else now
    before = (
        contact.lead_stage, contact.lead_stage_since, contact.lead_reason, contact.lead_next_step, was_touched,
        contact.lead_owner, contact.lead_branch_id, contact.lead_course_type, contact.lead_come_back_on,
    )
    after = (
        stage, since, result['reason'], result['next_step'], contact.lead_human_touched,
        result['owner'], result['branch_id'], result['course_type'], result['come_back_on'],
    )

    unchanged_since_read = Contact.objects.filter(pk=contact.pk, touched_at=contact.touched_at)
    moment = timezone.now()
    if before == after:
        unchanged_since_read.update(lead_checked_at=moment, lead_recheck_at=result['recheck_at'], lead_version=RULES_VERSION)
        changed = False
    else:
        kept = unchanged_since_read.update(
            lead_stage=stage,
            lead_stage_since=since,
            lead_reason=result['reason'],
            lead_next_step=result['next_step'],
            lead_human_touched=contact.lead_human_touched,
            lead_owner=result['owner'],
            lead_branch_id=result['branch_id'],
            lead_course_type=result['course_type'],
            lead_come_back_on=result['come_back_on'],
            lead_checked_at=moment,
            lead_recheck_at=result['recheck_at'],
            lead_version=RULES_VERSION,
            # The screen has to see it; and checked at the same instant, so the scan does not come back for it.
            touched_at=moment,
            updated_at=moment,
        )
        if not kept:
            return False
        if was and was != stage:
            state.log_event(
                contact.pk, EVENT_LEAD_STAGE,
                f'{STAGE_LABELS.get(was, was)} ← {result["label"] or "לא ליד לחוגים"}'
                + (f' ({result["reason"]})' if result['reason'] else ''),
            )
        elif was and contact.lead_version >= RULES_VERSION and contact.lead_owner != result['owner']:
            state.log_event(
                contact.pk, EVENT_LEAD_STAGE,
                f'{result["label"]} · הצעד הבא: {OWNER_WORDS.get(contact.lead_owner, "")} ← {OWNER_WORDS.get(result["owner"], "")}',
            )
        changed = True
    if follow:
        _keep_round(contact.pk, result, open_follow, now, behind=_round_is_behind(contact, result['follow']))
    return changed


def _round_is_behind(contact: Contact, follow: dict | None) -> bool:
    """
    The round that is open is the right one, but the rules now have it begin
    from a later step than it stands on: one of the system's own automations
    has meanwhile said what its open step would say.
    """
    kept = contact.lead_bot if isinstance(contact.lead_bot, dict) else {}
    if follow is None or kept.get('episode') != follow['key'] or kept.get('done'):
        return False
    return int(kept.get('passed') or 0) < int(follow.get('from_step') or 1) - 1


def _keep_round(contact_id, result: dict, open_follow, now, *, behind: bool = False) -> None:
    """The bot's open follow-up is the one the rules ask for — or it is put right. Never raises."""
    from apps.wahub import lead_followups

    try:
        wanted = (result['follow'] or {}).get('key', '')
        have = lead_followups.open_key(contact_id) if open_follow is _NOT_ASKED else (open_follow or '')
        if wanted != have or behind:
            lead_followups.sync(contact_id, now=now)
    except Exception:
        logger.exception('wahub lead stage: the follow-ups of contact %s were not brought in line', contact_id)


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
    """
    Contacts the scan still has to look at: never looked at, touched since,
    their time has come — or looked at under an older version of the rules.
    """
    return (
        Q(lead_checked_at__isnull=True) | Q(touched_at__gt=F('lead_checked_at')) | Q(lead_recheck_at__lte=now)
        | Q(lead_version__lt=RULES_VERSION)
    )


def pending_count(now=None) -> int:
    return Contact.objects.filter(pending_q(now or timezone.now())).count()


def scan(budget_seconds: float = SCAN_SECONDS, *, now=None, batch: int = SCAN_BATCH) -> dict:
    """
    The cron's look, inside its time: first the contacts something happened to
    since their stage was worked out (and those that never had one — the first
    runs fill the table a batch at a time, and so does a new version of the
    rules), then the contacts whose time has
    come (`lead_recheck_at`: thirty minutes with no reply, two days of quiet, a
    day to come back, and every open lead once a day, after midnight).

    Every contact is finished and saved on its own, so a call that runs out of
    time loses nothing and the next one carries on.
    """
    from apps.wahub import lead_followups

    started = time.monotonic()
    now = now or timezone.now()
    counts = {'lead_checked': 0, 'lead_changed': 0}

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    touched = Contact.objects.filter(
        Q(lead_checked_at__isnull=True) | Q(touched_at__gt=F('lead_checked_at')) | Q(lead_version__lt=RULES_VERSION)
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
        rounds = lead_followups.open_keys(row.pk for row in rows)
        for row in rows:
            if left() <= 0:
                break
            try:
                changed = refresh(
                    row, now=now, fresh=True, human_touched=row.pk in wrote, open_follow=rounds.get(row.pk, ''),
                )
            except Exception:
                logger.exception('wahub lead stage: contact %s failed', row.pk)
                # Left as it is, it would be first in line again every five minutes.
                moment = timezone.now()
                Contact.objects.filter(pk=row.pk).update(
                    lead_checked_at=moment, lead_recheck_at=moment + RETRY_AFTER, lead_version=RULES_VERSION,
                )
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
