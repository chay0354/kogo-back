"""
The lists and the counts of the "וואטסאפ ולידים" screens, as queries.

Every count on a screen is one aggregate over the contacts — never a loop —
because the live update asks for them every three seconds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from django.db.models import Count, F, Min, Q
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


def _flag(value) -> bool:
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
        if not _flag(params.get('show_hidden')):
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
    shown = Q() if _flag(params.get('show_hidden')) else ~HIDDEN
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


def summary() -> dict:
    """The "today" tab: three queries, whatever the number of contacts."""
    now = timezone.now()
    today = timezone.localtime(now).date()
    first_day = today - timedelta(days=6)
    zone = timezone.get_current_timezone()
    since = timezone.make_aware(datetime.combine(first_day, time.min), zone)

    totals = Contact.objects.aggregate(
        waiting=Count('id', filter=BOXES['waiting']),
        needs_human=Count('id', filter=BOXES['needs_human']),
        unread=Count('id', filter=BOXES['unread']),
        due_followups=Count('id', filter=due_q(today) & ~HIDDEN),
        oldest_waiting_since=Min('waiting_since'),
    )

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
    }
