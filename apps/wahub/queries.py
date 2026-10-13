"""
The lists and the counts of the "וואטסאפ ולידים" screens, as queries.

Every count on a screen is one aggregate over the contacts — never a loop —
because the live update asks for them every three seconds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from django.db.models import BooleanField, Case, Count, F, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.wahub import state
from apps.wahub.search import Search
from apps.wahub.models import (
    CALLBACK_COUNTS_UNDER,
    DIRECTION_IN,
    FLAG_LABELS,
    FOLLOWUP_ANSWERED,
    FOLLOWUP_LATER,
    FOLLOWUP_NO_ANSWER,
    FOLLOWUP_NOT_RELEVANT,
    FOLLOWUP_REGISTERED,
    FOLLOWUP_WAITING_US,
    HANDLED_BOT,
    HANDLED_HUMAN,
    HIDDEN_OUTCOMES,
    LEAD_GROUP_ACT,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    LEGACY_LEAD_STAGES,
    OUTCOME_IN_SYSTEM,
    OUTCOME_NOT_FOUND,
    OUTCOME_PENDING,
    OUTCOME_SIGNUP_DECLINED,
    OUTCOME_TRIAL_ONLY,
    OUTCOME_TRIAL_UPCOMING,
    STATUS_FAILED,
    Contact,
    Message,
)

VIEW_CHATS = 'chats'
VIEW_LEADS = 'leads'

BOXES = {
    'all': Q(),
    'waiting': Q(waiting_since__isnull=False),
    'needs_human': Q(needs_human=True),
    'unread': Q(unread_count__gt=0),
    'human': Q(handled_by=HANDLED_HUMAN),
    'bot': Q(handled_by=HANDLED_BOT),
}

# --- future leads, and what is "open" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג) -------------------
# A future lead is the office's own "בזמן אחר" with a day: one mark, whichever screen made it.
# Until the day it is parked — out of the list of open conversations — unless the customer
# writes first; on the day (from midnight, Israel's clock) it is open again.
FUTURE = Q(followup_status=FOLLOWUP_LATER, followup_due__isnull=False)
FUTURE_PARKED = 'parked'
FUTURE_DUE = 'due'
FUTURE_WROTE = 'wrote'
FUTURE_LABEL = 'ליד עתידי'

OPEN_NEEDS_HUMAN = 'needs_human'
OPEN_FUTURE_DUE = 'future_due'
OPEN_FUTURE_WROTE = 'future_wrote'
OPEN_HUMAN = 'human'
OPEN_LABELS = {
    OPEN_NEEDS_HUMAN: 'מבקש נציג',
    OPEN_FUTURE_DUE: 'ליד עתידי — הגיע היום',
    OPEN_FUTURE_WROTE: 'ליד עתידי — הלקוח כתב',
    OPEN_HUMAN: 'בטיפול נציג',
    '': '',
}


def _wrote_since_mark() -> Q:
    return Q(last_inbound_at__gt=F('followup_at'))


def parked_q(today) -> Q:
    """A future lead that still waits for its day, and whose customer has not written since it was set."""
    return FUTURE & Q(followup_due__gt=today) & ~_wrote_since_mark()


def open_q(today) -> Q:
    """
    "פתוחות": the conversations a person has to deal with now — the customer asks
    for one, a future lead whose day has come or whose customer wrote, or a
    conversation a person holds that is not parked for a later day.
    """
    return (
        Q(needs_human=True)
        | (FUTURE & (Q(followup_due__lte=today) | _wrote_since_mark()))
        | (Q(handled_by=HANDLED_HUMAN) & ~parked_q(today))
    )


def boxes(today=None) -> dict:
    """The boxes above the conversations, as conditions for today's date in Israel."""
    today = today or state.now_israel_date()
    parked = parked_q(today)
    return {
        **BOXES,
        'open': open_q(today),
        'future': parked,
        # A conversation a person holds — but not one put off to a day that has not come.
        'human': Q(handled_by=HANDLED_HUMAN) & ~parked,
    }


def future_state(contact: Contact, today) -> str:
    """'' for a contact that is no future lead; else parked / due / wrote — the same rule, for a contact in hand."""
    if contact.followup_status != FOLLOWUP_LATER or contact.followup_due is None:
        return ''
    if contact.followup_due <= today:
        return FUTURE_DUE
    wrote = (
        contact.last_inbound_at is not None and contact.followup_at is not None
        and contact.last_inbound_at > contact.followup_at
    )
    return FUTURE_WROTE if wrote else FUTURE_PARKED


def open_reason(contact: Contact, today) -> str:
    """Why the conversation is open for a person now; '' when it is not. The first that holds decides."""
    if contact.needs_human:
        return OPEN_NEEDS_HUMAN
    future = future_state(contact, today)
    if future == FUTURE_DUE:
        return OPEN_FUTURE_DUE
    if future == FUTURE_WROTE:
        return OPEN_FUTURE_WROTE
    if contact.handled_by == HANDLED_HUMAN and future != FUTURE_PARKED:
        return OPEN_HUMAN
    return ''

# The box a conversation is shown under when a search crosses the boxes (the same contract, ח.1.2).
BOX_BOT_LABEL = 'הבוט עונה'
BOX_WAITING_LABEL = 'ממתין למענה'


def box_of(contact: Contact, today) -> tuple:
    """(the most telling box the conversation is in, that in words) — the first that holds."""
    if contact.needs_human:
        return 'needs_human', OPEN_LABELS[OPEN_NEEDS_HUMAN]
    future = future_state(contact, today)
    if future == FUTURE_DUE:
        return 'open', OPEN_LABELS[OPEN_FUTURE_DUE]
    if future == FUTURE_WROTE:
        return 'open', OPEN_LABELS[OPEN_FUTURE_WROTE]
    if future == FUTURE_PARKED:
        return 'future', FUTURE_LABEL
    if contact.handled_by == HANDLED_HUMAN:
        return 'human', OPEN_LABELS[OPEN_HUMAN]
    if contact.waiting_since is not None:
        return 'waiting', BOX_WAITING_LABEL
    return 'all', BOX_BOT_LABEL


HIDDEN = Q(kogo_outcome__in=HIDDEN_OUTCOMES)


def due_q(today) -> Q:
    """
    "לחזור אליהם": waiting for us; "another time" whose day has come; or the
    customer said when to come back and nobody closed it since.
    """
    return (
        Q(followup_status=FOLLOWUP_WAITING_US)
        | Q(followup_status=FOLLOWUP_LATER, followup_due__lte=today)
        | Q(known_callback_on__lte=today, followup_status__in=CALLBACK_COUNTS_UNDER)
    )


def is_due(contact: Contact, today) -> bool:
    """The same rule, for a contact in hand."""
    if contact.followup_status == FOLLOWUP_WAITING_US:
        return True
    if contact.followup_status == FOLLOWUP_LATER and contact.followup_due and contact.followup_due <= today:
        return True
    return bool(
        contact.known_callback_on and contact.known_callback_on <= today
        and contact.followup_status in CALLBACK_COUNTS_UNDER
    )


def queues(today) -> dict:
    return {
        'all': Q(),
        'due': due_q(today),
        'none': Q(followup_status=''),
        'no_answer': Q(followup_status=FOLLOWUP_NO_ANSWER),
        'answered': Q(followup_status=FOLLOWUP_ANSWERED),
        'later': Q(followup_status=FOLLOWUP_LATER),
        'registered': Q(followup_status=FOLLOWUP_REGISTERED),
        'not_relevant': Q(followup_status=FOLLOWUP_NOT_RELEVANT),
    }


def listed() -> 'QuerySet[Contact]':
    """Contacts with what a row of the list shows, read in two queries."""
    return Contact.objects.select_related('followup_by').prefetch_related('tags')


def flag(value) -> bool:
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


# `common_filters` has a local of the same name (the `flag` filter of the lists).
_truthy = flag


def common_filters(queryset, params, *, found: Search | None = None, rank: bool = False):
    """
    The filters both lists and the counts share. `found` is the search of the
    request when the caller built it already (it needs it again, for the rows);
    `rank` adds `_search_rank` to what a search finds (search.py).
    """
    if found is None:
        found = Search(params.get('search'))
    if found:
        queryset = found.apply(queryset, rank=rank)

    tag = (params.get('tag') or '').strip()
    if tag:
        queryset = queryset.filter(tags__id=int(tag)) if tag.isdigit() else queryset.none()

    branch = (params.get('branch') or '').strip()
    if branch:
        try:
            queryset = queryset.filter(known_branch_id=uuid.UUID(branch))
        except ValueError:
            queryset = queryset.none()

    outcome = (params.get('outcome') or '').strip()
    if outcome:
        # "unchecked" is a contact the matching has not reached yet.
        wanted = ['' if value in ('unchecked', 'none') else value for value in outcome.split(',')]
        queryset = queryset.filter(kogo_outcome__in=wanted)

    topic = (params.get('topic') or '').strip()
    if topic:
        queryset = queryset.filter(known_topic=topic)

    interest = (params.get('interest') or '').strip()
    if interest:
        queryset = queryset.filter(known_interest=interest)

    flag = (params.get('flag') or '').strip()
    if flag:
        queryset = queryset.filter(known_flags__contains=[flag]) if flag in FLAG_LABELS else queryset.none()

    # live_test=1 — only the contacts the NEW bot answers for real (docs/WAHUB-CONTRACT-LIVE-TEST.md).
    if _truthy(params.get('live_test')):
        queryset = queryset.filter(live_test=True)

    # lead_stage=<key>[,<key>] and lead_group=act|wait|closed — the board's lists (docs/WAHUB-CONTRACT-LEAD-STAGES.md).
    stages, groups = _lead_filter(params)
    if stages:
        # A row the cron has not looked at since a stage got its present name is listed under that name.
        queryset = queryset.filter(lead_stage__in=stages + [old for old, new in LEGACY_LEAD_STAGES.items() if new in stages])
    if groups:
        queryset = queryset.filter(lead_stage__in=[stage for stage, group in LEAD_STAGE_GROUPS.items() if group in groups])
    return queryset


def _lead_filter(params) -> tuple[list, list]:
    """(the stages asked for, the groups asked for) — either may be empty."""
    def keys(name):
        return [key.strip() for key in (params.get(name) or '').split(',') if key.strip()]

    return keys('lead_stage'), keys('lead_group')


def _lead_order(stages: list, groups: list) -> list:
    """
    A stage's list is a queue of work: whoever has been in a stage that asks
    for something the longest comes first. The stages that ask for nothing
    (in motion, closed) read newest first.
    """
    asked = groups or [LEAD_STAGE_GROUPS.get(stage) for stage in stages]
    if asked and all(group == LEAD_GROUP_ACT for group in asked):
        return [F('lead_stage_since').asc(nulls_last=True), 'id']
    return [F('lead_stage_since').desc(nulls_last=True), '-id']


def contact_list(params, *, found: Search | None = None):
    """
    The list a screen asked for.

    chats — everything, newest message first, like any messaging app.
    leads — newest incoming message first, a fixed order that no mark changes;
            registered customers and trials still ahead are left out unless asked for.

    With lead_stage / lead_group it is the list behind a line of the board:
    whoever is in that stage, in either view — a stage of registered customers
    or of trials ahead is asked for by name, so nothing is left out — in the
    stage's own order (`_lead_order`).

    With a search (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.1) whoever was found by
    their own name or phone comes first, then through the family, then by a
    word in the conversation — each group in the list's own order. A search
    crosses the boxes when the screen asks for `box=all` with it.
    """
    if found is None:
        found = Search(params.get('search'))
    queryset = common_filters(listed(), params, found=found, rank=True)
    stages, groups = _lead_filter(params)
    by_stage = bool(stages or groups)
    view = (params.get('view') or VIEW_CHATS).strip()
    if view == VIEW_LEADS:
        if not by_stage and not flag(params.get('show_hidden')):
            queryset = queryset.exclude(HIDDEN)
        queue = (params.get('queue') or 'all').strip()
        queryset = queryset.filter(queues(state.now_israel_date()).get(queue, Q()))
        order = [F('last_inbound_at').desc(nulls_last=True), '-id']
    else:
        box = (params.get('box') or 'all').strip()
        queryset = queryset.filter(boxes().get(box, Q()))
        order = [F('last_message_at').desc(nulls_last=True), '-id']
    order = _lead_order(stages, groups) if by_stage else order
    return queryset.order_by(*((['_search_rank'] if found else []) + order))


def box_counts(queryset=None) -> dict:
    queryset = Contact.objects.all() if queryset is None else queryset
    # Counted under names of their own: a box called like a column ("needs_human") would hide the column
    # from the conditions of the boxes after it.
    row = queryset.aggregate(**{f'box_{name}': Count('id', filter=cond) for name, cond in boxes().items()})
    return {name[4:]: value for name, value in row.items()}


def all_counts(params) -> dict:
    """Boxes and queues in one query."""
    queryset = common_filters(Contact.objects.all(), params)
    # A stage asked for by name is never hidden — in the list (contact_list) and so in its counts.
    shown = Q() if flag(params.get('show_hidden')) or any(_lead_filter(params)) else ~HIDDEN
    wanted = {f'box_{name}': Count('id', filter=cond) for name, cond in boxes().items()}
    wanted.update({
        f'queue_{name}': Count('id', filter=cond & shown)
        for name, cond in queues(state.now_israel_date()).items()
    })
    wanted['queue_hidden'] = Count('id', filter=HIDDEN)
    row = queryset.aggregate(**wanted)
    return {
        'boxes': {name[4:]: value for name, value in row.items() if name.startswith('box_')},
        'queues': {name[6:]: value for name, value in row.items() if name.startswith('queue_')},
    }


# --- "שאלו ולא נרשמו" (docs/WAHUB-CONTRACT-STAGE3.md, א) -----------------------------------------

# Somebody who asked and did not register: nobody has the phone, somebody who
# never paid, a family that is there with nothing running (in_system: an old
# customer asking again is a lead), and a contact the matching has not reached
# yet ('' — the cron sleeps at night, and a lead who wrote at 23:00 is still a
# lead at 8:00). Only a paying child, from before or after the message, takes
# a person off the list — "מי שנרשם הוא לא ליד".
LEAD_OUTCOMES = (
    OUTCOME_NOT_FOUND, OUTCOME_PENDING, OUTCOME_SIGNUP_DECLINED, OUTCOME_TRIAL_ONLY, OUTCOME_TRIAL_UPCOMING,
    OUTCOME_IN_SYSTEM, '',
)
# "Hot": the conversation said "wants to register" (INTEREST_CHOICES 'hot' —
# 'warm' is curiosity, not a step), or the registrations show a step taken:
# a sign-up whose charge failed, a trial booked, a trial held.
HOT_INTERESTS = ('hot',)
HOT_OUTCOMES = (OUTCOME_SIGNUP_DECLINED, OUTCOME_TRIAL_UPCOMING, OUTCOME_TRIAL_ONLY)
# A person's mark that closes the lead.
CLOSED_FOLLOWUPS = (FOLLOWUP_REGISTERED, FOLLOWUP_NOT_RELEVANT)
UNREGISTERED_DAYS = 30
UNREGISTERED_MAX_DAYS = 365

HOT = Q(known_interest__in=HOT_INTERESTS) | Q(kogo_outcome__in=HOT_OUTCOMES)


def is_hot(contact: Contact) -> bool:
    """The same rule, for a contact in hand."""
    return contact.known_interest in HOT_INTERESTS or contact.kogo_outcome in HOT_OUTCOMES


def temperature(contact: Contact) -> str:
    """
    hot / warm / cold — the small dot beside a lead. Hot by the rule above;
    warm when the conversation shows interest and no step yet; cold for
    everything else: said "not now", or nothing is known.
    """
    if is_hot(contact):
        return 'hot'
    return 'warm' if contact.known_interest == 'warm' else 'cold'


def unregistered_days(raw) -> int:
    """The window a screen asked for: 30 days unless told, never more than a year, never less than a day."""
    text = str(raw or '').strip()
    if not text.isdigit():
        return UNREGISTERED_DAYS
    return max(1, min(int(text), UNREGISTERED_MAX_DAYS))


def unregistered_q(since) -> Q:
    """Wrote first within the window, the registrations say "did not register", and nobody closed it."""
    return Q(first_inbound_at__gte=since) & Q(kogo_outcome__in=LEAD_OUTCOMES) & ~Q(followup_status__in=CLOSED_FOLLOWUPS)


def days_since(moment, today) -> int | None:
    """Calendar days by the Israeli calendar: a message last night is "yesterday" at eight in the morning."""
    if moment is None:
        return None
    return max(0, (today - timezone.localtime(moment).date()).days)


def unregistered_leads(days: int, *, now=None):
    """
    Who asked in the last `days` days and did not register: the hot ones first,
    then whoever has waited longest since their last message. One query; the
    last incoming message rides along for the rows that have no summary yet.
    """
    now = now or timezone.now()
    return (
        Contact.objects.filter(unregistered_q(now - timedelta(days=days)))
        .annotate(
            hot=Case(When(HOT, then=Value(True)), default=Value(False), output_field=BooleanField()),
            last_inbound_text=Subquery(
                Message.objects.filter(contact=OuterRef('pk'), direction=DIRECTION_IN).order_by('-id').values('text')[:1]
            ),
        )
        .order_by('-hot', F('last_inbound_at').asc(nulls_last=True), 'id')
    )


def unregistered_counts(contacts, today) -> dict:
    """{total, hot, oldest_days} over rows in hand — the three numbers summary() computes in SQL."""
    waits = [days for days in (days_since(contact.last_inbound_at, today) for contact in contacts) if days is not None]
    return {
        'total': len(contacts),
        'hot': sum(1 for contact in contacts if is_hot(contact)),
        'oldest_days': max(waits) if waits else None,
    }


# The stages that ask the office for something, in the board's order.
ACT_STAGES = [(stage, label) for stage, label in LEAD_STAGE_CHOICES if LEAD_STAGE_GROUPS[stage] == LEAD_GROUP_ACT]


def summary() -> dict:
    """The "today" tab: three queries, whatever the number of contacts."""
    from apps.wahub import leads, waiting_list

    now = timezone.now()
    today = timezone.localtime(now).date()
    first_day = today - timedelta(days=6)
    zone = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(first_day, time.min), zone)
    unregistered = unregistered_q(now - timedelta(days=UNREGISTERED_DAYS))

    totals = Contact.objects.annotate(_told_at=leads._TOLD_AT).aggregate(
        waiting=Count('id', filter=BOXES['waiting']),
        needs_human=Count('id', filter=BOXES['needs_human']),
        unread=Count('id', filter=BOXES['unread']),
        due_followups=Count('id', filter=due_q(today) & ~HIDDEN),
        oldest_waiting_since=Min('waiting_since'),
        # "שאלו ולא נרשמו", the last 30 days — the same rule as leads/unregistered/.
        unregistered_total=Count('id', filter=unregistered),
        unregistered_hot=Count('id', filter=unregistered & HOT),
        unregistered_oldest_last_inbound=Min('last_inbound_at', filter=unregistered),
        # "דורש טיפול": how many contacts are in each stage that asks for something (lead_stage.py).
        **{f'lead_{stage}': Count('id', filter=Q(lead_stage=stage)) for stage, _label in ACT_STAGES},
        # "מחכים שיתפנה מקום" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3.1): the leads of the waiting list, counted
        # under their own name — how many there are, and how many of them ask for the office now.
        waiting_list_total=Count('id', filter=leads.ON_THE_WAITING_LIST),
        waiting_list_act=Count('id', filter=leads.ASKS_THE_OFFICE),
    )
    waiting = {
        'label': waiting_list.LIST_TITLE,
        'total': totals.pop('waiting_list_total'),
        'act': totals.pop('waiting_list_act'),
    }
    not_registered = {
        'total': totals.pop('unregistered_total'),
        'hot': totals.pop('unregistered_hot'),
        'oldest_days': days_since(totals.pop('unregistered_oldest_last_inbound'), today),
    }
    act = [(stage, label, totals.pop(f'lead_{stage}')) for stage, label in ACT_STAGES]
    lead_act = {
        'total': sum(count for _stage, _label, count in act),
        # Only the stages somebody is in: the card lists what there is to do.
        'stages': [{'key': stage, 'label': label, 'count': count} for stage, label, count in act if count],
    }

    days = {
        first_day + timedelta(days=offset): {'inbound': 0, 'outbound': 0, 'new_contacts': 0}
        for offset in range(7)
    }
    for row in (
        Message.objects.filter(sent_at__gte=since).exclude(status=STATUS_FAILED)
        .annotate(day=TruncDate('sent_at', tzinfo=zone)).values('day', 'direction').annotate(total=Count('id'))
    ):
        if row['day'] in days:
            days[row['day']]['inbound' if row['direction'] == DIRECTION_IN else 'outbound'] += row['total']
    for row in (
        Contact.objects.filter(created_at__gte=since)
        .annotate(day=TruncDate('created_at', tzinfo=zone)).values('day').annotate(total=Count('id'))
    ):
        if row['day'] in days:
            days[row['day']]['new_contacts'] += row['total']

    return {
        **totals,
        'waiting_list': waiting,
        'new_contacts_today': days[today]['new_contacts'],
        'new_contacts_7d': sum(day['new_contacts'] for day in days.values()),
        'inbound_today': days[today]['inbound'],
        'outbound_today': days[today]['outbound'],
        'by_day': [{'date': day.isoformat(), **counts} for day, counts in days.items()],
        'unregistered_leads': not_registered,
        'lead_act': lead_act,
    }
