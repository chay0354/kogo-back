"""
What the registrations say about a phone that wrote on WhatsApp.

Reads the customers' tables and writes one of eight answers on the contact
(`kogo_*`). It never writes to a family, a parent, a child or a payment, and
never to the follow-up marks a person fills in.

For the two answers about a trial the facts are kept beside the sentence
(`kogo_trial`: the day, whether the child came, the class), because the lead
stage reads them (apps/wahub/lead_stage.py) and a sentence is not a fact.

The answer, first rule that fits:

    not_found         no family has this phone
    registered_after  a paying child, registered after the contact first wrote
    customer_before   a paying child, registered before that
    trial_upcoming    a trial lesson is still ahead
    trial_only        a trial took place and nobody registered — or the child came to a
                      lesson without being registered, newer than any trial (a walk-in, below)
    signup_declined   tried to register and the charge failed (problem_flags)
    pending           filled in details and never paid
    trial_only        came to a lesson without being registered (a walk-in, below)
    in_system         a family is there, with none of the above

Phones in the customers' tables are stored as they were typed, so they are
compared digit for digit (apps/customers/phone_search.py).

A walk-in the instructor added on the attendance screen ("רפאים",
apps/enrollments/ghost_students.py) is nobody's registration: it never makes a
family a customer, never counts as one of its children, and a walk-in with no
present mark is ignored altogether. One that was marked present is a fact of its
own (the owner, 14.10.2026): the child came to a lesson without being registered
— the phone is the one the instructor typed for them. It is read beside the
trials, like one (`kogo_trial` with `walk_in`, the day of the last visit, the
first, how many): below a paying child, a trial ahead, a trial at least as new,
a failed charge and a registration begun; above "in the system". The sentence
says it in words of its own ("הגיע לשיעור … בלי להירשם · נוסף על ידי המדריך"),
and so does `outcome_label`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db.models import F, Q
from django.utils import timezone

from apps.customers.child_status import (
    CHILD_STATUS_RANK,
    LIVE_ENROLLMENT_STATUSES,
    STATUS_ACTIVE,
    STATUS_GHOST,
    STATUS_PAYMENT_PROBLEM,
    STATUS_PENDING,
    STATUS_TRIAL_COMPLETED,
    STATUS_TRIAL_SIGNED,
    canonical_status,
    status_label,
)
from apps.customers.models import Child, Family, Parent, Payment
from apps.customers.phone_search import _digits_only
from apps.enrollments.models import LessonEnrollment
from apps.wahub import state
from apps.wahub.models import (
    EVENT_OUTCOME,
    OUTCOME_CHOICES,
    OUTCOME_CUSTOMER_BEFORE,
    OUTCOME_IN_SYSTEM,
    OUTCOME_NOT_FOUND,
    OUTCOME_PENDING,
    OUTCOME_REGISTERED_AFTER,
    OUTCOME_SIGNUP_DECLINED,
    OUTCOME_TRIAL_ONLY,
    OUTCOME_TRIAL_UPCOMING,
    WALK_IN_LABEL,
    Contact,
)
from apps.wahub.phones import normalize_phone

logger = logging.getLogger(__name__)

OUTCOME_LABELS = dict(OUTCOME_CHOICES)
PAYING_STATUSES = (STATUS_ACTIVE, STATUS_PAYMENT_PROBLEM)
TRIAL_OUTCOME_LABELS = {'attended': 'הגיע', 'no_show': 'לא הגיע', 'unmarked': 'לא סומן', '': 'לא סומן'}
# Who wrote a walk-in down, said at the end of its sentence.
WALK_IN_BY = 'נוסף על ידי המדריך'

# A charge that registered a child: the registration fee, a month of a course.
# A paid trial carries trial_lesson_date and is not one.
_REGISTRATION_PAYMENT = (
    Q(lesson__isnull=False)
    | Q(bundle__isnull=False)
    | Q(description__startswith='דמי רישום')
    | Q(description__startswith='מנוי חודשי')
)


@dataclass
class Match:
    outcome: str
    detail: str = ''
    family_id: object = None
    child_ids: list = field(default_factory=list)
    # {on, outcome, course} for trial_upcoming / trial_only (and, for a trial that took place, what the system's
    # own automations have already written about it: after_test_sent, didnt_arrive_sent); empty otherwise.
    trial: dict = field(default_factory=dict)


def _trial_facts(when, outcome: str, course: str, *, asked_how=None, told_missed=None) -> dict:
    """
    The trial as the contact keeps it. "Not marked" is no outcome, whichever
    way it was stored. Two facts are kept only when they are so — what the
    system's own automations (apps/enrollments) have already written to the
    family about this very trial, so that the bot does not ask the same thing
    again (lead_stage.py): `after_test_sent`, the message that goes out two
    hours after the lesson, and `didnt_arrive_sent`, the one about not coming.
    """
    facts = {
        'on': when.isoformat() if when else '',
        'outcome': outcome if outcome in ('attended', 'no_show') else '',
        'course': (course or '').strip()[:200],
    }
    if asked_how is not None:
        facts['after_test_sent'] = True
    if told_missed is not None:
        facts['didnt_arrive_sent'] = True
    return facts


def _day(value) -> str:
    """2.10, and 2.10.2025 when it is not this year."""
    if value is None:
        return ''
    if hasattr(value, 'hour'):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    today = state.now_israel_date()
    return f'{value.day}.{value.month}' + ('' if value.year == today.year else f'.{value.year}')


def _names(children) -> str:
    return ', '.join(dict.fromkeys(f'{child.first_name} {child.last_name}'.strip() for child in children))


def family_ids_for_phone(phone: str) -> set:
    """Every family this phone belongs to: on the family, on any parent (the extra contacts too), on a child."""
    if not phone.startswith('972'):
        return set()
    local = '0' + phone[3:]
    variants = [local, phone, phone[3:]]
    found = set(
        Family.objects.annotate(_digits=_digits_only('phone')).filter(_digits__in=variants).values_list('id', flat=True)
    )
    found |= set(
        Parent.objects.annotate(_digits=_digits_only('phone')).filter(_digits__in=variants)
        .values_list('family_id', flat=True)
    )
    found |= set(
        Child.objects.exclude(status=STATUS_GHOST)
        .annotate(_digits=_digits_only('phone_number')).filter(_digits__in=variants)
        .values_list('family_id', flat=True)
    )
    return found


def family_phones(family) -> list[str]:
    """
    Every WhatsApp number a family could write from — the reverse of
    family_ids_for_phone: the card's phone, every parent's (the extra contacts
    too), every child's own (walk-ins left out, as there). Each in the spelling
    a contact is stored in (9725XXXXXXXX); a number that is not an Israeli
    mobile is nobody's WhatsApp and is dropped. Reads only.
    """
    raw = [family.phone]
    raw += Parent.objects.filter(family_id=family.id).order_by('-is_primary', 'created_at').values_list('phone', flat=True)
    raw += (
        Child.objects.filter(family_id=family.id).exclude(status=STATUS_GHOST)
        .order_by('created_at').values_list('phone_number', flat=True)
    )
    return list(dict.fromkeys(phone for phone in map(normalize_phone, raw) if phone))


def contacts_of_family(family) -> tuple[list[str], list[Contact]]:
    """(the phones looked for, the contacts found) — newest conversation first. Reads only."""
    phones = family_phones(family)
    if not phones:
        return phones, []
    contacts = list(Contact.objects.filter(phone__in=phones).order_by(F('last_message_at').desc(nulls_last=True), '-id'))
    return phones, contacts


def _phone_variants(phone: str) -> list[str]:
    """The spellings a phone in the customers' tables is compared in, digit for digit; [] for a non-Israeli number."""
    if not phone.startswith('972'):
        return []
    return ['0' + phone[3:], phone, phone[3:]]


def walk_ins_of_phone(phone: str, today=None) -> list[dict]:
    """
    Every day a walk-in behind this phone was marked present, the newest first —
    [{day, child_id, name, family_id, course}]. A walk-in is a Child in status
    `ghost`; its phone is the one the instructor typed, kept on the child and on
    its family. A mark of a day still ahead is no visit. Reads only.
    """
    variants = _phone_variants(phone)
    if not variants:
        return []
    from apps.enrollments.models import LessonAttendance

    today = today or state.now_israel_date()
    rows = (
        LessonAttendance.objects
        .filter(status='present', child__status=STATUS_GHOST, occurrence_date__isnull=False, occurrence_date__lte=today)
        .annotate(_own=_digits_only('child__phone_number'), _family=_digits_only('child__family__phone'))
        .filter(Q(_own__in=variants) | Q(_family__in=variants))
        .order_by('-occurrence_date', '-created_at')
        .values_list(
            'occurrence_date', 'child_id', 'child__first_name', 'child__last_name', 'child__family_id', 'lesson__course__name',
        )
    )
    return [
        {
            'day': day, 'child_id': child_id, 'name': f'{first or ""} {last or ""}'.strip(), 'family_id': family_id,
            'course': (course or '').strip(),
        }
        for day, child_id, first, last, family_id, course in rows
    ]


def _walk_in(visits: list[dict], also: list[str]) -> Match:
    """
    The answer for a walk-in: the newest visit names the class and the day, the
    family and the child behind it. `also` — the ids of the family's real
    children, kept after the walk-ins' own.
    """
    latest = visits[0]
    days = sorted({row['day'] for row in visits})
    where = f'לשיעור {latest["course"]}' if latest['course'] else 'לשיעור'
    if len(days) > 1:
        came = f'הגיע {len(days)} פעמים {where} (האחרונה ב-{_day(latest["day"])}) בלי להירשם'
    else:
        came = f'הגיע {where} ב-{_day(latest["day"])} בלי להירשם'
    names = ', '.join(dict.fromkeys(row['name'] for row in visits if row['name']))
    detail = ' · '.join(part for part in (came, names, WALK_IN_BY) if part)
    walk_ins = [str(child_id) for child_id in dict.fromkeys(row['child_id'] for row in visits)]
    return Match(
        OUTCOME_TRIAL_ONLY, detail[:300], latest['family_id'], list(dict.fromkeys([*walk_ins, *also])),
        {
            'on': latest['day'].isoformat(), 'outcome': 'attended', 'course': latest['course'][:200],
            'walk_in': True, 'first_on': days[0].isoformat(), 'visits': len(days),
        },
    )


def is_walk_in(contact: Contact) -> bool:
    """The matching found that the child behind this contact came to a lesson without being registered."""
    return contact.kogo_outcome == OUTCOME_TRIAL_ONLY and bool((contact.kogo_trial or {}).get('walk_in'))


def outcome_label(contact: Contact, unchecked: str = '') -> str:
    """The answer in the screens' words — a walk-in in its own, not as a trial nobody registered after."""
    if is_walk_in(contact):
        return WALK_IN_LABEL
    return OUTCOME_LABELS.get(contact.kogo_outcome, unchecked)


def _registered_at(children) -> dict:
    """
    {child id: when the child registered} for paying children.

    The first charge that registered them; with none (cash, cheques, a manual
    registration) the day their live enrolment was made; failing that, the day
    the card was opened.
    """
    ids = [child.id for child in children]
    moments: dict = {}
    for child_id, paid_at, created_at in (
        Payment.objects
        .filter(child_id__in=ids, status__in=('completed', 'refunded'), trial_lesson_date__isnull=True)
        .filter(_REGISTRATION_PAYMENT)
        .values_list('child_id', 'payment_date', 'created_at')
    ):
        moment = paid_at or created_at
        if child_id not in moments or moment < moments[child_id]:
            moments[child_id] = moment
    missing = [child_id for child_id in ids if child_id not in moments]
    if missing:
        for child_id, created_at in (
            LessonEnrollment.objects
            .filter(child_id__in=missing, status__in=LIVE_ENROLLMENT_STATUSES, trial_lesson_date__isnull=True)
            .values_list('child_id', 'created_at')
        ):
            if child_id not in moments or created_at < moments[child_id]:
                moments[child_id] = created_at
    for child in children:
        moments.setdefault(child.id, child.created_at)
    return moments


def _ever_registered(child_ids) -> set:
    return set(
        Payment.objects
        .filter(child_id__in=child_ids, status__in=('completed', 'refunded'), trial_lesson_date__isnull=True)
        .filter(_REGISTRATION_PAYMENT)
        .values_list('child_id', flat=True)
    )


def match_phone(phone: str, *, first_message_at=None) -> Match:
    """The answer for one phone. Reads only."""
    family_ids = family_ids_for_phone(phone)
    # The walk-ins behind the phone, read only when a rule gets to them (a paying child never does).
    seen: list = []

    def walk_ins() -> list[dict]:
        if not seen:
            seen.append(walk_ins_of_phone(phone))
        return seen[0]

    if not family_ids:
        # The phone of a walk-in may be on the child alone, with none on its family's card.
        return _walk_in(walk_ins(), []) if walk_ins() else Match(OUTCOME_NOT_FOUND)

    by_family: dict = {family_id: [] for family_id in family_ids}
    for child in Child.objects.filter(family_id__in=family_ids).only(
        'id', 'family_id', 'first_name', 'last_name', 'status', 'created_at',
    ):
        by_family[child.family_id].append(child)
    # A family whose only children are walk-ins is a note an instructor made.
    families = {
        family_id: [child for child in children if child.status != STATUS_GHOST]
        for family_id, children in by_family.items()
        if not children or any(child.status != STATUS_GHOST for child in children)
    }
    if not families:
        return _walk_in(walk_ins(), []) if walk_ins() else Match(OUTCOME_NOT_FOUND)

    def status_of(child) -> str:
        return canonical_status(child.status) or child.status

    children = sorted(
        (child for group in families.values() for child in group),
        key=lambda child: (CHILD_STATUS_RANK.get(status_of(child), 99), child.created_at),
    )
    child_ids = [str(child.id) for child in children]

    def answer(outcome: str, detail: str, about=None, trial=None) -> Match:
        family_id = about.family_id if about is not None else next(iter(families))
        return Match(outcome, detail[:300], family_id, child_ids, trial or {})

    # 2. somebody pays
    paying = [child for child in children if status_of(child) in PAYING_STATUSES]
    if paying:
        registered = _registered_at(paying)
        after = [
            child for child in paying
            if first_message_at is not None and registered[child.id] > first_message_at
        ]
        if after:
            first = min(after, key=lambda child: registered[child.id])
            return answer(
                OUTCOME_REGISTERED_AFTER, f'נרשם ב-{_day(registered[first.id])} · {_names(after)}', first,
            )
        first = min(paying, key=lambda child: registered[child.id])
        return answer(
            OUTCOME_CUSTOMER_BEFORE, f'לקוח רשום מ-{_day(registered[first.id])} · {_names(paying)}', first,
        )

    by_id = {child.id: child for child in children}
    today = state.now_israel_date()
    trials = list(
        LessonEnrollment.objects
        .filter(child_id__in=list(by_id))
        .filter(Q(trial_lesson_date__isnull=False) | Q(trial_held_on__isnull=False) | ~Q(trial_outcome=''))
        .values_list(
            'child_id', 'status', 'trial_lesson_date', 'trial_held_on', 'trial_outcome', 'lesson__course__name',
            'trial_followup_reminder_sent_at', 'didnt_arrive_whatsapp_sent_at',
        )
    )

    # 3. a trial still ahead
    ahead = sorted(
        (trial_date, child_id, course or '') for child_id, status, trial_date, _held, _outcome, course, *_told in trials
        if trial_date is not None and trial_date >= today and status in LIVE_ENROLLMENT_STATUSES
    )
    signed = [child for child in children if status_of(child) == STATUS_TRIAL_SIGNED]
    if ahead or signed:
        if ahead:
            trial_date, child_id, course = ahead[0]
            child = by_id[child_id]
            return answer(
                OUTCOME_TRIAL_UPCOMING, f'ניסיון ב-{_day(trial_date)} · {_names([child])}', child,
                trial=_trial_facts(trial_date, '', course),
            )
        return answer(OUTCOME_TRIAL_UPCOMING, f'רשום לשיעור ניסיון · {_names(signed)}', signed[0])

    # 4. a trial that took place, and nobody registered. A child who was once
    #    a paying student and left is not "tried and did not register".
    former = _ever_registered(list(by_id))
    past = sorted(
        (
            (held or trial_date, outcome, child_id, course, asked_how, told_missed)
            for child_id, _status, trial_date, held, outcome, course, asked_how, told_missed in trials
            if child_id not in former and (held or trial_date) is not None
            and ((held or trial_date) < today or outcome)
        ),
        key=lambda row: row[0], reverse=True,
    )
    completed = [child for child in children if status_of(child) == STATUS_TRIAL_COMPLETED]
    # A walk-in newer than every trial is the newer fact: the child came again, unregistered, since.
    # It waits below a failed charge and a registration begun (6w).
    newest_walk_in = walk_ins()[0]['day'] if walk_ins() else None
    if past and (newest_walk_in is None or past[0][0] >= newest_walk_in):
        when, outcome, child_id, course, asked_how, told_missed = past[0]
        child = by_id[child_id]
        return answer(
            OUTCOME_TRIAL_ONLY,
            f'ניסיון ב-{_day(when)}, {TRIAL_OUTCOME_LABELS.get(outcome, "לא סומן")} · {_names([child])}',
            child,
            trial=_trial_facts(when, outcome, course, asked_how=asked_how, told_missed=told_missed),
        )
    if completed and newest_walk_in is None:
        return answer(OUTCOME_TRIAL_ONLY, f'עשה שיעור ניסיון, לא סומן · {_names(completed)}', completed[0])

    # 5. tried to register and the charge failed
    declined = _signup_declined(children)
    if declined is not None:
        child, title = declined
        return answer(OUTCOME_SIGNUP_DECLINED, f'{title} · {_names([child])}', child)

    # 6. started registering and never paid
    pending = [child for child in children if status_of(child) == STATUS_PENDING]
    if pending:
        first = min(pending, key=lambda child: child.created_at)
        return answer(OUTCOME_PENDING, f'התחיל רישום ב-{_day(first.created_at)} · {_names(pending)}', first)

    # 6w. came to a lesson without being registered
    if walk_ins():
        return _walk_in(walk_ins(), child_ids)

    # 7. known, with nothing running
    if children:
        described = ', '.join(
            f'{child.first_name} {child.last_name}'.strip() + f' ({status_label(status_of(child))})'
            for child in children
        )
        return answer(OUTCOME_IN_SYSTEM, described, children[0])
    return answer(OUTCOME_IN_SYSTEM, 'משפחה במערכת בלי ילדים רשומים')


def _signup_declined(children):
    """(child, title) for the first child marked "tried to sign up and the charge failed"."""
    if not children:
        return None
    from apps.customers.problem_flags import SIGNUP_DECLINED, problems_for_children

    try:
        found = problems_for_children(children)
    except Exception:  # the mark is one rule of eight; a fault in it must not stop the rest
        logger.exception('wahub matching: could not read the sign-up problems')
        return None
    for child in children:
        for problem in found.get(child.id) or []:
            if problem.code == SIGNUP_DECLINED:
                return child, problem.title
    return None


def recheck_contact(contact: Contact) -> bool:
    """
    Match one contact again and keep the answer. True when it changed.

    Only the kogo_* fields are written, by name. A first answer is not a change
    worth a journal line; a different answer is.
    """
    match = match_phone(contact.phone, first_message_at=contact.first_inbound_at or contact.created_at)
    now = timezone.now()
    before = (
        contact.kogo_outcome, contact.kogo_detail, contact.kogo_family_id, list(contact.kogo_child_ids or []),
        dict(contact.kogo_trial or {}),
    )
    after = (match.outcome, match.detail, match.family_id, match.child_ids, match.trial)
    if before == after:
        Contact.objects.filter(pk=contact.pk).update(kogo_checked_at=now)
        contact.kogo_checked_at = now
        return False

    state.touch(
        contact.pk,
        kogo_outcome=match.outcome,
        kogo_detail=match.detail,
        kogo_family_id=match.family_id,
        kogo_child_ids=match.child_ids,
        kogo_trial=match.trial,
        kogo_checked_at=now,
    )
    if contact.kogo_outcome and contact.kogo_outcome != match.outcome:
        before_label = outcome_label(contact, contact.kogo_outcome)
        after_label = WALK_IN_LABEL if match.trial.get('walk_in') else OUTCOME_LABELS.get(match.outcome, match.outcome)
        state.log_event(
            contact.pk, EVENT_OUTCOME,
            f'{before_label} ← {after_label}' + (f' ({match.detail})' if match.detail else ''),
        )
    contact.kogo_outcome, contact.kogo_detail = match.outcome, match.detail
    contact.kogo_family_id, contact.kogo_child_ids = match.family_id, match.child_ids
    contact.kogo_trial = match.trial
    contact.kogo_checked_at = now
    return True


def children_of(contact: Contact) -> list[dict]:
    """The children behind the answer, for the open conversation."""
    ids = list(contact.kogo_child_ids or [])
    if not ids:
        return []
    rows = {
        str(child.id): child
        for child in Child.objects.filter(id__in=ids).only('id', 'first_name', 'last_name', 'status')
    }
    result = []
    for child_id in ids:
        child = rows.get(str(child_id))
        if child is None:
            continue
        status = canonical_status(child.status) or child.status
        result.append({
            'id': str(child.id),
            'name': f'{child.first_name} {child.last_name}'.strip(),
            'status': status,
            # "רפאים" is the staff's word; the bot and the conversation's card say what it means.
            'status_label': WALK_IN_LABEL if status == STATUS_GHOST else status_label(status),
        })
    return result
