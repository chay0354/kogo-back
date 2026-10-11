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
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils import timezone

from apps.wahub import lead_ai, lead_followups, lead_stage, queries, state
from apps.wahub.models import (
    EVENT_CREATED,
    EVENT_FOLLOWUP,
    EVENT_HANDLED_BY,
    EVENT_KIND_CHOICES,
    EVENT_LEAD_ACTION,
    EVENT_LEAD_FOLLOWUP,
    EVENT_LEAD_STAGE,
    EVENT_NEEDS_HUMAN,
    EVENT_OUTCOME,
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
    Contact,
    ContactEvent,
    LeadFollowUp,
)
from apps.wahub.phones import phone_display

TAB_OFFICE = 'office'
TAB_BOT = 'bot'
TAB_CLOSED = 'closed'
TABS = (TAB_OFFICE, TAB_BOT, TAB_CLOSED)
PAGE = 50
MAX_PAGE = 100
FOLLOWUPS_SHOWN = 30
TIMELINE_EVENTS = 10

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
NOTHING_PLANNED = 'אין חזרה מתוכננת'
WAITING_FOR_AN_ANSWER = 'הבוט חזר אליו — מחכים לתשובה'
KEPT_FOR_LATER = 'נשמר: נודיע כשנפתח'
TALKING_NOW = 'בשיחה עכשיו'
WEEKDAYS = ('שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון')        # Python's weekday(): Monday is 0
# What goes into the short timeline of one lead: what happened to it, not every summary and tag.
TIMELINE_KINDS = (
    EVENT_CREATED, EVENT_LEAD_STAGE, EVENT_LEAD_FOLLOWUP, EVENT_LEAD_ACTION, EVENT_OUTCOME, EVENT_FOLLOWUP,
    EVENT_NEEDS_HUMAN, EVENT_HANDLED_BY,
)


class LeadError(ValueError):
    """An action refused, with the reason in Hebrew."""


def _iso(moment):
    return moment.isoformat() if moment else None


# --- the lists -------------------------------------------------------------------------------------------

def tab_q(tab: str) -> Q:
    """
    Whose list a lead is on. Every open lead is on one of the two: the office's
    when the next move is a person's, the bot's otherwise — also when nobody has
    a step right now (a trial ahead, nothing to offer), so that no lead is out
    of sight. A contact with no stage is no lead, and is on none.
    """
    if tab == TAB_CLOSED:
        return Q(lead_stage__in=CLOSED_STAGES)
    if tab == TAB_OFFICE:
        return Q(lead_stage__in=OPEN_STAGES, lead_owner=LEAD_OWNER_OFFICE)
    return Q(lead_stage__in=OPEN_STAGES) & ~Q(lead_owner=LEAD_OWNER_OFFICE)


def counts() -> dict:
    """{office, bot, closed} over every lead, whatever the list is filtered by. One aggregate."""
    return Contact.objects.aggregate(**{tab: Count('id', filter=tab_q(tab)) for tab in TABS})


def stages_in(tab: str) -> list[dict]:
    """The stages somebody on this list is in, in the board's order — what the one filter offers."""
    found = dict(Contact.objects.filter(tab_q(tab)).values_list('lead_stage').annotate(total=Count('id')))
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
    GET leads/?owner=office|bot|closed&stage=&branch=&q=&cursor= — one page.
    The office's list is a queue of work: whoever is stuck in a registration
    first, then whoever has waited longest. The other two read newest first.
    """
    tab = (params.get('owner') or TAB_OFFICE).strip()
    if tab not in TABS:
        raise LeadError('owner: office, bot או closed.')
    rows = Contact.objects.filter(tab_q(tab))

    stages = [key.strip() for key in (params.get('stage') or '').split(',') if key.strip()]
    if stages:
        rows = rows.filter(lead_stage__in=stages + [old for old, new in LEGACY_LEAD_STAGES.items() if new in stages])
    branch = (params.get('branch') or '').strip()
    if branch:
        try:
            rows = rows.filter(lead_branch_id=uuid.UUID(branch))
        except ValueError:
            rows = rows.none()
    term = (params.get('q') or '').strip()
    if term:
        rows = rows.filter(queries.search_q(term) | Q(lead_reason__icontains=term) | Q(lead_ai__summary__icontains=term))

    if tab == TAB_OFFICE:
        rows = rows.annotate(
            _first=Case(When(lead_stage__in=OFFICE_FIRST, then=Value(0)), default=Value(1), output_field=IntegerField()),
        ).order_by('_first', F('lead_stage_since').asc(nulls_last=True), 'id')
    else:
        rows = rows.order_by(F('lead_stage_since').desc(nulls_last=True), '-id')

    start = _number(params.get('cursor'), 0, 0, 10_000_000)
    size = _number(params.get('limit'), PAGE, 1, MAX_PAGE)
    page = list(rows[start:start + size + 1])
    more = len(page) > size
    page = page[:size]
    now = timezone.now()
    follows = open_followups(contact.pk for contact in page)
    return {
        'counts': counts(),
        'stages': stages_in(tab),
        'results': [row_payload(contact, now=now, follow=follows.get(contact.pk)) for contact in page],
        'next_cursor': str(start + size) if more else None,
    }


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


def next_move(contact: Contact, follow: LeadFollowUp | None) -> dict:
    """
    "הצעד הבא": who, what, when — and whether the bot's step is planned or was
    due and did not leave (`state`: planned / due / none; `note` says why).
    """
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    nothing = {'who': 'none', 'what': '', 'when': None, 'state': 'none', 'note': ''}
    if not stage or LEAD_STAGE_GROUPS.get(stage) == LEAD_GROUP_CLOSED:
        return nothing
    if contact.lead_owner == LEAD_OWNER_OFFICE:
        return {**nothing, 'who': 'office', 'what': contact.lead_next_step}
    if follow is not None:
        return {
            'who': 'bot',
            'what': NEXT_BY_KIND.get(follow.kind, ''),
            'when': _iso(follow.due_at),
            'state': 'due' if follow.status == NUDGE_DUE else 'planned',
            'note': follow.outcome_note if follow.status == NUDGE_DUE else '',
        }
    if stage == LEAD_WAITING_DATE and contact.followup_status == FOLLOWUP_LATER and contact.followup_due:
        # A day the office set: when it comes, the lead is theirs again.
        return {**nothing, 'who': 'office', 'what': 'לחזור אליו', 'when': _iso(lead_stage._noon(contact.followup_due)), 'state': 'planned'}
    return {**nothing, 'what': no_step_because(contact, stage)}


def no_step_because(contact: Contact, stage: str) -> str:
    """Why nobody has a step on this open lead right now, in the screen's words."""
    said = _said(contact)
    if said.get('external'):
        return lead_stage.EXTERNAL_BRANCH
    if stage == LEAD_TRIAL_BOOKED:
        try:
            on = date.fromisoformat(str((contact.kogo_trial or {}).get('on') or ''))
        except ValueError:
            return 'מחכים לשיעור הניסיון'
        return f'מחכים לשיעור הניסיון ביום {WEEKDAYS[on.weekday()]}, {on.day}.{on.month}'
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


def row_payload(contact: Contact, *, now=None, follow: LeadFollowUp | None = None) -> dict:
    stage = LEGACY_LEAD_STAGES.get(contact.lead_stage, contact.lead_stage)
    said = _said(contact)
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
        # One sentence of "why is this a lead": the summary's when it wrote one, else the rules' own.
        'summary': (said.get('summary') or '').strip() or contact.lead_reason,
        'chips': chips(contact),
        'since': _iso(contact.lead_stage_since),
        'next': next_move(contact, follow),
        'last_message_at': _iso(contact.last_message_at),
    }


# --- one lead in full ------------------------------------------------------------------------------------

def trial_details(contact: Contact) -> dict | None:
    """
    The trial behind a "trial" stage — class, branch, day, instructor, child,
    whether they came — read from the registrations for the one lead that is
    open. None when the lead is not about a trial.
    """
    if contact.kogo_outcome not in (OUTCOME_TRIAL_ONLY, OUTCOME_TRIAL_UPCOMING):
        return None
    facts = contact.kogo_trial or {}
    came = {'attended': True, 'no_show': False}.get(facts.get('outcome'))
    trial = {
        'course': facts.get('course') or '', 'branch': '', 'date': facts.get('on') or None, 'instructor': '',
        'child': '', 'attended': came,
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
        'trial': trial_details(contact),
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

    return _act(contact_id, user, change)


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
