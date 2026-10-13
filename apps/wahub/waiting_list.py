"""
The waiting list of a lesson that is full (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד).

The owner, 13.10.2026: "the bot has to put him on the waiting list on the leads
page, and say exactly which lesson he would like to register to, and when a
lesson frees up it has to alert us."

A row is one contact waiting for one lesson — the class, the day, the hour and
the branch. "Full" is the registration form's own count and nothing else
(trial_offer.Seats.cap → `is_full`): the paying children against the tightest
of the lesson's, the group's and the room's limit. A lesson with no limit is
never full, and nobody waits for it.

Who writes a row: the office (`join`, from the screen), and the bot — whose
tool only marks its draft, like `request_human`, so that a row is written when
the answer really left for a real customer (`apply_bot_marks`, called where a
live answer is sent) and never by "נסה שאלה", the bot's self-checks, a shadow
reply or a demo conversation.

The cron looks at every row on the list (`check`): a seat is free → `freed`,
a line in the contact's journal, and the lead goes to the office's list; the
seat was taken again → back to `waiting`; the child registered → `handled`;
the lesson is gone → `removed`, with the reason. Nothing here sends anything,
to the customer or to the office. Telling the customer that a seat came free
is a person's press on one row, and lives in waiting_notify.py — the only
thing about the list that sends; this module only shows what it kept
(`notified_*`).

The screen of the whole list (the contract, ז.1–ז.2) reads two more things
here: the rows grouped by lesson with the lesson's seats as they are now
(`listed` → `lessons`), and the lessons somebody can be put on the list of
(`lessons_to_choose`). Both count seats the way the form does.

The contact carries a copy of its place on a list (`lead_waiting`), which is
what the lead stage and the lists read; `sync_contact` keeps it.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, F, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.utils import timezone

from apps.wahub import search, state
from apps.wahub.models import (
    EVENT_WAITING_LIST,
    WAITING_ACTIVE_STATUSES,
    WAITING_BY_BOT,
    WAITING_BY_PERSON,
    WAITING_FREED,
    WAITING_HANDLED,
    WAITING_REMOVED,
    WAITING_STATUS_CHOICES,
    WAITING_TOLD_CHOICES,
    WAITING_WAITING,
    Contact,
    WaitingListEntry,
)
from apps.wahub.phones import phone_display

logger = logging.getLogger(__name__)

TOOL_NAME = 'join_waiting_list'

# One cron call: how long the look may take, and how many rows it reads.
CHECK_SECONDS = 2.0
CHECK_BATCH = 200
CHILD_CHARS = 200
ENTRIES_SHOWN = 20
LIST_LIMIT = 500
# The lessons a person chooses from when putting somebody on a list.
LESSONS_LIMIT = 500
# What every reader of a row's payload loads with it.
ROW_RELATED = ('contact', 'added_by', 'closed_by', 'notified_by')

STATUS_LABELS = dict(WAITING_STATUS_CHOICES)
TOLD_LABELS = dict(WAITING_TOLD_CHOICES)
ON_THE_LIST = 'ברשימת המתנה'
# The heading the leads of the waiting list stand under (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3) — the owner,
# 13.10.2026: "'מחכים לך' is too general a title… write 'מחכים שיתפנה מקום', and let it stay so."
LIST_TITLE = 'מחכים שיתפנה מקום'
# Where a row stands, as that list says it: waiting; a seat came free; the customer was told; the customer answered.
STATE_WAITING, STATE_FREED, STATE_TOLD, STATE_ANSWERED = 'waiting', 'freed', 'told', 'answered'
STATE_LABELS = {STATE_WAITING: 'ממתין', STATE_FREED: 'התפנה מקום', STATE_TOLD: 'הודענו ללקוח', STATE_ANSWERED: 'הלקוח ענה'}
# The two that ask the office for something now.
NEEDS_OFFICE = (STATE_FREED, STATE_ANSWERED)
DAY_NAMES = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']

# Why a row was not written.
HAS_ROOM = 'has_room'
NO_LESSON = 'no_lesson'
BAD_LESSON = 'bad_lesson'
HAS_ROOM_DETAIL = 'יש מקום בשיעור הזה — אפשר לרשום, לא צריך רשימת המתנה.'
NO_LESSON_DETAIL = 'השיעור לא נמצא במערכת, אינו פעיל, או שייך לסניף חיצוני.'
BAD_LESSON_DETAIL = 'מזהה השיעור אינו תקין.'

# Why a row left the list by itself.
LESSON_GONE = 'השיעור נמחק'
LESSON_OFF = 'השיעור כבר לא מתקיים'
REGISTERED = 'נרשם לשיעור'
LEAD_CLOSED = 'הליד נסגר'
TAKEN_AGAIN = 'המקום נתפס שוב'


class WaitingListError(Exception):
    """A row that was not written, with the code the screen reads and the reason in Hebrew."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _iso(moment):
    return moment.isoformat() if moment else None


def _clock(value) -> str:
    return str(value)[:5] if value is not None else ''


# --- the lesson, and whether it is full ----------------------------------------------------------------

def _lesson_uuid(raw):
    try:
        return uuid.UUID(str(raw or '').strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _courses_with(lesson_ids) -> dict:
    """{lesson id: (lesson, course)} for the lessons that are there, loaded as the form's catalogue loads a class."""
    from apps.courses.models import Course
    from apps.wahub import course_tools

    lesson_ids = list(lesson_ids)
    if not lesson_ids:
        return {}
    courses = list(course_tools.with_lessons(Course.objects.filter(lessons__id__in=lesson_ids).distinct()))
    wanted = set(lesson_ids)
    return {lesson.id: (lesson, course) for course in courses for lesson in course.lessons.all() if lesson.id in wanted}


def _runs(lesson, course) -> bool:
    """A lesson somebody can still be registered to: scheduled, in an active class of a branch of ours."""
    branch = course.branch
    return bool(lesson.status == 'scheduled' and course.is_active and branch.is_active and not branch.is_external)


def look(lesson_id, *, now=None) -> dict:
    """
    One lesson as the waiting list sees it: {found, lesson, course, cap} — `cap`
    is the form's own count of its seats. `found` is False for an id that is
    no lesson, and for a lesson nobody can be registered to any more.
    """
    from apps.wahub.trial_offer import Seats

    wanted = _lesson_uuid(lesson_id)
    if wanted is None:
        return {'found': False, 'code': BAD_LESSON}
    pair = _courses_with([wanted]).get(wanted)
    if pair is None or not _runs(*pair):
        return {'found': False, 'code': NO_LESSON}
    lesson, course = pair
    return {'found': True, 'lesson': lesson, 'course': course, 'cap': Seats([course], now=now).cap(lesson, course)}


def seats_block(cap: dict | None) -> dict:
    """
    The seats of a lesson for a screen of the office: the form's own numbers
    (`_lesson_widget_capacity`) and one short line of them. None — the lesson
    is gone, or nobody can be registered to it any more — gives empty numbers.
    """
    if cap is None:
        return {'enrolled': None, 'capacity': None, 'seats_left': None, 'is_full': False, 'seats_label': ''}
    enrolled, capacity, left = cap['enrolled_count'], cap['capacity'], cap['available_spots']
    if capacity is None:
        label = f'{enrolled} רשומים · בלי מגבלת מקומות' if enrolled else 'אין רשומים · בלי מגבלת מקומות'
    elif cap['is_full']:
        label = f'{enrolled} מתוך {capacity} · מלא'
    else:
        label = f'{enrolled} מתוך {capacity} · ' + ('נשאר מקום אחד' if left == 1 else f'נשארו {left}')
    return {'enrolled': enrolled, 'capacity': capacity, 'seats_left': left, 'is_full': bool(cap['is_full']), 'seats_label': label}


# --- the words ------------------------------------------------------------------------------------------

def _day_name(day_of_week) -> str:
    return DAY_NAMES[day_of_week] if isinstance(day_of_week, int) and 0 <= day_of_week < 7 else ''


def lesson_label(course: str, day: str, start: str, branch: str) -> str:
    """ "קפוארה גן חובה · יום שלישי · 17:00 · מינץ" — only the parts that are known."""
    return ' · '.join(part for part in (course, f'יום {day}' if day else '', start, branch) if part)


def freed_title(course: str, day: str, start: str) -> str:
    """ "התפנה מקום בקפוארה גן חובה יום שלישי 17:00" — the title of a lead whose seat came free."""
    when = ' '.join(part for part in (f'יום {day}' if day else '', start) if part)
    return ' '.join(part for part in (f'התפנה מקום ב{course}' if course else 'התפנה מקום', when) if part)


def lesson_block(entry: WaitingListEntry) -> dict:
    day, start = _day_name(entry.day_of_week), _clock(entry.start_time)
    return {
        'id': str(entry.lesson_id),
        'course_id': str(entry.course_id) if entry.course_id else None,
        'course': entry.course_name,
        'day': day,
        'start': start,
        'end': _clock(entry.end_time),
        'branch': entry.branch_name,
        'label': lesson_label(entry.course_name, day, start, entry.branch_name),
    }


def title_of(entry: WaitingListEntry) -> str:
    block = lesson_block(entry)
    if entry.status == WAITING_FREED:
        return freed_title(block['course'], block['day'], block['start'])
    return f'{ON_THE_LIST}: {block["label"]}'


def state_of(status, told_at, wrote_at) -> str:
    """waiting / freed / told / answered — `told_at` is when the customer was told of the seat that is free now."""
    if status != WAITING_FREED:
        return STATE_WAITING
    if told_at is None:
        return STATE_FREED
    return STATE_ANSWERED if wrote_at is not None and wrote_at > told_at else STATE_TOLD


def state_block(where: str) -> dict:
    return {'state': where, 'state_label': STATE_LABELS[where], 'needs_office': where in NEEDS_OFFICE}


def _moment(value):
    """A moment kept as text in the contact's copy; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _first_names(entries) -> dict:
    """{child id: first name} for the children behind the rows' contacts. One query; none when nobody is known."""
    from apps.customers.models import Child

    ids = {str(child_id) for entry in entries for child_id in (entry.contact.kogo_child_ids or [])}
    if not ids:
        return {}
    return {str(child_id): name for child_id, name in Child.objects.filter(id__in=ids).values_list('id', 'first_name')}


def child_meant(entry: WaitingListEntry, names: dict):
    """
    The child of the family a row is about: the one whose first name stands in
    what was written about the child ("נועם, בן 5"). None when no name is
    there — the caller then opens the first child the matching gave.
    """
    written = f' {search.fold(entry.child)} '
    for child_id in entry.contact.kogo_child_ids or []:
        name = search.fold(names.get(str(child_id)))
        if len(name) >= 2 and f' {name} ' in written.replace(',', ' '):
            return str(child_id)
    return None


def payload(entry: WaitingListEntry, *, position: int | None = None, names: dict | None = None) -> dict:
    """One row of the list, as docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד.4 has it. `entry.contact` is read."""
    contact = entry.contact
    return {
        'id': entry.id,
        'contact_id': entry.contact_id,
        'name': contact.name,
        'phone_display': phone_display(contact.phone),
        'is_demo': contact.is_demo,
        'status': entry.status,
        'status_label': STATUS_LABELS.get(entry.status, entry.status),
        'lesson': lesson_block(entry),
        'child': entry.child,
        'added_by': entry.added_by_kind,
        'added_by_name': state.user_display_name(entry.added_by) if entry.added_by_id else None,
        'created_at': _iso(entry.created_at),
        'freed_at': _iso(entry.freed_at),
        'closed_at': _iso(entry.closed_at),
        'closed_by_name': state.user_display_name(entry.closed_by) if entry.closed_by_id else None,
        'closed_reason': entry.closed_reason,
        'position': position,
        'title': title_of(entry),
        # The last time the customer was told a seat came free (waiting_notify.py), and whether they wrote since.
        'notified_at': _iso(entry.notified_at),
        'notified_by_name': state.user_display_name(entry.notified_by) if entry.notified_by_id else None,
        'notified_how': entry.notified_how,
        'notified_how_label': TOLD_LABELS.get(entry.notified_how, ''),
        'customer_replied_at': _iso(replied_after(entry, contact)),
        # The customer's card: the family, and the child the row is about (the same contract, ח.2).
        'customer': search.customer_block(contact, child_id=child_meant(entry, names or {})),
    }


def replied_after(entry: WaitingListEntry, contact: Contact):
    """The customer's last message, when it was written after they were told of the seat; else None."""
    told, wrote = entry.notified_at, contact.last_inbound_at
    return wrote if told is not None and wrote is not None and wrote > told else None


def _positions(lesson_ids) -> dict:
    """{entry id: its place in the queue of its lesson} over the rows still on the list — the oldest is first."""
    places: dict = {}
    counters: dict = {}
    rows = WaitingListEntry.objects.filter(lesson_id__in=list(lesson_ids), status__in=WAITING_ACTIVE_STATUSES)
    for entry_id, lesson_id in rows.order_by('id').values_list('id', 'lesson_id'):
        counters[lesson_id] = counters.get(lesson_id, 0) + 1
        places[entry_id] = counters[lesson_id]
    return places


def payloads(entries) -> list:
    entries = list(entries)
    places = _positions({entry.lesson_id for entry in entries if entry.status in WAITING_ACTIVE_STATUSES})
    names = _first_names(entries)
    return [payload(entry, position=places.get(entry.id), names=names) for entry in entries]


# --- the copy on the contact ------------------------------------------------------------------------

def _cache_of(entries: list) -> dict:
    """What the contact carries: the row whose seat came free first, else the one waiting longest."""
    if not entries:
        return {}
    freed = [entry for entry in entries if entry.status == WAITING_FREED]
    chosen = (freed or entries)[0]
    block = lesson_block(chosen)
    return {
        'status': chosen.status,
        'entry_id': chosen.id,
        'lesson_id': block['id'],
        'label': block['label'],
        'course': block['course'],
        'day': block['day'],
        'start': block['start'],
        'branch': block['branch'],
        'child': chosen.child,
        'since': _iso(chosen.created_at),
        'freed_at': _iso(chosen.freed_at),
        'notified_at': _iso(told_of_this_seat(chosen)),
        'more': len(entries) - 1,
    }


def told_of_this_seat(entry: WaitingListEntry):
    """
    When the customer was told of the seat that is free NOW; None when they were
    not — also when they were told of an earlier one that was taken again since
    (the row keeps that moment as history; the lead does not go by it).
    """
    if entry.status != WAITING_FREED or entry.notified_at is None:
        return None
    return entry.notified_at if entry.freed_at is None or entry.notified_at >= entry.freed_at else None


def sync_contact(contact_id) -> bool:
    """
    Bring the contact's copy in line with its rows, and its lead stage with the
    copy. True when the copy changed. The stage never fails the caller.
    """
    from apps.wahub import lead_stage

    entries = list(
        WaitingListEntry.objects.filter(contact_id=contact_id, status__in=WAITING_ACTIVE_STATUSES).order_by('id')
    )
    wanted = _cache_of(entries)
    have = Contact.objects.filter(pk=contact_id).values_list('lead_waiting', flat=True).first()
    if have == wanted:
        return False
    state.touch(contact_id, lead_waiting=wanted)
    lead_stage.refresh_quietly(contact_id)
    return True


def contact_block(contact: Contact) -> dict | None:
    """The `waiting_list` of a contact's and of a lead's payload, out of the copy; None when it waits for nothing."""
    kept = contact.lead_waiting if isinstance(contact.lead_waiting, dict) else {}
    if kept.get('status') not in WAITING_ACTIVE_STATUSES:
        return None
    freed = kept['status'] == WAITING_FREED
    label = kept.get('label') or ''
    return {
        'status': kept['status'],
        'status_label': STATUS_LABELS[WAITING_FREED] if freed else ON_THE_LIST,
        # Where the row stands on "מחכים שיתפנה מקום", and whether it asks the office for something now (ח.3.1).
        **state_block(state_of(kept['status'], _moment(kept.get('notified_at')), contact.last_inbound_at)),
        # The place in the lesson's queue and its length: filled where the leads are listed (leads.py), not here.
        'position': None,
        'queue': None,
        'entry_id': kept.get('entry_id'),
        'lesson_id': kept.get('lesson_id'),
        'label': label,
        'course': kept.get('course') or '',
        'day': kept.get('day') or '',
        'start': kept.get('start') or '',
        'branch': kept.get('branch') or '',
        'child': kept.get('child') or '',
        'since': kept.get('since'),
        'freed_at': kept.get('freed_at'),
        'notified_at': kept.get('notified_at') if freed else None,
        'title': (
            freed_title(kept.get('course') or '', kept.get('day') or '', kept.get('start') or '') if freed
            else f'{ON_THE_LIST}: {label}'
        ),
        'more': int(kept.get('more') or 0),
    }


def freed_reason(kept: dict) -> str:
    """The sentence of a lead whose seat came free: the title, and the branch beside it."""
    title = freed_title(kept.get('course') or '', kept.get('day') or '', kept.get('start') or '')
    branch = (kept.get('branch') or '').strip()
    return f'{title} ({branch})' if branch else title


# --- putting somebody on the list ----------------------------------------------------------------------

def _short(text, limit: int) -> str:
    return ' '.join(str(text or '').split())[:limit]


def join(contact: Contact, lesson_id, *, child: str = '', by: str = WAITING_BY_PERSON, user=None, now=None) -> tuple:
    """
    Put the contact on the waiting list of a lesson: (the row, True when it was
    made now — False when the contact was already on that list). Raises
    WaitingListError for a lesson that is not one, and for one that has room.
    """
    seen = look(lesson_id, now=now)
    if not seen['found']:
        raise WaitingListError(seen['code'], BAD_LESSON_DETAIL if seen['code'] == BAD_LESSON else NO_LESSON_DETAIL)
    lesson, course = seen['lesson'], seen['course']
    existing = WaitingListEntry.objects.filter(
        contact=contact, lesson_id=lesson.id, status__in=WAITING_ACTIVE_STATUSES,
    ).select_related(*ROW_RELATED).first()
    if existing is not None:
        return existing, False
    if not seen['cap']['is_full']:
        raise WaitingListError(HAS_ROOM, HAS_ROOM_DETAIL)
    person = user if getattr(user, 'is_authenticated', False) else None
    try:
        with transaction.atomic():
            entry = WaitingListEntry.objects.create(
                contact=contact,
                lesson_id=lesson.id,
                course_id=course.id,
                branch_id=course.branch_id,
                course_name=(course.name or '')[:200],
                branch_name=(course.branch.name or '')[:200],
                day_of_week=lesson.day_of_week,
                start_time=lesson.start_time,
                end_time=lesson.end_time,
                child=_short(child, CHILD_CHARS),
                added_by_kind=by,
                added_by=person,
            )
            who = 'הבוט הוסיף' if by == WAITING_BY_BOT else 'נוסף'
            state.log_event(
                contact.pk, EVENT_WAITING_LIST,
                f'{who} לרשימת ההמתנה: {lesson_block(entry)["label"]}' + (f' ({entry.child})' if entry.child else ''),
                actor=person,
            )
    except IntegrityError:
        # The same contact and lesson in the same moment (the bot and the office together): the first row stands.
        entry = WaitingListEntry.objects.filter(
            contact=contact, lesson_id=lesson.id, status__in=WAITING_ACTIVE_STATUSES,
        ).first()
        if entry is None:
            raise
        return entry, False
    sync_contact(contact.pk)
    return entry, True


def bot_would_join(ctx, *, lesson_id: str = '', child: str = '') -> dict:
    """
    The bot's tool (shadow_tools.join_waiting_list). It writes nothing: it
    answers whether the lesson is full, and for one that is, leaves a mark on
    the run — as `request_human` does — for whoever really sends the answer.
    """
    seen = look(lesson_id, now=getattr(ctx, 'now', None))
    if not seen['found']:
        return {
            'ok': False,
            'error': (
                'lesson_id לא תקין — לקחת אותו מ-lessons[].id של find_courses או של course_card.' if seen['code'] == BAD_LESSON
                else 'השיעור לא נמצא במערכת (או שהוא בסניף חיצוני) — אין רשימת המתנה אליו.'
            ),
        }
    from apps.wahub import course_tools

    lesson, course, cap = seen['lesson'], seen['course'], seen['cap']
    day, start = _day_name(lesson.day_of_week), _clock(lesson.start_time)
    described = {
        'id': str(lesson.id), 'course_id': str(course.id), 'course': course.name, 'day': day, 'start': start,
        'end': _clock(lesson.end_time), 'branch': course.branch.name,
        'label': lesson_label(course.name, day, start, course.branch.name),
        'seats_text': course_tools.seats_text(day, start, cap),
    }
    if not cap['is_full']:
        return {
            'ok': False, 'has_room': True, 'lesson': described,
            'note': 'יש מקום בשיעור הזה — לא מוסיפים לרשימת המתנה. מציעים להירשם, ומצטטים את seats_text.',
        }
    contact = getattr(ctx, 'contact', None)
    if contact is not None and WaitingListEntry.objects.filter(
        contact=contact, lesson_id=lesson.id, status__in=WAITING_ACTIVE_STATUSES,
    ).exists():
        return {
            'ok': True, 'already': True, 'lesson': described,
            'note': 'הלקוח כבר ברשימת ההמתנה לשיעור הזה. אומרים בקצרה שהוא ברשימה ושנעדכן כשיתפנה מקום.',
        }
    mark = {'lesson_id': str(lesson.id), 'child': _short(child, CHILD_CHARS), 'label': described['label']}
    marks = getattr(ctx, 'waiting_list', None)
    if isinstance(marks, list) and not any(row['lesson_id'] == mark['lesson_id'] for row in marks):
        marks.append(mark)
    return {
        'ok': True, 'would_add': True, 'lesson': described,
        'note': (
            'סומן (לא נשלח דבר). הרשומה נכתבת כשהתשובה נשלחת ללקוח. בתשובה: בקצרה, שהוא ברשימת ההמתנה לשיעור הזה '
            'ושנעדכן כשיתפנה מקום. לא מבטיחים מקום ולא מועד.'
        ),
    }


def apply_bot_marks(contact: Contact, tools_used) -> int:
    """
    The rows behind an answer that really left for a customer: one for every
    call of the tool in it. The lesson is looked at again at this moment — a
    seat that came free while the answer was being written makes no row. How
    many rows were made. Never raises: the answer has already left.
    """
    made = 0
    for call in tools_used or []:
        if not isinstance(call, dict) or call.get('name') != TOOL_NAME:
            continue
        given = call.get('input') if isinstance(call.get('input'), dict) else {}
        try:
            _entry, created = join(contact, given.get('lesson_id'), child=str(given.get('child') or ''), by=WAITING_BY_BOT)
        except WaitingListError as exc:
            logger.info('wahub waiting list: the bot\'s mark for contact %s made no row (%s)', contact.pk, exc.code)
            continue
        except Exception:
            logger.exception('wahub waiting list: the bot\'s mark for contact %s could not be kept', contact.pk)
            continue
        made += int(created)
    return made


# --- what the office does to a row ----------------------------------------------------------------------

def _close(entry: WaitingListEntry, status: str, *, user=None, reason: str = '', line: str = '') -> None:
    """Take one row off the list (the caller holds it, or it is the cron's own)."""
    person = user if getattr(user, 'is_authenticated', False) else None
    entry.status = status
    entry.closed_at = timezone.now()
    entry.closed_by = person
    entry.closed_reason = reason[:200]
    entry.save(update_fields=['status', 'closed_at', 'closed_by', 'closed_reason'])
    state.log_event(entry.contact_id, EVENT_WAITING_LIST, line or f'{STATUS_LABELS[status]}: {lesson_block(entry)["label"]}', actor=person)


def _act(entry_id, status: str, user, words: str) -> WaitingListEntry:
    with transaction.atomic():
        entry = WaitingListEntry.objects.select_for_update().filter(pk=entry_id).first()
        if entry is None:
            raise WaitingListEntry.DoesNotExist
        if entry.status in WAITING_ACTIVE_STATUSES:
            _close(entry, status, user=user, line=f'{words}: {lesson_block(entry)["label"]}')
    sync_contact(entry.contact_id)
    return WaitingListEntry.objects.select_related(*ROW_RELATED).get(pk=entry_id)


def mark_handled(entry_id, user) -> WaitingListEntry:
    """ "טופל": the office dealt with it. A row that is already off the list is returned as it is."""
    return _act(entry_id, WAITING_HANDLED, user, 'רשימת המתנה — טופל')


def remove(entry_id, user) -> WaitingListEntry:
    """ "להסיר מהרשימה"."""
    return _act(entry_id, WAITING_REMOVED, user, 'הוסר מרשימת ההמתנה')


def close_for_contact(contact_id, reason: str = LEAD_CLOSED, user=None) -> int:
    """Every row of a contact still on a list, taken off — its lead was closed by a person. How many."""
    closed = 0
    with transaction.atomic():
        rows = WaitingListEntry.objects.select_for_update().filter(contact_id=contact_id, status__in=WAITING_ACTIVE_STATUSES)
        for entry in rows:
            _close(entry, WAITING_REMOVED, user=user, reason=reason, line=f'הוסר מרשימת ההמתנה ({reason}): {lesson_block(entry)["label"]}')
            closed += 1
    if closed:
        sync_contact(contact_id)
    return closed


# --- the cron's look ------------------------------------------------------------------------------------

def _registered(entries: list) -> set:
    """The rows whose contact has a child registered to the very lesson now, paying. One query."""
    from apps.enrollments.enrollment_counts import paying_enrollments
    from apps.enrollments.models import LessonEnrollment

    children: dict = {}
    for entry in entries:
        for child_id in entry.contact.kogo_child_ids or []:
            children.setdefault(str(child_id), []).append(entry)
    if not children:
        return set()
    taken = set(
        paying_enrollments(
            LessonEnrollment.objects.filter(child_id__in=list(children), lesson_id__in={entry.lesson_id for entry in entries})
        ).values_list('child_id', 'lesson_id')
    )
    return {
        entry.id for child_id, lesson_id in taken for entry in children.get(str(child_id), []) if entry.lesson_id == lesson_id
    }


def check(budget_seconds: float = CHECK_SECONDS, *, now=None, batch: int = CHECK_BATCH) -> dict:
    """
    One look at the rows on the list, the ones longest unlooked-at first. The
    seats of all their lessons are counted together, the way the form counts
    them. Every row is finished and saved on its own, so a call that runs out
    of time loses nothing and the next one carries on.
    """
    from apps.wahub.trial_offer import Seats

    started = time.monotonic()
    counts = {'waiting_freed': 0, 'waiting_closed': 0, 'waiting_checked': 0}
    entries = list(
        WaitingListEntry.objects.filter(status__in=WAITING_ACTIVE_STATUSES).select_related('contact')
        .order_by(F('checked_at').asc(nulls_first=True), 'id')[:batch]
    )
    if not entries:
        return counts
    lessons = _courses_with({entry.lesson_id for entry in entries})
    courses = {course.id: course for _lesson, course in lessons.values()}
    seats = Seats(list(courses.values()), now=now) if courses else None
    registered = _registered(entries)
    touched: set = set()
    for entry in entries:
        if time.monotonic() - started > budget_seconds:
            break
        try:
            with transaction.atomic():
                row = WaitingListEntry.objects.select_for_update().filter(pk=entry.pk, status__in=WAITING_ACTIVE_STATUSES).first()
                if row is None:
                    continue
                pair = lessons.get(row.lesson_id)
                label = lesson_block(row)['label']
                if pair is None:
                    _close(row, WAITING_REMOVED, reason=LESSON_GONE, line=f'הוסר מרשימת ההמתנה ({LESSON_GONE}): {label}')
                    counts['waiting_closed'] += 1
                elif not _runs(*pair):
                    _close(row, WAITING_REMOVED, reason=LESSON_OFF, line=f'הוסר מרשימת ההמתנה ({LESSON_OFF}): {label}')
                    counts['waiting_closed'] += 1
                elif row.id in registered:
                    _close(row, WAITING_HANDLED, reason=REGISTERED, line=f'ירד מרשימת ההמתנה ({REGISTERED}): {label}')
                    counts['waiting_closed'] += 1
                else:
                    full = seats.cap(*pair)['is_full']
                    moment = timezone.now()
                    if row.status == WAITING_WAITING and not full:
                        row.status, row.freed_at, row.checked_at = WAITING_FREED, moment, moment
                        row.save(update_fields=['status', 'freed_at', 'checked_at'])
                        block = lesson_block(row)
                        state.log_event(
                            row.contact_id, EVENT_WAITING_LIST,
                            freed_reason({'course': block['course'], 'day': block['day'], 'start': block['start'], 'branch': block['branch']}),
                        )
                        counts['waiting_freed'] += 1
                    elif row.status == WAITING_FREED and full:
                        row.status, row.freed_at, row.checked_at = WAITING_WAITING, None, moment
                        row.save(update_fields=['status', 'freed_at', 'checked_at'])
                        state.log_event(row.contact_id, EVENT_WAITING_LIST, f'{TAKEN_AGAIN} — חזר לרשימת ההמתנה: {label}')
                    else:
                        WaitingListEntry.objects.filter(pk=row.pk).update(checked_at=moment)
                        counts['waiting_checked'] += 1
                        continue
                touched.add(row.contact_id)
            counts['waiting_checked'] += 1
        except Exception:
            logger.exception('wahub waiting list: row %s was not looked at', entry.pk)
            WaitingListEntry.objects.filter(pk=entry.pk).update(checked_at=timezone.now())
    for contact_id in touched:
        try:
            sync_contact(contact_id)
        except Exception:
            logger.exception('wahub waiting list: the copy on contact %s was not brought in line', contact_id)
    return counts


# --- the list ---------------------------------------------------------------------------------------------

FILTERS = {
    'active': Q(status__in=WAITING_ACTIVE_STATUSES),
    'waiting': Q(status=WAITING_WAITING),
    'freed': Q(status=WAITING_FREED),
    'closed': Q(status__in=(WAITING_HANDLED, WAITING_REMOVED)),
    'all': Q(),
}


def counts() -> dict:
    """{waiting, freed} over the whole list, whatever a screen filters by. One aggregate."""
    return WaitingListEntry.objects.aggregate(
        waiting=Count('id', filter=Q(status=WAITING_WAITING)), freed=Count('id', filter=Q(status=WAITING_FREED)),
    )


def listed(params, *, now=None, found=None) -> dict:
    """
    GET waiting-list/?status=&lesson=&search= — whose seat came free first, then
    whoever has waited longest; and the same rows grouped by lesson, with its
    seats now. A search (search.py; `found` when the caller built it) looks in
    the contact — and the family behind it — and in the row itself: the class,
    the branch, the day, the hour, what was written of the child. With one,
    every row says what it was found by, and `found` is how many there are.
    """
    wanted = (params.get('status') or 'active').strip()
    if wanted not in FILTERS:
        raise WaitingListError('bad_status', 'status: active, waiting, freed, closed או all.')
    rows = WaitingListEntry.objects.filter(FILTERS[wanted]).select_related(*ROW_RELATED)
    lesson = (params.get('lesson') or '').strip()
    if lesson:
        lesson_id = _lesson_uuid(lesson)
        rows = rows.filter(lesson_id=lesson_id) if lesson_id is not None else rows.none()
    if found is None:
        found = search.Search(params.get('search') or params.get('q'))
    if found:
        rows = rows.alias(
            _course=search.folded('course_name'), _branch=search.folded('branch_name'), _child=search.folded('child'),
            _start=Cast('start_time', CharField()),
        )
        for index, token in enumerate(found.tokens):
            rows = rows.filter(Q(contact_id__in=found.contacts_with(index)) | _row_q(token))
    rows = rows.annotate(
        _first=Case(When(status=WAITING_FREED, then=Value(0)), default=Value(1), output_field=IntegerField()),
    ).order_by('_first', 'id')
    entries = list(rows[:LIST_LIMIT])
    results = payloads(entries)
    answer = {'counts': counts(), 'lessons': lessons_of(results, now=now), 'results': results}
    if found:
        why = found.found_by([entry.contact for entry in entries])
        for entry, row in zip(entries, results):
            said = why.get(entry.contact_id)
            if said is not None and said['kind'] == 'text' and any(_row_hit(token, entry) for token in found.tokens):
                said = {'kind': 'lesson', 'label': search.LABELS['lesson'], 'value': row['lesson']['label']}
            row['found_by'] = said
        answer['found'] = len(results)
    return answer


_DAY_WORDS = {search.fold(name): index for index, name in enumerate(DAY_NAMES)}
_ANY_DAY = search.fold('יום')


def _row_q(token) -> Q:
    """Where a word is found in the row itself: the class, the branch, the day, the hour, what was written of the child."""
    if not isinstance(token, search.Word):
        return Q(pk__in=[])
    word = token.folded
    cond = Q(_course__contains=word) | Q(_branch__contains=word) | Q(_child__contains=word) | Q(_start__startswith=token.raw)
    if word in _DAY_WORDS:
        cond |= Q(day_of_week=_DAY_WORDS[word])
    if word == _ANY_DAY:
        cond |= Q(day_of_week__isnull=False)
    return cond


def _row_hit(token, entry: WaitingListEntry) -> bool:
    """The same, for a row in hand."""
    if not isinstance(token, search.Word):
        return False
    word = token.folded
    return bool(
        any(word in search.fold(text) for text in (entry.course_name, entry.branch_name, entry.child))
        or _clock(entry.start_time).startswith(token.raw)
        or _DAY_WORDS.get(word, -1) == entry.day_of_week
        or (word == _ANY_DAY and entry.day_of_week is not None)
    )


def _active_by_lesson(lesson_ids) -> dict:
    """{lesson id: {waiting, freed}} over the whole list, for the lessons given. One query."""
    found: dict = {}
    rows = (
        WaitingListEntry.objects.filter(lesson_id__in=list(lesson_ids), status__in=WAITING_ACTIVE_STATUSES)
        .values_list('lesson_id', 'status').annotate(n=Count('id'))
    )
    for lesson_id, status, n in rows:
        found.setdefault(lesson_id, {WAITING_WAITING: 0, WAITING_FREED: 0})[status] = n
    return found


def lessons_of(results: list, *, now=None) -> list:
    """
    The rows of a list grouped by lesson (the contract, ז.1): one line per
    lesson, in the order its first row has in `results` — so a lesson a seat
    came free in is first — with the lesson's seats as the form counts them
    this minute, and how many wait for it on the whole list. A lesson that is
    gone, or that nobody can be registered to any more, is shown from the copy
    its rows keep, with `exists` False and no numbers.
    """
    from apps.wahub.trial_offer import Seats

    groups: dict = {}
    for row in results:
        lesson = row['lesson']
        group = groups.setdefault(lesson['id'], {**lesson, 'entry_ids': []})
        group['entry_ids'].append(row['id'])
    if not groups:
        return []
    wanted = [uuid.UUID(lesson_id) for lesson_id in groups]
    there = {lesson_id: pair for lesson_id, pair in _courses_with(wanted).items() if _runs(*pair)}
    seats = Seats(list({course.id: course for _lesson, course in there.values()}.values()), now=now) if there else None
    active = _active_by_lesson(wanted)
    by_lesson = []
    for lesson_id, group in zip(wanted, groups.values()):
        pair = there.get(lesson_id)
        waiting = active.get(lesson_id, {})
        # In queue order: the oldest first, whatever order the rows came in.
        group['entry_ids'].sort()
        by_lesson.append({
            **group,
            'exists': pair is not None,
            **seats_block(seats.cap(*pair) if pair is not None else None),
            'waiting': waiting.get(WAITING_WAITING, 0),
            'freed': waiting.get(WAITING_FREED, 0),
        })
    return by_lesson


def _contact_id(raw) -> int | None:
    raw = str(raw or '').strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise WaitingListError('bad_contact', 'contact: מזהה איש קשר (מספר).')
    return int(raw)


def lessons_to_choose(params, *, now=None) -> dict:
    """
    GET waiting-list/lessons/?q=&branch=&full=1&contact= — the lessons a person
    chooses from when putting somebody on a waiting list (the contract, ז.2):
    every lesson `join` would take (`_runs`), by branch, class, day and hour,
    with its seats as the form counts them. `can_join` is `is_full`: the office
    waits for a seat only where the system says there is none, as the bot does.
    Reads only.
    """
    from apps.courses.models import Course
    from apps.wahub import course_tools
    from apps.wahub.trial_offer import Seats

    branch = (params.get('branch') or '').strip()
    branch_id = _lesson_uuid(branch) if branch else None
    if branch and branch_id is None:
        raise WaitingListError('bad_branch', 'branch: מזהה סניף לא תקין.')
    contact_id = _contact_id(params.get('contact'))
    words = [word.casefold() for word in str(params.get('q') or '').split()]
    only_full = str(params.get('full') or '').strip().lower() in ('1', 'true', 'yes')

    courses = list(course_tools.with_lessons(
        Course.objects.filter(is_active=True, branch__is_active=True, branch__is_external=False)
    ))
    branches = {}
    found = []
    for course in courses:
        place = course.branch
        city = place.city.name if place.city_id else ''
        for lesson in course.lessons.all():
            if not _runs(lesson, course):
                continue
            branches[place.id] = place.name
            if branch_id is not None and place.id != branch_id:
                continue
            day, start = _day_name(lesson.day_of_week), _clock(lesson.start_time)
            searched = ' '.join((course.name or '', place.name or '', city, day, f'יום {day}', start)).casefold()
            if any(word not in searched for word in words):
                continue
            found.append((lesson, course))
    found.sort(key=lambda pair: (pair[1].branch.name or '', pair[1].name or '', pair[0].day_of_week, _clock(pair[0].start_time)))

    seats = Seats(list({course.id: course for _lesson, course in found}.values()), now=now) if found else None
    lesson_ids = [lesson.id for lesson, _course in found]
    active = _active_by_lesson(lesson_ids)
    on_list = set(
        WaitingListEntry.objects.filter(
            contact_id=contact_id, lesson_id__in=lesson_ids, status__in=WAITING_ACTIVE_STATUSES,
        ).values_list('lesson_id', flat=True)
    ) if contact_id is not None else set()
    results = []
    for lesson, course in found:
        block = seats_block(seats.cap(lesson, course))
        if only_full and not block['is_full']:
            continue
        day, start = _day_name(lesson.day_of_week), _clock(lesson.start_time)
        waiting = active.get(lesson.id, {})
        results.append({
            'id': str(lesson.id),
            'course_id': str(course.id),
            'course': course.name,
            'day': day,
            'start': start,
            'end': _clock(lesson.end_time),
            'branch_id': str(course.branch_id),
            'branch': course.branch.name,
            'label': lesson_label(course.name, day, start, course.branch.name),
            **block,
            # Only a lesson the system counts as full has a waiting list — for the office as for the bot.
            'can_join': block['is_full'],
            'waiting': waiting.get(WAITING_WAITING, 0) + waiting.get(WAITING_FREED, 0),
            'on_list': lesson.id in on_list,
        })
        if len(results) >= LESSONS_LIMIT:
            break
    return {
        'branches': [{'id': str(key), 'name': name} for key, name in sorted(branches.items(), key=lambda item: item[1] or '')],
        'total': len(results),
        'results': results,
    }


def entries_of(contact: Contact) -> list:
    """Every row of one contact, the newest first — what the lead's details show."""
    rows = WaitingListEntry.objects.filter(contact=contact).select_related(*ROW_RELATED).order_by('-id')
    return payloads(rows[:ENTRIES_SHOWN])
