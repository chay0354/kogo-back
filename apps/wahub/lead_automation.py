"""
"הכול אוטומטי" — what stands between the follow-ups the bot plans and their
leaving by themselves (docs/WAHUB-CONTRACT-LEADS-V2.md, י).

The engine (lead_followups.py) already plans every follow-up and acts on it
when its moment comes. Two things keep a message from really leaving: the
owner's switch (sending.sending_enabled), and — for everything due more than
24 hours after the customer's last message — an approved WhatsApp template
connected to that follow-up. This module is the settings card that shows where
that stands and lets him connect the templates:

    status()          GET leads/automation/ — four lines (is the lead understood,
                      is its follow-up planned, may messages leave, are the
                      templates connected), today's numbers, the next follow-ups.
    templates()       GET leads/templates/ — every text the bot comes back with:
                      when it is sent, in which stage the owner needs it, and
                      the ManyChat automation connected to it.
    connect()         PUT leads/templates/{template_key}/ — connect one, or take
                      the connection off.
    options()         GET leads/templates/options/ — the automations ManyChat
                      has, to choose from.

What is connected is a ManyChat flow (`flow_ns`) — exactly what the screen's
"שלח תבנית" sends (sending.send_flow), because that is the one way a template
leaves. Which flow a follow-up has, and how it got it — a person on this
screen, or the automation that carries the follow-up's own fixed name in
ManyChat — is lead_templates.py. A connection made here is kept only after
ManyChat itself listed that flow: a follow-up sent to a flow that does not
exist is a step of the round lost.

Nothing here sends anything, and nothing here touches the switch.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, F, Max, Q
from django.utils import timezone

from apps.wahub import lead_ai, lead_followups, lead_stage, lead_templates, leads, sending, settings_store
from apps.wahub.lead_templates import STAGE_ONE
from apps.wahub.models import (
    NUDGE_DATE_ARRIVED,
    NUDGE_DORMANT,
    NUDGE_DUE,
    NUDGE_FAILED,
    NUDGE_LINK_REMINDER,
    NUDGE_OPEN_STATUSES,
    NUDGE_OPENING,
    NUDGE_PLANNED,
    NUDGE_SENT,
    NUDGE_SKIPPED,
    NUDGE_TRIAL_FEEDBACK,
    NUDGE_TRIAL_NO_SHOW,
    NUDGE_WILL_CHECK,
    Contact,
    LeadFollowUp,
)
from apps.wahub.phones import phone_display

logger = logging.getLogger(__name__)

UPCOMING = 15
# A contact whose stage has waited this long to be worked out: the cron is not getting to it. The
# cron rests for seven hours every night (vercel.json), and every open lead is looked at again
# after midnight, a few hundred a call — so anything shorter would call every morning a fault.
STALE_AFTER = timedelta(hours=9)
# A follow-up whose moment passed this long ago and that nobody acted on. Follow-ups are due inside
# the office hours only, when the cron runs every five minutes.
LATE_AFTER = timedelta(hours=1)
DUE_ROWS_READ = 500

BLOCKED_SENDING = 'sending'
BLOCKED_MANYCHAT = 'manychat'
BLOCKED_TEMPLATE = 'template'
BLOCKED_WORDS = {
    BLOCKED_SENDING: 'השליחה כבויה',
    BLOCKED_MANYCHAT: 'השרת לא מחובר ל-ManyChat',
    BLOCKED_TEMPLATE: 'חסרה תבנית',
}
DEMO_WORDS = 'ייכתב לשיחת הדמו — לא נשלח לאף אחד'


class TemplateError(ValueError):
    """A connection refused, with the reason in Hebrew and the HTTP status that says whose fault it is."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status


# --- words -----------------------------------------------------------------------------------------------

def _count(number: int, one: str, many: str) -> str:
    return one if number == 1 else many.format(n=number)


def _ago(moment, now) -> str:
    """ "עודכן עכשיו", "עודכן לפני 3 דק׳", "עודכן לפני שעתיים" — how fresh the last look is."""
    minutes = int(max(timedelta(0), now - moment).total_seconds() // 60)
    if minutes < 1:
        return 'עודכן עכשיו'
    if minutes < 60:
        return f'עודכן לפני {minutes} דק׳'
    hours = minutes // 60
    if hours < 24:
        return 'עודכן לפני ' + ('שעה' if hours == 1 else 'שעתיים' if hours == 2 else f'{hours} שעות')
    days = hours // 24
    return 'עודכן ' + ('אתמול' if days == 1 else f'לפני {days} ימים')


def _delay_words(delay: timedelta) -> str:
    if delay < timedelta(days=1):
        hours = int(delay.total_seconds() // 3600)
        return 'שעה' if hours == 1 else 'שעתיים' if hours == 2 else f'{hours} שעות'
    return {1: 'יום', 2: 'יומיים', 7: 'שבוע'}.get(delay.days, f'{delay.days} ימים')


AGE = lead_ai.MIN_AGE_YEARS
# What a round counts its days from, to finish "יום אחרי …".
_AFTER = {
    (NUDGE_LINK_REMINDER, ''): 'שקיבל קישור להרשמה ולא נרשם',
    (NUDGE_LINK_REMINDER, lead_stage.VARIANT_SIGNUP): 'שהתחיל הרשמה באתר ולא שילם',
    (NUDGE_LINK_REMINDER, lead_stage.VARIANT_COMMITMENT): 'ששאל מה קורה אם החוג לא יתאים',
    (NUDGE_WILL_CHECK, ''): 'שאמר שיבדוק ויחזור',
    (NUDGE_TRIAL_FEEDBACK, ''): 'שיעור ניסיון שלא הסתיים בהרשמה',
    (NUDGE_TRIAL_NO_SHOW, ''): 'שיעור ניסיון שלא הגיעו אליו',
    (NUDGE_DATE_ARRIVED, ''): 'היום שהלקוח אמר',
    (NUDGE_DATE_ARRIVED, lead_stage.VARIANT_AGE): f'שמלאו לילד {AGE}',
    (NUDGE_OPENING, ''): 'פנייה שאין בה אף פרט ("היי", או רק הנוסח של המודעה)',
}
# The step that goes out on the day itself.
_ON_THE_DAY = {
    (NUDGE_DATE_ARRIVED, ''): 'ביום שהלקוח עצמו אמר ("אחרי החגים", "בעוד חצי שנה")',
    (NUDGE_DATE_ARRIVED, lead_stage.VARIANT_AGE): f'ביום שבו מלאו לילד {AGE}',
    (NUDGE_DATE_ARRIVED, lead_stage.VARIANT_NOT_READY): 'ב-1 בספטמבר, למי שאמר שהילד לא רצה',
    (NUDGE_DORMANT, ''): 'ביום שייקבע (פתיחת הרשמה או עונה). לא פעיל עד שנקבעים תאריך ונוסח',
}
# Rounds after which a lead who did not answer is the office's (lead_stage.py: THEN_OFFICE).
_THEN_OFFICE = (NUDGE_WILL_CHECK, NUDGE_TRIAL_FEEDBACK, NUDGE_TRIAL_NO_SHOW)
THEN_OFFICE_WORDS = 'בלי תשובה — עובר אליך'
# First steps that do not go out to a family the system's own automations have already written the same to
# (lead_stage: `from_step`).
_UNLESS_SAID = {
    (NUDGE_TRIAL_FEEDBACK, 1): 'לא יוצאת למי שכבר קיבל את ההודעה האוטומטית שאחרי השיעור',
    (NUDGE_TRIAL_NO_SHOW, 1): 'לא יוצאת למי שכבר קיבל הודעה אוטומטית על היעדרויות',
}
LAST_TIME_WORDS = 'הפנייה האחרונה'
ONCE_WORDS = 'פעם אחת בלבד'
OPENING_WORDS = 'יוצאת בלי תבנית; תבנית נדרשת רק למי שכתב בסוף השבוע'


def when_label(kind: str, variant: str, step: int) -> str:
    """
    When this text is sent, in the owner's words — worked out from the very
    delays the engine plans by (lead_stage.delays_for), so the screen cannot
    say one thing while the bot does another.
    """
    delays = lead_stage.delays_for(kind, variant)
    delay = delays[step - 1] if step <= len(delays) else None
    if delay is None:
        return ''
    if delay == timedelta(0):
        words = _ON_THE_DAY.get((kind, variant)) or _ON_THE_DAY.get((kind, ''), '')
    else:
        after = _AFTER.get((kind, variant)) or _AFTER.get((kind, ''), '')
        words = f'{_delay_words(delay)} אחרי {after}'.strip()
    notes = []
    last = step == len(delays)
    if len(delays) == 1 and kind != NUDGE_DORMANT:
        notes.append(ONCE_WORDS)
    if last and kind in _THEN_OFFICE:
        notes.append(THEN_OFFICE_WORDS)
    elif last and len(delays) > 1:
        notes.append(LAST_TIME_WORDS)
    if kind == NUDGE_OPENING:
        notes.append(OPENING_WORDS)
    if (kind, step) in _UNLESS_SAID:
        notes.append(_UNLESS_SAID[(kind, step)])
    return '. '.join([words, *notes]) if notes else words


# --- the templates ---------------------------------------------------------------------------------------

def templates() -> list[dict]:
    """
    GET leads/templates/ — every text the bot may come back with, the five of
    stage 1 first: when it is sent, the text, and the ManyChat automation it
    leaves through (`target`, its flow; `target_name`, what ManyChat calls it)
    — and how it got it (`by`): "manual", a person connected it on the screen
    (with who and when); "name", ManyChat has an automation under the
    follow-up's own fixed name (`flow_name`); null, it has none.
    """
    connected = lead_templates.connections()
    rows = lead_followups.template_rows()
    for row in rows:
        key = row['template_key']
        entry = connected.get(key) or {}
        flow = entry.get('flow') or ''
        row.update(
            kind_label=row['label'],
            when_label=when_label(row['kind'], row['variant'], row['step']),
            stage=1 if key in STAGE_ONE else 2,
            # A text that is live and cannot leave after 24 hours without a template. The opening
            # goes out two hours after the message, inside WhatsApp's own window.
            required=bool(row['active']) and row['kind'] != NUDGE_OPENING,
            mapped=bool(flow),
            target=flow or None,
            target_name=(entry.get('name') or flow) if flow else '',
            by=entry.get('by') if flow else None,
            connected_by=(entry.get('by_name') or '') if flow else '',
            connected_at=entry.get('at') if flow else None,
            # The automation's fixed name in ManyChat: one that carries it is connected by itself.
            flow_name=lead_templates.FLOW_NAMES.get(key, ''),
            suggested_name=lead_templates.FLOW_NAMES.get(key, ''),
        )
    order = {key: place for place, key in enumerate(STAGE_ONE)}
    # Stage 1 in the document's order; the rest as the phrasings are listed (a stable sort).
    return sorted(rows, key=lambda row: (row['stage'], order.get(row['template_key'], 0)))


def template(template_key: str) -> dict:
    for row in templates():
        if row['template_key'] == template_key:
            return row
    raise LookupError(template_key)


def options() -> dict:
    """
    GET leads/templates/options/ — {options: [{id, name}]}, or {options: null,
    reason} when ManyChat cannot be asked. A list that was read also brings the
    connections by name up to date, so the screen that follows shows them.
    """
    flows, why = lead_templates.manychat_flows(timeout=lead_templates.ASK_SECONDS)
    if flows is not None:
        lead_templates.remember(flows)
    return {'options': flows, 'reason': why}


def connect(template_key: str, target, user, *, target_name: str = '') -> dict:
    """
    PUT leads/templates/{template_key}/ {target}: connect this text to a
    ManyChat automation — by its flow, or by its name as ManyChat spells it —
    or, with no target, take the connection off. Returns the row as it now is.
    What a person decides here stands above the connection by name: a text
    somebody took the connection off stays without one, also when ManyChat has
    an automation under its fixed name — until a person connects one again.

    The connection is kept only when ManyChat itself lists that automation.
    When ManyChat cannot be asked nothing is kept (except on a developer's
    machine that only records sends, where there is nothing to ask).
    """
    row = template(template_key)                         # LookupError for a key that is no template
    if target is None or (isinstance(target, str) and not target.strip()):
        if row['mapped']:
            settings_store.set_lead_template(template_key, '', user=user)
            logger.info('wahub lead templates: %s disconnected by user %s', template_key, getattr(user, 'pk', None))
        return template(template_key)
    if not isinstance(target, str):
        raise TemplateError('target: מזהה או שם של האוטומציה ב-ManyChat, או null כדי לנתק.')
    wanted = ' '.join(target.split())
    flows, why = lead_templates.manychat_flows(timeout=lead_templates.ASK_SECONDS)
    if flows is None:
        if not sending.simulate_send():
            raise TemplateError(
                f'אי אפשר לבדוק עכשיו מול ManyChat שהאוטומציה קיימת ({why}). החיבור לא נשמר — נסו שוב בעוד רגע.',
                status=502,
            )
        flow, name = wanted, ' '.join(str(target_name or '').split())
    else:
        lead_templates.remember(flows)
        found = next((item for item in flows if item['id'] == wanted), None)
        if found is None:
            named = [item for item in flows if lead_templates.same_name(item['name']) == lead_templates.same_name(wanted)]
            if len(named) > 1:
                raise TemplateError('יש ב-ManyChat כמה אוטומציות בשם הזה. בחרו אחת מהרשימה.')
            found = named[0] if named else None
        if found is None:
            raise TemplateError('לא נמצאה ב-ManyChat אוטומציה בשם או במזהה הזה. החיבור לא נשמר.')
        flow, name = found['id'], found['name']
    settings_store.set_lead_template(template_key, flow, name=name, user=user)
    logger.info('wahub lead templates: %s connected to flow %s by user %s', template_key, flow, getattr(user, 'pk', None))
    # A follow-up that has been waiting for this very template is looked at on the next cron call,
    # not half an hour from now. The engine decides, as always, whether it may leave.
    LeadFollowUp.objects.filter(
        status=NUDGE_DUE, template_key=template_key, claimed_at__isnull=True, next_look_at__gt=timezone.now(),
    ).update(next_look_at=timezone.now())
    return template(template_key)


# --- what stops a follow-up ------------------------------------------------------------------------------

def _blocked(row: LeadFollowUp, contact: Contact, at, mapped: set, reachable: bool) -> str:
    """What keeps this follow-up from leaving at `at`: the switch, ManyChat, a template — or '' when nothing does."""
    if contact.is_demo:
        return ''
    if not sending.sending_enabled(contact):
        return BLOCKED_SENDING
    if not reachable:
        return BLOCKED_MANYCHAT
    if not sending.can_free_text(contact, at) and row.template_key not in mapped:
        return BLOCKED_TEMPLATE
    return ''


def _ahead(row: LeadFollowUp, now, mapped: set, labels: dict, reachable: bool) -> dict:
    contact = row.contact
    at = max(row.due_at, now)
    blocked = _blocked(row, contact, at, mapped, reachable)
    due = row.status == NUDGE_DUE
    return {
        'id': row.id,
        'contact_id': contact.id,
        'name': contact.name,
        'phone_display': phone_display(contact.phone),
        'is_demo': contact.is_demo,
        'kind': row.kind,
        'kind_label': labels.get(row.template_key) or lead_followups.KIND_LABELS.get(row.kind, row.kind),
        'step': row.step,
        'text': row.text,
        'when': row.due_at.isoformat(),
        'state': 'due' if due else 'planned',
        # More than 24 hours after the customer's last message by then: only a template may leave.
        'needs_template': not contact.is_demo and not sending.can_free_text(contact, at),
        'template_key': row.template_key,
        'template_mapped': row.template_key in mapped,
        'blocked': blocked,
        'note': DEMO_WORDS if contact.is_demo else BLOCKED_WORDS.get(blocked, ''),
    }


def _banner(now, mapped: set, reachable: bool) -> dict | None:
    """
    The one quiet line at the head of "הבוט מטפל": how many messages would be
    leaving right now, and what stops them. None when nothing is waiting.
    """
    reasons: dict = {}
    waiting = (
        LeadFollowUp.objects.filter(status=NUDGE_DUE, contact__is_demo=False).select_related('contact')
        .order_by('due_at', 'id')[:DUE_ROWS_READ]
    )
    for row in waiting:
        blocked = _blocked(row, row.contact, now, mapped, reachable)
        if blocked:
            reasons[blocked] = reasons.get(blocked, 0) + 1
    if not reasons:
        return None
    total = sum(reasons.values())
    reason = max(reasons, key=lambda name: (reasons[name], name == BLOCKED_SENDING))
    words = _count(total, 'הודעה אחת הייתה יוצאת עכשיו', '{n} הודעות היו יוצאות עכשיו')
    return {'count': total, 'reason': reason, 'reasons': reasons, 'text': f'{words} — {BLOCKED_WORDS[reason]}'}


# --- the four lines --------------------------------------------------------------------------------------

def _line(key: str, title: str, ok: bool, detail: str, how: str = '') -> dict:
    return {'key': key, 'ok': ok, 'title': title, 'detail': detail, 'how': '' if ok else how}


def _classify_line(now) -> dict:
    """Is every lead understood and staged — and does Kogo see enough of the conversation to do it?"""
    title = 'זיהוי וסיווג הלידים'
    counted = leads.counts()
    open_leads = counted[leads.TAB_OFFICE] + counted[leads.TAB_BOT]
    how_many = 'אין לידים פתוחים' if not open_leads else _count(open_leads, 'ליד פתוח אחד', '{n} לידים פתוחים')
    cutoff = now - STALE_AFTER
    seen = Contact.objects.aggregate(
        last=Max('lead_checked_at'),
        stale=Count('id', filter=(
            Q(lead_checked_at__isnull=True, created_at__lte=cutoff)
            | Q(touched_at__gt=F('lead_checked_at'), touched_at__lte=cutoff)
            | Q(lead_recheck_at__lte=cutoff)
        )),
    )
    if seen['stale']:
        hours = int(STALE_AFTER.total_seconds() // 3600)
        return _line(
            'classify', title, False,
            _count(seen['stale'], f'איש קשר אחד מחכה לסיווג יותר מ-{hours} שעות', '{n} אנשי קשר מחכים לסיווג יותר מ-' + f'{hours} שעות'),
            'הסיווג רץ לבד כל 5 דקות. אם המספר לא יורד — לפנות לתמיכה הטכנית',
        )
    # The old bot answers in ManyChat. Until ManyChat copies its answers, Kogo never sees the
    # registration link it sent — and "קיבל קישור ועוד לא נרשם", the commonest round, never begins.
    # (The same gate as lead_stage.replies_visible; the two change together.)
    if not lead_stage.old_bot_replies_seen(now):
        return _line(
            'classify', title, False,
            f'{how_many}. תשובות הבוט לא מגיעות ל-Kogo — לכן לא ידוע מי כבר קיבל קישור להרשמה',
            'ב-ManyChat: מוסיפים את הצעד שמעתיק את תשובות הבוט ל-Kogo, ולוחצים Update',
        )
    detail = how_many if seen['last'] is None else f'{how_many}, {_ago(seen["last"], now)}'
    return _line('classify', title, True, detail)


def _schedule_line(counted: dict) -> dict:
    """Is every follow-up planned, and is the cron getting to the ones whose moment came?"""
    title = 'תזמון החזרות'
    if counted['late']:
        return _line(
            'schedule', title, False,
            _count(counted['late'], 'חזרה אחת עברה את זמנה ועוד לא טופלה', '{n} חזרות עברו את זמנן ועוד לא טופלו'),
            'החזרות נבדקות לבד כל 5 דקות, בשעות המשרד. אם המספר לא יורד — לפנות לתמיכה הטכנית',
        )
    planned, due = counted['planned'], counted['due']
    parts = []
    if planned:
        parts.append(_count(planned, 'חזרה אחת מתוכננת', '{n} חזרות מתוכננות'))
    if due:
        parts.append(
            _count(due, 'אחת הגיע זמנה', '{n} הגיע זמנן') if planned
            else _count(due, 'חזרה אחת הגיע זמנה', '{n} חזרות הגיע זמנן')
        )
    detail = ', '.join(parts) or 'אין כרגע חזרות מתוכננות'
    if counted['demo']:
        detail += _count(counted['demo'], ' (ועוד אחת של איש קשר דמו)', ' (ועוד {n} של אנשי קשר דמו)')
    return _line('schedule', title, True, detail)


def _sending_line(reachable: bool) -> dict:
    """May a message leave for a customer at all: the owner's switch, and a ManyChat to send through."""
    title = 'שליחה ללקוחות'
    if sending.simulate_send():
        return _line('sending', title, True, 'הדמיה במחשב פיתוח — הודעות נרשמות ולא יוצאות לאף אחד')
    if not sending.sending_enabled():
        testing = Contact.objects.filter(live_test=True, is_demo=False).count()
        detail = 'כבויה — שום הודעה לא יוצאת ללקוחות'
        if testing:
            detail += _count(testing, ' (חוץ ממספר הבדיקה שלך)', ' (חוץ מ-{n} מספרי הבדיקה)')
        return _line('sending', title, False, detail, 'מדליקים בהגדרות המערכת (WAHUB_SENDING_ENABLED)')
    if not reachable:
        return _line(
            'sending', title, False, 'דלוקה, אבל השרת לא מחובר ל-ManyChat — שום הודעה לא יוצאת',
            'מגדירים את המפתח של ManyChat בהגדרות המערכת (MANYCHAT_KEY)',
        )
    return _line('sending', title, True, 'דלוקה — הודעות יוצאות ללקוחות, בשעות המשרד בלבד')


def _templates_line(rows: list[dict]) -> dict:
    """Are the templates of stage 1 connected? (Those the owner turned off are not asked for.)"""
    title = 'תבניות וואטסאפ'
    first = [row for row in rows if row['stage'] == 1 and row['required']]
    later = [row for row in rows if row['stage'] == 2 and row['required']]
    connected = sum(1 for row in first if row['mapped'])
    connected_later = sum(1 for row in later if row['mapped'])
    ok = connected == len(first)
    detail = f'{connected} מתוך {len(first)} של שלב 1 מחוברות'
    if ok and later:
        detail += f' · שלב 2: {connected_later} מתוך {len(later)}'
    return _line('templates', title, ok, detail, 'יוצרים ב-ManyChat ובוחרים כאן למטה')


def status(now=None) -> dict:
    """
    GET leads/automation/ — the four lines with `ok`, today's numbers and the
    next follow-ups. The numbers are of real customers; a demo contact's
    follow-ups are listed among the next ones, marked, and never counted.
    """
    now = now or timezone.now()
    rows = templates()
    mapped = {row['template_key'] for row in rows if row['mapped']}
    labels = {row['template_key']: row['kind_label'] for row in rows}
    reachable = sending.manychat_reachable()
    midnight = lead_stage._midnight(timezone.localtime(now).date())
    real = Q(contact__is_demo=False)
    counted = LeadFollowUp.objects.aggregate(
        planned=Count('id', filter=real & Q(status=NUDGE_PLANNED)),
        due=Count('id', filter=real & Q(status=NUDGE_DUE)),
        sent=Count('id', filter=real & Q(status=NUDGE_SENT, acted_at__gte=midnight)),
        skipped=Count('id', filter=real & Q(status=NUDGE_SKIPPED, acted_at__gte=midnight)),
        failed=Count('id', filter=real & Q(status=NUDGE_FAILED, acted_at__gte=midnight)),
        demo=Count('id', filter=Q(contact__is_demo=True, status__in=NUDGE_OPEN_STATUSES)),
        late=Count('id', filter=Q(status__in=NUDGE_OPEN_STATUSES, claimed_at__isnull=True, next_look_at__lte=now - LATE_AFTER)),
    )
    lines = [_classify_line(now), _schedule_line(counted), _sending_line(reachable), _templates_line(rows)]
    ahead = (
        LeadFollowUp.objects.filter(status__in=NUDGE_OPEN_STATUSES).select_related('contact')
        .order_by('due_at', 'id')[:UPCOMING]
    )
    return {
        'ready': all(line['ok'] for line in lines),
        'lines': lines,
        'today': {name: counted[name] for name in ('planned', 'due', 'sent', 'skipped', 'failed')},
        'upcoming': [_ahead(row, now, mapped, labels, reachable) for row in ahead],
        'banner': _banner(now, mapped, reachable),
    }
