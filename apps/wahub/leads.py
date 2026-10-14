"""
The leads screen, version 2 (docs/WAHUB-CONTRACT-LEADS-V2.md, ד): the lists by
who makes the next move, one lead in full, and the four things a person does
to a lead — close it, open it again, put it off to a day, take it or hand it
back to the bot.

Everything a row shows is already on the contact (lead_stage.py keeps it); the
one extra read is the bot's open follow-up, for "what happens next and when".
What a person decides here is kept in `lead_manual` — and a put-off is the
office's own "בזמן אחר" mark with its day, so the two screens agree — and is a
line in the contact's journal with who and when. Nothing here sends anything.
"""
from __future__ import annotations

import uuid
from datetime import date

from django.db import transaction
from django.db.models import Case, Count, DateTimeField, F, IntegerField, Q, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone

from apps.wahub import (
    lead_ai, lead_followups, lead_stage, office_clock, optout, queries, search, serializers, state, waiting_list, waiting_summary,
)
from apps.wahub.models import (
    EVENT_CLOSED,
    EVENT_CREATED,
    EVENT_FOLLOWUP,
    EVENT_HANDLED_BY,
    EVENT_KIND_CHOICES,
    EVENT_LEAD_ACTION,
    EVENT_LEAD_FOLLOWUP,
    EVENT_LEAD_STAGE,
    EVENT_NEEDS_HUMAN,
    EVENT_NOTE,
    EVENT_OUTCOME,
    EVENT_WAITING_LIST,
    FOLLOWUP_LATER,
    FOLLOWUP_NOT_RELEVANT,
    LEAD_CLOSE_CHOICES,
    LEAD_CLOSE_DELETED,
    LEAD_GROUP_CLOSED,
    LEAD_IN_CONVERSATION,
    LEAD_NOTHING_TO_OFFER,
    LEAD_OWNER_BOT,
    LEAD_OWNER_OFFICE,
    LEAD_SIGNUP_FAILED,
    LEAD_SIGNUP_STUCK,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    LEAD_TRIAL_BOOKED,
    LEAD_WAITING_DATE,
    LEAD_WAITING_LIST,
    LEGACY_LEAD_STAGES,
    NUDGE_DATE_ARRIVED,
    NUDGE_DORMANT,
    NUDGE_DUE,
    NUDGE_LINK_REMINDER,
    NUDGE_OPEN_STATUSES,
    NUDGE_OPENING,
    NUDGE_TRIAL_FEEDBACK,
    NUDGE_TRIAL_NO_SHOW,
    NUDGE_WILL_CHECK,
    OUTCOME_CHOICES,
    OUTCOME_TRIAL_ONLY,
    OUTCOME_TRIAL_UPCOMING,
    WAITING_FREED,
    Contact,
    ContactEvent,
    LeadFollowUp,
)
from apps.wahub.phones import phone_display

TAB_OFFICE = 'office'
TAB_BOT = 'bot'
TAB_CLOSED = 'closed'
# "מחכים שיתפנה מקום" — the leads of the waiting list, under a heading of their own
# (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3); and every lead at once, for a search that crosses the lists.
TAB_WAITING = 'waiting'
TAB_ALL = 'all'
TABS = (TAB_OFFICE, TAB_BOT, TAB_CLOSED)
ASKED_FOR = (*TABS, TAB_WAITING, TAB_ALL)
# The four lists a lead is shown under, and what each is called.
LIST_LABELS = {
    TAB_OFFICE: 'מחכים לך', TAB_BOT: 'הבוט מטפל', TAB_WAITING: waiting_list.LIST_TITLE, TAB_CLOSED: 'נסגרו',
}
PAGE = 50
MAX_PAGE = 100
FOLLOWUPS_SHOWN = 30
TIMELINE_EVENTS = 10
NOTES_SHOWN = 10

STAGE_LABELS = dict(LEAD_STAGE_CHOICES)
OUTCOME_LABELS = dict(OUTCOME_CHOICES)
EVENT_LABELS = dict(EVENT_KIND_CHOICES)
CLOSE_REASONS = dict(LEAD_CLOSE_CHOICES)
OPEN_STAGES = [stage for stage, _label in LEAD_STAGE_CHOICES if LEAD_STAGE_GROUPS[stage] != LEAD_GROUP_CLOSED]
# A row the cron has not looked at since a stage got its present name is closed like its heir.
CLOSED_STAGES = [stage for stage, _label in LEAD_STAGE_CHOICES if LEAD_STAGE_GROUPS[stage] == LEAD_GROUP_CLOSED] + [
    old for old, new in LEGACY_LEAD_STAGES.items() if LEAD_STAGE_GROUPS[new] == LEAD_GROUP_CLOSED
]
# Always at the head of "מחכים לך": somebody tried to register and could not (ח.12).
OFFICE_FIRST = (LEAD_SIGNUP_STUCK, LEAD_SIGNUP_FAILED)

# What the bot is about to do, by the kind of its follow-up.
NEXT_BY_KIND = {
    NUDGE_LINK_REMINDER: 'הבוט יזכיר את ההרשמה',
    NUDGE_WILL_CHECK: 'הבוט ישאל אם הספיקו לבדוק',
    NUDGE_TRIAL_FEEDBACK: 'הבוט ישאל איך היה בשיעור הניסיון',
    NUDGE_TRIAL_NO_SHOW: 'הבוט יציע מועד אחר לניסיון',
    NUDGE_DATE_ARRIVED: 'הבוט יחזור אליו במועד שדובר עליו',
    NUDGE_OPENING: 'הבוט ישאל מה הם מחפשים',
    NUDGE_DORMANT: 'הבוט יודיע שנפתחה הרשמה',
}
# ... and where a round of one kind asks something else than the kind's own (`variant`, read from the
# follow-up's template key — lead_followups.variant_of).
NEXT_BY_VARIANT = {
    lead_stage.VARIANT_EXTERNAL: 'הבוט יבדוק אם הסתדרו מול העירייה',
    lead_stage.VARIANT_SILENT: 'הבוט יציע שיעור ניסיון',
}
# ... and where a later step of a round says something else than its first.
NEXT_BY_STEP = {
    (NUDGE_TRIAL_FEEDBACK, 2): 'הבוט ישלח את הקישור להרשמה',
    (NUDGE_TRIAL_FEEDBACK, 3): 'הבוט ישאל אם החוג עדיין רלוונטי',
    (NUDGE_TRIAL_NO_SHOW, 2): 'הבוט יציע ימים אחרים לניסיון',
}
NOTHING_PLANNED = 'אין חזרה מתוכננת'
WAITING_FOR_AN_ANSWER = 'הבוט חזר אליו — מחכים לתשובה'
KEPT_FOR_LATER = 'נשמר: נודיע כשנפתח'
ON_A_WAITING_LIST = 'ברשימת המתנה — נתריע כשיתפנה מקום'
TALKING_NOW = 'בשיחה עכשיו'
WEEKDAYS = ('שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון')        # Python's weekday(): Monday is 0
# What goes into the short timeline of one lead: what happened to it, not every summary and tag.
TIMELINE_KINDS = (
    EVENT_CREATED, EVENT_LEAD_STAGE, EVENT_LEAD_FOLLOWUP, EVENT_LEAD_ACTION, EVENT_OUTCOME, EVENT_FOLLOWUP,
    EVENT_NEEDS_HUMAN, EVENT_HANDLED_BY, EVENT_CLOSED, EVENT_WAITING_LIST,
)


class LeadError(ValueError):
    """An action refused, with the reason in Hebrew."""


def _iso(moment):
    return moment.isoformat() if moment else None


# --- the lists -------------------------------------------------------------------------------------------

ON_THE_WAITING_LIST = Q(lead_stage=LEAD_WAITING_LIST)
# The lead's own sentences, searched beside the conversation's (search.py).
SEARCHED_SENTENCES = ('lead_reason', 'lead_ai__summary')


def tab_q(tab: str, *, apart: bool = False) -> Q:
    """
    Whose list a lead is on. Every open lead is on one of the two: the office's
    when the next move is a person's, the bot's otherwise — also when nobody has
    a step right now (a trial ahead, nothing to offer), so that no lead is out
    of sight. A contact with no stage is no lead, and is on none.

    `apart` — the screen shows the leads of the waiting list under their own
    heading (`waiting=apart`), so the office's list and the bot's are without
    them. Who answers the customer is not touched by it: a lead whose seat came
    free is still the office's.
    """
    if tab == TAB_CLOSED:
        return Q(lead_stage__in=CLOSED_STAGES)
    if tab == TAB_WAITING:
        return ON_THE_WAITING_LIST
    if tab == TAB_ALL:
        return Q(lead_stage__in=OPEN_STAGES + CLOSED_STAGES)
    rest = ~ON_THE_WAITING_LIST if apart else Q()
    if tab == TAB_OFFICE:
        return Q(lead_stage__in=OPEN_STAGES, lead_owner=LEAD_OWNER_OFFICE) & rest
    return Q(lead_stage__in=OPEN_STAGES) & ~Q(lead_owner=LEAD_OWNER_OFFICE) & rest


# When the customer was told of the seat that is free now, out of the contact's copy of its row
# (waiting_list._cache_of writes it as an ISO moment, or not at all). Read as a moment only when it
# looks like one: a list of every lead must not fall over one odd value.
_TOLD_AT = Case(
    When(
        lead_waiting__notified_at__regex=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',
        then=Cast(KeyTextTransform('notified_at', 'lead_waiting'), DateTimeField()),
    ),
    default=Value(None), output_field=DateTimeField(),
)
_SEAT_FREE = ON_THE_WAITING_LIST & Q(lead_waiting__status=WAITING_FREED)
# "הלקוח ענה" and "התפנה מקום" — the two that ask the office for something now (waiting_list.NEEDS_OFFICE).
_ANSWERED = _SEAT_FREE & Q(_told_at__isnull=False, last_inbound_at__gt=F('_told_at'))
_NOT_TOLD = _SEAT_FREE & Q(_told_at__isnull=True)
_TOLD = _SEAT_FREE & Q(_told_at__isnull=False)
# A condition over contacts that carry `_told_at` (_TOLD_AT — annotated, where the condition is counted).
ASKS_THE_OFFICE = _ANSWERED | _NOT_TOLD


def counts(*, apart: bool = False) -> dict:
    """
    {office, bot, closed, waiting, waiting_act} over every lead, whatever the
    list is filtered by. One aggregate. With `apart` the office's and the bot's
    numbers are without the leads of the waiting list.
    """
    return Contact.objects.annotate(_told_at=_TOLD_AT).aggregate(
        **{tab: Count('id', filter=tab_q(tab, apart=apart)) for tab in TABS},
        waiting=Count('id', filter=ON_THE_WAITING_LIST), waiting_act=Count('id', filter=ASKS_THE_OFFICE),
    )


def list_of(contact: Contact) -> str:
    """The list a lead is shown under — always one of the four, whatever the request asked for."""
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    if LEAD_STAGE_GROUPS.get(stage) == LEAD_GROUP_CLOSED:
        return TAB_CLOSED
    if stage == LEAD_WAITING_LIST:
        return TAB_WAITING
    return TAB_OFFICE if contact.lead_owner == LEAD_OWNER_OFFICE else TAB_BOT


def stages_in(tab: str, *, apart: bool = False) -> list[dict]:
    """The stages somebody on this list is in, in the board's order — what the one filter offers."""
    found = dict(Contact.objects.filter(tab_q(tab, apart=apart)).values_list('lead_stage').annotate(total=Count('id')))
    for old, new in LEGACY_LEAD_STAGES.items():
        if old in found:
            found[new] = found.get(new, 0) + found.pop(old)
    return [
        {'key': stage, 'label': label, 'count': found[stage]}
        for stage, label in LEAD_STAGE_CHOICES if found.get(stage)
    ]


def _number(raw, default: int, low: int, high: int) -> int:
    text = str(raw or '').strip()
    return max(low, min(int(text), high)) if text.isdigit() else default


def listed(params) -> dict:
    """
    GET leads/?owner=office|bot|waiting|closed|all&waiting=apart&stage=&branch=&q=&cursor= — one page.
    The office's list is a queue of work: whoever is stuck in a registration
    first, then whoever has waited longest. "מחכים שיתפנה מקום" is one too:
    whoever answered, then a seat that came free, then whoever was told, then
    whoever still waits — each the longest first. The others read newest first.

    A search (`q`, or `search`; search.py) is the one of the conversations, and
    looks in the lead's own sentences too. With one, whoever was found by name
    or phone comes first, `found` counts what was found on each of the four
    lists, and the first page says who else has that name or number: in the
    conversations without being a lead (`contacts`), and among the customers
    with no conversation (`customers`). `owner=all` is how a search crosses the lists.
    """
    tab = (params.get('owner') or TAB_OFFICE).strip()
    if tab not in ASKED_FOR:
        raise LeadError('owner: office, bot, waiting, closed או all.')
    apart = (params.get('waiting') or '').strip() == 'apart'
    # With whoever set a future lead, read in the same query: a row says whose it is.
    rows = Contact.objects.filter(tab_q(tab, apart=apart)).select_related('followup_by')

    stages = [key.strip() for key in (params.get('stage') or '').split(',') if key.strip()]
    if stages:
        rows = rows.filter(lead_stage__in=stages + [old for old, new in LEGACY_LEAD_STAGES.items() if new in stages])
    branch = (params.get('branch') or '').strip()
    if branch:
        try:
            rows = rows.filter(lead_branch_id=uuid.UUID(branch))
        except ValueError:
            rows = rows.none()
    found = search.Search(params.get('q') or params.get('search'))
    if found:
        rows = found.apply(rows, extra_text=SEARCHED_SENTENCES)
    first = ['_search_rank'] if found else []

    if tab == TAB_OFFICE:
        rows = rows.annotate(
            _first=Case(When(lead_stage__in=OFFICE_FIRST, then=Value(0)), default=Value(1), output_field=IntegerField()),
        ).order_by(*first, '_first', F('lead_stage_since').asc(nulls_last=True), 'id')
    elif tab == TAB_WAITING:
        rows = rows.alias(_told_at=_TOLD_AT).annotate(_first=Case(
            When(_ANSWERED, then=Value(0)), When(_NOT_TOLD, then=Value(1)), When(_TOLD, then=Value(2)),
            default=Value(3), output_field=IntegerField(),
        )).order_by(*first, '_first', F('lead_stage_since').asc(nulls_last=True), 'id')
    else:
        rows = rows.order_by(*first, F('lead_stage_since').desc(nulls_last=True), '-id')

    start = _number(params.get('cursor'), 0, 0, 10_000_000)
    size = _number(params.get('limit'), PAGE, 1, MAX_PAGE)
    page = list(rows[start:start + size + 1])
    more = len(page) > size
    page = page[:size]
    now = timezone.now()
    # The office's hours, laid out once: every row says how long its customer has waited for a person.
    clock = office_clock.Clock(now)
    follows = open_followups(contact.pk for contact in page)
    queues_ = waiting_queues(page)
    why = found.found_by(page) if found else None
    # Whether the bot's follow-ups will really leave: the switches and the templates, read once for the page.
    from apps.wahub import lead_automation

    gate = lead_automation.Gate(now)
    results = []
    for contact in page:
        row = row_payload(contact, now=now, follow=follows.get(contact.pk), queue=queues_, clock=clock, gate=gate)
        if why is not None:
            row['found_by'] = why.get(contact.pk)
        results.append(row)
    answer = {
        'counts': counts(apart=apart),
        'stages': stages_in(tab, apart=apart),
        'results': results,
        'next_cursor': str(start + size) if more else None,
    }
    if found and not start:
        every = found.apply(Contact.objects.filter(tab_q(TAB_ALL)), extra_text=SEARCHED_SENTENCES, rank=False)
        answer['found'] = every.aggregate(
            office=Count('id', filter=tab_q(TAB_OFFICE, apart=True)), bot=Count('id', filter=tab_q(TAB_BOT, apart=True)),
            waiting=Count('id', filter=tab_q(TAB_WAITING)), closed=Count('id', filter=tab_q(TAB_CLOSED)), total=Count('id'),
        )
        # Somebody the conversations know and the leads do not; a customer with no conversation at all.
        answer['contacts'] = serializers.found_elsewhere(found, Contact.objects.exclude(tab_q(TAB_ALL)), today=timezone.localtime(now).date())
        answer['customers'], answer['customers_more'] = found.customers()
    return answer


def waiting_queues(contacts) -> dict:
    """
    For the waiting-list rows the contacts given carry: {places: {entry id: its
    place in its lesson's queue}, lengths: {lesson id: how many wait for it}} —
    two queries, and none (an empty answer) when nobody among them waits for a seat.
    """
    lessons = set()
    for contact in contacts:
        kept = contact.lead_waiting if isinstance(contact.lead_waiting, dict) else {}
        lesson_id = waiting_list._lesson_uuid(kept.get('lesson_id')) if kept.get('entry_id') else None
        if lesson_id is not None:
            lessons.add(lesson_id)
    if not lessons:
        return {}
    places = waiting_list._positions(lessons)
    lengths: dict = {}
    for lesson_id, numbers in waiting_list._active_by_lesson(lessons).items():
        lengths[str(lesson_id)] = sum(numbers.values())
    return {'places': places, 'lengths': lengths}


def open_followups(contact_ids) -> dict:
    """{contact id: its open follow-up} for the contacts given. One query."""
    contact_ids = list(contact_ids)
    if not contact_ids:
        return {}
    return {
        row.contact_id: row
        for row in LeadFollowUp.objects.filter(contact_id__in=contact_ids, status__in=NUDGE_OPEN_STATUSES)
    }


# --- one row ---------------------------------------------------------------------------------------------

def _said(contact: Contact) -> dict:
    return contact.lead_ai if isinstance(contact.lead_ai, dict) else {}


def chips(contact: Contact) -> dict:
    """The quiet chips of a row: where, what, for whom — only what is known; '' for what is not."""
    said = _said(contact)
    result = {
        'branch': said.get('branch_name') or contact.known_branch_name or said.get('city') or contact.known_city or '',
        'course_type': contact.lead_course_type or said.get('course_type') or contact.known_course_type or '',
        'child': lead_ai.child_words(said.get('child')) or (
            (f'גיל {contact.known_child_age}' if contact.known_child_age[:1].isdigit() else contact.known_child_age)
            if contact.known_child_age else ''
        ),
    }
    if said.get('intent') == lead_ai.INTENT_ADD_CLASS:
        result['customer'] = lead_ai.INTENT_LABELS[lead_ai.INTENT_ADD_CLASS]
    if said.get('external'):
        result['external'] = lead_stage.EXTERNAL_BRANCH
    return result


def next_move(contact: Contact, follow: LeadFollowUp | None, *, gate=None, now=None) -> dict:
    """
    "הצעד הבא": who, what, when — and whether the bot's step is planned or was
    due and did not leave (`state`: planned / due / none; `note` says why).
    `hold` says whether the bot's step will really leave when its moment comes:
    '' when it will, else what stops it — the follow-ups switched off, sending
    off, no template (lead_automation.Gate) — so that a planned step is never
    shown as one that is about to go out when it is not. Always there; '' for
    every step that is not the bot's. `gate` is the one read of the switches
    and the templates for a whole list; without one it is read here.
    """
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    nothing = {'who': 'none', 'what': '', 'when': None, 'state': 'none', 'note': '', 'hold': ''}
    if not stage or LEAD_STAGE_GROUPS.get(stage) == LEAD_GROUP_CLOSED:
        return nothing
    if contact.lead_owner == LEAD_OWNER_OFFICE:
        return {**nothing, 'who': 'office', 'what': contact.lead_next_step}
    if follow is not None:
        if gate is None:
            from apps.wahub import lead_automation

            gate = lead_automation.Gate(now)
        return {
            'who': 'bot',
            'what': (
                NEXT_BY_STEP.get((follow.kind, follow.step)) or NEXT_BY_VARIANT.get(lead_followups.variant_of(follow))
                or NEXT_BY_KIND.get(follow.kind, '')
            ),
            'when': _iso(follow.due_at),
            'state': 'due' if follow.status == NUDGE_DUE else 'planned',
            'note': follow.outcome_note if follow.status == NUDGE_DUE else '',
            'hold': gate.hold(follow, contact, now or timezone.now()),
        }
    if stage == LEAD_WAITING_DATE and contact.followup_status == FOLLOWUP_LATER and contact.followup_due:
        # A day the office set: when it comes, the lead is theirs again.
        return {**nothing, 'who': 'office', 'what': 'לחזור אליו', 'when': _iso(lead_stage._noon(contact.followup_due)), 'state': 'planned'}
    return {**nothing, 'what': no_step_because(contact, stage)}


def no_step_because(contact: Contact, stage: str) -> str:
    """Why nobody has a step on this open lead right now, in the screen's words."""
    said = _said(contact)
    if optout.is_marked(contact):
        # Asked "הסר": whatever the stage, the bot starts nothing with them.
        return optout.HOLD
    if said.get('external'):
        return lead_stage.EXTERNAL_BRANCH
    if stage == LEAD_TRIAL_BOOKED:
        try:
            on = date.fromisoformat(str((contact.kogo_trial or {}).get('on') or ''))
        except ValueError:
            return 'מחכים לשיעור הניסיון'
        return f'מחכים לשיעור הניסיון ביום {WEEKDAYS[on.weekday()]}, {on.day}.{on.month}'
    if stage == LEAD_WAITING_LIST:
        return ON_A_WAITING_LIST
    if stage == LEAD_NOTHING_TO_OFFER:
        return KEPT_FOR_LATER
    if stage == LEAD_IN_CONVERSATION:
        return TALKING_NOW
    if said.get('blocker') in lead_ai.NO_NUDGE_BLOCKERS:
        return f'הבוט לא חוזר לבד: {lead_ai.BLOCKER_LABELS[said["blocker"]]}'
    round_ = contact.lead_bot if isinstance(contact.lead_bot, dict) else {}
    if contact.lead_owner == LEAD_OWNER_BOT and round_.get('done') and round_.get('attempts'):
        return WAITING_FOR_AN_ANSWER
    return NOTHING_PLANNED


def row_payload(contact: Contact, *, now=None, follow: LeadFollowUp | None = None, queue: dict | None = None,
                clock=None, gate=None) -> dict:
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    said = _said(contact)
    waits = waiting_list.contact_block(contact)
    if waits is not None and queue:
        # Where the lead stands in the queue of the lesson it waits for, and how long that queue is.
        waits['position'] = queue['places'].get(waits['entry_id'])
        waits['queue'] = queue['lengths'].get(waits['lesson_id'])
    listed_under = list_of(contact)
    # A seat came free: that is the line of the lead now, above whatever the conversation was summed up as.
    seat_free = waits is not None and waits['status'] == WAITING_FREED and stage == LEAD_WAITING_LIST
    return {
        # The contact's id under both names: `contact_id` is the contract's, `id` is what every other
        # list of the section calls it (and what the weekly audit follows into the lead itself).
        'id': contact.id,
        'contact_id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'is_demo': contact.is_demo,
        'stage': stage,
        'stage_label': STAGE_LABELS.get(stage, ''),
        'owner': contact.lead_owner,
        # The list the lead is shown under — one of four, "מחכים שיתפנה מקום" among them (the same contract, ח.3.1).
        'list': listed_under,
        'list_label': LIST_LABELS[listed_under],
        # One sentence of "why is this a lead": the summary's when it wrote one, else the rules' own.
        'summary': contact.lead_reason if seat_free else ((said.get('summary') or '').strip() or contact.lead_reason),
        'chips': chips(contact),
        'since': _iso(contact.lead_stage_since),
        'next': next_move(contact, follow, gate=gate, now=now),
        'last_message_at': _iso(contact.last_message_at),
        # The lesson the lead waits for a seat in, and the future lead a person set — the very blocks the
        # conversations carry (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג.1); null when there is none.
        'waiting_list': waits,
        'future': serializers.future_payload(contact, today=today),
        # The customer's card behind the lead (the same contract, ח.2); None for somebody the system does not know.
        'customer': search.customer_block(contact),
        # "הסר" — the customer asked not to get broadcast messages, and the bot does not come back to them
        # (the same contract, ט.1); null for everybody else.
        'opted_out': optout.payload(contact),
        # How long the customer has waited for a person, in the office's own hours — the very flag the
        # conversations carry (the same contract, ט.2): overdue, waited_office_minutes, waiting_label, waiting_from.
        **queries.waiting_block(contact, today, clock or office_clock.Clock(now)),
    }


# --- one lead in full ------------------------------------------------------------------------------------

def trial_details(contact: Contact) -> dict | None:
    """
    The trial behind a "trial" stage — class, branch, day, instructor, child,
    whether they came — read from the registrations for the one lead that is
    open. None when the lead is not about a trial. `child_id` rides along for
    the caller, which takes it out: it is the card to open, not a detail to show.
    """
    if contact.kogo_outcome not in (OUTCOME_TRIAL_ONLY, OUTCOME_TRIAL_UPCOMING):
        return None
    facts = contact.kogo_trial or {}
    came = {'attended': True, 'no_show': False}.get(facts.get('outcome'))
    trial = {
        'course': facts.get('course') or '', 'branch': '', 'date': facts.get('on') or None, 'instructor': '',
        'child': '', 'child_id': None, 'attended': came,
    }
    if contact.kogo_child_ids and facts.get('on'):
        from apps.enrollments.models import LessonEnrollment

        try:
            on = date.fromisoformat(facts['on'])
        except ValueError:
            return trial
        row = (
            LessonEnrollment.objects.filter(child_id__in=contact.kogo_child_ids)
            .filter(Q(trial_held_on=on) | Q(trial_lesson_date=on))
            .select_related('lesson__course__branch', 'lesson__instructor', 'child').order_by('-created_at').first()
        )
        if row is not None:
            instructor = row.lesson.instructor
            trial.update(
                course=row.lesson.course.name,
                branch=row.lesson.course.branch.name,
                instructor=f'{instructor.first_name} {instructor.last_name}'.strip() if instructor is not None else '',
                child=row.child.first_name,
                child_id=str(row.child_id),
            )
    return trial


def _in_system(contact: Contact) -> dict | None:
    if not contact.kogo_family_id:
        return None
    children = [str(child_id) for child_id in (contact.kogo_child_ids or [])]
    return {
        'family_id': str(contact.kogo_family_id),
        'child_ids': children,
        'outcome': contact.kogo_outcome,
        'outcome_label': OUTCOME_LABELS.get(contact.kogo_outcome, ''),
        'detail': contact.kogo_detail,
        # The customer's card, as the rest of the section links to it.
        'link': f'/customers?child={children[0]}' if children else '/customers',
    }


def details_payload(contact: Contact, *, now=None) -> dict:
    """GET leads/{contact_id}/ — the row, and under `details` everything a click opens."""
    now = now or timezone.now()
    followups = list(LeadFollowUp.objects.filter(contact=contact).order_by('-id')[:FOLLOWUPS_SHOWN])
    still_open = next((row for row in followups if row.status in NUDGE_OPEN_STATUSES), None)
    said = _said(contact)
    manual = contact.lead_manual if isinstance(contact.lead_manual, dict) else {}
    events = (
        ContactEvent.objects.filter(contact=contact, kind__in=TIMELINE_KINDS).select_related('actor')
        .order_by('-id')[:TIMELINE_EVENTS]
    )
    payload = row_payload(contact, now=now, follow=still_open)
    trial = trial_details(contact)
    waits = waiting_summary.of(contact, now=now)
    if waits is not None and payload['waiting_list'] is not None and payload['waiting_list']['entry_id'] == waits['entry_id']:
        # The place in the queue, as the summary has just counted it.
        payload['waiting_list'].update(position=waits['position'], queue=waits['queue'])
    # The card that opens from the lead: the child the lead is about, when it names one — the child of
    # the trial, the child the waiting-list row was written about.
    about = (waits or {}).get('customer') or {}
    child_id = (trial.pop('child_id', None) if trial else None) or about.get('child_id')
    if child_id:
        payload['customer'] = search.customer_block(contact, child_id=child_id)
    payload['details'] = {
        # Why: the rules' own sentence — with the customer's words when the lead was closed as not interested.
        'reason': contact.lead_reason,
        'intent': said.get('intent') or '',
        'intent_label': lead_ai.INTENT_LABELS.get(said.get('intent') or '', ''),
        'blocker': said.get('blocker') or '',
        'blocker_text': said.get('blocker_text') or '',
        'child': {**(said.get('child') if isinstance(said.get('child'), dict) else {}), 'words': lead_ai.child_words(said.get('child'))},
        'come_back_on': contact.lead_come_back_on.isoformat() if contact.lead_come_back_on else None,
        'come_back_why': said.get('come_back_why') or '',
        'link_sent_at': _iso(contact.lead_link_sent_at),
        'trial': trial,
        'followups': [lead_followups.payload(row) for row in followups],
        'timeline': [
            {
                'at': _iso(event.created_at), 'kind': event.kind, 'kind_label': EVENT_LABELS.get(event.kind, event.kind),
                'text': event.text, 'by': state.user_display_name(event.actor) if event.actor_id else None,
            }
            for event in events
        ],
        'in_system': _in_system(contact),
        'closed': manual.get('closed') if isinstance(manual.get('closed'), dict) else None,
        'taken': manual.get('owner') if isinstance(manual.get('owner'), dict) else None,
        'sending_enabled': _may_send(contact),
        # What a person wrote about the customer, and every lesson they waited for.
        'notes': [
            serializers.note_payload(event)
            for event in ContactEvent.objects.filter(contact=contact, kind=EVENT_NOTE).select_related('actor', 'edited_by').order_by('-id')[:NOTES_SHOWN]
        ],
        'waiting_list_entries': waiting_list.entries_of(contact),
        # What they wanted and where, in one sentence, and what can be offered instead now (the same contract, ח.3.2).
        'waiting_summary': waits,
    }
    return payload


def _may_send(contact: Contact) -> bool:
    from apps.wahub import sending

    return sending.sending_enabled(contact)


# --- what a person does to a lead ------------------------------------------------------------------------

def _stamp(user, now) -> dict:
    return {'at': now.isoformat(), 'by': getattr(user, 'pk', None), 'by_name': state.user_display_name(user)}


def _act(contact_id, user, change) -> Contact:
    """
    One decision of a person: `change(contact, manual, now)` edits the copy of
    `lead_manual` and returns (the fields to write beside it, the journal line).
    The contact is held while it is made, the journal says who and when, and the
    stage — and the bot's follow-up with it — follows at once.
    """
    now = timezone.now()
    with transaction.atomic():
        contact = Contact.objects.select_for_update().filter(pk=contact_id).first()
        if contact is None:
            raise Contact.DoesNotExist
        manual = dict(contact.lead_manual) if isinstance(contact.lead_manual, dict) else {}
        fields, line = change(contact, manual, now)
        state.touch(contact.pk, lead_manual=manual, **fields)
        state.log_event(contact.pk, EVENT_LEAD_ACTION, line, actor=user)
    lead_stage.refresh_quietly(contact_id)
    return Contact.objects.get(pk=contact_id)


def close(contact_id, reason: str, note: str, user) -> Contact:
    """
    "לא רלוונטי" on the leads screen: deleted, or not interested. The lead is
    out of the lists until the customer writes again — then it opens by itself.
    """
    if reason not in CLOSE_REASONS:
        raise LeadError('reason: deleted או not_interested.')
    note = ' '.join(str(note or '').split())[:300]

    def change(contact, manual, now):
        manual['closed'] = {'reason': reason, 'note': note, **_stamp(user, now)}
        # A closed lead is nobody's to take.
        manual.pop('owner', None)
        words = 'הליד נמחק מהרשימה' if reason == LEAD_CLOSE_DELETED else 'הליד נסגר: לא מעוניין'
        return {}, f'{words}: {note}' if note else words

    contact = _act(contact_id, user, change)
    # Nobody waits for a seat on behalf of a lead a person closed.
    if waiting_list.close_for_contact(contact_id, user=user):
        contact = Contact.objects.get(pk=contact_id)
    return contact


def reopen(contact_id, user) -> Contact:
    """
    A closed lead back into the lists. Whatever closed it stops closing it: the
    close of this screen, the office's "לא רלוונטי" mark (cleared, as pressing
    it off does), and the customer's own "not interested" from before this moment.
    """
    def change(contact, manual, now):
        manual.pop('closed', None)
        manual['reopened_at'] = now.isoformat()
        fields = {}
        if contact.followup_status == FOLLOWUP_NOT_RELEVANT:
            fields = dict(followup_status='', followup_due=None, followup_by=user, followup_at=now)
        return fields, 'הליד נפתח מחדש'

    return _act(contact_id, user, change)


def snooze(contact_id, until, note: str, user) -> Contact:
    """
    "דחה למועד": the office's own "בזמן אחר" with its day — one mark, on both
    screens. Until that day the lead waits and the bot does not write to it;
    on the day it is the office's ("הגיע הזמן לחזור אליו").
    """
    try:
        day = until if isinstance(until, date) else date.fromisoformat(str(until or '').strip())
    except ValueError:
        raise LeadError('until: תאריך בצורה YYYY-MM-DD.')
    if day <= state.now_israel_date():
        raise LeadError('המועד צריך להיות אחרי היום.')
    note = str(note or '').strip()[:2000]

    def change(contact, manual, now):
        manual.pop('closed', None)
        fields = dict(followup_status=FOLLOWUP_LATER, followup_due=day, followup_by=user, followup_at=now)
        if note:
            fields['followup_note'] = note
        return fields, f'נדחה ל-{day.day}.{day.month}.{day.year}' + (f': {note}' if note else '')

    return _act(contact_id, user, change)


def set_owner(contact_id, owner: str, user) -> Contact:
    """
    "קח אליי" — the lead is the office's until it is handed back; "החזר לבוט" —
    the pin comes off and the bot's round starts afresh. Where the stage itself
    asks for a person (an unanswered question, a charge that failed) the lead
    stays the office's, and the answer says so in its `owner`.
    """
    if owner not in (LEAD_OWNER_OFFICE, LEAD_OWNER_BOT):
        raise LeadError('owner: office או bot.')

    def change(contact, manual, now):
        if owner == LEAD_OWNER_OFFICE:
            manual['owner'] = {'value': LEAD_OWNER_OFFICE, **_stamp(user, now)}
            return {}, 'הליד נלקח לטיפול המשרד'
        manual.pop('owner', None)
        # A round the bot had finished is no longer finished: it comes back from the first step.
        return {'lead_bot': {}}, 'הליד הוחזר לבוט'

    return _act(contact_id, user, change)
