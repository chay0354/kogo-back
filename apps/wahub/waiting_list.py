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
to the customer or to the office.

The contact carries a copy of its place on a list (`lead_waiting`), which is
what the lead stage and the lists read; `sync_contact` keeps it.
"""
from __future__ import annotations

import logging
import time
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import (
    EVENT_WAITING_LIST,
    WAITING_ACTIVE_STATUSES,
    WAITING_BY_BOT,
    WAITING_BY_PERSON,
    WAITING_FREED,
    WAITING_HANDLED,
    WAITING_REMOVED,
    WAITING_STATUS_CHOICES,
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

STATUS_LABELS = dict(WAITING_STATUS_CHOICES)
ON_THE_LIST = 'ברשימת המתנה'
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


def payload(entry: WaitingListEntry, *, position: int | None = None) -> dict:
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
    }


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
    return [payload(entry, position=places.get(entry.id)) for entry in entries]


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
        'more': len(entries) - 1,
    }


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
    ).select_related('contact', 'added_by', 'closed_by').first()
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
    return WaitingListEntry.objects.select_related('contact', 'added_by', 'closed_by').get(pk=entry_id)


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


def listed(params) -> dict:
    """GET waiting-list/?status=&lesson= — whose seat came free first, then whoever has waited longest."""
    wanted = (params.get('status') or 'active').strip()
    if wanted not in FILTERS:
        raise WaitingListError('bad_status', 'status: active, waiting, freed, closed או all.')
    rows = WaitingListEntry.objects.filter(FILTERS[wanted]).select_related('contact', 'added_by', 'closed_by')
    lesson = (params.get('lesson') or '').strip()
    if lesson:
        lesson_id = _lesson_uuid(lesson)
        rows = rows.filter(lesson_id=lesson_id) if lesson_id is not None else rows.none()
    rows = rows.annotate(
        _first=Case(When(status=WAITING_FREED, then=Value(0)), default=Value(1), output_field=IntegerField()),
    ).order_by('_first', 'id')
    return {'counts': counts(), 'results': payloads(rows[:LIST_LIMIT])}


def entries_of(contact: Contact) -> list:
    """Every row of one contact, the newest first — what the lead's details show."""
    rows = WaitingListEntry.objects.filter(contact=contact).select_related('contact', 'added_by', 'closed_by').order_by('-id')
    return payloads(rows[:ENTRIES_SHOWN])
