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
sends nothing and marks the contact "needs a person"; so does a self-call that
did not leave, and so does the cron for a message no call ever answered
(`flag_unanswered`). Nothing here touches a contact that is not under the live
test, and a demo contact never is.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.wahub import sending, shadow, state, waiting_list
from apps.wahub.models import (
    DELIVERED_STATUSES,
    DIRECTION_IN,
    DIRECTION_OUT,
    EVENT_LIVE_TEST,
    EVENT_NEEDS_HUMAN,
    HANDLED_HUMAN,
    Contact,
    ContactEvent,
    Message,
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
# A customer is waiting: Claude's ceiling is lower than the shadow's 16,000 (thinking counts toward it).
DEFAULT_MAX_TOKENS = 4000

MODEL_FAILED_REASON = 'הבוט החדש לא ענה (בדיקה חיה)'
SEND_FAILED_REASON = 'השליחה ללקוח נכשלה (בדיקה חיה)'
FLOW_FAILED_REASON = 'האוטומציה שאחרי התשובה לא נשלחה (בדיקה חיה)'
NO_KEY = 'אין בשרת מפתח Anthropic — אין בוט חדש שיענה'
# How the journal line of an answer that went out begins (the cron's look tells it from a line about a failure).
SENT_LINE = 'תשובה חיה נשלחה'
EMPTY_ANSWER = 'תשובה ריקה'
NOT_STARTED = 'התשובה החיה לא הופעלה'
NEVER_ANSWERED = 'הודעה בבדיקה החיה לא נענתה'
# How long a live-test contact's message may wait before the cron says nobody answered it.
UNANSWERED_AFTER = timedelta(minutes=2)
UNEXPECTED = 'תקלה לא צפויה'
DAILY_LIMIT_TEXT = 'הגבלה: 30 תשובות חיות ביום — הבוט החדש לא עונה יותר היום'

OUTCOME_SENT = 'sent'
OUTCOME_FAILED = 'failed'
OUTCOME_BUSY = 'busy'
OUTCOME_NOTHING = 'nothing'
OUTCOME_NOT_FOUND = 'not_found'
OUTCOME_NOT_LIVE_TEST = 'not_live_test'
OUTCOME_HUMAN = 'human_handles'
OUTCOME_DAILY_LIMIT = 'daily_limit'
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

def kick(contact_id: int, url: str) -> bool:
    """
    Start the live answer for a contact: POST our own live-test/reply/ and let
    go. The read timeout is the normal way out — the answer takes seconds, and
    ManyChat, which is waiting on the inbound door, gets its 200 at once. True
    when the call left; False when it did not (no token, no connection, a
    refusal) — then nobody will answer this message, so the log says why, and
    so does the contact: a line in its journal and "needs a person".
    """
    token = cron_token()
    if not token:
        return _not_started(contact_id, 'אין בשרת אסימון cron (CRON_TOKEN)')
    try:
        response = requests.post(url, json={'contact': contact_id}, headers={'X-Cron-Token': token}, timeout=KICK_TIMEOUT)
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
        _needs_human(contact_id, f'{MODEL_FAILED_REASON}: {NOT_STARTED} — {why}')
        state.log_event(contact_id, EVENT_LIVE_TEST, f'{NOT_STARTED}: {why}')
    except Exception:
        logger.exception('wahub live test: the fault of contact %s could not be written down', contact_id)
    return False


# --- the claim ------------------------------------------------------------------------------------------

def claim(contact_id: int) -> tuple[Contact | None, str]:
    """
    Take the contact for one live answer: (contact, '') when taken, else
    (None, why) — busy (another call is answering), not_found, not_live_test.

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
                if not contact.live_test or contact.is_demo:
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

def _needs_human(contact_id: int, reason: str) -> None:
    """Mark the contact "needs a person", once: a contact already marked keeps its own reason."""
    reason = reason[:200]
    moment = timezone.now()
    marked = Contact.objects.filter(pk=contact_id, needs_human=False).update(
        needs_human=True, needs_human_reason=reason, needs_human_at=moment, touched_at=moment, updated_at=moment,
    )
    if marked:
        state.log_event(contact_id, EVENT_NEEDS_HUMAN, reason)


def _clock(moment) -> str:
    return timezone.localtime(moment).strftime('%H:%M:%S.%f')[:-3] if moment else '—'


def _secs(start, end) -> str:
    return f'{(end - start).total_seconds():.1f} שנ\''


def _day_start(now):
    local = timezone.localtime(now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _over_daily_limit(contact: Contact, now) -> bool:
    day_start = _day_start(now)
    if Message.objects.filter(contact=contact, new_bot=True, sent_at__gte=day_start).count() < DAILY_LIMIT:
        return False
    already = ContactEvent.objects.filter(contact=contact, kind=EVENT_LIVE_TEST, text=DAILY_LIMIT_TEXT, created_at__gte=day_start).exists()
    if not already:
        state.log_event(contact.pk, EVENT_LIVE_TEST, DAILY_LIMIT_TEXT)
    logger.warning('wahub live test: contact %s reached the daily limit', contact.pk)
    return True


def _no_answer(contact: Contact, reason: str, received, started) -> dict:
    """Claude gave no answer: nothing is sent, the contact asks for a person, the journal says why."""
    _needs_human(contact.pk, f'{MODEL_FAILED_REASON}: {reason}')
    text = f'תשובה חיה נכשלה, לא נשלח דבר: {reason} (התקבלה {_clock(received)}, חישוב מ-{_clock(started)})'
    state.log_event(contact.pk, EVENT_LIVE_TEST, text)
    logger.warning('wahub live test: contact %s — %s', contact.pk, text)
    return {'outcome': OUTCOME_MODEL_ERROR, 'error': reason}


def flag_unanswered(now=None) -> int:
    """
    The cron's look at the live test, every five minutes: a message of a
    live-test contact that nobody answered and nobody explained — its self-call
    never arrived, or the call that took it was cut — is said on the contact:
    "needs a person" and a line in the journal, once. Nothing is sent from
    here: an answer that comes minutes late is not an answer to what he asked.
    Returns how many contacts were marked.
    """
    now = now or timezone.now()
    marked = 0
    for contact in Contact.objects.filter(live_test=True, is_demo=False).exclude(handled_by=HANDLED_HUMAN):
        if contact.live_test_busy_until and contact.live_test_busy_until > now:
            continue
        messages = shadow.unanswered_messages(contact)
        if not messages:
            continue
        last = messages[-1]
        if last.created_at > now - UNANSWERED_AFTER:
            continue
        # Somebody other than the new bot wrote to him since (the office) — the new bot's own lines
        # answer other messages: this one is unanswered by the shadow's own count.
        if Message.objects.filter(
            contact=contact, direction=DIRECTION_OUT, id__gt=last.id, new_bot=False, status__in=DELIVERED_STATUSES,
        ).exists():
            continue
        # Or the journal already says what happened to it (a failure, the daily limit, this very
        # line), or the test was turned on after it. A line about an answer that went out says nothing about it.
        said = ContactEvent.objects.filter(contact=contact, kind=EVENT_LIVE_TEST, created_at__gte=last.created_at)
        if said.exclude(text__startswith=SENT_LINE).exists():
            continue
        _needs_human(contact.pk, f'{MODEL_FAILED_REASON}: {NEVER_ANSWERED}')
        state.log_event(contact.pk, EVENT_LIVE_TEST, f'{NEVER_ANSWERED}: התקבלה {_clock(last.created_at)} ואף קריאה לא ענתה עליה — לא נשלח דבר')
        logger.warning('wahub live test: message %s of contact %s was never answered', last.pk, contact.pk)
        marked += 1
    return marked


# --- the answer -----------------------------------------------------------------------------------------

def reply(contact_id: int) -> dict:
    """
    The live answer for the contact's unanswered messages. Always returns a dict
    with `outcome` (see OUTCOME_*), never raises to the caller; the inbound door
    that started it is not listening anyway.

    A message that arrives while an answer is being computed finds the contact
    claimed, and its own call leaves with "busy" — the call that holds the claim
    notices it, drops the answer it was computing and answers everything
    together (`_answer`). One that arrives in the instant the answer leaves is
    seen once it has left, and answered by the same call too, for as long as
    FOLLOW_UP_SECONDS and MAX_ANSWERS allow (the first answer's result is
    returned, with what followed it). What is still unanswered after that is
    answered by the next message's call, or said by the cron (`flag_unanswered`).
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
            result = _answer(contact, held)
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
        _needs_human(contact_id, f'{MODEL_FAILED_REASON}: {UNEXPECTED}')
        state.log_event(contact_id, EVENT_LIVE_TEST, f'תשובה חיה: {UNEXPECTED} — בדקו בשיחה אם נשלחה תשובה')
    except Exception:
        logger.exception('wahub live test: the fault of contact %s could not be written down', contact_id)


def _left_unanswered(contact_id: int) -> bool:
    contact = Contact.objects.filter(pk=contact_id, live_test=True, is_demo=False).first()
    return contact is not None and bool(shadow.unanswered_messages(contact))


def _answer(contact: Contact, held: dict) -> dict:
    """One answer for everything unanswered; `held` is this call's claim, taken anew each time the computation starts over."""
    if contact.handled_by == HANDLED_HUMAN:
        # A person holds the conversation: the bot keeps quiet, as the old one does.
        return {'outcome': OUTCOME_HUMAN}
    waiting = shadow.unanswered_messages(contact)
    if not waiting:
        return {'outcome': OUTCOME_NOTHING}
    if _over_daily_limit(contact, timezone.now()):
        return {'outcome': OUTCOME_DAILY_LIMIT}
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
        # The wait is seconds long: the test may have been turned off in it, or a person may have taken over.
        if not contact.live_test or contact.is_demo:
            return {'outcome': OUTCOME_NOT_LIVE_TEST}
        if contact.handled_by == HANDLED_HUMAN:
            return {'outcome': OUTCOME_HUMAN}
        if not shadow.unanswered_messages(contact):
            return {'outcome': OUTCOME_NOTHING}
        # The last time round nobody is asked whether he wrote again: what is ready leaves.
        watch = restarts < MAX_RESTARTS
        try:
            proposal = shadow.answer_now(
                contact, deadline=deadline, max_tokens=max_tokens(), effort=effort(),
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
            return _no_answer(contact, str(exc), _last_received(contact) or started, started)
        break
    if proposal is None:
        return {'outcome': OUTCOME_NOTHING}
    received = _covered_until(proposal) or started
    if not proposal.text:
        return {**_no_answer(contact, EMPTY_ANSWER, received, started), 'shadow_id': proposal.id}

    # Claude took seconds too. What leaves, leaves only for a contact that is under the test right now.
    contact.refresh_from_db()
    if not contact.live_test or contact.is_demo:
        return {'outcome': OUTCOME_NOT_LIVE_TEST, 'shadow_id': proposal.id}
    if contact.handled_by == HANDLED_HUMAN:
        return {'outcome': OUTCOME_HUMAN, 'shadow_id': proposal.id}

    message = sending.send_new_bot_reply(contact, proposal.text)
    sent = timezone.now()
    delivered = message.status in DELIVERED_STATUSES
    if not delivered:
        _needs_human(contact.pk, f'{SEND_FAILED_REASON}: {message.error}')
    if proposal.request_human:
        _needs_human(contact.pk, proposal.request_human_reason or 'הבוט החדש ביקש נציג')
    if delivered:
        # The answer told the customer they are on a waiting list: now they are (the tool itself only marked it).
        waiting_list.apply_bot_marks(contact, proposal.tools_used)

    # What the answer said follows it (the holidays board): only after a text that left.
    flows = _send_flows(contact, proposal) if delivered else []

    text = (
        f'{SENT_LINE if delivered else "תשובה חיה נכשלה"}: התקבלה {_clock(received)} → חישוב {_clock(started)} (+{_secs(received, started)}) → '
        f'{"נשלחה" if delivered else "ניסיון שליחה"} {_clock(sent)} (+{_secs(started, sent)}, מהם Claude {proposal.took_ms / 1000:.1f} שנ\') · '
        f'סה"כ {_secs(received, sent)}'
    )
    if restarts:
        text += f' · חושב מחדש {restarts} פעמים (הגיעו עוד הודעות לפני השליחה)'
    if message.error:
        text += f' — {message.error}'
    for flow in flows:
        # In the same line: it opens with SENT_LINE, and the cron's look reads a line that does not as a failure of the answer.
        text += f' · אחריה נשלחה האוטומציה {flow["name"]}' if flow['sent'] else f' · האוטומציה {flow["name"]} לא נשלחה — {flow["error"]}'
    state.log_event(contact.pk, EVENT_LIVE_TEST, text)
    logger.info('wahub live test: contact %s — %s', contact.pk, text)
    return {
        'outcome': OUTCOME_SENT if delivered else OUTCOME_FAILED,
        'message_id': message.id,
        'shadow_id': proposal.id,
        'seconds': round((sent - received).total_seconds(), 1),
        'restarts': restarts,
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
            _needs_human(contact.pk, f'{FLOW_FAILED_REASON}: {flow["name"]} — {error}')
        outcomes.append({'name': flow['name'], 'sent': sent, 'message_id': message_id, 'error': error})
    return outcomes


def _last_received(contact: Contact):
    """When the customer's last unanswered message reached Kogo; None when there is none."""
    messages = shadow.unanswered_messages(contact)
    return messages[-1].created_at if messages else None


def _covered_until(proposal):
    """When the last message this answer covers reached Kogo — what the delay is measured from."""
    return Message.objects.filter(pk=proposal.after_message_id).values_list('created_at', flat=True).first()
