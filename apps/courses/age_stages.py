"""
Reading a course's age band.

``Course.min_age`` / ``max_age`` are **not years**, despite the names. They are
stage codes, and the whole catalogue uses them consistently: 1 and 2 are the two
kindergarten bands a course name writes as "3-4.5" and "4.5-6", and from 3
upwards they are school grades, 3 being א. So "קפוארה ג-ד" carries (5, 6) and
"חטיבה / תיכון ריקוד" carries (9, 14) — ז through יב.

``ledger_dimensions.age_label`` renders these as years ("גילאי 5–6"), which reads
wrong for every course in the catalogue. That is an older bug on the dashboards
and is not fixed here; this module exists so a new screen does not repeat it.
"""
from __future__ import annotations

GRADES = ['א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט', 'י', 'יא', 'יב']

KINDERGARTEN = {1: 'גן 3-4.5', 2: 'גן 4.5-6'}

# The first school grade. Codes below this are kindergarten bands.
FIRST_GRADE_CODE = 3


def stage_name(code) -> str:
    """One stage code as the office writes it."""
    if not code:
        return ''
    code = int(code)
    if code in KINDERGARTEN:
        return KINDERGARTEN[code]
    index = code - FIRST_GRADE_CODE
    if 0 <= index < len(GRADES):
        return f'כיתה {GRADES[index]}'
    return 'בוגרים'


def stage_label(min_code, max_code) -> str:
    """The band a course covers, e.g. 'גן 3-4.5' or 'כיתה ג–כיתה ו'."""
    low, high = stage_name(min_code), stage_name(max_code)
    if not low and not high:
        return ''
    if not high or low == high:
        return low or high
    if not low:
        return high
    return f'{low}–{high}'


def age_key_for(min_code, max_code) -> str:
    """A stable filter value, matching ledger_dimensions.age_key's shape."""
    if not (min_code or max_code):
        return ''
    return f'{min_code or ""}-{max_code or ""}'
