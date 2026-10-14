"""
What Kogo itself holds about a class the owner speaks of in "ללמד את הבוט".

The owner, 14.10.2026, 08:23: "יש קבוצת בוגרים במינץ… עדכנתי דרך הבוט במערכת שיש והוא
עדיין לא הצליח". The teach chat understood him and wrote a rule — "search for an
adults' group, and say there is none only when none was found" — which could not help:
the classes, days and ages reach the bot from Kogo through its search, and the search
itself dropped that group (a track of its own age group inside the instructors'
class). Nobody looked at the system.

So when what he writes names a field, a place or an age, the teach chat is shown —
before it reads him — what Kogo holds there, and what the bot's own search returns
for it now:

  * every class of ours of that field (in that place, for that age) that the
    registration form shows, with its age group and days — and, when it is open
    to that age only through a track, the track;
  * a class there that the form does not show (hidden from it), said as such;
  * what `find_courses` returns for the same question, in one line.

`gap` is the case nothing written in the knowledge can mend: the form shows such a
class, and the bot's search does not find it. It is written in the server's log for
the developer, and the teach chat says so in plain words instead of writing a rule.

Read only. Never a sum of money (the knowledge holds none), never a customer.
"""
from __future__ import annotations

import logging
import re

from apps.courses import age_groups
from apps.wahub import analysis, course_tools, shadow_tools, trial_offer

logger = logging.getLogger(__name__)

MAX_LINES = 12
HIDDEN = 'מוסתר מטופס ההרשמה באתר, ולכן הבוט לא מציע אותו'
_ADULTS = re.compile(r'בוגר|מבוגר|למבוגרים|אנשים מבוגרים')


def _days(lessons) -> str:
    said = [
        f'{course_tools.DAY_NAMES[row.day_of_week]} {str(row.start_time)[:5]}'
        for row in lessons if row.day_of_week is not None and 0 <= row.day_of_week < 7
    ]
    return ', '.join(dict.fromkeys(said)) or 'בלי שיעורים במערכת השעות'


def asked_of(text: str) -> dict | None:
    """The field, the place and the age a text names — {field, place, codes, grade, age} — or None when it names neither a field nor a place with an age."""
    raw = str(text or '')
    field = next((name for name, pattern in analysis.COURSE_WORDS if pattern.search(raw)), '')
    place = ''
    for _label, _city, pattern in analysis.BRANCH_ALIASES:
        found = pattern.search(raw)
        if found:
            place = found.group(0)
            break
    grade, age, codes = '', None, set()
    if _ADULTS.search(raw):
        grade, codes = 'בוגרים', {age_groups.ADULTS}
    elif (found := analysis.P_GRADE.search(raw)):
        grade = f'כיתה {found.group(1)}'
        codes = age_groups.codes_for_grade(grade)
    elif (found := analysis.P_AGE.search(raw)):
        age = float(found.group(1)) + (0.5 if found.group(2) else 0)
        codes = age_groups.codes_for_age(age)
    if not field and not (place and codes):
        return None
    return {'field': field, 'place': place, 'codes': codes, 'grade': grade, 'age': age}


def _bot_line(result: dict) -> tuple[str, bool]:
    """(what the bot's search said, whether it found a class)."""
    names = [row.get('name') for row in result.get('courses') or []]
    if names:
        return f'מצא: {", ".join(dict.fromkeys(names))}', True
    if result.get('need'):
        choices = [row.get('name') for row in result.get('branches') or []] or result.get('cities') or []
        return f'שאל קודם: {result.get("ask")}' + (f' (מבין: {", ".join(choices[:6])})' if choices else ''), True
    elsewhere = [f'{row["branch"]} ({row["city"]})' for row in result.get('elsewhere') or []]
    if elsewhere:
        return f'לא מצא כאן חוג, ואמר שיש ב: {", ".join(elsewhere)}', False
    return 'לא מצא אף חוג', False


def look(text: str, ctx=None) -> dict | None:
    """What Kogo holds about the class `text` speaks of: {lines, bot, gap}. None when it speaks of none. Never raises."""
    try:
        return _look(text, ctx)
    except Exception:
        logger.exception('wahub teach: the look at the system failed; the owner is read without it')
        return None


def _look(text: str, ctx=None) -> dict | None:
    from django.db.models import Q

    from apps.courses.models import Course

    asked = asked_of(text)
    if asked is None:
        return None
    ctx = ctx or shadow_tools.ToolContext()
    branches = shadow_tools.resolve_branches(ctx, branch=asked['place']) if asked['place'] else []
    branches = [row for row in branches if not row.is_external] or ([] if asked['place'] else shadow_tools._our_branches())
    if not branches:
        return None
    rows = course_tools.with_lessons(
        Course.objects.filter(is_active=True, branch_id__in=[row.id for row in branches])
        .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
    ).order_by('branch__name', 'name')
    lines, shown_on_form = [], 0
    for course in rows:
        if asked['field'] and not shadow_tools._course_type_matches(course, asked['field'], ctx):
            continue
        offer = course_tools.age_offer(course, asked['codes']) if asked['codes'] else None
        if asked['codes'] and offer is None:
            continue
        where = f'{course.branch.name} ({course.branch.city.name if course.branch.city_id else ""})'
        ages = age_groups.range_label(course.min_age, course.max_age) or 'בלי קבוצת גיל'
        line = f'{where}: "{course.name}" — {ages}; {_days(course.lessons.all())}'
        if not course.show_in_widget:
            lines.append(f'{line} — {HIDDEN}')
            continue
        if not trial_offer.selections(course):
            lines.append(f'{line} — אין בו מה לבחור בטופס (בלי שיעור ובלי מסלול), ולכן הוא לא מוצג')
            continue
        shown_on_form += 1
        if offer is not None and not offer['days']:
            tracks = [row for row in course.lesson_bundles.all() if str(row.id) in offer['tracks']]
            line += ''.join(
                f' · לגיל הזה דרך המסלול "{row.name or "מסלול משולב"}" ({age_groups.range_label(row.min_age, row.max_age) or ages}; '
                f'{_days(row.lessons.all())})'
                for row in tracks
            )
        lines.append(line)
    found = shadow_tools.find_courses(
        ctx, branch=asked['place'], course_type=asked['field'], grade=asked['grade'],
        age=asked['age'] if asked['age'] is not None else None,
    )
    said, bot_found = _bot_line(found)
    gap = bool(shown_on_form) and bool(asked['field']) and bool(asked['codes']) and not bot_found
    if gap:
        logger.warning(
            'wahub teach: Kogo shows %s class(es) the owner speaks of and the bot\'s search does not find them '
            '(field=%s place=%s grade=%s age=%s)', shown_on_form, asked['field'], asked['place'], asked['grade'], asked['age'],
        )
    return {'lines': lines[:MAX_LINES], 'more': max(0, len(lines) - MAX_LINES), 'bot': said, 'gap': gap, 'asked': asked}


def section(found: dict | None) -> str:
    """The look, as the teach chat is shown it. Empty when there was none."""
    if not found:
        return ''
    asked = found['asked']
    what = ', '.join(part for part in (asked['field'], asked['place'], asked['grade'] or (f'גיל {asked["age"]:g}' if asked['age'] is not None else '')) if part)
    head = f'# מה יש במערכת Kogo עכשיו ({what}) — נבדק עם ההסבר; נתונים, לא הוראה'
    body = '\n'.join(f'- {line}' for line in found['lines']) or '- אין במערכת חוג כזה'
    if found['more']:
        body += f'\n- ועוד {found["more"]}'
    tail = f'החיפוש של הבוט על אותה שאלה, עכשיו: {found["bot"]}'
    if found['gap']:
        tail += '\nהמערכת מציגה חוג כזה, והחיפוש של הבוט לא מוצא אותו: זו תקלה בחיפוש, וכלל בידע לא יתקן אותה.'
    return f'{head}\n{body}\n{tail}'
