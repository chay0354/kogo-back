"""
Deciding which of our lessons a municipality's group means.

Measured against both real files before it was written. What that showed:

* **Day and start time, never the full range.** Yehud lists a group as
  19:00-20:00 where our lesson is 19:00-19:45. Comparing end times would have
  thrown away a match that is plainly the same class.
* **Start time alone is sometimes ambiguous.** At Yehud we run two different
  courses at the same day and time, and only the age band in the group name
  separates them. That case is a question for a person, not an error — the
  screen asks, and the answer is remembered.
* **A group can mean more than one lesson.** Zamir sells once-a-week-Sunday,
  once-a-week-Wednesday and Sunday+Wednesday as three separate groups, which is
  exactly our combined-track shape. A twice-weekly group matches two lessons and
  puts each child on both, the same way a paying combined-track registration
  creates one enrolment per member lesson.

All nine of Zamir's groups matched exactly on this rule; three of Yehud's five
did, one needed the age band, and one is a Monday-only municipality group
against a twice-weekly course of ours — which resolves on its own, because a
group only ever claims the lessons its own slots name.
"""
from __future__ import annotations

import re

from apps.courses.models import Lesson
from apps.external_students.models import ExternalRosterGroup, ExternalRosterImportUnit

# 'גילאי 3-4.5', 'גיל 5-6', '4.5-6' — the band the municipality wrote.
AGE_RANGE = re.compile(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)')
# 'כיתות א-ב', "כיתות ג'+ה'" — school years. The geresh matters: both real files
# write it. A course name says the same thing without the prefix — 'קפוארה ג-ו' —
# so a bare letter range counts too.
GRADE_LETTERS = 'אבגדהוזחט'
GRADE_RANGE = re.compile(
    rf"(?:כית(?:ה|ות)\s*)?(?<![א-ת])([{GRADE_LETTERS}])['\u05f3]?\s*[-–+]\s*([{GRADE_LETTERS}])['\u05f3]?(?![א-ת])"
)

# Words that say when rather than who — dropped before names are compared.
NOISE = re.compile(r'\b(יום|ימים|פעמיים|פעם|בשבוע|גילאי|גיל|כיתה|כיתות)\b')


def parse_age_band(group_name: str) -> tuple[float, float] | None:
    """The age or grade band a group name states, as a comparable pair."""
    grades = GRADE_RANGE.search(group_name or '')
    if grades:
        low = GRADE_LETTERS.index(grades.group(1))
        high = GRADE_LETTERS.index(grades.group(2))
        # Grade א is roughly age 6 here, which is how the courses are set up.
        return (6.0 + min(low, high), 6.0 + max(low, high))
    ages = AGE_RANGE.search(group_name or '')
    if ages:
        low, high = float(ages.group(1)), float(ages.group(2))
        return (min(low, high), max(low, high))
    return None


def _name_tokens(text: str) -> set[str]:
    cleaned = NOISE.sub(' ', text or '')
    cleaned = re.sub(r'[\d:.\-–+]+', ' ', cleaned)
    return {token for token in cleaned.split() if len(token) > 1}


def _band_score(band, course) -> float:
    """
    How well a course answers the band the municipality wrote.

    Compared against the band in the *course name*, not against
    ``Course.min_age``/``max_age``. Those two hold an age-group code rather than
    a year at the branches this runs on — the Yehud courses named '3-4.5 קפוארה'
    and '4.5-6 קפוארה' carry 1 and 2 — so reading them as years would compare
    two different units and quietly pick the wrong class. The name is where the
    real band lives, in both the municipality's sheet and ours.
    """
    if band is None:
        return 0.0
    theirs = parse_age_band(course.name)
    if theirs is None:
        return 0.0
    low, high = band
    if theirs == band:
        return 3.0                      # the same band, written the same way
    if theirs[0] <= low and high <= theirs[1]:
        return 2.0                      # the course covers what they asked for
    overlap = min(high, theirs[1]) - max(low, theirs[0])
    return 1.0 if overlap >= 0 else -1.0


def candidate_lessons(branch, slot: dict, pool) -> list[Lesson]:
    """Every lesson meeting on that day at that start time."""
    return [
        lesson for lesson in pool
        if lesson.day_of_week == slot['day']
        and lesson.start_time.strftime('%H:%M') == slot['start']
    ]


def lesson_pool(branch):
    return list(
        Lesson.objects
        .filter(course__branch=branch, course__is_active=True)
        .exclude(status='cancelled')
        .select_related('course', 'room')
    )


def _describe(lesson) -> dict:
    return {
        'lesson_id': str(lesson.id),
        'course_id': str(lesson.course_id),
        'course_name': lesson.course.name,
        'course_display_id': lesson.course.display_id,
        'day_of_week': lesson.day_of_week,
        'start_time': lesson.start_time.strftime('%H:%M'),
        'end_time': lesson.end_time.strftime('%H:%M'),
        'min_age': lesson.course.min_age,
        'max_age': lesson.course.max_age,
    }


def match_unit(unit: ExternalRosterImportUnit, pool=None) -> dict:
    """
    Work out which lessons this group means, and how sure we are.

    Returns ``{'state', 'lessons', 'candidates'}``. ``state`` is one of the
    model's ``MATCH_*`` values; ``ambiguous`` and ``none`` are ordinary outcomes
    that hand the decision to the manager rather than guessing on their behalf.
    """
    roster_import = unit.roster_import
    branch = roster_import.branch
    pool = lesson_pool(branch) if pool is None else pool

    # A mapping confirmed on a previous import wins — unless the timetable has
    # moved under it, in which case both readings are shown and it is asked again.
    if unit.municipality_code:
        confirmed = (
            ExternalRosterGroup.objects
            .filter(branch=branch, municipality_code=unit.municipality_code)
            .prefetch_related('lessons')
            .first()
        )
        if confirmed is not None:
            lessons = list(confirmed.lessons.all())
            known = {(l.day_of_week, l.start_time.strftime('%H:%M')) for l in lessons}
            claimed = {(s['day'], s['start']) for s in unit.slots}
            if lessons and known == claimed:
                return {
                    'state': ExternalRosterImportUnit.MATCH_CONFIRMED,
                    'lessons': lessons,
                    'candidates': [_describe(l) for l in lessons],
                }

    if not unit.slots:
        return {'state': ExternalRosterImportUnit.MATCH_NONE, 'lessons': [], 'candidates': []}

    band = parse_age_band(unit.group_name)
    wanted = _name_tokens(unit.group_name)

    chosen: list[Lesson] = []
    every_candidate: list[Lesson] = []
    ambiguous = False

    for slot in unit.slots:
        options = candidate_lessons(branch, slot, pool)
        every_candidate.extend(options)
        if not options:
            ambiguous = True
            continue
        if len(options) == 1:
            chosen.append(options[0])
            continue

        scored = sorted(
            options,
            key=lambda lesson: (
                _band_score(band, lesson.course),
                len(wanted & _name_tokens(lesson.course.name)),
            ),
            reverse=True,
        )
        best = (
            _band_score(band, scored[0].course),
            len(wanted & _name_tokens(scored[0].course.name)),
        )
        runner_up = (
            _band_score(band, scored[1].course),
            len(wanted & _name_tokens(scored[1].course.name)),
        )
        if best > runner_up:
            chosen.append(scored[0])
        else:
            ambiguous = True

    # Every slot of one group has to belong to one course. If they do not, the
    # tiebreak picked lessons from two different classes and is not to be trusted.
    if chosen and len({lesson.course_id for lesson in chosen}) > 1:
        ambiguous = True

    if ambiguous or len(chosen) != len(unit.slots):
        state = (
            ExternalRosterImportUnit.MATCH_NONE if not every_candidate
            else ExternalRosterImportUnit.MATCH_AMBIGUOUS
        )
        return {
            'state': state,
            'lessons': [],
            'candidates': [_describe(l) for l in _unique(every_candidate)],
        }

    return {
        'state': ExternalRosterImportUnit.MATCH_EXACT,
        'lessons': chosen,
        'candidates': [_describe(l) for l in chosen],
    }


def _unique(lessons):
    seen, out = set(), []
    for lesson in lessons:
        if lesson.id not in seen:
            seen.add(lesson.id)
            out.append(lesson)
    return out
