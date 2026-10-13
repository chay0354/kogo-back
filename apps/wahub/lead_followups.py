"""
The bot comes back to a lead by itself (docs/WAHUB-CONTRACT-LEADS-V2.md, ג and ח).

lead_stage.compute says which round of follow-ups a contact should have open —
the kind, when its days are counted from, how many times and how far apart.
This module keeps exactly that one row open (`sync`), and when the row's
moment comes, acts on it (`run`, a slice of the five-minute cron):

    planned   the moment is ahead. Always inside the office hours, never on a
              Friday, a Saturday or a special day: a moment outside them moves
              to the next opening.
    due       the moment came and the message did not leave: the owner's switch
              is off for this contact, the server has no ManyChat to send
              through, or WhatsApp's 24 hours have passed and no approved
              template is connected to this follow-up. "The bot would have
              sent this now." It is tried again for DUE_HOLD, then let go.
    sent      it went out — or, for a demo contact, was written into the demo
              conversation as the new bot's line: nothing ever leaves for a demo.
    skipped   the customer wrote meanwhile, registered, was closed, a person
              took the lead — or the moment passed with nothing sent.
    failed    ManyChat refused, or a send was cut in the middle. Never repeated:
              nobody knows whether the customer got it.

Nothing leaves except through sending.py, and only when
`sending.sending_enabled(contact)` says a message may leave for this contact —
and never to somebody who asked "הסר" (optout.py): the rules plan no round for
them, a round planned before they asked is skipped with that reason, and the
system's list is read once more right before a send; when it cannot be read,
nothing is sent.
The words are written when the row is planned — from the phrasings the owner
edits (lead_followup_seed.py) and what is known about the lead — and put
together again from the same phrasing right before they leave, in case he has
rewritten it since; no model is asked anything at sending time. Right before a send the registrations are read
again (matching.recheck_contact: every phone the family has), because a lead
who registered yesterday must not be asked today whether they still want to.

Two cron calls never act on one row: a call takes the row under
`select_for_update(skip_locked=True)` and marks it claimed before it does
anything, and a claim nobody released is a send of unknown outcome — closed as
failed, not tried again.

Only `LeadFollowUp` rows and the contact's `lead_bot` are written here (and a
line in its journal); the stage follows through lead_stage.refresh.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, time as day_time, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.whatsapp_optout import OptOutListUnavailable
from apps.wahub import (
    knowledge, lead_ai, lead_followup_seed, lead_stage, lead_templates, matching, optout, sending, settings_store, state,
)
from apps.wahub.models import (
    DELIVERED_STATUSES,
    EVENT_LEAD_FOLLOWUP,
    KIND_LINK,
    KIND_PHRASING,
    LEAD_COLD,
    LEAD_GROUP_CLOSED,
    LEAD_OWNER_OFFICE,
    LEAD_REGISTERED,
    NUDGE_DORMANT,
    NUDGE_DUE,
    NUDGE_FAILED,
    NUDGE_KIND_CHOICES,
    NUDGE_OPEN_STATUSES,
    NUDGE_PLANNED,
    NUDGE_SENT,
    NUDGE_SKIPPED,
    NUDGE_TRIAL_FEEDBACK,
    NUDGE_TRIAL_NO_SHOW,
    SCOPE_BUSINESS,
    SPECIAL_OPEN,
    Contact,
    KnowledgeItem,
    LeadFollowUp,
)

logger = logging.getLogger(__name__)

# One cron call: how long the follow-ups may take, and how many rows it looks at.
RUN_SECONDS = 3.0
BATCH = 40
# A follow-up whose moment came and that could not leave waits this long for the switch or the
# template — then it is let go, so turning the switch on never releases weeks of old reminders.
DUE_HOLD = timedelta(days=2)
# How often a `due` row is tried again meanwhile.
LOOK_AGAIN = timedelta(minutes=30)
# A claim older than this was cut in the middle.
CLAIM_STALE = timedelta(minutes=10)
# Two steps of one round are never closer than this, however late the round began.
STEP_GAP = timedelta(days=2)
# How far ahead an opening of the office is looked for.
OPENING_HORIZON = 30
# The office's hours until somebody files them ("שעות ומועדים"): the owner's own, 10:30–18:00, Sunday to Thursday.
DEFAULT_HOURS = (day_time(10, 30), day_time(18, 0))
# Never on these, whatever the office hours say (Python's weekday(): Friday 4, Saturday 5).
NEVER_ON = (4, 5)
# Dormant leads are written to once, on the day the owner set and for a week after it — to those
# who wrote within the last year.
DORMANT_WINDOW = timedelta(days=7)
DORMANT_WITHIN = timedelta(days=365)
DORMANT_BATCH = 100

KIND_LABELS = dict(NUDGE_KIND_CHOICES)

SENDING_OFF = 'השליחה ללקוחות כבויה'
NO_TEMPLATE = 'חסרה תבנית: עברו 24 שעות מההודעה האחרונה של הלקוח, ואז וואטסאפ מרשה רק תבנית מאושרת'
NO_MANYCHAT = 'השרת לא מחובר ל-ManyChat'
DEMO_NOTE = 'נכתב לשיחת הדמו — לא נשלח לאף אחד'
CUT_SHORT = 'השליחה נקטעה באמצע. לא ידוע אם ההודעה יצאה, ולכן היא לא נשלחת שוב'
NO_WORDS = 'אין נוסח פעיל לחזרה הזאת'
# The system's own automations (apps/enrollments) had already written this to the family: the bot does not repeat it.
ALREADY_SAID = {
    NUDGE_TRIAL_FEEDBACK: 'ההורה כבר קיבל את ההודעה האוטומטית שאחרי שיעור הניסיון — הבוט לא שואל שוב',
    NUDGE_TRIAL_NO_SHOW: 'ההורה כבר קיבל הודעה אוטומטית על כך שלא הגיעו — הבוט לא כותב על זה שוב',
}
ALREADY_SAID_OTHER = 'ההודעה הזאת כבר נשלחה ללקוח באוטומציה אחרת של המערכת'


# --- the office hours ------------------------------------------------------------------------------

def _hours_of(day: date, weekly, specials) -> tuple | None:
    """(opens, closes) on `day` as moments, or None when no follow-up leaves on it."""
    if day.weekday() in NEVER_ON:
        return None
    for start, end, state_ in specials:
        if start <= day <= end and state_ != SPECIAL_OPEN:
            # A holiday, its eve, a day of other hours: nobody is nudged on it.
            return None
    opens, closes = DEFAULT_HOURS
    if weekly is not None:
        line = weekly.get(knowledge._WEEKDAY_TO_KEY[day.weekday()]) or {}
        opens, closes = knowledge._parse_time(line.get('from')), knowledge._parse_time(line.get('to'))
        if not line.get('open') or opens is None or closes is None or closes <= opens:
            return None
    zone = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(day, opens), zone),
        timezone.make_aware(datetime.combine(day, closes), zone),
    )


def _calendar(day_from: date) -> tuple:
    """The weekly hours and the special days ahead, read once for a whole look."""
    item = knowledge.office_hours_item()
    weekly = (item.data or {}).get('weekly') if item is not None else None
    specials = []
    for row in KnowledgeItem.objects.filter(kind=knowledge.KIND_SPECIAL_DAY, is_active=True, scope_level=SCOPE_BUSINESS):
        data = row.data or {}
        try:
            start = date.fromisoformat(data.get('date_from') or '')
            end = date.fromisoformat(data.get('date_to') or data.get('date_from') or '')
        except ValueError:
            continue
        if end >= day_from:
            specials.append((start, max(start, end), data.get('state') or ''))
    return weekly if isinstance(weekly, dict) else None, specials


def next_opening(moment, *, calendar=None, whole_day: bool = False):
    """
    `moment` itself when the office is open then; otherwise the next time it
    opens. With `whole_day` it is the opening of that day (or of the next day
    the office is open) whatever the hour — "a day later, when the office opens".
    """
    local = timezone.localtime(moment)
    weekly, specials = calendar if calendar is not None else _calendar(local.date())
    for offset in range(OPENING_HORIZON + 1):
        hours = _hours_of(local.date() + timedelta(days=offset), weekly, specials)
        if hours is None:
            continue
        opens, closes = hours
        if offset == 0 and not whole_day:
            if opens <= moment < closes:
                return moment
            if moment >= closes:
                continue
        return opens
    # No opening in a month: the hours are filed wrong. Nothing is sent meanwhile.
    return moment + timedelta(days=OPENING_HORIZON)


def is_open(moment, *, calendar=None) -> bool:
    return next_opening(moment, calendar=calendar) == moment


def due_at(anchor, delay: timedelta, now, *, previous=None, calendar=None):
    """
    When a step is due: `delay` after the fact the round counts from, never
    before now, never sooner than STEP_GAP after the step before it — and then
    the next moment the office is open. A delay of whole days lands when the
    office opens on that day; a shorter one is that long after the fact.
    """
    whole_days = delay >= timedelta(days=1) or delay == timedelta(0)
    wanted = (anchor or now) + delay
    if whole_days:
        wanted = next_opening(wanted, calendar=calendar, whole_day=True)
    if previous is not None:
        wanted = max(wanted, previous + STEP_GAP)
    return next_opening(max(wanted, now), calendar=calendar)


# --- the words ---------------------------------------------------------------------------------------

_OPTIONAL = re.compile(r'\[([^\[\]]*)\]')
_VARIABLE = re.compile(r'\{([^{}]+)\}')
_NO_CLASS_NAMED = {'ל': 'לחוג', 'ב': 'בחוג', 'מ': 'מהחוג', 'ה': 'החוג'}


def render(body: str, values: dict) -> str:
    """
    A phrasing with what is known about the lead put in. A stretch in square
    brackets is dropped whole when a variable inside it has no value; outside
    brackets a missing {חוג} reads "החוג" (after ל / ב: "לחוג", "בחוג") and any
    other missing variable is simply left out. Nothing is ever invented.
    """
    def known(name: str) -> str:
        return str(values.get(name.strip()) or '').strip()

    def optional(match) -> str:
        inside = match.group(1)
        if any(not known(name) for name in _VARIABLE.findall(inside)):
            return ''
        return _VARIABLE.sub(lambda found: known(found.group(1)), inside)

    text = _OPTIONAL.sub(optional, body or '')

    def required(match) -> str:
        value = known(match.group(2))
        if value:
            return f'{match.group(1)}{value}'
        if match.group(2).strip() == 'חוג':
            return _NO_CLASS_NAMED.get(match.group(1), f'{match.group(1)}החוג')
        return ''

    text = re.sub(r'(?<![א-ת])([לבמה]?)\{([^{}]+)\}', required, text)
    text = _VARIABLE.sub(lambda found: known(found.group(1)), text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r' +([.,?!])', r'\1', text)
    return '\n'.join(line.strip() for line in text.splitlines()).strip()


def _texts(key: str) -> list[str] | None:
    """
    The texts of a phrasing, the first time first: the owner's, from the bot's
    knowledge; the draft when nothing was filed under that key; [] when he
    turned it off. None when there is no such phrasing at all.
    """
    item = KnowledgeItem.objects.filter(kind=KIND_PHRASING, key=key).order_by('-is_active', '-id').first()
    if item is not None:
        if not item.is_active:
            return []
        later = (item.data or {}).get('variants')
        return [item.body, *[text for text in (later if isinstance(later, list) else []) if isinstance(text, str)]]
    draft = lead_followup_seed.DEFAULTS.get(key)
    if draft is None:
        return None
    return list(draft['texts']) if draft['active'] else []


def phrasing(kind: str, step: int, variant: str = '') -> tuple[str, str]:
    """
    (the text as it is filed, the name of its WhatsApp template) for one step
    of a round. A variant has its own first words and takes the kind's own for
    the steps it does not spell out. ('', '') when there are no live words.
    """
    own = _texts(lead_followup_seed.phrasing_key(kind, variant)) if variant else None
    if own is not None and len(own) >= step and own[step - 1].strip():
        return own[step - 1], f'{kind}_{variant}_{step}'
    if own == [] and step == 1:
        return '', ''
    base = _texts(lead_followup_seed.phrasing_key(kind)) or []
    if len(base) >= step and base[step - 1].strip():
        return base[step - 1], f'{kind}_{step}'
    return '', ''


def registration_link() -> str:
    item = KnowledgeItem.objects.filter(kind=KIND_LINK, is_active=True, key__startswith='registration').order_by('id').first()
    url = (item.data or {}).get('url') if item is not None else ''
    if not url:
        from apps.wahub import knowledge_seed

        url = knowledge_seed.VARIABLES['{קישור_הרשמה}']
    return url


def values_for(contact: Contact) -> dict:
    """What a phrasing may name: only what the conversation said, or what Kogo knows for certain."""
    kept = contact.lead_ai if isinstance(contact.lead_ai, dict) else {}
    child = kept.get('child') if isinstance(kept.get('child'), dict) else {}
    name = (child.get('name') or '').strip()
    if not name and len(contact.kogo_child_ids or []) == 1:
        # One child on the family's card: that is the child, by the registrations themselves.
        from apps.customers.models import Child

        name = (Child.objects.filter(pk=contact.kogo_child_ids[0]).values_list('first_name', flat=True).first() or '').strip()
    come_back = kept.get('come_back') if isinstance(kept.get('come_back'), dict) else {}
    until = come_back.get('until_years')
    return {
        'חוג': contact.lead_course_type or kept.get('course_type') or contact.known_course_type,
        'סניף': kept.get('branch_name') or contact.known_branch_name or kept.get('city') or contact.known_city,
        'ילד': name,
        'גיל': until if isinstance(until, int) and not isinstance(until, bool) else lead_ai.MIN_AGE_YEARS,
        'קישור': registration_link(),
    }


def words_now(row: LeadFollowUp, contact: Contact) -> str:
    """
    The row's words as its phrasing stands this minute: the owner — or a
    deploy that brought the phrasings to new words — may have rewritten it
    since the row was planned. Still the phrasing put together with what is
    known about the lead; no model is asked. When the phrasing has no live
    words any more, what was planned stands.
    """
    rest = row.template_key[len(row.kind) + 1:] if row.template_key.startswith(f'{row.kind}_') else ''
    variant = rest.rsplit('_', 1)[0] if '_' in rest else ''
    body, _template_key = phrasing(row.kind, row.step, variant)
    return (render(body, values_for(contact)) if body else '') or row.text


def reword_open() -> int:
    """
    Bring every follow-up that is still ahead to the words its phrasing has
    now — for the day the phrasings themselves are rewritten, so the screen
    shows what will really be said. A row a cron call is acting on this moment
    is left to that call (it rewords for itself). Returns how many changed.
    """
    changed = 0
    for row in LeadFollowUp.objects.filter(status__in=NUDGE_OPEN_STATUSES, claimed_at__isnull=True).select_related('contact'):
        words = words_now(row, row.contact)
        if words != row.text:
            changed += LeadFollowUp.objects.filter(
                pk=row.pk, status__in=NUDGE_OPEN_STATUSES, claimed_at__isnull=True,
            ).update(text=words)
    return changed


def template_rows() -> list[dict]:
    """GET leads/templates/ — every text the bot may come back with, and whether a template is connected to it."""
    mapped = lead_templates.connections()
    rows = []
    for kind, variant, _title, _when, _body, _later, _active in lead_followup_seed.PHRASINGS:
        key = lead_followup_seed.phrasing_key(kind, variant)
        draft = lead_followup_seed.DEFAULTS[key]
        texts = _texts(key)
        # A text that leaves on a person's press has no round, and so no days to count.
        delays = () if kind in lead_followup_seed.BY_HAND else lead_stage.delays_for(kind, variant)
        for step, text in enumerate(texts or [], 1):
            if not text.strip():
                continue
            template_key = f'{kind}_{variant}_{step}' if variant else f'{kind}_{step}'
            rows.append({
                'kind': kind,
                'variant': variant,
                'step': step,
                'label': draft['title'].removeprefix('חזרה לליד: ').removeprefix('חזרה לליד רדום: '),
                'when': draft['when'],
                'text': text,
                'template_key': template_key,
                'mapped': template_key in mapped,
                'active': True,
                'days': delays[step - 1].days if step <= len(delays) else None,
            })
        if not texts:
            rows.append({
                'kind': kind, 'variant': variant, 'step': 1,
                'label': draft['title'].removeprefix('חזרה לליד: ').removeprefix('חזרה לליד רדום: '),
                'when': draft['when'], 'text': draft['texts'][0],
                'template_key': f'{kind}_{variant}_1' if variant else f'{kind}_1',
                'mapped': False, 'active': False, 'days': delays[0].days if delays else None,
            })
    return rows


# --- which round is open ----------------------------------------------------------------------------

def open_key(contact_id) -> str:
    """The round of the follow-up that is open for this contact; '' when none is."""
    return (
        LeadFollowUp.objects.filter(contact_id=contact_id, status__in=NUDGE_OPEN_STATUSES)
        .exclude(kind=NUDGE_DORMANT).values_list('episode', flat=True).first() or ''
    )


def open_keys(contact_ids) -> dict:
    """{contact id: the round open for it} for the contacts given that have one. One query."""
    contact_ids = list(contact_ids)
    if not contact_ids:
        return {}
    return dict(
        LeadFollowUp.objects.filter(contact_id__in=contact_ids, status__in=NUDGE_OPEN_STATUSES)
        .exclude(kind=NUDGE_DORMANT).values_list('contact_id', 'episode')
    )


def _round_of(contact: Contact, key: str) -> dict:
    kept = contact.lead_bot if isinstance(contact.lead_bot, dict) else {}
    return dict(kept) if kept.get('episode') == key else {}


def _keep_round(contact: Contact, round_: dict) -> None:
    """
    The round's place, kept on the contact. The screen has to see it (the next
    step changed); and it is stamped as looked at in the same instant, the way
    lead_stage.refresh does, because every caller here has just worked the
    stage out or is about to — the scan need not come back for this write.
    """
    moment = timezone.now()
    Contact.objects.filter(pk=contact.pk).update(
        lead_bot=round_, touched_at=moment, updated_at=moment, lead_checked_at=moment,
    )
    contact.lead_bot = round_


def _close(row: LeadFollowUp, status: str, note: str, now, *, message=None) -> None:
    row.status, row.outcome_note, row.acted_at, row.claimed_at = status, (note or '')[:300], now, None
    row.message = message
    row.save(update_fields=['status', 'outcome_note', 'acted_at', 'claimed_at', 'message'])


def _why_skipped(contact: Contact, result: dict, row: LeadFollowUp) -> str:
    """Why a planned follow-up is no longer the one to send, in the screen's words."""
    if result['stage'] == LEAD_REGISTERED:
        return 'נרשם'
    if not result['stage']:
        return 'לא ליד לחוגים'
    if result['group'] == LEAD_GROUP_CLOSED:
        return f'הליד נסגר: {result["label"]}'
    if optout.is_marked(contact):
        return optout.NOT_SENT
    wrote = contact.last_inbound_at is not None and contact.last_inbound_at > row.created_at
    if result['owner'] == LEAD_OWNER_OFFICE:
        return 'הלקוח כתב בינתיים — עבר לטיפול המשרד' if wrote else 'עבר לטיפול המשרד'
    if wrote:
        return 'הלקוח כתב בינתיים'
    return f'המצב השתנה: {result["label"]}'


def _plan(contact: Contact, follow: dict, why: str, now, *, calendar=None) -> LeadFollowUp | None:
    """
    The next step of the round `follow`, as one planned row; the round's place
    is kept on the contact. None when the round has no step left, or no live
    words for it — and then the round is marked over, so nobody asks again.
    """
    key, kind, delays = follow['key'], follow['kind'], follow['delays']
    round_ = _round_of(contact, key)
    if not round_:
        # The fact the round counts its days from is kept as it was when the round began: the bot's
        # own follow-ups are messages too, and must not move it.
        anchor = follow.get('anchor')
        # A round the system's own automations have already begun (lead_stage: `from_step`) starts from
        # the bot's first step of its own, on that step's own day.
        passed = max(0, int(follow.get('from_step') or 1) - 1)
        round_ = {
            'episode': key, 'kind': kind, 'started_at': now.isoformat(), 'anchor': anchor.isoformat() if anchor else None,
            'steps': len(delays), 'step': passed, 'attempts': 0, 'passed': passed, 'last_at': None, 'acted_at': None,
            'done': False, 'hot': bool(follow.get('hot')), 'note': '',
        }
    if round_.get('done'):
        return None
    step = int(round_.get('step') or 0) + 1
    if step > len(delays):
        _keep_round(contact, {**round_, 'done': True})
        return None
    body, template_key = phrasing(kind, step, follow.get('variant') or '')
    text = render(body, values_for(contact)) if body else ''
    if not text:
        _keep_round(contact, {**round_, 'done': True, 'note': NO_WORDS})
        return None
    previous = lead_stage._moment(round_.get('acted_at'))
    anchor = lead_stage._moment(round_.get('anchor')) or follow.get('anchor')
    when = due_at(anchor, delays[step - 1], now, previous=previous, calendar=calendar)
    try:
        with transaction.atomic():
            row = LeadFollowUp.objects.create(
                contact=contact, kind=kind, step=step, episode=key, due_at=when, next_look_at=when,
                status=NUDGE_PLANNED, text=text, template_key=template_key, why=(why or '')[:300],
            )
    except IntegrityError:
        # Another call planned this contact's follow-up in the same moment: one is enough.
        return None
    _keep_round(contact, {**round_, 'step': step})
    return row


def _already_said(row: LeadFollowUp, follow: dict | None) -> bool:
    """The step this row is has meanwhile been said by one of the system's own automations."""
    return follow is not None and row.kind != NUDGE_DORMANT and row.step < int(follow.get('from_step') or 1)


def _pass_over(contact: Contact, row: LeadFollowUp, follow: dict, now) -> None:
    """
    Close a step the system's own automations have already said, and move the
    round past it: the customer is not asked the same thing twice, and the
    round goes on from its next step — nothing is counted as the bot's own.
    """
    why = ALREADY_SAID.get(row.kind, ALREADY_SAID_OTHER)
    _close(row, NUDGE_SKIPPED, why, now)
    _log(contact, row, f'בוטלה — {why}')
    round_ = _round_of(contact, row.episode)
    if round_:
        passed = int(follow.get('from_step') or 1) - 1
        round_.update(step=max(int(round_.get('step') or 0), passed), passed=max(int(round_.get('passed') or 0), passed))
        _keep_round(contact, round_)


def sync(contact_id, *, now=None) -> None:
    """
    Bring the contact's open follow-up in line with what the rules ask for
    (lead_stage.compute): a follow-up of another round — the customer wrote,
    registered, was closed, a person took the lead — is skipped with the reason,
    and the round the rules name, if any, gets its next step planned. A row a
    cron call is acting on this very moment is left to that call.
    """
    now = now or timezone.now()
    with transaction.atomic():
        contact = Contact.objects.select_for_update().filter(pk=contact_id).first()
        if contact is None:
            return
        result = lead_stage.compute(contact, now)
        follow = result['follow']
        rows = list(LeadFollowUp.objects.select_for_update().filter(contact=contact, status__in=NUDGE_OPEN_STATUSES))
        standing = False
        for row in rows:
            if row.kind == NUDGE_DORMANT:
                if result['stage'] == LEAD_COLD or row.claimed_at is not None:
                    standing = True
                    continue
            elif (follow is not None and row.episode == follow['key']) or row.claimed_at is not None:
                if row.claimed_at is None and _already_said(row, follow):
                    # The same round, one step on: the next step is planned below.
                    _pass_over(contact, row, follow, now)
                    continue
                standing = True
                continue
            _close(row, NUDGE_SKIPPED, _why_skipped(contact, result, row), now)
        planned = _plan(contact, follow, result['reason'], now) if follow is not None and not standing else None
        over = follow is not None and not standing and planned is None
    if over:
        # The round ended where it should have begun (no live words): the stage says so — a hot
        # lead with nothing to say to it is the office's.
        lead_stage.refresh(contact_id, now=now, follow=False)


# --- acting on a row whose moment has come ---------------------------------------------------------------

def _claim(row_id: int, now) -> LeadFollowUp | None:
    with transaction.atomic():
        row = (
            LeadFollowUp.objects.select_for_update(skip_locked=True)
            .filter(pk=row_id, status__in=NUDGE_OPEN_STATUSES, claimed_at__isnull=True).first()
        )
        if row is None:
            return None
        row.claimed_at = now
        row.save(update_fields=['claimed_at'])
        return row


def _wait(row: LeadFollowUp, until, *, status: str | None = None, note: str | None = None) -> None:
    """Put the row down again, to be looked at at `until`."""
    row.claimed_at, row.next_look_at = None, until
    fields = ['claimed_at', 'next_look_at']
    if status is not None:
        row.status = status
        fields.append('status')
    if note is not None:
        row.outcome_note = note[:300]
        fields.append('outcome_note')
    row.save(update_fields=fields)


def _log(contact: Contact, row: LeadFollowUp, words: str) -> None:
    state.log_event(contact.pk, EVENT_LEAD_FOLLOWUP, f'{KIND_LABELS.get(row.kind, row.kind)}, פנייה {row.step}: {words}')


def _after_step(contact: Contact, row: LeadFollowUp, now, *, sent_at=None, note: str = '') -> None:
    """
    A step is behind us — it went out (`sent_at`), or it will not. The round
    moves on: its next step is planned, unless it was the last one, or it could
    not leave and the lead is a hot one — nobody waits another week for a
    message that does not go out; the office takes it. The stage follows.
    """
    if row.kind == NUDGE_DORMANT:
        return
    contact.refresh_from_db()
    round_ = _round_of(contact, row.episode)
    if round_:
        round_['acted_at'] = now.isoformat()
        if sent_at is not None:
            round_['attempts'] = int(round_.get('attempts') or 0) + 1
            round_['last_at'] = sent_at.isoformat()
        last = row.step >= int(round_.get('steps') or row.step)
        if last or (sent_at is None and round_.get('hot')):
            round_.update(done=True, note=note if sent_at is None else '')
        _keep_round(contact, round_)
    # The stage first (an owner may change), then whatever step the rules now ask for.
    lead_stage.refresh(contact.pk, now=now, follow=False)
    sync(contact.pk, now=now)


def _hold(contact: Contact, row: LeadFollowUp, why: str, now) -> str:
    """The moment came and the message may not leave: "the bot would have sent this now" — for DUE_HOLD."""
    lets_go = row.due_at + DUE_HOLD
    if now >= lets_go:
        _close(row, NUDGE_SKIPPED, f'לא נשלח: {why}', now)
        _log(contact, row, f'לא נשלח — {why}')
        _after_step(contact, row, now, note=why)
        return NUDGE_SKIPPED
    _wait(row, min(now + LOOK_AGAIN, lets_go), status=NUDGE_DUE, note=why)
    return NUDGE_DUE


def _still_wanted(contact: Contact, row: LeadFollowUp, result: dict) -> bool:
    if row.kind == NUDGE_DORMANT:
        return result['stage'] == LEAD_COLD
    follow = result['follow']
    return follow is not None and follow['key'] == row.episode


def _act(row: LeadFollowUp, now, *, calendar=None) -> str:
    """One claimed row: skipped, put off to the next opening, held as due, or sent. Returns what became of it."""
    contact = Contact.objects.get(pk=row.contact_id)
    # The registrations, read again this minute — by every phone on the family's card, not only the
    # one that wrote — and the stage with them: a lead who registered yesterday is not asked today.
    try:
        matching.recheck_contact(contact)
    except Exception:
        logger.exception('wahub follow-ups: the registrations of contact %s could not be read again', contact.pk)
        _wait(row, now + LOOK_AGAIN)
        return 'waiting'
    lead_stage.refresh(contact.pk, now=now, follow=False)
    contact.refresh_from_db()
    result = lead_stage.compute(contact, now)
    if not _still_wanted(contact, row, result):
        why = _why_skipped(contact, result, row)
        _close(row, NUDGE_SKIPPED, why, now)
        _log(contact, row, f'בוטלה — {why}')
        sync(contact.pk, now=now)
        return NUDGE_SKIPPED
    if _already_said(row, result['follow']):
        # Read this minute with the registrations: the system's own message went out since this was planned.
        _pass_over(contact, row, result['follow'], now)
        sync(contact.pk, now=now)
        return NUDGE_SKIPPED
    # "הסר": the system's list, read this minute — also for a phone another part of the system put on it,
    # and for the dormant leads, whom no rule of the stage looks at. A list that cannot be read sends nothing.
    try:
        asked_out = optout.blocked(contact)
    except OptOutListUnavailable:
        logger.warning('wahub follow-ups: the opt-out list could not be read for contact %s; nothing is sent', contact.pk)
        _wait(row, now + LOOK_AGAIN)
        return 'waiting'
    if asked_out:
        _close(row, NUDGE_SKIPPED, optout.NOT_SENT, now)
        _log(contact, row, f'בוטלה — {optout.NOT_SENT}')
        _after_step(contact, row, now, note=optout.NOT_SENT)
        return NUDGE_SKIPPED

    opens = next_opening(now, calendar=calendar)
    if opens != now:
        _wait(row, opens)
        return 'waiting'

    # The words as the phrasing stands now, not as it stood on the day this was planned.
    words = words_now(row, contact)
    if words != row.text:
        row.text = words
        row.save(update_fields=['text'])

    if contact.is_demo:
        # Nothing ever leaves for a demo contact. The line is written into its conversation, as the
        # new bot's, so the whole round can be watched.
        message = sending.keep_new_bot_reply(contact, row.text)
        _close(row, NUDGE_SENT, DEMO_NOTE, now, message=message)
        _log(contact, row, f'{row.text[:300]} ({DEMO_NOTE})')
        _after_step(contact, row, now, sent_at=now)
        return NUDGE_SENT
    if not sending.sending_enabled(contact):
        return _hold(contact, row, SENDING_OFF, now)
    if not sending.manychat_reachable():
        # A certain "not sent": it waits like any message that may not leave, and costs the round no step.
        return _hold(contact, row, NO_MANYCHAT, now)
    if sending.can_free_text(contact, now):
        message = sending.send_lead_followup(contact, row.text)
    else:
        flow = lead_templates.flow_for(row.template_key, now)
        if not flow:
            return _hold(contact, row, NO_TEMPLATE, now)
        message = sending.send_lead_followup_flow(contact, flow, row.text)
    if message.status in DELIVERED_STATUSES:
        _close(row, NUDGE_SENT, '', now, message=message)
        _log(contact, row, row.text[:300])
        _after_step(contact, row, now, sent_at=now)
        return NUDGE_SENT
    _close(row, NUDGE_FAILED, message.error or 'השליחה נכשלה', now, message=message)
    _log(contact, row, f'השליחה נכשלה — {message.error}')
    _after_step(contact, row, now, note=message.error or 'השליחה נכשלה')
    return NUDGE_FAILED


def _release_cut_claims(now) -> int:
    """A row claimed long ago and never finished: a call was cut in the middle of it. Closed, never repeated."""
    cut = list(
        LeadFollowUp.objects.filter(status__in=NUDGE_OPEN_STATUSES, claimed_at__lt=now - CLAIM_STALE).select_related('contact')
    )
    for row in cut:
        _close(row, NUDGE_FAILED, CUT_SHORT, now)
        try:
            _log(row.contact, row, CUT_SHORT)
            _after_step(row.contact, row, now, note=CUT_SHORT)
        except Exception:
            logger.exception('wahub follow-ups: the round of contact %s was not moved on', row.contact_id)
    return len(cut)


def plan_dormant(now) -> int:
    """
    Dormant leads are written to once, on the day the owner set
    (settings_store.LEAD_DORMANT_ON) — registration opening, a new season. With
    no day set, or no live words for it, nothing is planned: that is how it
    stands until he decides. One row per contact per day set.
    """
    raw = settings_store.get(settings_store.LEAD_DORMANT_ON, '')
    try:
        day = date.fromisoformat(str(raw or ''))
    except ValueError:
        return 0
    today = timezone.localtime(now).date()
    if not (day <= today <= day + DORMANT_WINDOW):
        return 0
    body, template_key = phrasing(NUDGE_DORMANT, 1)
    if not body:
        return 0
    key = f'{NUDGE_DORMANT}|{day.isoformat()}'
    asked = LeadFollowUp.objects.filter(kind=NUDGE_DORMANT, episode=key).values('contact_id')
    busy = LeadFollowUp.objects.filter(status__in=NUDGE_OPEN_STATUSES).values('contact_id')
    planned = 0
    when = next_opening(now)
    for contact in (
        # Never to somebody who asked "הסר": this is the one message here that goes to many at once.
        Contact.objects.filter(lead_stage=LEAD_COLD, last_inbound_at__gte=now - DORMANT_WITHIN, opted_out={})
        .exclude(pk__in=asked).exclude(pk__in=busy).order_by('-last_inbound_at')[:DORMANT_BATCH]
    ):
        text = render(body, values_for(contact))
        if not text:
            continue
        try:
            with transaction.atomic():
                LeadFollowUp.objects.create(
                    contact=contact, kind=NUDGE_DORMANT, step=1, episode=key, due_at=when, next_look_at=when,
                    status=NUDGE_PLANNED, text=text, template_key=template_key, why='ליד רדום — נפתחה הרשמה',
                )
        except IntegrityError:
            continue
        state.touch(contact.pk)
        planned += 1
    return planned


def run(budget_seconds: float = RUN_SECONDS, *, now=None, batch: int = BATCH) -> dict:
    """
    The cron's slice: every follow-up whose moment has come, oldest first, each
    finished and saved on its own — a call that runs out of time loses nothing
    and the next one carries on. Sends only what `_act` lets through.
    """
    started = time.monotonic()
    now = now or timezone.now()
    counts = {'followups_sent': 0, 'followups_due': 0, 'followups_skipped': 0, 'followups_failed': 0}

    def left() -> float:
        return budget_seconds - (time.monotonic() - started)

    if left() <= 0:
        return counts
    counts['followups_failed'] += _release_cut_claims(now)
    try:
        plan_dormant(now)
    except Exception:
        logger.exception('wahub follow-ups: the dormant leads were not planned')
    row_ids = list(
        LeadFollowUp.objects.filter(status__in=NUDGE_OPEN_STATUSES, next_look_at__lte=now, claimed_at__isnull=True)
        .order_by('next_look_at', 'id').values_list('id', flat=True)[:batch]
    )
    calendar = _calendar(timezone.localtime(now).date()) if row_ids else None
    for row_id in row_ids:
        if left() <= 0:
            break
        row = _claim(row_id, now)
        if row is None:
            continue
        try:
            outcome = _act(row, now, calendar=calendar)
        except Exception:
            # Whatever happened, a message that may have left is never sent a second time: the claim
            # stays, and the next calls close the row as cut short.
            logger.exception('wahub follow-ups: follow-up %s of contact %s failed', row.pk, row.contact_id)
            continue
        name = f'followups_{outcome}'
        if name in counts:
            counts[name] += 1
    return counts


# --- what a screen shows --------------------------------------------------------------------------------

def payload(row: LeadFollowUp) -> dict:
    from apps.wahub.models import NUDGE_STATUS_CHOICES

    return {
        'id': row.id,
        'kind': row.kind,
        'kind_label': KIND_LABELS.get(row.kind, row.kind),
        'step': row.step,
        'status': row.status,
        'status_label': dict(NUDGE_STATUS_CHOICES).get(row.status, row.status),
        'due_at': row.due_at.isoformat() if row.due_at else None,
        'acted_at': row.acted_at.isoformat() if row.acted_at else None,
        'text': row.text,
        'template_key': row.template_key,
        'why': row.why,
        'outcome_note': row.outcome_note,
    }
