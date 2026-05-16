"""Lesson-style weekday (0=Sunday .. 6=Saturday), aligned with Lesson.day_of_week and JS Date.getDay()."""
from __future__ import annotations

from datetime import date


def lesson_style_dow_from_date(d: date) -> int:
    """Python date.weekday() is Mon=0..Sun=6; convert to lesson/JS Sunday=0..Saturday=6."""
    return (d.weekday() + 1) % 7


def python_weekday_from_lesson_dow(lesson_dow: int) -> int:
    """Inverse: Mon=0..Sun=6."""
    return (lesson_dow + 6) % 7


def normalized_weekly_repeat_lesson_dows(event) -> list[int]:
    """Weekly repeat days as sorted unique ints in 0..6; default to anchor day from event_date."""
    raw = getattr(event, 'weekly_repeat_days', None) or []
    if not isinstance(raw, list) or len(raw) == 0:
        return [lesson_style_dow_from_date(event.event_date)]
    out: list[int] = []
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i <= 6:
            out.append(i)
    return sorted(set(out)) if out else [lesson_style_dow_from_date(event.event_date)]
