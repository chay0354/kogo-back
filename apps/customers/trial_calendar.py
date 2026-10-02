"""
A trial lesson as a calendar event, for the "add to calendar" button the
registration form shows once the lesson is booked.

One event is built here from the lesson itself — when, where, who teaches, what
to bring — and written twice from the same facts: as a Google Calendar link,
and as an .ics file for Apple Calendar (and every other calendar). Nothing of
the family is in it: an event is the same for whoever asks, so it can be
handed out by lesson and date alone.

How it is written, so a parent can read it at a glance:

  * the title says what and at what hour: "שיעור ניסיון בקפוארה - 16:45";
  * the notes are a few short lines, the most needed first; each opens with one
    sign, and nowhere else is there one;
  * the day and the hours are written in the notes too, not only in the event's
    own time — a note is what is read on a lock screen and in a shared invite;
  * the address stands in the location field (a calendar turns it into a map),
    and how to get in from the street stands under it in the notes;
  * the office phone closes the notes, for whatever is still unclear.

Google keeps simple markup in an event's notes (bold, a link, a line break), so
its notes are set in bold where it helps. An .ics file is plain text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from html import escape as html_escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

OFFICE_PHONE = '050-9424755'
OFFICE_NAME = 'משרד קוגומלו'
LOCAL_TIME = ZoneInfo('Asia/Jerusalem')
# How far ahead of today a lesson can be put in a calendar, and how far behind.
DAYS_AHEAD = 120
DAYS_BEHIND = 1
# The alert an .ics event carries: a reminder before the lesson starts.
REMIND_BEFORE = timedelta(hours=1)

GOOGLE_TEMPLATE = 'https://calendar.google.com/calendar/render'

_DAY_NAMES = ('ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת')


class NoSuchEvent(ValueError):
    """The lesson does not take place on that day, or the day is out of range."""


@dataclass(frozen=True)
class Line:
    """One line of the notes: a sign, the words to set in bold (if any), and the rest."""
    sign: str
    strong: str = ''
    text: str = ''
    # A second, plain line right under it (how to get in from the street).
    under: str = ''
    phone: str = ''


@dataclass(frozen=True)
class TrialEvent:
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str
    lines: list[Line] = field(default_factory=list)


# ── the facts ────────────────────────────────────────────────────────────────

def lesson_weekday(lesson) -> int:
    """The lesson's day as Python counts it (Monday is 0); the lesson counts from Sunday."""
    return (int(lesson.day_of_week) - 1) % 7


def takes_place_on(lesson, on: date) -> bool:
    if not lesson.is_recurring and lesson.lesson_date:
        return lesson.lesson_date == on
    return on.weekday() == lesson_weekday(lesson)


def duration_words(minutes: int) -> str:
    if minutes == 60:
        return 'שעה'
    if minutes == 75:
        return 'שעה ורבע'
    if minutes == 90:
        return 'שעה וחצי'
    if minutes == 120:
        return 'שעתיים'
    return f'{minutes} דקות'


def short_course_name(lesson) -> str:
    """ "קפוארה", not "קפוארה 3-4.5 בוי יום שני": the field, by its short name when it has one."""
    course = lesson.course
    course_type = getattr(course, 'course_type', None)
    if course_type is not None:
        return (course_type.short_name or course_type.name or '').strip() or course.name.strip()
    return course.name.strip()


def location_of(branch) -> str:
    """The street address and the city — what a calendar hands to a map."""
    if branch is None:
        return ''
    address = (branch.address or '').strip()
    city = (branch.city.name if branch.city_id else '').strip()
    if not address:
        return ', '.join(part for part in ((branch.name or '').strip(), city) if part)
    if city and city not in address:
        return f'{address}, {city}'
    return address


def build_trial_event(lesson, on: date, *, today: date | None = None) -> TrialEvent:
    """The event for this lesson on that day. Raises NoSuchEvent when there is none."""
    today = today or datetime.now(LOCAL_TIME).date()
    if not takes_place_on(lesson, on):
        raise NoSuchEvent('the lesson does not take place on that day')
    if not (today - timedelta(days=DAYS_BEHIND) <= on <= today + timedelta(days=DAYS_AHEAD)):
        raise NoSuchEvent('the day is out of range')

    start = datetime.combine(on, lesson.start_time, tzinfo=LOCAL_TIME)
    end = datetime.combine(on, lesson.end_time, tzinfo=LOCAL_TIME)
    if end <= start:
        raise NoSuchEvent('the lesson has no length')
    minutes = int((end - start).total_seconds() // 60)
    hours = f'{start:%H:%M}–{end:%H:%M}'
    day = f'יום {_DAY_NAMES[(on.weekday() + 1) % 7]} {on.day}.{on.month}'

    course = lesson.course
    branch = course.branch
    course_type = getattr(course, 'course_type', None)
    location = location_of(branch)

    lines = [Line(sign='🕒', strong=f'{day}, {hours}', text=f'({duration_words(minutes)})')]
    instructor = lesson.instructor.full_name if lesson.instructor_id else ''
    if instructor:
        lines.append(Line(sign='👤', text='בהדרכת', strong=instructor))
    bring = ((course_type.trial_bring_note if course_type is not None else '') or '').strip()
    if bring:
        lines.append(Line(sign='🎒', strong='להביא:', text=bring))
    if location:
        lines.append(Line(sign='📍', strong=location, under=(branch.arrival_directions or '').strip()))
    lines.append(Line(sign='📞', strong='שאלות?', text=OFFICE_NAME, phone=OFFICE_PHONE))

    return TrialEvent(
        uid=f'trial-{lesson.id}-{on:%Y%m%d}@cogomelo.co.il',
        title=f'שיעור ניסיון ב{short_course_name(lesson)} - {start:%H:%M}',
        start=start,
        end=end,
        location=location,
        lines=lines,
    )


# ── the notes ────────────────────────────────────────────────────────────────

def _words(line: Line) -> list[str]:
    """The line's words in reading order: the bold part leads, except after "בהדרכת"."""
    if line.sign == '👤':
        return [line.text, line.strong]
    return [line.strong, line.text]


def plain_notes(event: TrialEvent) -> str:
    """The notes as plain text: one short line for each fact."""
    out = []
    for line in event.lines:
        words = ' '.join(word for word in _words(line) if word)
        out.append(' '.join(part for part in (line.sign, words, line.phone) if part))
        if line.under:
            out.append(line.under)
    return '\n'.join(out)


def _as_text(value: str) -> str:
    """Markup in a name is written as text. Quotes are left alone: "גראנג'י" is a name, not an attribute."""
    return html_escape(value, quote=False)


def google_notes(event: TrialEvent) -> str:
    """The same notes with the markup Google keeps: bold, a line break, a phone link."""
    out = []
    for line in event.lines:
        words = []
        for word in _words(line):
            if not word:
                continue
            words.append(f'<b>{_as_text(word)}</b>' if word == line.strong else _as_text(word))
        if line.phone:
            digits = ''.join(ch for ch in line.phone if ch.isdigit())
            words.append(f'<a href="tel:{digits}">{_as_text(line.phone)}</a>')
        out.append(f'{line.sign} {" ".join(words)}')
        if line.under:
            out.append(_as_text(line.under))
    return '<br>'.join(out)


# ── Google ───────────────────────────────────────────────────────────────────

def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def google_url(event: TrialEvent) -> str:
    """
    The link that opens Google Calendar with the event filled in.

    Both ends of `dates` are in UTC and end in Z — Google reads the pair as UTC
    only then — and `ctz` shows it to the parent in Israel time wherever the
    phone thinks it is.
    """
    return f'{GOOGLE_TEMPLATE}?' + urlencode({
        'action': 'TEMPLATE',
        'text': event.title,
        'dates': f'{_utc_stamp(event.start)}/{_utc_stamp(event.end)}',
        'ctz': 'Asia/Jerusalem',
        'details': google_notes(event),
        'location': event.location,
    })


# ── the .ics file ────────────────────────────────────────────────────────────

def _ics_text(value: str) -> str:
    """Escaped as RFC 5545 asks of a text value: backslash, semicolon, comma, new line."""
    return (
        value.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,')
        .replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n')
    )


def _folded(line: str) -> list[str]:
    """
    A content line cut to 75 octets a row, as the standard asks; the rows after
    the first open with a space. Cut between characters, never inside one: a
    Hebrew letter is two octets, and half of one is a broken file.
    """
    rows, row, size = [], '', 0
    limit = 75
    for char in line:
        octets = len(char.encode('utf-8'))
        if size + octets > limit:
            # An escape ("\\n", "\\,") is kept on one row: the standard lets a
            # line be cut anywhere, but not every calendar reads it back right.
            carried = ''
            if row.endswith('\\') and (len(row) - len(row.rstrip('\\'))) % 2 == 1:
                row, carried = row[:-1], '\\'
            rows.append(row)
            row, size = ' ' + carried, 1 + len(carried)
        row += char
        size += octets
    rows.append(row)
    return rows


def ics_file(event: TrialEvent, *, now: datetime | None = None) -> str:
    """The event as an .ics file: UTC times, escaped text, folded lines, CRLF line ends."""
    now = now or datetime.now(dt_timezone.utc)
    before = int(REMIND_BEFORE.total_seconds() // 60)
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Cogomelo//Kogo trial lesson//HE',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        f'UID:{event.uid}',
        f'DTSTAMP:{_utc_stamp(now)}',
        f'DTSTART:{_utc_stamp(event.start)}',
        f'DTEND:{_utc_stamp(event.end)}',
        f'SUMMARY:{_ics_text(event.title)}',
    ]
    if event.location:
        lines.append(f'LOCATION:{_ics_text(event.location)}')
    lines += [
        f'DESCRIPTION:{_ics_text(plain_notes(event))}',
        'STATUS:CONFIRMED',
        'TRANSP:OPAQUE',
        'BEGIN:VALARM',
        'ACTION:DISPLAY',
        f'DESCRIPTION:{_ics_text(event.title)}',
        f'TRIGGER:-PT{before}M',
        'END:VALARM',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    rows = [row for line in lines for row in _folded(line)]
    return '\r\n'.join(rows) + '\r\n'
