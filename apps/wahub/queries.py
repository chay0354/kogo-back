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

from apps.customers.phone_search import phone_query_digits
from apps.wahub import state
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


def search_q(term: str) -> Q:
    """A name, a phone however it is typed, the last message, the summary."""
    cond = Q(name__icontains=term) | Q(last_message_text__icontains=term) | Q(known_summary__icontains=term)
    digits = phone_query_digits(term)
    if digits:
        # Stored as 9725XXXXXXXX: a typed 050… is the same number.
        cond |= Q(phone__contains='972' + digits[1:] if digits.startswith('0') else digits)
    return cond


def common_filters(queryset, params):
    """The filters both lists and the counts share."""
    term = (params.get('search') or '').strip()
    if term:
        queryset = queryset.filter(search_q(term))

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
    return queryset


def contact_list(params):
    """
    The list a screen asked for.

    chats — everything, newest message first, like any messaging app.
    leads — newest incoming message first, a fixed order that no mark changes;
            registered customers and trials still ahead are left out unless asked for.
    """
    queryset = common_filters(listed(), params)
    view = (params.get('view') or VIEW_CHATS).strip()
    if view == VIEW_LEADS:
        if not flag(params.get('show_hidden')):
            queryset = queryset.exclude(HIDDEN)
        queue = (params.get('queue') or 'all').strip()
        queryset = queryset.filter(queues(state.now_israel_date()).get(queue, Q()))
        return queryset.order_by(F('last_inbound_at').desc(nulls_last=True), '-id')
    box = (params.get('box') or 'all').strip()
    queryset = queryset.filter(BOXES.get(box, Q()))
    return queryset.order_by(F('last_message_at').desc(nulls_last=True), '-id')


def box_counts(queryset=None) -> dict:
    queryset = Contact.objects.all() if queryset is None else queryset
    return queryset.aggregate(**{name: Count('id', filter=cond) for name, cond in BOXES.items()})


def all_counts(params) -> dict:
    """Boxes and queues in one query."""
    queryset = common_filters(Contact.objects.all(), params)
    shown = Q() if flag(params.get('show_hidden')) else ~HIDDEN
    wanted = {f'box_{name}': Count('id', filter=cond) for name, cond in BOXES.items()}
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


def summary() -> dict:
    """The "today" tab: three queries, whatever the number of contacts."""
    now = timezone.now()
    today = timezone.localtime(now).date()
    first_day = today - timedelta(days=6)
    zone = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(first_day, time.min), zone)
    unregistered = unregistered_q(now - timedelta(days=UNREGISTERED_DAYS))

    totals = Contact.objects.aggregate(
        waiting=Count('id', filter=BOXES['waiting']),
        needs_human=Count('id', filter=BOXES['needs_human']),
        unread=Count('id', filter=BOXES['unread']),
        due_followups=Count('id', filter=due_q(today) & ~HIDDEN),
        oldest_waiting_since=Min('waiting_since'),
        # "שאלו ולא נרשמו", the last 30 days — the same rule as leads/unregistered/.
        unregistered_total=Count('id', filter=unregistered),
        unregistered_hot=Count('id', filter=unregistered & HOT),
        unregistered_oldest_last_inbound=Min('last_inbound_at', filter=unregistered),
    )
    not_registered = {
        'total': totals.pop('unregistered_total'),
        'hot': totals.pop('unregistered_hot'),
        'oldest_days': days_since(totals.pop('unregistered_oldest_last_inbound'), today),
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
        'new_contacts_today': days[today]['new_contacts'],
        'new_contacts_7d': sum(day['new_contacts'] for day in days.values()),
        'inbound_today': days[today]['inbound'],
        'outbound_today': days[today]['outbound'],
        'by_day': [{'date': day.isoformat(), **counts} for day, counts in days.items()],
        'unregistered_leads': not_registered,
    }
