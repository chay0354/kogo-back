"""
The words a script is filled with and the facts its checks expect — read from
the system when a run begins.

A script says "יש לכם {field} ב{place} ל{child}?" and expects the trial price
of that class in the answer. Which city, which field, which child and which
price are decided here, from the catalogue the registration form shows
(the same one the bot's tools read), so that nothing in a script is true of
one database only. A piece the system has nothing for — an external branch, a
class whose trial is paid — is listed in `missing` with the reason, and the
scripts that need it are skipped.

The facts are read from the course cards directly (`Course.price`,
`trial_lesson_price`, the lessons), not through the bot's tools: an answer is
held against the system, not against what the tool said the system holds.

Read only. Nothing here writes.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from apps.courses import age_groups

DAY_NAMES = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
GRADE_LETTERS = ('א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט', 'י', 'י״א', 'י״ב')
# Cities with no branch, to ask about: the first one the system really has none in is used.
FAR_CITIES = ('אילת', 'טבריה', 'קריית שמונה', 'דימונה', 'ערד', 'נהריה')
# Activities a parent may ask for: the first one the stage branch really has not is used.
OTHER_ACTIVITIES = ('ג׳ודו', 'שחייה', 'שחמט', 'כדורגל', 'קרמיקה', 'רובוטיקה')
YOUNG_CHILD = 'בן שנתיים ושבעה חודשים'


def _child(code: int) -> dict | None:
    """A child who sits on one position of the age-group scale only: the words a parent would use, and what the tool is given."""
    if code == age_groups.YOUNG:
        return {'words': 'בן 4', 'age': 4.0, 'grade': '', 'code': code}
    if code == age_groups.KINDERGARTEN:
        return {'words': 'בן 5', 'age': 5.0, 'grade': '', 'code': code}
    if age_groups.FIRST_GRADE <= code <= age_groups.LAST_GRADE:
        letter = GRADE_LETTERS[code - age_groups.FIRST_GRADE]
        return {'words': f'בכיתה {letter}', 'age': None, 'grade': f'כיתה {letter}', 'code': code}
    if code == age_groups.ADULTS:
        return {'words': 'מבוגר', 'age': None, 'grade': 'בוגרים', 'code': code}
    return None


def _money(value) -> str | None:
    from decimal import Decimal

    return None if value is None else f'{Decimal(str(value)).quantize(Decimal("0.01"))}'


def _field(course) -> str:
    from apps.wahub.shadow_tools import _field_of

    return _field_of(course)


def _course_facts(course, child: dict) -> dict:
    from apps.core.payment_service import registration_fee_amount

    priced = bool(course.trial_lesson_is_paid and course.trial_lesson_price is not None and course.trial_lesson_price > 0)
    return {
        'id': str(course.id),
        'name': course.name,
        'field': _field(course),
        'branch': course.branch.name,
        'city': course.branch.city.name if course.branch.city_id else '',
        'ages': age_groups.range_label(course.min_age, course.max_age),
        'child': child['words'],
        'child_age': child['age'],
        'child_grade': child['grade'],
        'price_monthly': _money(course.price),
        'registration_fee': _money(registration_fee_amount(course)),
        'trial_is_paid': bool(course.trial_lesson_is_paid),
        'trial_price': _money(course.trial_lesson_price) if priced else None,
        'bring': ((course.course_type.trial_bring_note if course.course_type_id else '') or '').strip(),
        'lessons': [
            {
                'day': DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
                'start': str(lesson.start_time)[:5],
                'end': str(lesson.end_time)[:5],
                'instructor': lesson.instructor.full_name if lesson.instructor_id else '',
            }
            for lesson in course.lessons.all()
        ],
    }


def _same_question(course, others, child: dict, tool_ctx) -> list:
    """The classes a parent's question — this field, this child, this branch — is about: what the bot may answer with."""
    from apps.wahub.shadow_tools import _course_type_matches

    field = _field(course)
    return [
        other for other in others
        if other.branch_id == course.branch_id
        and age_groups.fits(other.min_age, other.max_age, {child['code']})
        and _course_type_matches(other, field, tool_ctx)
    ]


def _clock(now, *, is_open: bool, start: date | None = None):
    """The nearest moment, from `start` on, at which the office is open (11:00) or closed (22:00)."""
    from apps.wahub import knowledge

    day = start or timezone.localtime(now).date()
    hour = time(11, 0) if is_open else time(22, 0)
    for step in range(0, 21):
        moment = timezone.make_aware(datetime.combine(day + timedelta(days=step), hour))
        if knowledge.office_hours_now(moment)['open'] == is_open:
            return moment
    return timezone.make_aware(datetime.combine(day, hour))


def _clocks(now) -> dict:
    """
    The pretend clocks of the scripts, as ISO strings. `open` and `closed` are
    near today, so that dates and seats are today's. The two around the
    cancellation deadline (1.4 of the season) are inside the season the
    deadline belongs to.
    """
    today = timezone.localtime(now).date()
    deadline_year = today.year + 1 if today.month >= 9 else today.year
    after = _clock(now, is_open=True, start=date(deadline_year, 5, 10))
    in_season_before = today if (today.month >= 9 or today < date(today.year, 4, 1)) else date(today.year, 11, 10)
    return {
        'open': _clock(now, is_open=True).isoformat(),
        'closed': _clock(now, is_open=False).isoformat(),
        'before_cancel_deadline': _clock(now, is_open=True, start=in_season_before).isoformat(),
        'after_cancel_deadline': after.isoformat(),
    }


def _knowledge_words(words: dict, facts: dict, missing: dict, today: date) -> None:
    from apps.wahub import knowledge
    from apps.wahub.models import KIND_CONTACT, KIND_LINK, KIND_PHRASING

    items = knowledge.relevant_items(today=today)
    links = {item.key: (item.data or {}).get('url') or '' for item in items if item.kind == KIND_LINK and item.key}
    facts['links'] = links
    facts['phrasings'] = {item.key: item.body for item in items if item.kind == KIND_PHRASING and item.key}
    office = next((item for item in items if item.kind == KIND_CONTACT and 'משרד' in (item.title or '') and (item.data or {}).get('phone')), None)
    facts['office_phone'] = str((office.data or {}).get('phone')) if office else ''
    if not items:
        missing['knowledge'] = 'ידע הבוט ריק — הכללים, הנוסחים והקישורים עוד לא נכנסו למערכת'
    for key in ('registration', 'cancellation_form'):
        if not links.get(key):
            missing[f'link_{key}'] = f'בידע הבוט אין קישור "{key}"'


def build(now=None) -> dict:
    """
    {words, facts, missing, clocks}: plain JSON, kept on the run so that a run
    continued in a later request plays the same conversation.
    """
    from django.db.models import Q

    from apps.core.models import Branch
    from apps.customers.widget_views import _widget_catalog_courses
    from apps.wahub import course_tools, shadow_tools

    now = now or timezone.now()
    today = timezone.localtime(now).date()
    words: dict = {'young_child': YOUNG_CHILD}
    facts: dict = {}
    missing: dict = {}
    _knowledge_words(words, facts, missing, today)
    tool_ctx = shadow_tools.ToolContext(None, now=now)

    # Sorted here and not by the database: its collation decides how Hebrew sorts, and the same script
    # has to choose the same class on a developer's machine and on the server.
    branches = sorted(Branch.objects.filter(is_active=True).select_related('city'), key=lambda row: (row.name, str(row.id)))
    ours = [row for row in branches if not row.is_external]
    courses = sorted(
        course_tools.with_lessons(
            _widget_catalog_courses()
            .filter(branch_id__in=[row.id for row in ours])
            .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
        ),
        key=lambda course: (course.branch.name, _field(course), course.name, str(course.id)),
    )
    by_branch: dict = {}
    for course in courses:
        by_branch.setdefault(course.branch_id, []).append(course)
    per_city: dict = {}
    for row in ours:
        if row.id in by_branch and row.city_id:
            per_city.setdefault(row.city.name, []).append(row)

    # --- the stage: one branch of ours, alone in its city, with more than one field -------------------
    def askable(branch_courses):
        """(course, child) pairs a parent can ask about by age: a class with a lower end and a lesson."""
        pairs = []
        for course in branch_courses:
            child = _child(course.min_age) if course.min_age else None
            if child is not None and list(course.lessons.all()):
                pairs.append((course, child))
        return pairs

    def score(branch):
        mine = by_branch.get(branch.id, [])
        pairs = askable(mine)
        fields = {_field(course) for course in mine}
        if len(fields) < 2 or not pairs:
            return None
        alone = len(per_city.get(branch.city.name if branch.city_id else '', [])) == 1
        paid = any(course.trial_lesson_is_paid and course.trial_lesson_price for course, _child_ in pairs)
        free = any(not course.trial_lesson_is_paid for course, _child_ in pairs)
        return (alone, paid and free, paid, free, -len(mine), branch.name)

    scored = sorted(((score(row), row) for row in ours if score(row) is not None), key=lambda pair: pair[0], reverse=True)
    if not scored:
        missing['stage'] = 'אין במערכת סניף שלנו עם שני תחומים לפחות ועם חוג שמוגדרת לו קבוצת גיל'
        return {'words': words, 'facts': facts, 'missing': missing, 'clocks': _clocks(now)}
    stage = scored[0][1]
    stage_courses = by_branch[stage.id]
    alone = len(per_city.get(stage.city.name, [])) == 1 if stage.city_id else False
    # How a parent names the place: the city when it has one branch of ours, the branch itself otherwise.
    words['place'] = stage.city.name if alone else stage.name
    facts['stage'] = {
        'branch': stage.name, 'city': stage.city.name if stage.city_id else '',
        'fields': sorted({_field(course) for course in stage_courses}),
        'address': stage.address or '', 'phone': stage.phone or '',
    }

    pairs = askable(stage_courses)

    def narrow(pair):
        """How many classes share the question of this one: one is the cleanest script."""
        return len(_same_question(pair[0], stage_courses, pair[1], tool_ctx))

    def describe(pair) -> dict:
        course, child = pair
        entry = _course_facts(course, child)
        entry['same_question'] = [_course_facts(other, child) for other in _same_question(course, stage_courses, child, tool_ctx)]
        return entry

    paid_pairs = sorted((pair for pair in pairs if pair[0].trial_lesson_is_paid and pair[0].trial_lesson_price), key=narrow)
    free_pairs = sorted((pair for pair in pairs if not pair[0].trial_lesson_is_paid), key=narrow)
    paid = paid_pairs[0] if paid_pairs else None
    free = next((pair for pair in free_pairs if paid is None or (_field(pair[0]), pair[1]['code']) != (_field(paid[0]), paid[1]['code'])), None)
    main = free or paid or sorted(pairs, key=narrow)[0]
    facts['main'] = describe(main)
    words.update(field=facts['main']['field'], child=facts['main']['child'], course=facts['main']['name'])
    if facts['main']['lessons'] and facts['main']['lessons'][0]['instructor']:
        words['instructor'] = facts['main']['lessons'][0]['instructor']
    else:
        missing['instructor'] = 'לחוג שנבחר לבדיקה אין מדריך במערכת'
    if paid is not None:
        facts['paid'] = describe(paid)
        words.update(paid_field=facts['paid']['field'], paid_child=facts['paid']['child'])
    else:
        missing['paid'] = 'בסניף שנבחר לבדיקה אין חוג ששיעור הניסיון שלו בתשלום (עם מחיר)'
    if free is not None:
        facts['free'] = describe(free)
        words.update(free_field=facts['free']['field'], free_child=facts['free']['child'])
    else:
        missing['free'] = 'בסניף שנבחר לבדיקה אין חוג ששיעור הניסיון שלו ללא עלות'

    # A second class, of another question, to ask about in the same conversation.
    main_key = (facts['main']['field'], main[1]['code'])
    others = [pair for pair in pairs if (_field(pair[0]), pair[1]['code']) != main_key and pair[0].id != main[0].id]
    others.sort(key=lambda pair: (pair[0].price == main[0].price, narrow(pair)))
    if others:
        facts['second'] = describe(others[0])
        words.update(second_field=facts['second']['field'], second_child=facts['second']['child'])
    else:
        missing['second'] = 'בסניף שנבחר לבדיקה אין חוג נוסף, של תחום או גיל אחר'

    # Another field of the same branch than the one asked about.
    other_field = next((name for name in facts['stage']['fields'] if name != facts['main']['field']), '')
    if other_field:
        words['other_field'] = other_field

    # A class whose type says what to bring to a trial.
    bring = next((pair for pair in pairs if (pair[0].course_type.trial_bring_note if pair[0].course_type_id else '')), None)
    if bring is not None:
        facts['bring'] = describe(bring)
        words.update(bring_field=facts['bring']['field'], bring_child=facts['bring']['child'])
    else:
        missing['bring'] = 'לאף תחום בסניף שנבחר לא כתוב "מה להביא לשיעור ניסיון"'

    # A child the field has no class for, in this branch.
    for code in (age_groups.LAST_GRADE, age_groups.LAST_GRADE - 1, age_groups.FIRST_GRADE + 5, age_groups.KINDERGARTEN, age_groups.YOUNG):
        child = _child(code)
        if child and not _same_question(main[0], stage_courses, child, tool_ctx):
            words['no_class_child'] = child['words']
            break
    else:
        missing['no_class_child'] = 'לתחום שנבחר יש חוג לכל גיל — אין גיל לשאול עליו בלי חוג'

    # A field with two groups that meet at six — גן חובה and כיתה א — so that "בן 6" is not yet an answer.
    def groups_at_six(field_name: str) -> set:
        return {
            age_groups.range_label(course.min_age, course.max_age) for course in stage_courses
            if _field(course) == field_name and age_groups.fits(course.min_age, course.max_age, {age_groups.KINDERGARTEN, age_groups.FIRST_GRADE})
            and (course.min_age or course.max_age)
        }

    boundary = next((name for name in facts['stage']['fields'] if len(groups_at_six(name)) > 1), '')
    if boundary:
        words['boundary_field'] = boundary
    else:
        missing['boundary_field'] = 'אין בסניף שנבחר תחום עם שתי קבוצות גיל שנפגשות בגיל 6'

    # An activity this branch does not have.
    activity = next((
        name for name in OTHER_ACTIVITIES
        if not any(shadow_tools._course_type_matches(course, name, tool_ctx) for course in stage_courses)
    ), '')
    if activity:
        words['missing_field'] = activity

    # --- other places ------------------------------------------------------------------------------------
    several = next((city for city, rows in sorted(per_city.items()) if len(rows) > 1), '')
    if several:
        words['multi_city'] = several
        facts['multi_city'] = {'city': several, 'branches': [row.name for row in per_city[several]]}
    else:
        missing['multi_city'] = 'אין עיר שיש לנו בה יותר מסניף אחד עם חוגים'

    external = [row for row in branches if row.is_external]
    only_external = next((
        row for row in external
        if row.city_id and not any(other.city_id == row.city_id and not other.is_external for other in branches)
    ), None)
    chosen = only_external or (external[0] if external else None)
    if chosen is not None:
        words['external_place'] = chosen.city.name if (only_external is not None and chosen.city_id) else chosen.name
        facts['external'] = {'branch': chosen.name, 'city': chosen.city.name if chosen.city_id else ''}
    else:
        missing['external_place'] = 'אין במערכת סניף חיצוני (עירייה / מתנ"ס)'

    far = next((name for name in FAR_CITIES if not shadow_tools.resolve_branches(tool_ctx, city=name)), '')
    if far:
        words['far_city'] = far
    else:
        missing['far_city'] = 'לא נמצאה עיר לשאול עליה שאין לנו בה סניף'

    facts['cities'] = sorted({row.city.name for row in branches if row.city_id})
    return {'words': words, 'facts': facts, 'missing': missing, 'clocks': _clocks(now)}


class Catalog:
    """
    The names a customer could be told — every class, field, instructor, branch
    and city of the system — so that an answer naming one of them can be asked
    where it got it. Read on every request; never kept.
    """

    def __init__(self):
        from apps.core.models import Branch
        from apps.courses.models import Course, CourseType, Instructor

        self.fields = {name.strip() for name in CourseType.objects.filter(is_active=True).values_list('name', flat=True) if name}
        self.courses = {name.strip() for name in Course.objects.filter(is_active=True).values_list('name', flat=True) if name}
        self.instructors = {
            f'{first} {last}'.strip() for first, last in Instructor.objects.filter(is_active=True).values_list('first_name', 'last_name')
            if (first or '').strip() and (last or '').strip()
        }
        rows = list(Branch.objects.filter(is_active=True).select_related('city'))
        self.branches = {row.name.strip() for row in rows if row.name}
        self.cities = {row.city.name.strip() for row in rows if row.city_id and row.city.name}
        self.activities = set(OTHER_ACTIVITIES)
