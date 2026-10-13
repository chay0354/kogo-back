"""
Whom the NEW bot answers for real, and what stops it (docs/WAHUB-CONTRACT-GO-LIVE.md).

Two switches the owner flips from the screen, both kept in the database
(settings_store) and both off until he says otherwise:

    the mode        "test" — only the contacts of the live test are answered,
                    as before; "all" — every contact that writes is answered.
    the follow-ups  whether the bot's own follow-ups to leads may leave
                    (lead_followups.py).

Neither opens anything by itself: the owner's general switch
(WAHUB_SENDING_ENABLED, sending.sending_enabled) is the hard gate. A switch is
"live" only from the first moment this server saw it on AND the general switch
on — `live_since`, `followups_since`. That moment is what keeps the past out:
a message that reached Kogo before it is never answered, a follow-up that fell
due before it never leaves. So whatever order tomorrow's steps are taken in —
the switch first, the deploy that turns sending on first — nothing that was
waiting is released. Sending seen off again forgets the moment; the next "on"
is a new beginning.

What the live answers came to is counted here too (`record`): today's numbers
for the status screen, and the last outcomes the emergency brake reads. A run
of failures puts the mode back to "test" by itself (`trip`) — nothing more is
sent until a person chooses "all" again — and says so in the log, on the status
screen and to the office.

Nothing in this module sends a message or calls another company (the office's
alert goes through apps/core/office_alerts.py, as every alert does).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.wahub import settings_store, state

logger = logging.getLogger(__name__)

MODE_TEST = 'test'
MODE_ALL = 'all'
MODES = (MODE_TEST, MODE_ALL)
# What the request must say besides the value itself: these two switches reach real customers.
CONFIRM_ALL = 'all'
CONFIRM_FOLLOWUPS = 'on'

# --- the emergency brake -------------------------------------------------------------------------------
# After it nobody answers the customers (the old bot is disconnected in ManyChat by then), so it takes a
# clear run of failures: this many in a row inside the window, or a failing half of a busy window, or —
# when few people write — a longer run over a longer time.
BRAKE_IN_A_ROW = 5
BRAKE_WINDOW = timedelta(minutes=10)
BRAKE_RATE_MIN = 10
BRAKE_RATE_PERCENT = 50
BRAKE_IN_A_ROW_SLOW = 10
BRAKE_SLOW_WINDOW = timedelta(minutes=60)
# How many outcomes are kept for it, and how many lines of the log.
RECENT_KEPT = 60
LOG_KEPT = 50
LOG_SHOWN = 20

# What an outcome was (`record`).
KIND_SENT = 'sent'
KIND_MODEL = 'model'                # Claude gave no answer
KIND_SEND = 'send'                  # ManyChat refused, or nobody knows whether it left
KIND_UNEXPECTED = 'unexpected'      # a fault of ours in the middle of an answer
KIND_NOT_STARTED = 'not_started'    # the self-call did not leave
KIND_UNANSWERED = 'unanswered'      # no answer in time: the message went to a person
KIND_LABELS = {
    KIND_MODEL: 'Claude לא נתן תשובה',
    KIND_SEND: 'השליחה ללקוח נכשלה',
    KIND_UNEXPECTED: 'תקלה לא צפויה בזמן תשובה',
    KIND_NOT_STARTED: 'הקריאה העצמית של השרת לא יצאה',
    KIND_UNANSWERED: 'הודעה לא נענתה בזמן',
}
COUNTS = ('sent', 'failed', 'late', 'needs_human', 'fallback', 'unanswered', 'not_started')
# Which of today's numbers a failure adds to.
_COUNTED_AS = {KIND_NOT_STARTED: 'not_started', KIND_UNANSWERED: 'unanswered'}

# Why a follow-up may not leave (`followup_refusal`), and the words kept on its row.
FOLLOWUPS_OFF = 'off'
FOLLOWUPS_BEFORE = 'before'
FOLLOWUPS_OFF_NOTE = 'החזרות האוטומטיות כבויות'
FOLLOWUPS_BEFORE_NOTE = 'דולג — הגיע זמנו לפני שהחזרות הודלקו'

ALL_BUT_NOT_SENDING = (
    'המצב "כולם" נבחר, אבל השליחה כבויה — שום דבר לא יוצא. '
    'הבוט החדש יתחיל לענות רק להודעות שיגיעו אחרי שהשליחה תודלק'
)
NOT_READY = 'אי אפשר לעבור ל"כולם" עכשיו — השרת לא יכול לענות ללקוחות'


class Refused(Exception):
    """A change of a switch that was not made, with the reason in Hebrew and what the screen should do with it."""

    def __init__(self, detail: str, code: str, status: int = 400, problems: list | None = None):
        super().__init__(detail)
        self.detail, self.code, self.status, self.problems = detail, code, status, problems or []


# --- small things ----------------------------------------------------------------------------------------

def _moment(raw):
    """A kept moment as an aware datetime; None for anything that is not one."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


def _clock(moment) -> str:
    return timezone.localtime(moment).strftime('%d.%m %H:%M') if moment else ''


def _dict(key: str) -> dict:
    value = settings_store.get(key, {})
    return value if isinstance(value, dict) else {}


def _who(user) -> dict:
    actor = user if getattr(user, 'is_authenticated', False) else None
    return {'by': getattr(actor, 'pk', None), 'by_name': state.user_display_name(actor)}


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


def sending_on() -> bool:
    """The owner's general switch (or a developer's machine that only records sends)."""
    from apps.wahub import sending

    return sending.sending_enabled()


def _log(what: str, to: str, text: str, user=None, now=None) -> None:
    """One line of who changed what and when, newest first. Never raises: the change itself is already made."""
    entry = {'at': (now or timezone.now()).isoformat(), 'what': what, 'to': to, 'text': text, **_who(user)}
    try:
        settings_store.change(
            settings_store.BOT_MODE_LOG,
            lambda rows: [entry, *(rows if isinstance(rows, list) else [])][:LOG_KEPT],
            default=[], user=user,
        )
    except Exception:
        logger.exception('wahub bot mode: a line of the log could not be kept: %s', text)
    logger.warning('wahub bot mode: %s (by user %s)', text, entry['by'])


# --- the mode ----------------------------------------------------------------------------------------------

def mode() -> str:
    """What the owner chose. Nothing kept is "test"."""
    return MODE_ALL if _dict(settings_store.BOT_MODE).get('mode') == MODE_ALL else MODE_TEST


def _armed(key: str, wanted, now, *, clear: bool = False):
    """
    Write — or, with `clear`, forget — the moment a switch became live, as one
    step, and only while the switch still reads `wanted(value)`. Returns
    (the moment kept afterwards, whether this call changed it).
    """
    done = {'since': None, 'changed': False}

    def mutate(value):
        value = dict(value) if isinstance(value, dict) else {}
        if not wanted(value):
            return None
        kept = _moment(value.get('since'))
        if clear:
            if kept is None:
                return None
            value['since'] = None
            done['changed'] = True
            return value
        if kept is not None:
            done['since'] = kept
            return None
        value['since'] = now.isoformat()
        done.update(since=now, changed=True)
        return value

    settings_store.change(key, mutate, default={})
    return done['since'], done['changed']


def live_since(*, arm: bool = False):
    """
    The moment from which the new bot answers everybody: the first moment this
    server saw the mode "all" together with sending on. None while it does not
    — the mode is "test", or sending is off. A customer's message that reached
    Kogo before this moment is never answered.

    Only a caller that passes `arm` writes anything: the door a message comes
    in through, the cron's look and the switch itself. They set the moment the
    first time both are on, and forget it when sending is seen off, so that
    sending coming back is a new beginning. Everybody else only reads.
    """
    kept = _dict(settings_store.BOT_MODE)
    if kept.get('mode') != MODE_ALL:
        return None
    since = _moment(kept.get('since'))

    def chosen(value) -> bool:
        return value.get('mode') == MODE_ALL

    if not sending_on():
        if arm and since is not None:
            _since, changed = _armed(settings_store.BOT_MODE, chosen, timezone.now(), clear=True)
            if changed:
                _log('live', MODE_ALL, 'השליחה כבויה — הבוט החדש הפסיק לענות לכולם עד שהיא תודלק')
        return None
    if since is None and arm:
        now = timezone.now()
        since, changed = _armed(settings_store.BOT_MODE, chosen, now)
        if changed:
            _log('live', MODE_ALL, f'הבוט החדש עונה לכולם מ-{_clock(now)} — רק להודעות שמגיעות מהרגע הזה', now=now)
    return since


def answered_live(contact) -> tuple:
    """
    (is this contact answered for real by the new bot right now, the moment
    before which its messages are not answered). A demo contact never is; a
    live-test contact always is, with no such moment — as before the mode
    existed; everybody else only while the mode is live.
    """
    if contact.is_demo:
        return False, None
    if contact.live_test:
        return True, None
    since = live_since()
    return since is not None, since


def readiness(self_call=None) -> list:
    """
    What keeps this server from answering customers at all — checked before the
    mode may become "all": a Claude key, a ManyChat to send through, and that
    the server reaches itself (`self_call()` returns '' or why not). [] when
    nothing does.
    """
    from apps.wahub import sending, shadow

    problems = []
    if not shadow.shadow_configured() and not sending.simulate_send():
        problems.append({'code': 'no_claude_key', 'text': 'אין בשרת מפתח Anthropic — אין בוט חדש שיענה'})
    if not sending.manychat_reachable():
        problems.append({'code': 'no_manychat', 'text': 'השרת לא מחובר ל-ManyChat (MANYCHAT_KEY) — אין דרך לשלוח תשובה'})
    if self_call is not None:
        try:
            why = self_call()
        except Exception:
            logger.exception('wahub bot mode: the check of the self-call failed unexpectedly')
            why = 'תקלה לא צפויה בבדיקה'
        if why:
            problems.append({'code': 'self_call_failed', 'text': f'השרת לא הצליח לקרוא לעצמו: {why}'})
    return problems


def set_mode(wanted, user, *, confirm=None, self_call=None) -> bool:
    """
    The owner chooses whom the new bot answers. "all" has to be confirmed and
    is refused while the server cannot answer (`readiness`). The same value
    twice changes nothing — `live_since` does not move. Choosing a value also
    acknowledges a stop by the brake. Returns whether anything changed.
    """
    if wanted not in MODES:
        raise Refused('mode: test או all.', 'bad_mode')
    kept = _dict(settings_store.BOT_MODE)
    current = MODE_ALL if kept.get('mode') == MODE_ALL else MODE_TEST
    if current == wanted and not kept.get('stopped'):
        if wanted == MODE_ALL:
            live_since(arm=True)
        return False
    if wanted == MODE_ALL:
        if confirm != CONFIRM_ALL:
            raise Refused('מעבר ל"כולם" דורש אישור מפורש (confirm: "all").', 'confirm_required')
        problems = readiness(self_call)
        if problems:
            raise Refused(NOT_READY, 'not_ready', 409, problems)
    now = timezone.now()

    def mutate(value):
        value = dict(value) if isinstance(value, dict) else {}
        value.update(mode=wanted, chosen_at=now.isoformat(), since=None, stopped=None, **_who(user))
        return value

    settings_store.change(settings_store.BOT_MODE, mutate, default={}, user=user)
    if wanted == MODE_ALL:
        # Yesterday's failures are not this beginning's: the brake counts from here.
        _forget_recent()
        _log('mode', MODE_ALL, 'המצב שונה ל"כולם": הבוט החדש עונה לכל מי שכותב', user, now)
        live_since(arm=True)
    else:
        _log('mode', MODE_TEST, 'המצב שונה ל"בדיקה": הבוט החדש עונה רק למספרי הבדיקה', user, now)
    return True


# --- the follow-ups to leads ---------------------------------------------------------------------------------

def followups_on() -> bool:
    return _dict(settings_store.BOT_FOLLOWUPS).get('on') is True


def followups_since(*, arm: bool = False):
    """
    The moment from which the bot's follow-ups to leads may leave: the first
    moment this server saw their switch on together with sending on. None
    while they may not. A follow-up that fell due before it never leaves.
    `arm` as in `live_since`: lead_followups.py, the cron's look and the
    switch itself set and forget the moment.
    """
    kept = _dict(settings_store.BOT_FOLLOWUPS)
    if kept.get('on') is not True:
        return None
    since = _moment(kept.get('since'))

    def on(value) -> bool:
        return value.get('on') is True

    if not sending_on():
        if arm and since is not None:
            _armed(settings_store.BOT_FOLLOWUPS, on, timezone.now(), clear=True)
        return None
    if since is None and arm:
        now = timezone.now()
        since, changed = _armed(settings_store.BOT_FOLLOWUPS, on, now)
        if changed:
            _log('followups', 'on', f'חזרות הבוט ללידים יוצאות מ-{_clock(now)} — רק מה שמועדו אחרי הרגע הזה', now=now)
    return since


def followup_refusal(contact, due_at) -> str:
    """
    Why this follow-up may not leave for this contact: FOLLOWUPS_OFF (the
    switch is off — it waits), FOLLOWUPS_BEFORE (it fell due before the switch
    became live — it is let go), or '' when the switch has nothing against it.
    A live-test contact is the owner's own number and is not asked: its
    follow-ups leave as they did before the switch existed. Asked only once
    sending.sending_enabled has said a message may leave for the contact.
    """
    if contact.live_test:
        return ''
    since = followups_since(arm=True)
    if since is None:
        return FOLLOWUPS_OFF
    return FOLLOWUPS_BEFORE if due_at < since else ''


def set_followups(on, user, *, confirm=None) -> bool:
    """The owner turns the follow-ups on (confirmed) or off. The same value twice changes nothing."""
    if not isinstance(on, bool):
        raise Refused('on: true או false.', 'bad_value')
    if followups_on() == on:
        if on:
            followups_since(arm=True)
        return False
    if on and confirm != CONFIRM_FOLLOWUPS:
        raise Refused('הדלקת החזרות דורשת אישור מפורש (confirm: "on").', 'confirm_required')
    now = timezone.now()
    settings_store.change(
        settings_store.BOT_FOLLOWUPS,
        lambda _value: {'on': on, 'at': now.isoformat(), 'since': None, **_who(user)},
        default={}, user=user,
    )
    if on:
        _log('followups', 'on', 'חזרות הבוט ללידים הודלקו', user, now)
        if followups_since(arm=True) is not None:
            _look_at_the_late_ones(now)
    else:
        _log('followups', 'off', 'חזרות הבוט ללידים כובו', user, now)
    return True


def _look_at_the_late_ones(now) -> None:
    """
    The follow-ups that were waiting when the switch came on are looked at by
    the next cron call instead of half an hour from now, so the screen soon
    shows them let go. The engine decides, as always; nothing is sent from here.
    """
    from apps.wahub.models import NUDGE_OPEN_STATUSES, LeadFollowUp

    LeadFollowUp.objects.filter(
        status__in=NUDGE_OPEN_STATUSES, claimed_at__isnull=True, due_at__lt=now, next_look_at__gt=now,
    ).update(next_look_at=now)


def sync() -> None:
    """The cron's look: both moments are set, or forgotten, by what the general switch says now. Never raises."""
    try:
        live_since(arm=True)
        followups_since(arm=True)
    except Exception:
        logger.exception('wahub bot mode: the two switches could not be looked at')


# --- what the live answers came to -------------------------------------------------------------------------

def _today() -> str:
    return timezone.localtime(timezone.now()).date().isoformat()


def _health(value) -> dict:
    """The kept numbers as they stand today: a new day starts from zero, the last outcomes carry over."""
    value = dict(value) if isinstance(value, dict) else {}
    counts = value.get('counts') if value.get('day') == _today() and isinstance(value.get('counts'), dict) else {}
    recent = value.get('recent') if isinstance(value.get('recent'), list) else []
    return {
        'day': _today(),
        'counts': {name: int(counts.get(name) or 0) for name in COUNTS},
        'recent': [row for row in recent if isinstance(row, list) and len(row) == 3],
        'last_failure': value.get('last_failure') if isinstance(value.get('last_failure'), dict) else None,
    }


def _forget_recent() -> None:
    def mutate(value):
        health = _health(value)
        health['recent'] = []
        return health

    try:
        settings_store.change(settings_store.BOT_HEALTH, mutate, default={})
    except Exception:
        logger.exception('wahub bot mode: the last outcomes could not be forgotten')


def count(name: str, number: int = 1) -> None:
    """Add to one of today's numbers that is no outcome of an answer (needs_human, fallback). Never raises."""
    def mutate(value):
        health = _health(value)
        health['counts'][name] = health['counts'].get(name, 0) + number
        return health

    try:
        settings_store.change(settings_store.BOT_HEALTH, mutate, default={})
    except Exception:
        logger.exception('wahub bot mode: %s could not be counted', name)


def brake_reason(recent: list, now) -> str:
    """Why the brake stops the bot on these outcomes ([moment, 1 | 0, kind], oldest first); '' when it does not."""
    rows = [(moment, bool(ok)) for moment, ok in ((_moment(row[0]), row[1]) for row in recent) if moment is not None]
    run = []
    for moment, ok in reversed(rows):
        if ok:
            break
        run.append(moment)
    in_a_row, minutes = _setting('WAHUB_BRAKE_IN_A_ROW', BRAKE_IN_A_ROW), int(BRAKE_WINDOW.total_seconds() // 60)
    if len([moment for moment in run if moment >= now - BRAKE_WINDOW]) >= in_a_row:
        return f'{in_a_row} תשובות נכשלו ברצף בתוך {minutes} דקות'
    slow, slow_minutes = _setting('WAHUB_BRAKE_IN_A_ROW_SLOW', BRAKE_IN_A_ROW_SLOW), int(BRAKE_SLOW_WINDOW.total_seconds() // 60)
    if len([moment for moment in run if moment >= now - BRAKE_SLOW_WINDOW]) >= slow:
        return f'{slow} תשובות נכשלו ברצף בתוך {slow_minutes} דקות'
    window = [ok for moment, ok in rows if moment >= now - BRAKE_WINDOW]
    failed = len([ok for ok in window if not ok])
    if len(window) >= _setting('WAHUB_BRAKE_RATE_MIN', BRAKE_RATE_MIN) and failed * 100 >= BRAKE_RATE_PERCENT * len(window):
        return f'{failed} מתוך {len(window)} תשובות נכשלו בתוך {minutes} דקות'
    return ''


def record(kind: str, *, late: bool = False, text: str = '', now=None) -> None:
    """
    One outcome of the live answers: an answer that went out (KIND_SENT, `late`
    when the cron's safety net started it) or a failure of one of the other
    kinds. Counted for today, kept among the last outcomes, and — on a failure,
    while the mode is "all" — held against the brake. `text` is what went
    wrong, with nothing of the customer's in it. Never raises: the answer has
    been sent, or not, already.
    """
    now = now or timezone.now()
    ok = kind == KIND_SENT
    seen = {}

    def mutate(value):
        health = _health(value)
        counts = health['counts']
        if ok:
            counts['sent'] += 1
            counts['late'] += int(late)
        else:
            counts[_COUNTED_AS.get(kind, 'failed')] += 1
            health['last_failure'] = {'at': now.isoformat(), 'kind': kind, 'text': (text or KIND_LABELS.get(kind, kind))[:300]}
        health['recent'] = [*health['recent'], [now.isoformat(), int(ok), kind]][-RECENT_KEPT:]
        seen['recent'] = health['recent']
        return health

    try:
        settings_store.change(settings_store.BOT_HEALTH, mutate, default={})
        if not ok and mode() == MODE_ALL:
            reason = brake_reason(seen.get('recent') or [], now)
            if reason:
                trip(reason, now=now)
    except Exception:
        logger.exception('wahub bot mode: an outcome (%s) could not be recorded', kind)


def trip(reason: str, *, now=None) -> bool:
    """
    The emergency brake: the mode goes back to "test" by itself, and nothing
    more is sent to anybody but the live-test contacts until a person chooses
    "all" again. Said in the log, on the status screen (`stopped`) and to the
    office. False when the mode was not "all" (another call stopped it first).
    """
    now = now or timezone.now()
    done = {}

    def mutate(value):
        value = dict(value) if isinstance(value, dict) else {}
        if value.get('mode') != MODE_ALL:
            return None
        value.update(mode=MODE_TEST, since=None, stopped={'at': now.isoformat(), 'reason': reason})
        done['yes'] = True
        return value

    settings_store.change(settings_store.BOT_MODE, mutate, default={})
    if not done:
        return False
    logger.error('wahub bot mode: the emergency brake stopped the new bot — %s', reason)
    _log('brake', MODE_TEST, f'הבוט החדש עצר את עצמו: {reason}', now=now)
    _alert_office(reason, now)
    return True


def _alert_office(reason: str, now) -> None:
    """Tell the office that the bot stopped, once an hour at the most (apps/core/office_alerts.py). Never raises."""
    try:
        from apps.core.office_alerts import raise_office_alert

        frontend = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip().rstrip('/')
        raise_office_alert(
            kind='wahub_bot_stopped',
            dedup_key=f'wahub_bot_stopped:{now.strftime("%Y-%m-%dT%H")}',
            title='הבוט החדש בוואטסאפ עצר את עצמו',
            where='וואטסאפ ולידים — הבוט החדש',
            what=reason,
            why='רצף כשלים בתשובות ללקוחות',
            action=(
                'מהרגע הזה אף אחד לא עונה ללקוחות אוטומטית. לבדוק ב"וואטסאפ ולידים" ← הגדרות מה נכשל, '
                'ואז לבחור שוב "כולם" — או לחבר חזרה את הבוט הישן ב-ManyChat.'
            ),
            link=f'{frontend}/wahub' if frontend else '',
            details={'reason': reason},
        )
    except Exception:
        logger.exception('wahub bot mode: the office could not be told that the bot stopped')


# --- what a screen shows -------------------------------------------------------------------------------------

def _mode_text(kept: dict, live, sending: bool, testing: int) -> tuple:
    """(the one sentence that says where the mode stands, the warning beside it — or '')."""
    stopped = kept.get('stopped') if isinstance(kept.get('stopped'), dict) else None
    if kept.get('mode') == MODE_ALL:
        if live is not None:
            return f'הבוט החדש עונה לכל הלקוחות, להודעות שהגיעו מ-{_clock(live)}', ''
        if not sending:
            return 'נבחר "כולם", והבוט החדש עוד לא עונה לאף לקוח', ALL_BUT_NOT_SENDING
        return 'נבחר "כולם" — הבוט החדש יתחיל לענות עם ההודעה הבאה שתגיע', ''
    numbers = 'אין כרגע מספר בדיקה' if not testing else 'מספר בדיקה אחד' if testing == 1 else f'{testing} מספרי בדיקה'
    text = f'מצב בדיקה: הבוט החדש עונה רק למספרי הבדיקה ({numbers})'
    if stopped:
        return text, 'הבוט החדש עצר את עצמו — מאז אף לקוח לא נענה אוטומטית, עד שבוחרים שוב "כולם"'
    return text, ''


def _followups_text(kept: dict, since, sending: bool) -> str:
    if kept.get('on') is not True:
        return 'כבויות — הבוט מתכנן חזרות ולא שולח אותן'
    if since is not None:
        return f'דלוקות — יוצאות רק חזרות שמועדן אחרי {_clock(since)}'
    if not sending:
        return 'דלוקות, אבל השליחה כבויה — שום חזרה לא יוצאת'
    return 'דלוקות — יתחילו לצאת בדקות הקרובות, רק מה שמועדו מעכשיו'


def followups_state() -> dict:
    """The follow-ups' switch as the screens read it: {on, since, turned_on_at, by, text}. Reads only."""
    kept = _dict(settings_store.BOT_FOLLOWUPS)
    on, sending = kept.get('on') is True, sending_on()
    since = _moment(kept.get('since')) if on and sending else None
    return {
        'on': on,
        'since': since.isoformat() if since else None,
        'turned_on_at': kept.get('at') if on and isinstance(kept.get('at'), str) else None,
        'by': str(kept.get('by_name') or '') if on else '',
        'text': _followups_text(kept, since, sending),
    }


def limits() -> dict:
    from apps.wahub import live_test

    return {
        'late_after_seconds': int(live_test.UNANSWERED_AFTER.total_seconds()),
        'late_within_minutes': int(live_test.LATE_WITHIN.total_seconds() // 60),
        'late_per_run': live_test.LATE_PER_RUN,
        'per_hour': live_test.HOURLY_LIMIT,
        'per_day': live_test.DAILY_LIMIT,
        'brake': {
            'in_a_row': _setting('WAHUB_BRAKE_IN_A_ROW', BRAKE_IN_A_ROW),
            'minutes': int(BRAKE_WINDOW.total_seconds() // 60),
            'rate_min': _setting('WAHUB_BRAKE_RATE_MIN', BRAKE_RATE_MIN),
            'rate_percent': BRAKE_RATE_PERCENT,
            'in_a_row_slow': _setting('WAHUB_BRAKE_IN_A_ROW_SLOW', BRAKE_IN_A_ROW_SLOW),
            'slow_minutes': int(BRAKE_SLOW_WINDOW.total_seconds() // 60),
        },
    }


def status(*, with_log: bool = False) -> dict:
    """
    GET settings/bot-mode/ (with the log) and the `bot` key of GET status/: the
    mode, whether it is live and since when, what stopped it, today's numbers,
    and the follow-ups' switch. Reads only — a screen that looks never moves
    `live_since`.
    """
    from apps.wahub.models import NUDGE_DUE, Contact, LeadFollowUp

    kept = _dict(settings_store.BOT_MODE)
    sending = sending_on()
    chosen = MODE_ALL if kept.get('mode') == MODE_ALL else MODE_TEST
    live = _moment(kept.get('since')) if chosen == MODE_ALL and sending else None
    testing = Contact.objects.filter(live_test=True, is_demo=False).count()
    text, warning = _mode_text(kept, live, sending, testing)
    stopped = kept.get('stopped') if isinstance(kept.get('stopped'), dict) else None
    health = _health(settings_store.get(settings_store.BOT_HEALTH, {}))
    answer = {
        'mode': chosen,
        'live': live is not None,
        'live_since': live.isoformat() if live else None,
        'chosen_at': kept.get('chosen_at') if isinstance(kept.get('chosen_at'), str) else None,
        'chosen_by': str(kept.get('by_name') or ''),
        'sending_enabled': sending,
        'text': text,
        'warning': warning,
        'stopped': {
            'at': stopped.get('at'), 'reason': str(stopped.get('reason') or ''),
            'text': f'הבוט החדש עצר את עצמו: {stopped.get("reason") or ""}',
        } if stopped else None,
        'test_contacts': testing,
        'today': {name: health['counts'][name] for name in COUNTS},
        'last_failure': health['last_failure'],
        'followups': {
            **followups_state(),
            'due_now': LeadFollowUp.objects.filter(status=NUDGE_DUE, contact__is_demo=False).count(),
        },
        'limits': limits(),
    }
    if with_log:
        rows = settings_store.get(settings_store.BOT_MODE_LOG, [])
        answer['log'] = [
            {'at': row.get('at'), 'what': row.get('what'), 'to': row.get('to'), 'by': str(row.get('by_name') or ''), 'text': row.get('text')}
            for row in (rows if isinstance(rows, list) else [])[:LOG_SHOWN] if isinstance(row, dict)
        ]
    return answer
