"""
The live test from the owner's own WhatsApp (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב).

For a contact marked `live_test` (the owner's number, muted for the old bot in
ManyChat by handoff.start_live_test) the NEW bot answers for real: ManyChat's
copy of his message lands in inbound/manychat/, which at once calls
live-test/reply/ on this same server and does not wait for it (`kick`). That
call claims the contact, waits until the burst has been quiet for a few seconds
(a new message restarts the wait, three times at most) — fetching and
transcribing what he sent meanwhile — computes the answer with the very code the
demo conversation uses (shadow.answer_now) and sends it through ManyChat
(sending.send_new_bot_reply). The answer is kept in the conversation with
sender "bot", `new_bot=True` and the status the send got; the timings are
written in the contact's journal so the delay can be measured.

An answer may be followed by an automation — the holidays board of a branch
(holiday_flows.py): what the answer's own tool call named (shadow.flows_to_send)
leaves through ManyChat right after the text (sending.send_new_bot_flow), once,
and only after a text that did leave. One that does not leave is said as a text
that did not: in the journal, with the reason, and "needs a person".

One answer for a burst: people write three short messages seven seconds apart.
The computation starts early (SETTLE seconds of quiet) so a single message is
not kept waiting, but nothing leaves before he has been quiet for QUIET
seconds — and a message that arrives before the answer left drops that answer
(shadow.Superseded: nothing kept, nothing sent) and the computation starts
over on everything he wrote. MAX_RESTARTS times at most; then what is ready is
sent, so somebody who never stops writing still gets an answer.

Guards: one answer at a time per contact (the claim), one answer per five
seconds, thirty a day (a loop would otherwise talk to itself), nothing while a
person holds the conversation. A failure of Claude — or no Claude key at all —
sends nothing and marks the contact "needs a person".

The same path answers EVERY contact once the owner has chosen the mode "all"
and sending is on (bot_mode.py, docs/WAHUB-CONTRACT-GO-LIVE.md) — a demo
contact never, and never a message that reached Kogo before the mode became
live (`bot_mode.live_since`): every step that could answer one looks at that
moment again. For those contacts there are a few things more: twelve answers
an hour at the most, and when Claude fails the customer is told, once in three
hours, that a person will come back to them — with the phrasing the bot's
knowledge already has.

A message is not lost: a self-call that did not leave, or a call that was cut
before it answered, is started again by the cron while the message is still
young (`answer_late`) — the same call, the same claim, so never a second
answer; a message nobody answered in time is said on the contact
(`flag_unanswered`), and so is an answer that was computed with no sign that
it left — that one is never sent again. Every outcome is counted
(bot_mode.record), and a run of failures stops the mode by itself.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.wahub import birthday, bot_mode, knowledge, sending, shadow, state, waiting_list
from apps.wahub.models import (
    DELIVERED_STATUSES,
    DIRECTION_IN,
    DIRECTION_OUT,
    EVENT_LIVE_TEST,
    EVENT_NEEDS_HUMAN,
    HANDLED_HUMAN,
    KIND_PHRASING,
    TYPE_TEXT,
    Contact,
    ContactEvent,
    KnowledgeItem,
    Message,
    ShadowReply,
)

logger = logging.getLogger(__name__)

REPLY_PATH = 'live-test/reply/'
# Connect quickly, read for a second: the answer takes seconds and this call does not wait for it.
KICK_TIMEOUT = (2, 1)
DEFAULT_SETTLE_SECONDS = 4.0
# No answer leaves before the customer has been quiet for this long (he may be in the middle of writing).
DEFAULT_QUIET_SECONDS = 10.0
# How often the wait for that quiet looks whether he wrote again, and what it leaves of the deadline for the send.
HOLD_STEP = 1.0
HOLD_RESERVE = 5.0
# An answer dropped because he wrote again is computed again, this many times at most; then what is ready leaves.
MAX_RESTARTS = 3
# A message that arrives during the wait restarts it, this many waits at most (4 + 4 + 4 = 12 seconds).
MAX_SETTLE_ROUNDS = 3
# One answer, from the claim to the send: the wait, the files, Claude.
REPLY_SECONDS = 50
# What one call answers: the burst that started it, and whatever arrived while an answer was being
# computed — as long as this many seconds have not passed since the call began, and this many answers at most.
FOLLOW_UP_SECONDS = 120
MAX_ANSWERS = 6
# A claim nobody released (a cut call) expires by itself. Every computation that starts over takes it anew.
CLAIM_SECONDS = 90
# The contact's row held by somebody else for a moment: asked for again this many times, this far apart.
CLAIM_TRIES = 5
CLAIM_RETRY_SECONDS = 0.2
MIN_GAP = timedelta(seconds=5)
DAILY_LIMIT = 30
# A contact answered because the mode is "all" (not the owner's own test number): this many answers an
# hour at the most — somebody else's automatic reply talking to the bot — then a person is asked for.
HOURLY_LIMIT = 12
# A customer is waiting: Claude's ceiling is lower than the shadow's 16,000 (thinking counts toward it).
DEFAULT_MAX_TOKENS = 4000

MODEL_FAILED_REASON = 'הבוט החדש לא ענה (בדיקה חיה)'
SEND_FAILED_REASON = 'השליחה ללקוח נכשלה (בדיקה חיה)'
FLOW_FAILED_REASON = 'האוטומציה שאחרי התשובה לא נשלחה (בדיקה חיה)'
# The same reasons on a contact that is no test number say nothing about a test (`_why`).
TEST_MARK = ' (בדיקה חיה)'
NO_KEY = 'אין בשרת מפתח Anthropic — אין בוט חדש שיענה'
# How the journal line of an answer that went out begins (the cron's look tells it from a line about a failure).
SENT_LINE = 'תשובה חיה נשלחה'
EMPTY_ANSWER = 'תשובה ריקה'
NOT_STARTED = 'התשובה החיה לא הופעלה'
NEVER_ANSWERED = 'הודעה בבדיקה החיה לא נענתה'
NEVER_ANSWERED_PLAIN = 'הודעה לא נענתה'
# An answer was computed and nothing says it left (the call was cut between the two): never sent again.
UNKNOWN_SENT = 'חושבה תשובה ולא ידוע אם נשלחה'
# The journal lines that say nothing about what became of a message: an answer that went out is about
# other messages, and a self-call that did not leave is started again by the cron.
SILENT_LINES = (SENT_LINE, NOT_STARTED)
# The cron's look. A message this young is left alone: its own call may still be on its way.
UNANSWERED_AFTER = timedelta(minutes=1)
# Older than that and younger than this, with no call having answered it: answered now, late.
LATE_WITHIN = timedelta(minutes=15)
# Older still: no answer is sent, by any path — it is said on the contact and a person takes it.
TOO_OLD = timedelta(minutes=20)
# An answer computed this long ago with no sign that it left is said on the contact.
UNKNOWN_AFTER = timedelta(minutes=2)
# How far back the look goes (hours in which the cron did not run are still said in the end), how many
# contacts it reads — the latest to write — how many late answers one call starts, and how long starting
# them may take.
LOOK_BACK = timedelta(hours=12)
WATCHED = 200
LATE_PER_RUN = 8
LATE_SECONDS = 12.0
LATE_NOTE = 'נענתה באיחור (רשת הביטחון)'
# A business's automatic reply gets no answer (shadow.propose); said once, so the office sees why and the cron's
# look does not come back to it.
AUTOMATIC_LINE = 'לא נענתה: מענה אוטומטי של עסק, לא שאלה של לקוח'
# Somebody else wrote to the customer while the answer was being computed: the new bot keeps its own.
ANSWERED_MEANWHILE = 'תשובה חיה לא נשלחה: מישהו אחר ענה ללקוח בינתיים (המשרד, או הבוט הישן שעדיין מחובר ב-ManyChat)'
UNEXPECTED = 'תקלה לא צפויה'
DAILY_LIMIT_TEXT = 'הגבלה: 30 תשובות חיות ביום — הבוט החדש לא עונה יותר היום'
HOURLY_LIMIT_TEXT = f'הגבלה: {HOURLY_LIMIT} תשובות בשעה — הבוט החדש הפסיק לענות לו, כנראה מענה אוטומטי שמתכתב איתו'
# Claude failed while the bot answers everybody: the customer is told a person will come back, with the
# phrasing the knowledge has for a handoff — once in this long per contact, not on every message.
HANDOFF_PHRASING = 'handoff_notice'
FALLBACK_EVERY = timedelta(hours=3)
FALLBACK_LINE = 'נשלח ללקוח נוסח ההעברה לנציג'
PING_TIMEOUT = (3, 8)

OUTCOME_SENT = 'sent'
OUTCOME_FAILED = 'failed'
OUTCOME_BUSY = 'busy'
OUTCOME_NOTHING = 'nothing'
OUTCOME_NOT_FOUND = 'not_found'
OUTCOME_NOT_LIVE_TEST = 'not_live_test'
OUTCOME_HUMAN = 'human_handles'
OUTCOME_DAILY_LIMIT = 'daily_limit'
OUTCOME_HOURLY_LIMIT = 'hourly_limit'
OUTCOME_TOO_OLD = 'too_old'
OUTCOME_ANSWERED = 'answered_by_another'
OUTCOME_MODEL_ERROR = 'model_error'


def settle_seconds() -> float:
    try:
        value = float(getattr(settings, 'WAHUB_LIVE_TEST_SETTLE_SECONDS', DEFAULT_SETTLE_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_SETTLE_SECONDS
    return max(0.0, value)


def quiet_seconds() -> float:
    try:
        value = float(getattr(settings, 'WAHUB_LIVE_TEST_QUIET_SECONDS', DEFAULT_QUIET_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_QUIET_SECONDS
    return max(0.0, value)


def max_tokens() -> int:
    try:
        return max(1000, int(getattr(settings, 'WAHUB_LIVE_TEST_MAX_TOKENS', DEFAULT_MAX_TOKENS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS


def effort() -> str:
    """Claude's effort for a live answer; '' leaves the shadow's own (the same answer the demo conversation gets)."""
    return str(getattr(settings, 'WAHUB_LIVE_TEST_EFFORT', '') or '').strip()


def cron_token() -> str:
    """The token this server accepts on its cron doors; the self-call proves itself with it."""
    from apps.customers.views import _cron_allowed_secrets

    allowed = sorted(_cron_allowed_secrets())
    return allowed[0] if allowed else ''


# --- the self-call ------------------------------------------------------------------------------------

def kick(contact_id: int, url: str, *, late: bool = False) -> bool:
    """
    Start the live answer for a contact: POST our own live-test/reply/ and let
    go. The read timeout is the normal way out — the answer takes seconds, and
    ManyChat, which is waiting on the inbound door, gets its 200 at once. True
    when the call left; False when it did not (no token, no connection, a
    refusal) — the log says why, and so does the contact's journal. The message
    is not given up on: the cron starts the call again while the message is
    young (`answer_late`, which passes `late`), and says so on the contact when
    it no longer is (`flag_unanswered`).
    """
    token = cron_token()
    if not token:
        return _not_started(contact_id, 'אין בשרת אסימון cron (CRON_TOKEN)')
    body = {'contact': contact_id, 'late': True} if late else {'contact': contact_id}
    try:
        response = requests.post(url, json=body, headers={'X-Cron-Token': token}, timeout=KICK_TIMEOUT)
    except requests.ReadTimeout:
        logger.info('wahub live test: the answer for contact %s was started (not waited for)', contact_id)
        return True
    except requests.RequestException as exc:
        return _not_started(contact_id, f'השרת לא הגיע לעצמו ({type(exc).__name__})')
    if response.status_code != 200:
        return _not_started(contact_id, f'השרת ענה לעצמו {response.status_code}')
    logger.info('wahub live test: the self-call for contact %s came back at once', contact_id)
    return True


def _not_started(contact_id: int, why: str) -> bool:
    logger.warning('wahub live test: the answer for contact %s was not started: %s', contact_id, why)
    try:
        state.log_event(contact_id, EVENT_LIVE_TEST, f'{NOT_STARTED}: {why}')
    except Exception:
        logger.exception('wahub live test: the fault of contact %s could not be written down', contact_id)
    bot_mode.record(bot_mode.KIND_NOT_STARTED, text=f'{NOT_STARTED}: {why}')
    return False


def ping(url: str) -> str:
    """
    Does this server reach its own live-test/reply/ — the address, the token,
    the route? Asked before the mode may become "all" (bot_mode.readiness):
    the door starts every answer with this very call. '' when it does;
    otherwise why not, in Hebrew. The call answers nobody and sends nothing.
    """
    token = cron_token()
    if not token:
        return 'אין בשרת אסימון cron (CRON_TOKEN)'
    try:
        response = requests.post(url, json={'ping': True}, headers={'X-Cron-Token': token}, timeout=PING_TIMEOUT)
    except requests.RequestException as exc:
        return f'השרת לא הגיע לעצמו ({type(exc).__name__})'
    if response.status_code != 200:
        return f'השרת ענה לעצמו {response.status_code}'
    try:
        answered = response.json().get('pong') is True
    except (ValueError, AttributeError):
        answered = False
    return '' if answered else 'בכתובת של השרת ענה משהו אחר'


# --- the claim ------------------------------------------------------------------------------------------

def claim(contact_id: int) -> tuple[Contact | None, str]:
    """
    Take the contact for one live answer: (contact, '') when taken, else
    (None, why) — busy (another call is answering), not_found, not_live_test
    (the new bot does not answer this contact: bot_mode.answered_live).

    What says "another call is answering" is `live_test_busy_until`; the row
    lock only makes reading and writing it one step, and covers the claim
    alone, not the answer — the inbound door locks the same row for every
    message, and a customer's next message must not wait behind Claude. A row
    somebody else holds is not waited for (skip_locked): it is held for a
    moment by the door storing his next message, so it is asked for again a
    few times before this call gives up as "busy".
    """
    for attempt in range(CLAIM_TRIES):
        now = timezone.now()
        with transaction.atomic():
            contact = Contact.objects.select_for_update(skip_locked=True).filter(pk=contact_id).first()
            if contact is not None:
                if not bot_mode.answered_live(contact)[0]:
                    return None, OUTCOME_NOT_LIVE_TEST
                if contact.live_test_busy_until and contact.live_test_busy_until > now:
                    return None, OUTCOME_BUSY
                until = now + timedelta(seconds=CLAIM_SECONDS)
                Contact.objects.filter(pk=contact.pk).update(live_test_busy_until=until)
                contact.live_test_busy_until = until
                return contact, ''
        if not Contact.objects.filter(pk=contact_id).exists():
            return None, OUTCOME_NOT_FOUND
        if attempt + 1 < CLAIM_TRIES:
            time.sleep(CLAIM_RETRY_SECONDS)
    return None, OUTCOME_BUSY


def _release(contact_id: int, until) -> None:
    """Give the claim back — this call's own claim: one that expired and was taken by another call is that call's."""
    Contact.objects.filter(pk=contact_id, live_test_busy_until=until).update(live_test_busy_until=None)


def _renew(contact_id: int, held: dict) -> bool:
    """
    The claim, taken anew for a computation that starts over. False when it is
    no longer this call's (it expired and another call took the contact): then
    this call stops, and that one answers.
    """
    until = timezone.now() + timedelta(seconds=CLAIM_SECONDS)
    if not Contact.objects.filter(pk=contact_id, live_test_busy_until=held['until']).update(live_test_busy_until=until):
        held['until'] = None
        return False
    held['until'] = until
    return True


def _now():
    """The clock the wait is measured by."""
    return timezone.now()


# --- the wait --------------------------------------------------------------------------------------------

def _newest_inbound(contact_id: int):
    """(id, when it reached Kogo) of the customer's last message; None when there is none."""
    return Message.objects.filter(contact_id=contact_id, direction=DIRECTION_IN).order_by('-id').values_list('id', 'created_at').first()


def _settle(contact: Contact, *, deadline: float) -> Contact:
    """
    Wait for the burst to end: until `settle_seconds` have passed since the
    customer's last message reached Kogo, and again from every message that
    arrives meanwhile, MAX_SETTLE_ROUNDS waits at most — then everything
    unanswered gets one answer. The same wait keeps two answers five seconds
    apart. It is not idle: the files he sent are fetched and the recordings
    transcribed in it (shadow.prepare_media), so the draft finds them ready.
    """
    seconds = settle_seconds()
    longest = max(seconds, MIN_GAP.total_seconds())
    last_answer_at = Message.objects.filter(contact=contact, new_bot=True).order_by('-id').values_list('sent_at', flat=True).first()
    prepared: list[int] = []
    for _round in range(MAX_SETTLE_ROUNDS):
        newest = _newest_inbound(contact.pk)
        prepared += shadow.prepare_media(contact, deadline=deadline, skip=prepared)
        now = _now()
        wait = seconds - (now - newest[1]).total_seconds() if newest else 0.0
        if last_answer_at is not None:
            wait = max(wait, (last_answer_at + MIN_GAP - now).total_seconds())
        wait = min(wait, longest)
        if wait > 0:
            time.sleep(wait)
        if _newest_inbound(contact.pk) == newest:
            break
    return Contact.objects.get(pk=contact.pk)


def _written_since(contact_id: int, message_id: int) -> bool:
    """Did the customer write after that message?"""
    return Message.objects.filter(contact_id=contact_id, direction=DIRECTION_IN, id__gt=message_id).exists()


def _hold(contact_id: int, last_id: int, deadline: float) -> None:
    """
    The answer is ready: it does not leave before the customer has been quiet
    for `quiet_seconds` since the last message it covers. Usually there is
    nothing left to wait — Claude took longer than that. Looks every HOLD_STEP
    whether he wrote again and returns at once when he did (the caller's look
    then drops the answer); never eats the time the send needs.
    """
    newest = _newest_inbound(contact_id)
    if newest is None or newest[0] != last_id:
        return
    remaining = quiet_seconds() - (_now() - newest[1]).total_seconds()
    while remaining > 0:
        step = min(remaining, HOLD_STEP)
        if time.monotonic() + step > deadline - HOLD_RESERVE:
            return
        time.sleep(step)
        remaining -= step
        if _written_since(contact_id, last_id):
            return


# --- what is written when it fails ----------------------------------------------------------------------

def _why(contact: Contact, reason: str) -> str:
    """A reason as it is kept on the contact: only a test number's says "(בדיקה חיה)"."""
    return reason if contact.live_test else reason.replace(TEST_MARK, '')


def _needs_human(contact_id: int, reason: str) -> None:
    """Mark the contact "needs a person", once: a contact already marked keeps its own reason."""
    reason = reason[:200]
    moment = timezone.now()
    marked = Contact.objects.filter(pk=contact_id, needs_human=False).update(
        needs_human=True, needs_human_reason=reason, needs_human_at=moment, touched_at=moment, updated_at=moment,
    )
    if marked:
        state.log_event(contact_id, EVENT_NEEDS_HUMAN, reason)
        bot_mode.count('needs_human')


def _clock(moment) -> str:
    return timezone.localtime(moment).strftime('%H:%M:%S.%f')[:-3] if moment else '—'


def _secs(start, end) -> str:
    return f'{(end - start).total_seconds():.1f} שנ\''


def _day_start(now):
    local = timezone.localtime(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _answers_since(contact: Contact, moment) -> int:
    return Message.objects.filter(contact=contact, new_bot=True, sent_at__gte=moment).count()


def _limited(contact: Contact, now) -> str:
    """
    The limit this contact is over — OUTCOME_DAILY_LIMIT, OUTCOME_HOURLY_LIMIT —
    or ''. Thirty a day for everybody; twelve an hour for a contact that is
    answered because the mode is "all" (the owner's own test number is not
    somebody's automatic reply). Reads only.
    """
    if _answers_since(contact, _day_start(now)) >= DAILY_LIMIT:
        return OUTCOME_DAILY_LIMIT
    if not contact.live_test and _answers_since(contact, now - timedelta(hours=1)) >= HOURLY_LIMIT:
        return OUTCOME_HOURLY_LIMIT
    return ''


def _over_limit(contact: Contact, now) -> str:
    """
    `_limited`, said on the contact the first time: a line in its journal, and —
    for a contact that is no test number — "needs a person", because the bot
    has stopped answering somebody real.
    """
    limit = _limited(contact, now)
    if not limit:
        return ''
    text, since = (DAILY_LIMIT_TEXT, _day_start(now)) if limit == OUTCOME_DAILY_LIMIT else (HOURLY_LIMIT_TEXT, now - timedelta(hours=1))
    already = ContactEvent.objects.filter(contact=contact, kind=EVENT_LIVE_TEST, text=text, created_at__gte=since).exists()
    if not already:
        state.log_event(contact.pk, EVENT_LIVE_TEST, text)
        if not contact.live_test:
            _needs_human(contact.pk, text)
    logger.warning('wahub live test: contact %s reached a limit (%s)', contact.pk, limit)
    return limit


def _handoff_words(now) -> str:
    """
    What a customer is told when a person will come back to them, in words the
    bot's knowledge already has — the bot's own rule for a handoff: outside the
    office hours, the closed message the owner filed for that day; otherwise
    the phrasing "נציג יחזור". '' when there are no such words (he turned the
    phrasing off): then nothing is said.
    """
    try:
        hours = knowledge.office_hours_now(now)
        closed = str(hours.get('message_if_closed') or '').strip()
        if hours.get('configured') and not hours.get('open') and closed and closed != knowledge.DEFAULT_CLOSED_MESSAGE:
            return closed
    except Exception:
        logger.exception('wahub live test: the office hours could not be read for the handoff words')
    today = timezone.localtime(now).date()
    for item in KnowledgeItem.objects.filter(kind=KIND_PHRASING, key=HANDOFF_PHRASING, is_active=True).order_by('-id'):
        if knowledge._valid_on(item, today) and (item.body or '').strip():
            return item.body.strip()
    return ''


def _say_a_person_will_answer(contact: Contact) -> str:
    """
    Claude failed while the bot answers everybody: the customer is not left
    with nothing. The handoff words go out through the same door as every
    answer, once in FALLBACK_EVERY per contact — the journal line it leaves is
    what the next failure looks for, also when the send itself did not get
    through (a send is never repeated by itself). In the mode "test" nothing is
    sent, as before. Returns what to add to the failure's own line; '' when
    nothing went out. Never raises.
    """
    try:
        if bot_mode.live_since() is None:
            return ''
        contact.refresh_from_db()
        if contact.is_demo or contact.handled_by == HANDLED_HUMAN:
            return ''
        now = timezone.now()
        told = ContactEvent.objects.filter(
            contact=contact, kind=EVENT_LIVE_TEST, text__startswith=FALLBACK_LINE, created_at__gte=now - FALLBACK_EVERY,
        )
        if told.exists():
            return ''
        words = _handoff_words(now)
        if not words:
            return ''
        message = sending.send_new_bot_reply(contact, words)
        if message.status in DELIVERED_STATUSES:
            state.log_event(contact.pk, EVENT_LIVE_TEST, FALLBACK_LINE)
            bot_mode.count('fallback')
            return FALLBACK_LINE
        state.log_event(contact.pk, EVENT_LIVE_TEST, f'{FALLBACK_LINE} — לא יצא: {message.error}')
    except Exception:
        logger.exception('wahub live test: contact %s could not be told that a person will answer', contact.pk)
    return ''


def _no_answer(contact: Contact, reason: str, received, started) -> dict:
    """
    Claude gave no answer: the contact asks for a person and the journal says
    why. No answer is sent — but while the bot answers everybody the customer
    is told that a person will come back (`_say_a_person_will_answer`).
    """
    _needs_human(contact.pk, _why(contact, f'{MODEL_FAILED_REASON}: {reason}'))
    told = _say_a_person_will_answer(contact)
    times = f'(התקבלה {_clock(received)}, חישוב מ-{_clock(started)})'
    text = f'תשובה חיה נכשלה: {reason} {times} — {told}' if told else f'תשובה חיה נכשלה, לא נשלח דבר: {reason} {times}'
    state.log_event(contact.pk, EVENT_LIVE_TEST, text)
    logger.warning('wahub live test: contact %s — %s', contact.pk, text)
    bot_mode.record(bot_mode.KIND_MODEL, text=reason)
    return {'outcome': OUTCOME_MODEL_ERROR, 'error': reason, **({'fallback': True} if told else {})}


# --- the cron's look: nothing is lost ----------------------------------------------------------------------

def _waiting(contact: Contact, since) -> list:
    """
    The customer's messages no answer covers yet — and, for a contact answered
    because the mode is "all" (`since` is when it became live), only those that
    reached Kogo from that moment on. What came before is never answered.
    """
    rows = shadow.unanswered_messages(contact)
    return rows if since is None else [row for row in rows if row.created_at >= since]


def _nobody_asked(contact: Contact, since) -> bool:
    """Nothing to answer here: no message of the mode's time, or a business's automatic reply (shadow.propose answers none)."""
    rows = _waiting(contact, since)
    if not rows:
        return True
    last_at = rows[-1].sent_at
    burst = [row for row in rows if last_at - row.sent_at <= shadow.MERGE_WINDOW * 3] or rows[-1:]
    return all(row.message_type == TYPE_TEXT and shadow.is_automatic_reply(row.text) for row in burst)


def _overdue(now) -> list:
    """
    Who wrote, is answered by the new bot, and has no answer and no explanation:
    [{contact, last, computed, since}], whoever has waited longest first.
    `last` is the customer's last message; `computed` says an answer to it was
    worked out (and nothing shows that it left); `since` is the moment before
    which this contact's messages are not answered (None for a test number).

    Six queries whatever the number of contacts — the mode "all" makes every
    contact one of these, so nothing here is asked contact by contact. Looked
    at: whoever wrote lately and either still has a message no answer was
    computed for (`needs_shadow`) or wrote last in the conversation. Left out:
    a demo contact, a conversation a person holds, a contact a call is
    answering this moment, whoever somebody else wrote to since (the office,
    the old bot), a message the journal already speaks of, and — always — a
    message from before the mode became live.
    """
    bot_mode.sync()
    since = bot_mode.live_since()
    rows = Contact.objects.filter(is_demo=False, last_inbound_at__gte=now - LOOK_BACK).filter(
        Q(needs_shadow=True) | Q(last_message_direction=DIRECTION_IN),
    ).exclude(handled_by=HANDLED_HUMAN).filter(Q(live_test_busy_until__isnull=True) | Q(live_test_busy_until__lte=now))
    if since is None:
        rows = rows.filter(live_test=True)
    else:
        # The row's own time is ManyChat's when it gave one; the message's arrival is looked at below.
        rows = rows.filter(Q(live_test=True) | Q(last_inbound_at__gte=since - timedelta(minutes=5)))
    # The latest to write are the ones read (what is already said on a contact must not crowd out a new
    # message), and whoever has waited longest among them comes first.
    latest = list(rows.order_by('-last_inbound_at', '-id')[:WATCHED])
    contacts = {contact.id: contact for contact in reversed(latest)}
    if not contacts:
        return []
    last_ids = (
        Message.objects.filter(contact_id__in=contacts, direction=DIRECTION_IN)
        .values('contact_id').annotate(last=Max('id')).values_list('last', flat=True)
    )
    lasts = {
        message.contact_id: message
        for message in Message.objects.filter(id__in=list(last_ids)).only('id', 'contact_id', 'created_at')
    }
    if not lasts:
        return []
    computed = dict(
        ShadowReply.objects.filter(contact_id__in=contacts, after_message_id__isnull=False)
        .values('contact_id').annotate(last=Max('after_message_id')).values_list('contact_id', 'last')
    )
    # Somebody other than the new bot wrote to him since (the office, the old bot's copy): the new bot's own
    # lines answer other messages, and a line that did not leave answered nobody.
    answered = dict(
        Message.objects.filter(contact_id__in=contacts, direction=DIRECTION_OUT, new_bot=False, status__in=DELIVERED_STATUSES)
        .values('contact_id').annotate(last=Max('id')).values_list('contact_id', 'last')
    )
    oldest = min(message.created_at for message in lasts.values())
    said: dict = {}
    spoken = ContactEvent.objects.filter(contact_id__in=contacts, kind=EVENT_LIVE_TEST, created_at__gte=oldest)
    for contact_id, text, created_at in spoken.values_list('contact_id', 'text', 'created_at'):
        if not text.startswith(SILENT_LINES):
            said[contact_id] = max(said.get(contact_id, created_at), created_at)
    overdue = []
    for contact_id, contact in contacts.items():
        last = lasts.get(contact_id)
        if last is None or last.created_at > now - UNANSWERED_AFTER or answered.get(contact_id, 0) > last.id:
            continue
        mine = None if contact.live_test else since
        if not contact.live_test and (since is None or last.created_at < since):
            continue
        # The journal already says what happened to it (a failure, a limit, this very look), or the
        # test was turned on after it.
        if contact_id in said and said[contact_id] >= last.created_at:
            continue
        overdue.append({'contact': contact, 'last': last, 'computed': computed.get(contact_id, 0) >= last.id, 'since': mine})
    return overdue


def flag_unanswered(now=None) -> int:
    """
    The cron's look, every five minutes, at what will not be answered any more:
    a message nobody answered within LATE_WITHIN — its self-call never arrived
    and the late tries did not get through either — and an answer that was
    computed with no sign that it left (the call was cut between the two). Both
    are said on the contact: "needs a person" and a line in the journal, once.
    Nothing is sent from here; an answer that may have left is never sent
    again. A younger message is `answer_late`'s. Returns how many contacts were
    marked.
    """
    now = now or timezone.now()
    marked = 0
    for row in _overdue(now):
        contact, last = row['contact'], row['last']
        age = now - last.created_at
        if row['computed']:
            if age < UNKNOWN_AFTER:
                continue
            reason = f'{MODEL_FAILED_REASON}: {UNKNOWN_SENT}'
            line = f'{UNKNOWN_SENT}: התקבלה {_clock(last.created_at)} — התשובה לא נשלחת שוב, בדקו בשיחה אם הלקוח קיבל אותה'
        else:
            if age <= LATE_WITHIN:
                continue
            if _nobody_asked(contact, row['since']) or _limited(contact, now):
                continue
            never = NEVER_ANSWERED if contact.live_test else NEVER_ANSWERED_PLAIN
            reason = f'{MODEL_FAILED_REASON}: {never}'
            line = f'{never}: התקבלה {_clock(last.created_at)} ואף קריאה לא ענתה עליה — לא נשלח דבר'
        _needs_human(contact.pk, _why(contact, reason))
        state.log_event(contact.pk, EVENT_LIVE_TEST, line)
        logger.warning('wahub live test: message %s of contact %s — %s', last.pk, contact.pk, reason)
        bot_mode.record(bot_mode.KIND_UNANSWERED, text=UNKNOWN_SENT if row['computed'] else NEVER_ANSWERED_PLAIN)
        marked += 1
    return marked


def answer_late(reply_url: str, now=None) -> dict:
    """
    The cron's safety net: a message between UNANSWERED_AFTER and LATE_WITHIN
    old that no call has answered or explained is answered now — by the very
    call the door makes (`kick`, marked late), so the claim, the limits and
    the look at the mode are the same and nobody is answered twice. The oldest
    first, LATE_PER_RUN a call at the most, and never an answer that was
    already computed. Returns {live_late_started, live_late_not_started}.
    """
    counts = {'live_late_started': 0, 'live_late_not_started': 0}
    if not reply_url:
        return counts
    now = now or timezone.now()
    began = time.monotonic()
    for row in _overdue(now):
        if sum(counts.values()) >= LATE_PER_RUN or time.monotonic() - began > LATE_SECONDS:
            break
        contact, last = row['contact'], row['last']
        if row['computed'] or now - last.created_at > LATE_WITHIN:
            continue
        if _nobody_asked(contact, row['since']) or _limited(contact, now):
            continue
        started = kick(contact.pk, reply_url, late=True)
        counts['live_late_started' if started else 'live_late_not_started'] += 1
    return counts


# --- the answer -----------------------------------------------------------------------------------------

def reply(contact_id: int, *, late: bool = False) -> dict:
    """
    The live answer for the contact's unanswered messages. Always returns a dict
    with `outcome` (see OUTCOME_*), never raises to the caller; the inbound door
    that started it is not listening anyway. `late` says the cron's safety net
    started this call: the answer's line in the journal says so, and it is
    counted as a late one.

    A message that arrives while an answer is being computed finds the contact
    claimed, and its own call leaves with "busy" — the call that holds the claim
    notices it, drops the answer it was computing and answers everything
    together (`_answer`). One that arrives in the instant the answer leaves is
    seen once it has left, and answered by the same call too, for as long as
    FOLLOW_UP_SECONDS and MAX_ANSWERS allow (the first answer's result is
    returned, with what followed it). What is still unanswered after that is
    answered by the next message's call, or by the cron (`answer_late`).
    """
    began = time.monotonic()
    results: list[dict] = []
    for _ in range(MAX_ANSWERS):
        held = {'until': None}
        try:
            contact, refused = claim(contact_id)
            if contact is None:
                if not results:
                    return {'outcome': refused}
                break
            held['until'] = contact.live_test_busy_until
            result = _answer(contact, held, late=late and not results)
        except Exception:
            logger.exception('wahub live test: the answer for contact %s failed unexpectedly', contact_id)
            result = {'outcome': OUTCOME_MODEL_ERROR, 'error': UNEXPECTED}
            _unexpected(contact_id)
        finally:
            if held['until'] is not None:
                _free(contact_id, held['until'])
        results.append(result)
        # Released first, looked at second: a message stored before the release is seen here,
        # one stored after it claims the contact by its own call.
        if result['outcome'] != OUTCOME_SENT or time.monotonic() - began > FOLLOW_UP_SECONDS or not _left_unanswered(contact_id):
            break
    first = results[0]
    if len(results) > 1:
        first = {**first, 'followed_by': [result['outcome'] for result in results[1:]]}
    return first


def _free(contact_id: int, until) -> None:
    """Release the claim; a fault here is logged, never raised — the claim then expires by itself."""
    try:
        _release(contact_id, until)
    except Exception:
        logger.exception('wahub live test: the claim on contact %s could not be released', contact_id)


def _unexpected(contact_id: int) -> None:
    """A fault of ours in the middle of an answer: say so on the contact, and never raise from here."""
    try:
        reason = f'{MODEL_FAILED_REASON}: {UNEXPECTED}'
        if not Contact.objects.filter(pk=contact_id, live_test=True).exists():
            reason = reason.replace(TEST_MARK, '')
        _needs_human(contact_id, reason)
        state.log_event(contact_id, EVENT_LIVE_TEST, f'תשובה חיה: {UNEXPECTED} — בדקו בשיחה אם נשלחה תשובה')
    except Exception:
        logger.exception('wahub live test: the fault of contact %s could not be written down', contact_id)
    bot_mode.record(bot_mode.KIND_UNEXPECTED)


def _left_unanswered(contact_id: int) -> bool:
    contact = Contact.objects.filter(pk=contact_id).first()
    if contact is None:
        return False
    live, since = bot_mode.answered_live(contact)
    return live and bool(_waiting(contact, since))


def _answer(contact: Contact, held: dict, *, late: bool = False) -> dict:
    """One answer for everything unanswered; `held` is this call's claim, taken anew each time the computation starts over."""
    if contact.handled_by == HANDLED_HUMAN:
        # A person holds the conversation: the bot keeps quiet, as the old one does.
        return {'outcome': OUTCOME_HUMAN}
    live, since = bot_mode.answered_live(contact)
    if not live:
        return {'outcome': OUTCOME_NOT_LIVE_TEST}
    waiting = _waiting(contact, since)
    if not waiting:
        return {'outcome': OUTCOME_NOTHING}
    if waiting[-1].created_at < timezone.now() - TOO_OLD:
        # Whichever way this call was started, an answer this late is not an answer to what was asked.
        return {'outcome': OUTCOME_TOO_OLD}
    limit = _over_limit(contact, timezone.now())
    if limit:
        return {'outcome': limit}
    if not shadow.shadow_configured() and not sending.simulate_send():
        # Without a key there is no new bot. The stub's rule-built line is for a developer's screen
        # (where nothing leaves), never for a real phone.
        return _no_answer(contact, NO_KEY, waiting[-1].created_at, timezone.now())

    contact_id = contact.pk
    restarts = 0
    while True:
        deadline = time.monotonic() + REPLY_SECONDS
        contact = _settle(contact, deadline=deadline)
        started = timezone.now()
        # The wait is seconds long: the test — or the mode, by a person or by the brake — may have been
        # turned off in it, or a person may have taken over.
        live, since = bot_mode.answered_live(contact)
        if not live:
            return {'outcome': OUTCOME_NOT_LIVE_TEST}
        if contact.handled_by == HANDLED_HUMAN:
            return {'outcome': OUTCOME_HUMAN}
        if not _waiting(contact, since):
            return {'outcome': OUTCOME_NOTHING}
        # The last time round nobody is asked whether he wrote again: what is ready leaves.
        watch = restarts < MAX_RESTARTS
        try:
            proposal = shadow.answer_now(
                contact, deadline=deadline, max_tokens=max_tokens(), effort=effort(), not_before=since,
                superseded=(lambda last_id: _written_since(contact_id, last_id)) if watch else None,
                before_keep=(lambda last_id: _hold(contact_id, last_id, deadline)) if watch else None,
            )
        except shadow.Superseded:
            restarts += 1
            logger.info('wahub live test: contact %s wrote again before the answer left; computing again (%s)', contact_id, restarts)
            if not _renew(contact_id, held):
                return {'outcome': OUTCOME_BUSY}
            continue
        except shadow.ShadowModelError as exc:
            return _no_answer(contact, str(exc), _last_received(contact, since) or started, started)
        break
    if proposal is None:
        if _waiting(contact, since):
            # There was something to answer and the shadow answered none of it: a machine wrote, not a person.
            state.log_event(contact.pk, EVENT_LIVE_TEST, AUTOMATIC_LINE)
        return {'outcome': OUTCOME_NOTHING}
    received = _covered_until(proposal) or started
    if not proposal.text:
        return {**_no_answer(contact, EMPTY_ANSWER, received, started), 'shadow_id': proposal.id}

    # Claude took seconds too. What leaves, leaves only for a contact the new bot answers right now.
    contact.refresh_from_db()
    if not bot_mode.answered_live(contact)[0]:
        return {'outcome': OUTCOME_NOT_LIVE_TEST, 'shadow_id': proposal.id}
    if contact.handled_by == HANDLED_HUMAN:
        return {'outcome': OUTCOME_HUMAN, 'shadow_id': proposal.id}
    if _answered_by_another(contact.pk, proposal.after_message_id):
        # The office wrote from the screen — or the old bot is still connected in ManyChat and its answer was
        # copied here first. A second answer on top of that one is the fault to avoid; the journal says why
        # nothing left, and the status screen counts the old bot's answers (bot_mode.status).
        state.log_event(contact.pk, EVENT_LIVE_TEST, ANSWERED_MEANWHILE)
        logger.warning('wahub live test: contact %s was answered by somebody else meanwhile; the new bot sent nothing', contact.pk)
        return {'outcome': OUTCOME_ANSWERED, 'shadow_id': proposal.id}

    message = sending.send_new_bot_reply(contact, proposal.text)
    sent = timezone.now()
    delivered = message.status in DELIVERED_STATUSES
    if not delivered:
        _needs_human(contact.pk, _why(contact, f'{SEND_FAILED_REASON}: {message.error}'))
    if proposal.request_human:
        _needs_human(contact.pk, proposal.request_human_reason or 'הבוט החדש ביקש נציג')
    if delivered:
        # The answer told the customer they are on a waiting list: now they are (the tool itself only marked it).
        waiting_list.apply_bot_marks(contact, proposal.tools_used)
        # And the tags a topic ends with ("ברכת יום הולדת" — the office's box of who waits for the greeting).
        birthday.apply_tag_marks(contact, proposal.tools_used)

    # What the answer said follows it (the holidays board): only after a text that left.
    flows = _send_flows(contact, proposal) if delivered else []

    text = (
        f'{SENT_LINE if delivered else "תשובה חיה נכשלה"}: התקבלה {_clock(received)} → חישוב {_clock(started)} (+{_secs(received, started)}) → '
        f'{"נשלחה" if delivered else "ניסיון שליחה"} {_clock(sent)} (+{_secs(started, sent)}, מהם Claude {proposal.took_ms / 1000:.1f} שנ\') · '
        f'סה"כ {_secs(received, sent)}'
    )
    if restarts:
        text += f' · חושב מחדש {restarts} פעמים (הגיעו עוד הודעות לפני השליחה)'
    if late:
        text += f' · {LATE_NOTE}'
    if message.error:
        text += f' — {message.error}'
    for flow in flows:
        # In the same line: it opens with SENT_LINE, and the cron's look reads a line that does not as a failure of the answer.
        text += f' · אחריה נשלחה האוטומציה {flow["name"]}' if flow['sent'] else f' · האוטומציה {flow["name"]} לא נשלחה — {flow["error"]}'
    state.log_event(contact.pk, EVENT_LIVE_TEST, text)
    logger.info('wahub live test: contact %s — %s', contact.pk, text)
    if delivered:
        bot_mode.record(bot_mode.KIND_SENT, late=late)
    else:
        bot_mode.record(bot_mode.KIND_SEND, text=message.error)
    return {
        'outcome': OUTCOME_SENT if delivered else OUTCOME_FAILED,
        'message_id': message.id,
        'shadow_id': proposal.id,
        'seconds': round((sent - received).total_seconds(), 1),
        'restarts': restarts,
        **({'late': True} if late else {}),
        **({'flows': flows} if flows else {}),
    }


def _send_flows(contact: Contact, proposal) -> list:
    """
    The automations the answer sends after its text (shadow.flows_to_send),
    each once, through the same doors as every send. Returns what happened to
    each: [{name, sent, message_id, error}]. One that did not leave marks the
    contact "needs a person" — the customer was told a board is on its way.
    Never raises: the text has left already, and its line must still be written.
    """
    outcomes = []
    for flow in shadow.flows_to_send(proposal):
        shown = f'{flow["label"] or "אוטומציה"} ({flow["name"]})'
        try:
            message = sending.send_new_bot_flow(contact, flow['ns'], shown)
            sent, error, message_id = message.status in DELIVERED_STATUSES, message.error, message.id
        except Exception:
            logger.exception('wahub live test: the automation %s for contact %s failed unexpectedly', flow['name'], contact.pk)
            sent, error, message_id = False, UNEXPECTED, None
        if not sent:
            _needs_human(contact.pk, _why(contact, f'{FLOW_FAILED_REASON}: {flow["name"]} — {error}'))
        outcomes.append({'name': flow['name'], 'sent': sent, 'message_id': message_id, 'error': error})
    return outcomes


def _answered_by_another(contact_id: int, after_message_id) -> bool:
    """Did anybody but the new bot write to the customer, and get through, since the last message this answer covers?"""
    if not after_message_id:
        return False
    return Message.objects.filter(
        contact_id=contact_id, direction=DIRECTION_OUT, new_bot=False, status__in=DELIVERED_STATUSES, id__gt=after_message_id,
    ).exists()


def _last_received(contact: Contact, since=None):
    """When the customer's last unanswered message reached Kogo; None when there is none."""
    messages = _waiting(contact, since)
    return messages[-1].created_at if messages else None


def _covered_until(proposal):
    """When the last message this answer covers reached Kogo — what the delay is measured from."""
    return Message.objects.filter(pk=proposal.after_message_id).values_list('created_at', flat=True).first()
