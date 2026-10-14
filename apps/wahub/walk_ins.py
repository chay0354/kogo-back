"""
A child who came to a lesson without being registered, and whose phone the
instructor wrote down, is a lead (the owner, 14.10.2026: "אם הוסיפו את המספר
טלפון... הוא ייכנס לרשימת הלידים... המערכת היא תשלח לו אוטומטי... והוא ייכנס
ללופ הזה של הלידים בוואטסאפ").

The attendance screen keeps such a child as a walk-in ("רפאים",
apps/enrollments/ghost_students.py): a Child in status `ghost`, the phone on the
child and on its family, and a present mark on the day they came. Nothing here
touches any of it. Once in a cron call (`find`) the walk-ins marked present in
the last WITHIN days whose phone is an Israeli mobile are looked at, and each
such phone is made a contact matched against the registrations since the
walk-in was written down:

    no contact for the phone          when the registrations read it as a walk-in — and not, say,
                                      as a paying child the instructor did not find on the list —
                                      one is opened (`source` kogo_walk_in), with a line in its
                                      journal, matched and given its lead stage at once
    a contact matched before it       matched again, and its stage worked out again
    a contact matched since, and      nothing to do
    read as the walk-in (or as a
    customer)

"Since" is the newest of the present mark, the child and the family. A phone the
instructor adds to a walk-in days after the lesson (ghost_students.set_ghost_contact,
the same two fields) is a phone that was not there before: a contact for it is
opened then, and a contact that already had it is matched again.

What the lead then is, and what the bot sends it, is decided where it is decided
for every lead. matching.py reads a walk-in with a present mark as a lesson they
came to without registering (`kogo_trial.walk_in` — a fact beside the trials, and
like them below a paying child, a trial ahead, a newer trial, a failed charge and
a registration begun); lead_stage.py gives it the round of a trial nobody
registered after, with a first message of its own; and lead_followups.py sends
it through the doors every follow-up goes through — the owner's switch, the
follow-ups' own switch, the office hours, "הסר", and an approved template (for
somebody who never wrote there are no 24 hours: the first message is always the
template, `kogo_lead_walkin_1`).

Nothing here sends anything, and nothing outside wahub is written.
"""
from __future__ import annotations

import logging
import time

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.wahub import lead_stage, matching, state
from apps.wahub.models import CUSTOMER_OUTCOMES, EVENT_CREATED, SOURCE_WALK_IN, Contact
from apps.wahub.phones import normalize_phone

logger = logging.getLogger(__name__)

# A walk-in older than this is not looked for: a round of follow-ups no longer begins for it
# (lead_stage.START_WITHIN), and the regular match, twice a day, keeps a contact already there up to date.
WITHIN = lead_stage.START_WITHIN
# One cron call: how long the look may take, and how many phones it opens or matches.
CHECK_SECONDS = 2.0
BATCH = 30


def _name_of(last_name: str) -> str:
    """What the contact is called until the parent writes: the family, as the instructor typed it."""
    last = ' '.join(str(last_name or '').split())
    return f'משפחת {last}'[:200] if last else ''


def recent(today) -> dict:
    """
    {phone: {at, first, last, course, day}} — every phone of a walk-in marked
    present in the last WITHIN days, with the newest moment anything about it was
    written (`at`: the mark, the child, the family) and the walk-in behind that
    moment (the child's names, the class, the day of the mark).
    One query, whatever the number of walk-ins.
    """
    from apps.customers.child_status import STATUS_GHOST
    from apps.enrollments.models import LessonAttendance

    rows = (
        LessonAttendance.objects
        .filter(
            status='present', child__status=STATUS_GHOST,
            occurrence_date__gte=today - WITHIN, occurrence_date__lte=today,
        )
        .values_list(
            'created_at', 'occurrence_date', 'child__first_name', 'child__last_name', 'child__phone_number',
            'child__updated_at', 'child__family__phone', 'child__family__updated_at', 'lesson__course__name',
        )
    )
    found: dict = {}
    for marked_at, day, first, last, own_phone, child_at, family_phone, family_at, course in rows:
        moments = [moment for moment in (marked_at, child_at, family_at) if moment is not None]
        at = max(moments) if moments else None
        for phone in {normalize_phone(own_phone), normalize_phone(family_phone)} - {''}:
            kept = found.get(phone)
            if kept is None or (at is not None and (kept['at'] is None or at > kept['at'])):
                found[phone] = {
                    'at': at, 'first': (first or '').strip(), 'last': (last or '').strip(),
                    'course': (course or '').strip(), 'day': day,
                }
    return found


def _open(phone: str, walk_in: dict) -> Contact:
    """A contact for a phone that never wrote to us. Somebody who wrote in the same moment keeps theirs."""
    try:
        with transaction.atomic():
            contact = Contact.objects.create(phone=phone, name=_name_of(walk_in['last']), source=SOURCE_WALK_IN)
            child = f'{walk_in["first"]} {walk_in["last"]}'.strip() or 'ילד'
            course = f' {walk_in["course"]}' if walk_in['course'] else ''
            day = walk_in['day']
            state.log_event(
                contact.pk, EVENT_CREATED,
                f'נוסף מהנוכחות: {child} הגיע לשיעור{course} ב-{day.day}.{day.month} בלי להירשם, '
                'והמדריך רשם את הטלפון',
            )
        return contact
    except IntegrityError:
        return Contact.objects.get(phone=phone)


def find(*, now=None, deadline: float | None = None, batch: int = BATCH) -> dict:
    """
    The cron's look: every phone of a recent walk-in is a contact, matched since
    the walk-in was written down — the oldest news first. Each phone is finished
    on its own, so a call that runs out of time loses nothing.
    """
    now = now or timezone.now()
    deadline = time.monotonic() + CHECK_SECONDS if deadline is None else deadline
    counts = {'walk_ins_seen': 0, 'walk_in_contacts_opened': 0, 'walk_in_contacts_matched': 0}
    found = recent(timezone.localtime(now).date())
    counts['walk_ins_seen'] = len(found)
    if not found:
        return counts
    contacts = {contact.phone: contact for contact in Contact.objects.filter(phone__in=list(found))}
    waiting = []
    for phone, walk_in in found.items():
        contact = contacts.get(phone)
        if contact is not None and contact.kogo_checked_at is not None and (
            walk_in['at'] is None or contact.kogo_checked_at >= walk_in['at']
        ) and (matching.is_walk_in(contact) or contact.kogo_outcome in CUSTOMER_OUTCOMES):
            # Matched since, and already read as the walk-in (or as a customer, which no walk-in changes). Any
            # other answer is matched again: a phone the instructor adds afterwards (ghost_students.set_ghost_contact)
            # is saved without moving the child's or the family's `updated_at`, so the moment cannot tell.
            continue
        waiting.append((walk_in['at'] or now, phone))
    for _at, phone in sorted(waiting)[:batch]:
        if time.monotonic() >= deadline:
            break
        try:
            contact = contacts.get(phone)
            if contact is None:
                if not matching.match_phone(phone, first_message_at=now).trial.get('walk_in'):
                    # A customer, a trial ahead, a registration begun: no lead of a walk-in, and no contact opened for it.
                    continue
                contact = _open(phone, found[phone])
                counts['walk_in_contacts_opened'] += 1
            matching.recheck_contact(contact)
            counts['walk_in_contacts_matched'] += 1
        except Exception:
            logger.exception('wahub walk-ins: the walk-in at phone ending %s was not brought in', phone[-4:])
            continue
        # The stage at once, and the bot's round with it (lead_stage.refresh → lead_followups.sync).
        lead_stage.refresh_quietly(contact.pk)
    return counts
