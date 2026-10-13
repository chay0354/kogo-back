"""
How long somebody has been waiting for a person — counted in the office's own
hours (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.2).

The owner, 13.10.2026: "מי שלא ענו לו מעל 3 שעות — לא נחשב אם הם היו בלילה
וחיכו לנציג". A customer who wrote at 20:00 and is read at 11:00 the next
morning has waited half an hour, not fifteen: the night, the weekend and a day
the office is closed are not counted.

The hours are the ones the bot itself reads (knowledge.py): the weekly office
hours — 10:30–18:00, Sunday to Thursday, until somebody files them — and the
special days of the whole business. A day marked closed counts for nothing, a
day of other hours counts by those hours, and "open as usual" or "quiet" keep
the weekly line.

A `Clock` is built once for a request. It holds the stretches the office was
open in the last LOOK_BACK_DAYS up to now, and answers two things from them
with no query: how many office minutes have passed since a moment (`waited`),
and the one moment since which exactly OVERDUE_MINUTES of them have passed
(`cutoff`) — so a list is sorted and counted by comparing a column to that
moment, never by working anything out for each row in the database.

Nothing here writes anything.
"""
from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.wahub import knowledge
from apps.wahub.models import KIND_SPECIAL_DAY, SCOPE_BUSINESS, SPECIAL_CLOSED, SPECIAL_HOURS, KnowledgeItem

# Three hours of the office's time: from here on a conversation that waits for a person is pinned to the
# head of the list and its mark blinks. One number, in the code; there is no switch for it.
OVERDUE_MINUTES = 180
# How far back the office's hours are laid out. Somebody waiting longer gets the minutes of these days.
LOOK_BACK_DAYS = 30
# The hours until somebody files them: the owner's own, Sunday to Thursday (Python's weekday(): Monday is 0).
DEFAULT_HOURS = (day_time(10, 30), day_time(18, 0))
DEFAULT_DAYS = (6, 0, 1, 2, 3)
# The hours and the special days are read once a minute, whoever asks: the live update asks every three seconds.
CALENDAR_KEY = 'wahub:office_clock:calendar'
CALENDAR_SECONDS = 60

JUST_NOW = 'כתב עכשיו'
OFFICE_CLOSED = 'כתב מחוץ לשעות המשרד'


def read_calendar() -> tuple:
    """(the weekly hours or None, the special days of the whole business) — two small queries."""
    item = knowledge.office_hours_item()
    weekly = (item.data or {}).get('weekly') if item is not None else None
    specials = []
    for row in KnowledgeItem.objects.filter(kind=KIND_SPECIAL_DAY, is_active=True, scope_level=SCOPE_BUSINESS).order_by('id'):
        data = row.data or {}
        try:
            start = date.fromisoformat(data.get('date_from') or '')
            end = date.fromisoformat(data.get('date_to') or data.get('date_from') or '')
        except ValueError:
            continue
        specials.append((start, max(start, end), data.get('state') or '', data.get('hours_from') or '', data.get('hours_to') or ''))
    return (weekly if isinstance(weekly, dict) else None), specials


def calendar() -> tuple:
    """`read_calendar`, kept for a minute."""
    kept = cache.get(CALENDAR_KEY)
    if kept is None:
        kept = read_calendar()
        cache.set(CALENDAR_KEY, kept, CALENDAR_SECONDS)
    return kept


def hours_of(day: date, weekly, specials) -> tuple | None:
    """(opens, closes) of the office on `day`, as times; None for a day it does not open."""
    if weekly is None:
        opens, closes = DEFAULT_HOURS if day.weekday() in DEFAULT_DAYS else (None, None)
        is_open = opens is not None
    else:
        line = weekly.get(knowledge._WEEKDAY_TO_KEY[day.weekday()]) or {}
        opens, closes = knowledge._parse_time(line.get('from')), knowledge._parse_time(line.get('to'))
        is_open = bool(line.get('open'))
    # A special day of the whole business overrides the weekly line, as it does for the bot: the first one filed.
    special = next((row for row in specials if row[0] <= day <= row[1]), None)
    if special is not None:
        _start, _end, state_, hours_from, hours_to = special
        if state_ == SPECIAL_CLOSED:
            return None
        if state_ == SPECIAL_HOURS:
            opens = knowledge._parse_time(hours_from) or opens
            closes = knowledge._parse_time(hours_to) or closes
            is_open = True
    if not is_open or opens is None or closes is None or closes <= opens:
        return None
    return opens, closes


def label(minutes: int, open_now: bool) -> str:
    """ "מחכה 3:20 שע׳" — what the mark on a row says."""
    if minutes < 1:
        return JUST_NOW if open_now else OFFICE_CLOSED
    if minutes < 60:
        return f'מחכה {minutes} דק׳'
    return f'מחכה {minutes // 60}:{minutes % 60:02d} שע׳'


class Clock:
    """The office's open stretches up to `now`, laid out once; see the module's opening."""

    def __init__(self, now=None, *, hours=None):
        self.now = now or timezone.now()
        weekly, specials = hours if hours is not None else calendar()
        zone = timezone.get_current_timezone()
        today = timezone.localtime(self.now).date()
        self.stretches = []
        for back in range(LOOK_BACK_DAYS, -1, -1):
            day = today - timedelta(days=back)
            found = hours_of(day, weekly, specials)
            if found is None:
                continue
            opens = timezone.make_aware(datetime.combine(day, found[0]), zone)
            closes = min(timezone.make_aware(datetime.combine(day, found[1]), zone), self.now)
            if opens < closes:
                self.stretches.append((opens, closes))
        self.open_now = bool(self.stretches) and self.stretches[-1][1] == self.now

    def waited(self, since) -> int | None:
        """Whole office minutes from `since` to now; None for no moment."""
        if since is None:
            return None
        seconds = 0.0
        for opens, closes in reversed(self.stretches):
            if closes <= since:
                break
            seconds += (closes - max(opens, since)).total_seconds()
        return int(seconds // 60)

    def cutoff(self, minutes: int = OVERDUE_MINUTES):
        """
        The moment since which exactly `minutes` office minutes have passed:
        whoever has waited from then or from before it has waited at least that
        long. None when the office was not open that long in the days looked at.
        """
        left = timedelta(minutes=minutes)
        for opens, closes in reversed(self.stretches):
            if closes - opens >= left:
                return closes - left
            left -= closes - opens
        return None

    def label(self, minutes: int) -> str:
        return label(minutes, self.open_now)
