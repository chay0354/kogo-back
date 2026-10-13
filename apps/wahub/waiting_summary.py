"""
What somebody on a waiting list wanted, said so that the office can pick up
the phone (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3.2).

The owner, 13.10.2026: "the moment one goes into the customer — a very clear
summary of what he wanted and where, because maybe one can simply call and
say 'listen, there is another day'. So I need the information."

So for the lead that is open: the lesson they asked for — class, day, hour,
branch, instructor — for whom, since when, their place in the queue, how full
the lesson is this minute, the number to call, and what could be offered
instead right now: the other hours of the same class, then other classes of
the same field and the same age group in the same branch, then in the same
city — only lessons with a free seat, by the registration form's own count
(trial_offer.Seats, as everywhere in the section).

Everything is read at the moment of asking, in a fixed number of queries
whatever the number of classes; nothing is kept, and nothing is sent.
"""
from __future__ import annotations

import re

from django.db.models import Q
from django.utils import timezone

from apps.courses import age_groups
from apps.wahub import search, state, waiting_list
from apps.wahub.models import WAITING_ACTIVE_STATUSES, WAITING_FREED, Contact, WaitingListEntry
from apps.wahub.phones import phone_display

ALTERNATIVES_SHOWN = 6
OTHERS_SHOWN = 4

SAME_COURSE, SAME_BRANCH, SAME_CITY = 'same_course', 'same_branch', 'same_city'
KIND_LABELS = {
    SAME_COURSE: 'מועד אחר של אותו חוג',
    SAME_BRANCH: 'חוג אחר באותו סניף',
    SAME_CITY: 'סניף אחר באותה עיר',
}
_TIERS = {SAME_COURSE: 0, SAME_BRANCH: 1, SAME_CITY: 2}
NOTHING_TO_OFFER = 'אין כרגע מועד אחר עם מקום — לא באותו חוג ולא בחוג דומה באותה עיר'
ROOM_NO_LIMIT = 'יש מקום · בלי מגבלה'
PLACE_WORDS = {1: 'ראשונים בתור', 2: 'שניים בתור', 3: 'שלישיים בתור'}
ALONE_IN_THE_QUEUE = 'היחידים בתור'

_AGE = re.compile(r'(?:בן|בת|גיל|בני|בנות)\s*(\d{1,2}(?:\.\d)?)')


def _iso(moment):
    return moment.isoformat() if moment else None


def _day_label(day, today) -> str:
    return f'{day.day}.{day.month}' + ('' if day.year == today.year else f'.{day.year}')


def _minutes(value) -> int:
    return value.hour * 60 + value.minute if value is not None else 0


# --- the classes the summary reads, loaded once ------------------------------------------------------

def _courses_for(lesson_ids) -> dict:
    """
    {course id: course} — the classes of the lessons asked for, and every class
    that could stand in for one of them: the same field, in the same branch or
    in another branch of ours in the same city. Loaded as the form's catalogue
    loads a class, in one go.
    """
    from apps.courses.models import Course, Lesson
    from apps.wahub import course_tools

    wanted = list(
        Lesson.objects.filter(id__in=list(lesson_ids))
        .values_list('course_id', 'course__course_type_id', 'course__branch_id', 'course__branch__city_id')
    )
    if not wanted:
        return {}
    own = Q(id__in={course_id for course_id, *_rest in wanted})
    fields = {field for _course, field, _branch, _city in wanted if field}
    branches = {branch for _course, field, branch, _city in wanted if field and branch}
    cities = {city for _course, field, _branch, city in wanted if field and city}
    alike = Q(pk__in=[])
    if fields:
        alike = (
            Q(is_active=True, branch__is_active=True, branch__is_external=False, course_type_id__in=fields)
            & (Q(branch_id__in=branches) | Q(branch__city_id__in=cities))
        )
    return {course.id: course for course in course_tools.with_lessons(Course.objects.filter(own | alike))}


def _child_codes(text, course) -> set:
    """
    The age groups to look for a stand-in in: the child's own, when what was
    written of the child names an age or a grade inside the class's range;
    otherwise the whole range of the class they asked for. Empty — the class
    has no range, and anything of the field fits.
    """
    low, high = age_groups._code(course.min_age), age_groups._code(course.max_age)
    if low is None and high is None:
        return set()
    low, high = low or 1, high or len(age_groups.AGE_GROUP_LABELS)
    whole = set(range(min(low, high), max(low, high) + 1))
    named = age_groups.codes_for_grade(text)
    if not named:
        aged = _AGE.search(str(text or ''))
        named = age_groups.codes_for_age(aged.group(1)) if aged else set()
    return (named & whole) or whole


def _city_of(course) -> str:
    place = course.branch
    return place.city.name if place.city_id else ''


def _lesson_words(lesson, course) -> dict:
    """A lesson as the summary names it — the same words for the one asked for and for a stand-in."""
    day, start = waiting_list._day_name(lesson.day_of_week), waiting_list._clock(lesson.start_time)
    return {
        'lesson_id': str(lesson.id),
        'course_id': str(course.id),
        'course': course.name,
        'day': day,
        'start': start,
        'end': waiting_list._clock(lesson.end_time),
        'branch': course.branch.name,
        'city': _city_of(course),
        'instructor': lesson.instructor.full_name if lesson.instructor_id else '',
        'ages': age_groups.range_label(course.min_age, course.max_age),
        'label': waiting_list.lesson_label(course.name, day, start, course.branch.name),
    }


def _room(cap: dict) -> str:
    left = cap['available_spots']
    if cap['capacity'] is None:
        return ROOM_NO_LIMIT
    return 'נשאר מקום אחד' if left == 1 else f'נשארו {left}'


def _alternatives(lesson, course, child: str, pool: dict, seats, taken: set) -> list:
    """
    What can be offered instead now, the nearest first: another hour of the
    same class; a class of the same field and age group in the same branch;
    the same in another branch of ours in the same city. Only a lesson that
    runs and has a seat; never one the customer already waits for.
    """
    codes = _child_codes(child, course)
    found = []

    def offer(other, its_course, kind: str, exact: bool):
        if other.id == lesson.id or other.id in taken or not waiting_list._runs(other, its_course):
            return
        cap = seats.cap(other, its_course)
        if cap['is_full']:
            return
        apart = abs(other.day_of_week - lesson.day_of_week)
        found.append((
            (_TIERS[kind], 0 if exact else 1, min(apart, 7 - apart), abs(_minutes(other.start_time) - _minutes(lesson.start_time)),
             its_course.branch.name or '', its_course.name or '', str(other.id)),
            {
                **_lesson_words(other, its_course),
                'kind': kind,
                'kind_label': KIND_LABELS[kind],
                'enrolled': cap['enrolled_count'],
                'capacity': cap['capacity'],
                'seats_left': cap['available_spots'],
                'seats_label': _room(cap),
            },
        ))

    # A class whose every day is required has no single day to offer.
    if not course.must_attend_all_lessons:
        for other in course.lessons.all():
            offer(other, course, SAME_COURSE, True)
    if course.course_type_id:
        city = course.branch.city_id
        for other_course in pool.values():
            if other_course.id == course.id or other_course.course_type_id != course.course_type_id:
                continue
            if other_course.must_attend_all_lessons or not other_course.is_active:
                continue
            if codes and not age_groups.fits(other_course.min_age, other_course.max_age, codes):
                continue
            if other_course.branch_id == course.branch_id:
                kind = SAME_BRANCH
            elif city and other_course.branch.city_id == city:
                kind = SAME_CITY
            else:
                continue
            exact = (other_course.min_age, other_course.max_age) == (course.min_age, course.max_age)
            for other in other_course.lessons.all():
                offer(other, other_course, kind, exact)
    found.sort(key=lambda row: row[0])
    return [words for _key, words in found[:ALTERNATIVES_SHOWN]]


# --- the sentence -------------------------------------------------------------------------------------

def _for_child(text: str) -> str:
    """ "לבת 5", "לנועם", "לגיל 5" — the child as the sentence says it; '' when nothing was written."""
    text = ' '.join(str(text or '').split())
    if not text:
        return ''
    return f'לגיל {text}' if text[0].isdigit() else f'ל{text}'


def _sentence(wanted: dict, child: str, since_label: str, position, *, bold: bool) -> str:
    course = wanted['course']
    asked = ' '.join(part for part in (
        'רצו',
        (f'*{course}*' if bold else course) if course else 'שיעור',
        f'ביום {wanted["day"]}' if wanted['day'] else '',
        wanted['start'],
        f'ב{wanted["branch"]}' if wanted['branch'] else '',
    ) if part)
    for_child = _for_child(child)
    if for_child:
        asked += f', {for_child}'
    waits = f'ממתינים מ-{since_label}'
    if position:
        waits += f', {PLACE_WORDS.get(position, f"מקום {position} בתור")}'
    return f'{asked} · {waits}'


def _place_label(position, queue) -> str:
    if not position:
        return ''
    if queue == 1:
        return ALONE_IN_THE_QUEUE
    words = PLACE_WORDS.get(position, f'מקום {position} בתור')
    return f'{words} · {queue} ממתינים' if queue else words


# --- one row, and the whole summary -------------------------------------------------------------------

def _one(entry: WaitingListEntry, contact: Contact, *, pool: dict, lessons: dict, seats, places: dict, lengths: dict,
         taken: set, today, names: dict) -> dict:
    copy = waiting_list.lesson_block(entry)
    lesson, course = lessons.get(entry.lesson_id, (None, None))
    runs = lesson is not None and waiting_list._runs(lesson, course)
    if runs:
        wanted = {**_lesson_words(lesson, course), 'exists': True}
        cap = seats.cap(lesson, course)
        alternatives = _alternatives(lesson, course, entry.child, pool, seats, taken)
    else:
        # The lesson is gone, or nobody can be registered to it any more: said from the copy the row keeps.
        wanted = {
            'lesson_id': copy['id'], 'course_id': copy['course_id'], 'course': copy['course'], 'day': copy['day'],
            'start': copy['start'], 'end': copy['end'], 'branch': copy['branch'], 'city': '', 'instructor': '', 'ages': '',
            'label': copy['label'], 'exists': False,
        }
        cap, alternatives = None, []
    since = timezone.localtime(entry.created_at).date()
    since_label = _day_label(since, today)
    position = places.get(entry.id)
    queue = sum(lengths.get(entry.lesson_id, {}).values()) or None
    where = waiting_list.state_of(entry.status, waiting_list.told_of_this_seat(entry), contact.last_inbound_at)
    return {
        'sentence': _sentence(wanted, entry.child, since_label, position, bold=True),
        'sentence_plain': _sentence(wanted, entry.child, since_label, position, bold=False),
        'entry_id': entry.id,
        **waiting_list.state_block(where),
        'wanted': wanted,
        'child': entry.child,
        'since': _iso(entry.created_at),
        'since_label': since_label,
        'days': max(0, (today - since).days),
        'position': position,
        'queue': queue,
        'position_label': _place_label(position, queue),
        'seats': waiting_list.seats_block(cap),
        'notified_at': _iso(entry.notified_at),
        'notified_by_name': state.user_display_name(entry.notified_by) if entry.notified_by_id else None,
        'customer_replied_at': _iso(waiting_list.replied_after(entry, contact)),
        'alternatives': alternatives,
        'alternatives_note': '' if alternatives or not runs else NOTHING_TO_OFFER,
        '_child_id': waiting_list.child_meant(entry, names),
    }


def of(contact: Contact, *, now=None) -> dict | None:
    """
    `waiting_summary` of a lead and of a conversation: the row whose seat came
    free first (else the one waiting longest) in full, the customer's number to
    call and card to open, and under `others` whatever else they wait for.
    None for a contact that waits for no lesson. Reads only.
    """
    from apps.wahub.trial_offer import Seats

    kept = contact.lead_waiting if isinstance(contact.lead_waiting, dict) else {}
    if kept.get('status') not in WAITING_ACTIVE_STATUSES:
        return None
    entries = list(
        WaitingListEntry.objects.filter(contact=contact, status__in=WAITING_ACTIVE_STATUSES)
        .select_related(*waiting_list.ROW_RELATED).order_by('id')
    )
    if not entries:
        return None
    for entry in entries:
        entry.contact = contact         # the row in hand, not one more read of it
    main = next((entry for entry in entries if entry.status == WAITING_FREED), entries[0])
    shown = [main, *[entry for entry in entries if entry is not main][:OTHERS_SHOWN]]
    lesson_ids = {entry.lesson_id for entry in shown}
    pool = _courses_for(lesson_ids)
    seats = Seats(list(pool.values()), now=now) if pool else None
    today = timezone.localtime(now or timezone.now()).date()
    common = dict(
        pool=pool, seats=seats, places=waiting_list._positions(lesson_ids), lengths=waiting_list._active_by_lesson(lesson_ids),
        # The lessons asked for, each with its class — found by the lesson, whatever the row's copy says.
        lessons={lesson.id: (lesson, course) for course in pool.values() for lesson in course.lessons.all() if lesson.id in lesson_ids},
        taken={entry.lesson_id for entry in entries}, today=today, names=waiting_list._first_names([main]),
    )
    blocks = [_one(entry, contact, **common) for entry in shown]
    child_id = blocks[0]['_child_id']
    for block in blocks:
        del block['_child_id']
    summary = blocks[0]
    summary.update({
        'phone': contact.phone,
        'phone_display': phone_display(contact.phone),
        'tel': f'tel:+{contact.phone}',
        'customer': search.customer_block(contact, child_id=child_id),
        'others': blocks[1:],
    })
    return summary
