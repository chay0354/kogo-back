"""
What the registrations say about a phone that wrote on WhatsApp.

Reads the customers' tables and writes one of eight answers on the contact
(`kogo_*`). It never writes to a family, a parent, a child or a payment, and
never to the follow-up marks a person fills in.

The answer, first rule that fits:

    not_found         no family has this phone
    registered_after  a paying child, registered after the contact first wrote
    customer_before   a paying child, registered before that
    trial_upcoming    a trial lesson is still ahead
    trial_only        a trial took place and nobody registered
    signup_declined   tried to register and the charge failed (problem_flags)
    pending           filled in details and never paid
    in_system         a family is there, with none of the above

Phones in the customers' tables are stored as they were typed, so they are
compared digit for digit (apps/customers/phone_search.py). A walk-in the
instructor added ("רפאים") is nobody's registration and is ignored.
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
    Contact,
)
from apps.wahub.phones import normalize_phone

logger = logging.getLogger(__name__)

OUTCOME_LABELS = dict(OUTCOME_CHOICES)
PAYING_STATUSES = (STATUS_ACTIVE, STATUS_PAYMENT_PROBLEM)
TRIAL_OUTCOME_LABELS = {'attended': 'הגיע', 'no_show': 'לא הגיע', 'unmarked': 'לא סומן', '': 'לא סומן'}

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
    if not family_ids:
        return Match(OUTCOME_NOT_FOUND)

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
        return Match(OUTCOME_NOT_FOUND)

    def status_of(child) -> str:
        return canonical_status(child.status) or child.status

    children = sorted(
        (child for group in families.values() for child in group),
        key=lambda child: (CHILD_STATUS_RANK.get(status_of(child), 99), child.created_at),
    )
    child_ids = [str(child.id) for child in children]

    def answer(outcome: str, detail: str, about=None) -> Match:
        family_id = about.family_id if about is not None else next(iter(families))
        return Match(outcome, detail[:300], family_id, child_ids)

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
        .values_list('child_id', 'status', 'trial_lesson_date', 'trial_held_on', 'trial_outcome')
    )

    # 3. a trial still ahead
    ahead = sorted(
        (trial_date, child_id) for child_id, status, trial_date, _held, _outcome in trials
        if trial_date is not None and trial_date >= today and status in LIVE_ENROLLMENT_STATUSES
    )
    signed = [child for child in children if status_of(child) == STATUS_TRIAL_SIGNED]
    if ahead or signed:
        if ahead:
            trial_date, child_id = ahead[0]
            child = by_id[child_id]
            return answer(OUTCOME_TRIAL_UPCOMING, f'ניסיון ב-{_day(trial_date)} · {_names([child])}', child)
        return answer(OUTCOME_TRIAL_UPCOMING, f'רשום לשיעור ניסיון · {_names(signed)}', signed[0])

    # 4. a trial that took place, and nobody registered. A child who was once
    #    a paying student and left is not "tried and did not register".
    former = _ever_registered(list(by_id))
    past = sorted(
        (
            (held or trial_date, outcome, child_id)
            for child_id, _status, trial_date, held, outcome in trials
            if child_id not in former and (held or trial_date) is not None
            and ((held or trial_date) < today or outcome)
        ),
        key=lambda row: row[0], reverse=True,
    )
    completed = [child for child in children if status_of(child) == STATUS_TRIAL_COMPLETED]
    if past:
        when, outcome, child_id = past[0]
        child = by_id[child_id]
        return answer(
            OUTCOME_TRIAL_ONLY,
            f'ניסיון ב-{_day(when)}, {TRIAL_OUTCOME_LABELS.get(outcome, "לא סומן")} · {_names([child])}',
            child,
        )
    if completed:
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
    before = (contact.kogo_outcome, contact.kogo_detail, contact.kogo_family_id, list(contact.kogo_child_ids or []))
    after = (match.outcome, match.detail, match.family_id, match.child_ids)
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
        kogo_checked_at=now,
    )
    if contact.kogo_outcome and contact.kogo_outcome != match.outcome:
        state.log_event(
            contact.pk, EVENT_OUTCOME,
            f'{OUTCOME_LABELS.get(contact.kogo_outcome, contact.kogo_outcome)} ← '
            f'{OUTCOME_LABELS.get(match.outcome, match.outcome)}'
            + (f' ({match.detail})' if match.detail else ''),
        )
    contact.kogo_outcome, contact.kogo_detail = match.outcome, match.detail
    contact.kogo_family_id, contact.kogo_child_ids = match.family_id, match.child_ids
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
            'status_label': status_label(status),
        })
    return result
