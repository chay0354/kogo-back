"""
The age-group scale of a course.

`Course.min_age` and `Course.max_age` do not hold years. They hold a position
on one fixed scale — two preschool bands, the grades א–י״ב, then adults and
instructors — the scale the CRM's age pickers and the widget's age step show
(kogo-front `src/lib/courseUtils.ts`, AGE_GROUP_LABELS; the labels here are
that list, and a change there is a change here).

So a child's age in years is never compared with a course directly. A boy of
six is a "4.5-6" or a "כיתה א"; read as a position, 6 is "כיתה ד". The
WhatsApp bot did exactly that (11.10.2026): asked about capoeira for a
five-year-old in ראש העין it looked at the group for grades ג–ד, whose trial
lesson is free, and not at "קפוארה 4.5-6", whose trial costs ₪30.
"""
from __future__ import annotations

import re

AGE_GROUP_LABELS = (
    '3-4.5', '4.5-6',
    'כיתה א', 'כיתה ב', 'כיתה ג', 'כיתה ד', 'כיתה ה', 'כיתה ו',
    'כיתה ז', 'כיתה ח', 'כיתה ט', 'כיתה י', 'כיתה י״א', 'כיתה י״ב',
    'בוגרים', 'מדריכים',
)
YOUNG, KINDERGARTEN = 1, 2
FIRST_GRADE, LAST_GRADE = 3, 14
ADULTS, INSTRUCTORS = 15, 16

_GRADE_LETTERS = ('א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט', 'י', 'יא', 'יב')


def _code(value) -> int | None:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return code if 1 <= code <= len(AGE_GROUP_LABELS) else None


def years_of(code) -> tuple[float | None, float | None]:
    """The ages, in years, a position stands for: (from, to). Adults have no upper end; instructors have no age."""
    code = _code(code)
    if code is None or code == INSTRUCTORS:
        return None, None
    if code == YOUNG:
        return 3.0, 4.5
    if code == KINDERGARTEN:
        return 4.5, 6.0
    if code == ADULTS:
        return 18.0, None
    grade = code - FIRST_GRADE + 1          # כיתה א = 1: six to seven
    return 5.0 + grade, 6.0 + grade


def codes_for_age(age) -> set[int]:
    """
    The positions a child of this age may sit on. An age on the line between
    two bands belongs to both — a six-year-old may be in גן חובה or in כיתה א —
    and whoever asks is then asked which.
    """
    try:
        years = float(age)
    except (TypeError, ValueError):
        return set()
    found = set()
    for code in range(1, ADULTS + 1):
        low, high = years_of(code)
        if low is not None and low <= years and (high is None or years <= high):
            found.add(code)
    return found


def codes_for_grade(text) -> set[int]:
    """ "כיתה ג", "ג'", "י״א", "גן חובה", "גן", "בוגרים" → the positions meant. Empty when the text names none."""
    raw = str(text or '').strip()
    if not raw:
        return set()
    if re.search(r'בוגר|מבוגר', raw):
        return {ADULTS}
    if 'חובה' in raw and 'טרום' not in raw:
        return {KINDERGARTEN}
    if re.search(r'גן|טרום', raw):
        return {YOUNG, KINDERGARTEN}
    letters = re.sub(r'כיתות|כיתה|כתה|[\s"״\'׳.\-]', '', raw)
    if letters in _GRADE_LETTERS:
        return {FIRST_GRADE + _GRADE_LETTERS.index(letters)}
    if letters.isdigit() and 1 <= int(letters) <= 12:
        return {FIRST_GRADE + int(letters) - 1}
    return set()


def fits(min_code, max_code, codes) -> bool:
    """Whether a course whose range is min–max takes any of these positions. An empty end is an open end."""
    low = _code(min_code) or 1
    high = _code(max_code) or len(AGE_GROUP_LABELS)
    return any(low <= code <= high for code in codes)


def _one(code: int) -> str:
    label = AGE_GROUP_LABELS[code - 1]
    return f'גיל {label}' if code in (YOUNG, KINDERGARTEN) else label


def range_label(min_code, max_code) -> str:
    """The range in the words a parent uses: "גיל 4.5-6", "כיתות א–ב", "כיתה ז עד כיתה י״ב". Empty when no range is set."""
    low, high = _code(min_code), _code(max_code)
    if low is None and high is None:
        return ''
    if low is None or high is None or low == high:
        return _one(low or high)
    if low > high:
        low, high = high, low
    if high <= KINDERGARTEN:
        return 'גיל 3-6'
    if low >= FIRST_GRADE and high <= LAST_GRADE:
        return f'כיתות {AGE_GROUP_LABELS[low - 1][5:]}–{AGE_GROUP_LABELS[high - 1][5:]}'
    return f'{_one(low)} עד {_one(high)}'


def years_range(min_code, max_code) -> tuple[float | None, float | None]:
    """The range in years: from the youngest age of the lower end to the oldest of the upper one."""
    low = years_of(min_code)[0] if _code(min_code) else None
    high = years_of(max_code)[1] if _code(max_code) else None
    return low, high
