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
# How parents shorten a city: the first one the system has a branch of ours in is used.
SHORT_CITIES = (('פתח תקווה', 'פ"ת'), ('כפר סבא', 'כ"ס'), ('ראש העין', 'רה"ע'), ('רמת גן', 'ר"ג'), ('תל אביב', 'ת"א'), ('הוד השרון', 'הוד"ש'))


def typo(word: str) -> str:
    """
    The word as a parent in a hurry types it: a final ה for א ("קפוארא"), a
    doubled ו written once ("תקוה"), or two neighbouring letters changed
    places. Never the word itself.
    """
    word = str(word or '').strip()
    if 'וו' in word:
        return word.replace('וו', 'ו', 1)
    if word.endswith('ה') and len(word) > 2:
        return word[:-1] + 'א'
    letters = [index for index, char in enumerate(word) if 'א' <= char <= 'ת']
    for left, right in zip(letters[1:], letters[2:]):
        if right == left + 1 and word[left] != word[right]:
            return word[:left] + word[right] + word[left] + word[right + 1:]
    return word + word[-1:] if word else word


def glued(word: str) -> str:
    """The same words with no space between them: "ראשהעין"."""
    return ''.join(str(word or '').split())


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


def _trial_dates(offer, at) -> dict:
    """
    The trial dates of a class as the registration form has them at `at` —
    read through apps/wahub/trial_offer.py, which asks the form's own functions
    and is held against the form itself (apps/wahub/tests/test_trial_offer.py).
    Dates as they are written to a customer: "13.10".
    """
    from apps.wahub import trial_offer

    today = timezone.localtime(at).date()
    week = today - timedelta(days=(today.weekday() + 1) % 7)
    opened = [slot.day for slot in offer.open]
    return {
        'can_book': offer.can_book,
        'open': [day.strftime('%d.%m') for day in opened],
        'full': list(dict.fromkeys(slot.day.strftime('%d.%m') for slot in offer.full)),
        'skipped': list(dict.fromkeys(
            skip.day.strftime('%d.%m') for skip in offer.skipped if skip.why != trial_offer.SKIP_STARTED
        )),
        'today_open': today in opened,
        'tomorrow_open': today + timedelta(days=1) in opened,
        'this_week_open': any(week <= day < week + timedelta(days=7) for day in opened),
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
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    return {
        'open': _clock(now, is_open=True).isoformat(),
        'closed': _clock(now, is_open=False).isoformat(),
        # A Saturday morning: nobody is in the office, and most classes do not meet.
        'weekend': timezone.make_aware(datetime.combine(saturday, time(10, 0))).isoformat(),
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
    from apps.wahub import course_tools, shadow_tools, trial_offer

    now = now or timezone.now()
    today = timezone.localtime(now).date()
    words: dict = {'young_child': YOUNG_CHILD}
    facts: dict = {}
    missing: dict = {}
    clocks = _clocks(now)
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
    # As the tools have it: a class with nothing to choose on the form is not offered, and so not asked about.
    courses = [course for course in courses if trial_offer.selections(course)]
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
        return {'words': words, 'facts': facts, 'missing': missing, 'clocks': clocks}
    stage = scored[0][1]
    stage_courses = by_branch[stage.id]
    alone = len(per_city.get(stage.city.name, [])) == 1 if stage.city_id else False
    # How a parent names the place: the city when it has one branch of ours, the branch itself otherwise.
    words['place'] = stage.city.name if alone else stage.name
    facts['stage'] = {
        'branch': stage.name, 'city': stage.city.name if stage.city_id else '',
        'fields': sorted({_field(course) for course in stage_courses}),
        'address': stage.address or '', 'phone': stage.phone or '',
        'directions': (stage.arrival_directions or '').strip(),
    }
    # The place as a parent in a hurry writes it: a letter wrong, or no space at all.
    words.update(place_typo=typo(words['place']), place_glued=glued(words['place']))
    if facts['stage']['directions']:
        words['way_place'] = words['place']
    else:
        missing['way_place'] = 'לסניף שנבחר לבדיקה לא כתוב במערכת "איך מגיעים"'

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
    words['field_typo'] = typo(facts['main']['field'])
    facts['stage']['place'] = words['place']
    # The child of the main class by his date of birth, when he is asked about by his age in years: a quarter past it.
    if main[1]['age'] is not None:
        born = today - timedelta(days=round((main[1]['age'] + 0.25) * 365.25))
        words['birth_date'] = born.strftime('%d.%m.%Y')
    else:
        missing['birth_date'] = 'החוג שנבחר לבדיקה הוא של כיתה, ולא של גיל בשנים — אין תאריך לידה לשאול לפיו'
    # A day of the week none of the classes this question is about meets on — Saturday, where it is free.
    taken = {lesson['day'] for course in facts['main']['same_question'] for lesson in course['lessons']}
    free_day = next((day for day in ['שבת'] + DAY_NAMES[:6] if day not in taken), '')
    if free_day:
        words['off_day'] = free_day
    else:
        missing['off_day'] = 'החוג שנבחר לבדיקה מתקיים בכל ימות השבוע — אין יום לשאול עליו בלי שיעור'
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

    # --- when a trial can be booked: what the form offers, at the clock the scripts are played on -------
    at = datetime.fromisoformat(clocks['open'])
    seats = trial_offer.Seats(stage_courses, now=at)
    offers = {course.id: trial_offer.for_course(course, seats) for course, _child_ in pairs}

    def with_trial(pair) -> dict:
        entry = describe(pair)
        entry['trial_dates'] = _trial_dates(offers[pair[0].id], at)
        return entry

    by_narrow = [main] + [pair for pair in sorted(pairs, key=narrow) if pair[0].id != main[0].id]
    # A class a trial can be booked in now — the main one when it is such.
    bookable = next((pair for pair in by_narrow if offers[pair[0].id].can_book), None)
    if bookable is not None:
        facts['trial'] = with_trial(bookable)
        words.update(trial_field=facts['trial']['field'], trial_child=facts['trial']['child'])
        if facts['trial']['lessons'] and facts['trial']['lessons'][0]['day']:
            words['trial_day'] = facts['trial']['lessons'][0]['day']
        else:
            missing['trial_day'] = 'לחוג שאפשר להירשם בו לשיעור ניסיון אין יום קבוע בשבוע'
    else:
        missing['trial_field'] = missing['trial_child'] = missing['trial_day'] = 'בסניף שנבחר לבדיקה אין כרגע חוג שאפשר להירשם בו לשיעור ניסיון'
    # A class one of whose coming dates is full for a trial.
    taken = next((pair for pair in by_narrow if offers[pair[0].id].full), None)
    if taken is not None:
        facts['full_trial'] = with_trial(taken)
        words.update(full_field=facts['full_trial']['field'], full_child=facts['full_trial']['child'])
    else:
        missing['full_field'] = missing['full_child'] = 'בסניף שנבחר לבדיקה אין כרגע חוג שאחד התאריכים הקרובים שלו לשיעור ניסיון מלא'
    # A class with a coming date the form skips: a day closed to trials, a cancelled lesson.
    skipped = next((
        pair for pair in by_narrow
        if any(skip.why != trial_offer.SKIP_STARTED for skip in offers[pair[0].id].skipped)
    ), None)
    if skipped is not None:
        facts['skipped_trial'] = with_trial(skipped)
        words.update(skipped_field=facts['skipped_trial']['field'], skipped_child=facts['skipped_trial']['child'])
    else:
        missing['skipped_field'] = missing['skipped_child'] = (
            'בסניף שנבחר לבדיקה אין כרגע חוג שתאריך קרוב שלו סגור לשיעור ניסיון (חופשה או שיעור שבוטל)'
        )

    # --- how full a lesson is: the form's own count, per lesson (trial_offer.Seats.cap → _lesson_widget_capacity) ---
    def day_of(lesson) -> str:
        return DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else ''

    def with_seats(pair, lesson) -> dict:
        """The class, and the lesson of it a script asks about: its day and hour, and its seats as they stood when the run began."""
        entry = describe(pair)
        cap = seats.cap(lesson, pair[0])
        entry.update(
            seat_day=day_of(lesson), seat_start=str(lesson.start_time)[:5],
            seats={'enrolled': cap['enrolled_count'], 'capacity': cap['capacity'], 'seats_left': cap['available_spots'], 'is_full': cap['is_full']},
        )
        return entry

    def a_lesson(holds):
        """The first class, alone in its question, with a lesson `holds` is true of — on a day no other lesson of that class has."""
        for pair in by_narrow:
            lessons = list(pair[0].lessons.all())
            if narrow(pair) != 1 or pair[0].must_attend_all_lessons:
                continue
            for lesson in lessons:
                if day_of(lesson) and sum(1 for other in lessons if day_of(other) == day_of(lesson)) == 1 and holds(seats.cap(lesson, pair[0])):
                    return pair, lesson
        return None

    main_lessons = list(main[0].lessons.all())
    facts['main'] = with_seats(main, main_lessons[0])
    words['main_day'] = facts['main']['seat_day']
    for key, holds, reason in (
        ('busy', lambda cap: cap['capacity'] is not None and cap['enrolled_count'] >= 1 and not cap['is_full'],
         'בסניף שנבחר לבדיקה אין כרגע שיעור שיש בו רשומים ועוד נשאר בו מקום'),
        ('packed', lambda cap: cap['is_full'], 'בסניף שנבחר לבדיקה אין כרגע שיעור מלא'),
        ('empty', lambda cap: cap['capacity'] is not None and cap['enrolled_count'] == 0, 'בסניף שנבחר לבדיקה אין כרגע שיעור שעוד אין בו רשומים'),
    ):
        found = a_lesson(holds)
        if found is not None:
            facts[key] = with_seats(*found)
            words.update({f'{key}_field': facts[key]['field'], f'{key}_child': facts[key]['child'], f'{key}_day': facts[key]['seat_day']})
        else:
            missing[f'{key}_field'] = missing[f'{key}_child'] = missing[f'{key}_day'] = reason
    # A class of several days, asked about one of them: the day with the most children, and another day of it.
    several = None
    for pair in by_narrow:
        lessons = [lesson for lesson in pair[0].lessons.all() if day_of(lesson)]
        if narrow(pair) == 1 and not pair[0].must_attend_all_lessons and len({day_of(lesson) for lesson in lessons}) == len(lessons) > 1:
            asked = max(lessons, key=lambda lesson: seats.cap(lesson, pair[0])['enrolled_count'])
            other = next(lesson for lesson in lessons if lesson.id != asked.id)
            several = (pair, asked, other)
            break
    if several is not None:
        facts['two_day'] = with_seats(several[0], several[1])
        facts['two_day']['other_day'] = day_of(several[2])
        words.update(
            two_day_field=facts['two_day']['field'], two_day_child=facts['two_day']['child'],
            two_day_day=facts['two_day']['seat_day'], two_day_other=facts['two_day']['other_day'],
        )
    else:
        missing['two_day_field'] = missing['two_day_child'] = missing['two_day_day'] = missing['two_day_other'] = (
            'בסניף שנבחר לבדיקה אין חוג שמתקיים בכמה ימים שאפשר לבחור ביניהם'
        )

    # Another child of the same family: the same field, another age group.
    sibling = next((pair for pair in pairs if _field(pair[0]) == facts['main']['field'] and pair[1]['code'] != main[1]['code']), None)
    if sibling is not None:
        facts['sibling'] = describe(sibling)
        words['sibling_child'] = facts['sibling']['child']
    else:
        missing['sibling_child'] = 'בסניף שנבחר לבדיקה אין לתחום שנבחר חוג לקבוצת גיל נוספת'

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
        facts['stage']['other_field'] = other_field

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
        # The two groups that meet at six, each as the question of its own child: the one still in גן, the one in כיתה א.
        for key, code in (('young', age_groups.KINDERGARTEN), ('old', age_groups.FIRST_GRADE)):
            child = _child(code)
            group = [course for course in stage_courses if _field(course) == boundary and age_groups.fits(course.min_age, course.max_age, {code})]
            if group:
                facts.setdefault('boundary', {})[key] = {
                    'field': boundary, 'child': child['words'], 'same_question': [_course_facts(course, child) for course in group],
                }
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

    # --- the network of a branch: one of ours whose card holds the name and the password, and one whose card holds neither ---
    def named(branch) -> str:
        """How a parent names the branch: by its city when it is our only one there, by its own name otherwise."""
        return branch.city.name if branch.city_id and len(per_city.get(branch.city.name, [])) == 1 else branch.name

    with_courses = [stage] + [row for row in ours if row.id in by_branch and row.id != stage.id]
    online = next((row for row in with_courses if (row.wifi_name or '').strip() and (row.wifi_code or '').strip()), None)
    if online is not None:
        words['wifi_place'] = named(online)
        facts['wifi'] = {'branch': online.name, 'name': online.wifi_name.strip(), 'code': online.wifi_code.strip()}
    else:
        missing['wifi_place'] = 'אין במערכת סניף שלנו שרשומים בכרטיס שלו שם הרשת והסיסמה'
    offline = next((row for row in with_courses if not (row.wifi_name or '').strip() and not (row.wifi_code or '').strip()), None)
    if offline is not None:
        words['no_wifi_place'] = named(offline)
        facts['no_wifi'] = {'branch': offline.name}
    else:
        missing['no_wifi_place'] = 'לכל הסניפים שלנו רשומים שם רשת וסיסמה — אין סניף לשאול עליו בלי'

    # A city as parents shorten it ("פ"ת"), when we have a branch there.
    short = next(((city, word) for city, word in SHORT_CITIES if city in per_city), None)
    if short is not None:
        words['short_city'] = short[1]
        facts['short_city'] = {'city': short[0], 'word': short[1], 'several': len(per_city[short[0]]) > 1}
    else:
        missing['short_city'] = 'אין לנו סניף בעיר שנוהגים לקצר את שמה'

    facts['cities'] = sorted({row.city.name for row in branches if row.city_id})
    return {'words': words, 'facts': facts, 'missing': missing, 'clocks': clocks}


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
