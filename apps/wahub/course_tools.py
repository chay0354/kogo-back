"""
What the bot may know about a class (docs/WAHUB-CONTRACT-BOT-TOOLS.md).

The owner (11.10.2026), after the bot told a parent the trial lesson in ראש העין
costs nothing: "I want the bot to have access to everything about the lessons —
how many pupils, times, teachers, dates, costs, what the lesson is, how much it
is if they register now, the registration fee, discounts, the limit of a lesson.
All of the system except money, for example what a teacher earns."

So this reads a class the way the registration form shows it, with the form's
own functions (apps/customers/widget_views.py for the catalogue, the seats and
the dates; apps/core/registration_quote.py for "how much if I register now"),
and it reads nothing else: no salary, no bonus, nothing a family paid, no
invoice, no card. Every function here only reads.

`Course.min_age` / `max_age` are positions on the age-group scale, not years
(apps/courses/age_groups.py). The bot is told a child's age in years, so every
comparison goes through that scale, and a class is described by its group in
words ("גיל 4.5-6", "כיתות ג–ד") with the years beside it.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone

from apps.courses import age_groups

DAY_NAMES = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
MONTH_NAMES = ['ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']
STATUS_LABELS = {'scheduled': 'מתוכנן', 'completed': 'הושלם', 'cancelled': 'בוטל'}

CARD_DATES = 3          # the dates the form's trial picker offers
LIST_DATES = 2
MAX_INSTRUCTORS = 5
MAX_INSTRUCTOR_NAMES = 40
SHORT_DESCRIPTION = 280

TRIAL_FREE = 'שיעור ניסיון ללא עלות'
TRIAL_NO_PRICE = 'שיעור הניסיון בתשלום, והמחיר לא מוגדר במערכת — לבדוק במשרד, לא לומר שהוא ללא עלות'
TRIAL_PER_COURSE = (
    'שיעור ניסיון נקבע לכל חוג בנפרד. עונים רק על החוג שמתאים לגיל ולתחום של הילד; '
    'לא אומרים "הניסיון בסניף הזה ללא עלות" או "בתשלום" על הסניף כולו'
)
REGISTRATION_FEE_NOTE = 'דמי רישום נגבים פעם אחת לכל ילד, בהרשמה הראשונה שלו; ילד שכבר משלם על חוג לא משלם אותם שוב'
HIDDEN_NOTE = 'החוג לא מוצג בהרשמה באתר, ואי אפשר להירשם אליו משם. לא מציעים אותו; מפנים למשרד'
NOT_OPEN = 'חוג זה אינו פתוח להרשמה'


def today_of(ctx) -> date:
    """The Israeli calendar day of the tool run — the pretend clock of "נסה שאלה" when there is one."""
    from apps.core.payment_service import JERUSALEM_TZ

    now = getattr(ctx, 'now', None) or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, JERUSALEM_TZ)
    return now.astimezone(JERUSALEM_TZ).date()


def _money(value) -> str | None:
    return None if value is None else f'{Decimal(str(value)).quantize(Decimal("0.01"))}'


def _shekels(value) -> str:
    from apps.customers.trial_credit import money, shekels

    return f'₪{shekels(money(value))}'


def _day(value: date) -> str:
    return value.strftime('%d.%m.%Y')


def _short(text, limit: int) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


# --- the catalogue, read once ----------------------------------------------------------------------

def with_lessons(queryset):
    """Courses with their lessons and tracks as the form's catalogue loads them."""
    from django.db.models import Prefetch

    from apps.customers.widget_views import _widget_bundles_queryset, _widget_lesson_queryset

    return (
        queryset
        .select_related('course_type', 'branch', 'branch__city')
        .prefetch_related(
            Prefetch('lessons', queryset=_widget_lesson_queryset()),
            Prefetch(
                'lesson_bundles',
                queryset=_widget_bundles_queryset().prefetch_related(
                    Prefetch('lessons', queryset=_widget_lesson_queryset()),
                ),
            ),
        )
    )


class Seats:
    """Who sits where, for a set of courses: the form's own counts, fetched once for all their lessons."""

    def __init__(self, courses):
        from apps.customers.widget_views import (
            _batch_occurrence_context,
            _batch_paying_enrollment_counts,
            _batch_upcoming_trial_counts,
        )
        from apps.enrollments.trial_policy import trials_open_by_default

        lesson_ids = [lesson.id for course in courses for lesson in course.lessons.all()]
        self.enrolled = _batch_paying_enrollment_counts(lesson_ids)
        self.trials = _batch_upcoming_trial_counts(lesson_ids)
        self.occurrences = _batch_occurrence_context(lesson_ids)
        self.trials_default = trials_open_by_default()


# --- one lesson, one class -----------------------------------------------------------------------------

def lesson_payload(lesson, course, seats: Seats, *, dates: int, detail: bool = False) -> dict:
    """A weekly lesson: when, with whom, how full, and its coming dates."""
    from apps.customers.widget_views import _lesson_widget_capacity, trial_seats_by_date
    from apps.enrollments.trial_policy import trial_registration_open_for

    cap = _lesson_widget_capacity(lesson, course, seats.enrolled, seats.trials, seats.occurrences)
    cancelled, blocked = seats.occurrences
    coming = trial_seats_by_date(
        lesson, cap['capacity'], cap['enrolled_count'], seats.trials.get(lesson.id, {}),
        cancelled=cancelled.get(lesson.id, set()), blocked=blocked.get(lesson.id), count=dates,
    )
    payload = {
        'id': str(lesson.id),
        'day': DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
        'start': str(lesson.start_time)[:5],
        'end': str(lesson.end_time)[:5],
        'instructor': lesson.instructor.full_name if lesson.instructor_id else '',
        'capacity': cap['capacity'],
        'enrolled': cap['enrolled_count'],
        'seats_left': cap['available_spots'],
        'is_full': cap['is_full'],
        'trial_open': trial_registration_open_for(lesson, default=seats.trials_default),
        'trial_is_full': cap['trial_is_full'],
        'trial_seats_left': cap['trial_spots_left'],
        'next_dates': [{'date': _day(day), 'trial_seats_left': left} for day, left in coming],
    }
    if lesson.status != 'scheduled' or detail:
        payload['status'] = STATUS_LABELS.get(lesson.status, lesson.status)
    if not lesson.is_recurring:
        payload['one_time_date'] = _day(lesson.lesson_date) if lesson.lesson_date else ''
    if detail:
        payload['room'] = (lesson.room.name if lesson.room_id else '') or lesson.room_text or ''
        payload['price_options'] = [
            {
                'id': str(option.id),
                'title': option.display_title,
                'price_monthly': _money(option.monthly_price),
                'ages': age_groups.range_label(option.min_age, option.max_age),
            }
            for option in lesson.price_options.all()
        ]
        second = _second_class_price(lesson, course)
        if second is not None:
            payload['price_as_second_class'] = _money(second)
    return payload


def _second_class_price(lesson, course):
    """What the lesson costs a child who already has another class, when the lesson has a price of its own for that."""
    from apps.core.payment_service import lesson_price_for_index

    price, tiered = lesson_price_for_index(lesson, 2)
    return price if tiered else None


def trial_payload(course, lessons: list) -> dict:
    """
    The trial lesson of a class as the form shows it: paid or not and for how
    much come from the course's own card (`trial_lesson_is_paid`,
    `trial_lesson_price`) and from nowhere else — the form reads the same two
    fields (widget_views.WidgetTrialRegisterView, the catalogue payload).
    """
    from apps.customers.trial_credit import credit_rule

    is_paid = bool(course.trial_lesson_is_paid)
    price = course.trial_lesson_price
    priced = is_paid and price is not None and price > 0
    if not is_paid:
        text = TRIAL_FREE
    elif priced:
        text = f'שיעור ניסיון בעלות {_shekels(price)}'
    else:
        text = TRIAL_NO_PRICE
    is_open = any(lesson['trial_open'] for lesson in lessons)
    return {
        'is_paid': is_paid,
        'price': _money(price) if priced else None,
        'text': text,
        # What the form tells a parent before a paid trial: the price comes off the first payment.
        'credit_rule': credit_rule(price) if priced else '',
        # "מה להביא" lives on the course TYPE, the text the trial's calendar event sends.
        'bring': ((course.course_type.trial_bring_note if course.course_type_id else '') or '').strip(),
        'open': is_open,
        'full': bool(is_open and all(lesson['trial_is_full'] for lesson in lessons if lesson['trial_open'])),
    }


def track_payloads(course) -> list:
    """The combined tracks (twice a week and the like) the form offers for the class, with their monthly price."""
    from apps.core.payment_service import bundle_monthly_price
    from apps.courses.bundles import catalog_bundles_for_course

    tracks = []
    for bundle in catalog_bundles_for_course(course):
        members = list(bundle.lessons.all())
        tracks.append({
            'id': str(bundle.id),
            'name': bundle.name or 'מסלול משולב',
            'price_monthly': _money(bundle_monthly_price(bundle)),
            'lessons': [
                f'{DAY_NAMES[member.day_of_week]} {str(member.start_time)[:5]}–{str(member.end_time)[:5]}'
                for member in members if 0 <= member.day_of_week < 7
            ],
            'ages': age_groups.range_label(bundle.min_age, bundle.max_age),
        })
    return tracks


def course_payload(course, seats: Seats, *, detail: bool = False) -> dict:
    """A class as the bot may describe it. `detail` adds what only the card of one class carries."""
    from apps.core.payment_service import registration_fee_amount

    lessons = [
        lesson_payload(lesson, course, seats, dates=CARD_DATES if detail else LIST_DATES, detail=detail)
        for lesson in course.lessons.all()
    ]
    youngest, oldest = age_groups.years_range(course.min_age, course.max_age)
    description = course.description or (course.course_type.description if course.course_type_id else '') or ''
    payload = {
        'id': str(course.id),
        'name': course.name,
        'course_type': course.course_type.name if course.course_type_id else '',
        'branch': course.branch.name,
        'city': course.branch.city.name if course.branch.city_id else '',
        'description': description.strip() if detail else _short(description, SHORT_DESCRIPTION),
        # The age group in words, and the same in years. Never the stored position.
        'ages': age_groups.range_label(course.min_age, course.max_age),
        'min_age': youngest,
        'max_age': oldest,
        'is_adult': course.is_adult,
        'must_attend_all_lessons': course.must_attend_all_lessons,
        'price_monthly': _money(course.price),
        'registration_fee': _money(registration_fee_amount(course)),
        'trial': trial_payload(course, lessons),
        'external_link': course.external_link or '',
        'lessons': lessons,
        'tracks': track_payloads(course),
    }
    if detail:
        payload['course_type_description'] = (course.course_type.description if course.course_type_id else '') or ''
        payload['registration_fee_note'] = REGISTRATION_FEE_NOTE
        payload['standing_order_starts_at_once'] = course.charge_standing_order_immediately
        payload['show_in_widget'] = course.show_in_widget
        if not course.show_in_widget:
            payload['note'] = HIDDEN_NOTE
    return payload


def brief_course(course) -> dict:
    """A class in one line, for the ones a long answer does not spell out — its trial and price are still said."""
    priced = course.trial_lesson_is_paid and course.trial_lesson_price is not None and course.trial_lesson_price > 0
    return {
        'id': str(course.id),
        'name': course.name,
        'course_type': course.course_type.name if course.course_type_id else '',
        'branch': course.branch.name,
        'ages': age_groups.range_label(course.min_age, course.max_age),
        'price_monthly': _money(course.price),
        'trial_is_paid': bool(course.trial_lesson_is_paid),
        'trial_price': _money(course.trial_lesson_price) if priced else None,
    }


def trial_summary(courses) -> dict:
    """Whether the trial is one answer for all these classes or not — said outright, so one class is never read as the branch."""
    paid = [course for course in courses if course.trial_lesson_is_paid]
    free = [course for course in courses if not course.trial_lesson_is_paid]
    prices = sorted({
        Decimal(str(course.trial_lesson_price)) for course in paid
        if course.trial_lesson_price is not None and course.trial_lesson_price > 0
    })
    priced = ', '.join(_shekels(price) for price in prices)
    if paid and free:
        text = (
            f'שיעור הניסיון שונה מחוג לחוג: בתשלום ב-{len(paid)} חוגים{f" ({priced})" if priced else ""}, '
            f'ללא עלות ב-{len(free)}. {TRIAL_PER_COURSE}'
        )
    elif paid:
        text = f'בכל {len(paid)} החוגים שנמצאו שיעור הניסיון בתשלום{f" ({priced})" if priced else ""}'
    elif free:
        text = f'בכל {len(free)} החוגים שנמצאו שיעור הניסיון ללא עלות'
    else:
        text = ''
    return {'paid_courses': len(paid), 'free_courses': len(free), 'mixed': bool(paid and free), 'text': text}


def trial_line(courses: list) -> str:
    """One line about the trial of the classes an answer lists — each class by its own card."""
    texts = [course['trial']['text'] for course in courses]
    if not texts:
        return ''
    if len(set(texts)) == 1:
        return 'שיעור הניסיון בתשלום, את המחיר כדאי לבדוק במשרד' if texts[0] == TRIAL_NO_PRICE else texts[0]
    parts = []
    for course in courses:
        trial = course['trial']
        if not trial['is_paid']:
            what = 'ללא עלות'
        elif trial['price']:
            what = f'בעלות {_shekels(trial["price"])}'
        else:
            what = 'בתשלום (את המחיר כדאי לבדוק במשרד)'
        parts.append(f'{course["name"]} {what}')
    return 'שיעור ניסיון: ' + ', '.join(parts)


# --- discounts as the CRM configured them -------------------------------------------------------------

def _discount_value(row) -> str:
    if row.discount_type == 'percentage':
        return f'{row.value.normalize():f}% הנחה מהמחיר החודשי'
    if row.discount_type == 'fixed_final_price':
        return f'מחיר חודשי קבוע {_shekels(row.value)} (כשהוא נמוך ממחיר החוג)'
    return f'{_shekels(row.value)} הנחה בחודש'


def discounts_available(today: date) -> list:
    """
    The discounts a registration applies by itself, in words, with the amounts
    as the CRM holds them. A built-in discount still at 0 is one nobody
    configured (discount_service.py), so it is not listed — and none is invented.
    """
    from apps.customers.discount_service import DiscountService
    from apps.customers.financial_models import Discount

    rows = []
    sibling = DiscountService.built_in_discount(DiscountService.SECOND_CHILD_IDENTIFIER)
    if sibling is not None:
        rows.append({
            'name': sibling.name,
            'rule': 'ילד שני ומעלה במשפחה, כשאח או אחות כבר רשומים לחוג. מתעדכן לבד בהרשמה באתר',
            'value': _discount_value(sibling),
        })
    another = DiscountService.built_in_discount(DiscountService.ADDITIONAL_LESSON_IDENTIFIER)
    if another is not None:
        rows.append({
            'name': another.name,
            'rule': 'חוג נוסף לאותו ילד, מהחוג השני שלו. מתעדכן לבד בהרשמה באתר',
            'value': _discount_value(another),
        })
    early = Discount.objects.filter(
        is_active=True, is_built_in=True, value__gt=0, end_date__gte=today,
        name__contains=DiscountService.EARLY_SIGNUP_IDENTIFIER,
    ).order_by('start_date')
    for row in early:
        if row.start_date is None:
            continue
        rows.append({
            'name': row.name,
            'rule': f'למי שנרשם ומשלם בין {_day(row.start_date)} ל-{_day(row.end_date)}',
            'value': _discount_value(row),
            'active_now': row.start_date <= today,
        })
    return rows


# --- the card of one class ---------------------------------------------------------------------------

def _uuid(value) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        return ''


def _external_answer(course) -> dict:
    from apps.wahub import shadow_tools

    return {
        'found': True,
        'is_external': True,
        'course': {
            'id': str(course.id), 'name': course.name,
            'course_type': course.course_type.name if course.course_type_id else '',
            'external_link': course.external_link or course.branch.external_link or '',
        },
        'branch': shadow_tools._branch_payload(course.branch),
        'note': shadow_tools.EXTERNAL_NOTE,
    }


def _match_row(course) -> dict:
    """One of several classes a name fits. A class of an external branch is named and nothing more."""
    if course.branch.is_external:
        return {
            'id': str(course.id), 'name': course.name, 'branch': course.branch.name,
            'course_type': course.course_type.name if course.course_type_id else '', 'is_external': True,
        }
    return brief_course(course)


def _courses_by_name(ctx, *, name: str, branch: str, city: str) -> list:
    from apps.courses.models import Course
    from apps.wahub import shadow_tools

    queryset = Course.objects.filter(is_active=True, branch__is_active=True).select_related('course_type', 'branch', 'branch__city')
    if branch or city:
        branches = shadow_tools.resolve_branches(ctx, city=city, branch=branch)
        if not branches:
            return []
        queryset = queryset.filter(branch_id__in=[row.id for row in branches])
    wanted = shadow_tools._norm(name)
    rows = [
        course for course in queryset.order_by('branch__name', 'course_type__name', 'name')
        if wanted and (wanted in shadow_tools._norm(course.name) or shadow_tools._norm(course.name) in wanted)
    ]
    exact = [course for course in rows if shadow_tools._norm(course.name) == wanted]
    return exact or rows


def course_card(ctx, *, course_id: str = '', name: str = '', branch: str = '', city: str = '') -> dict:
    """Everything the system holds about one class that a parent may hear."""
    from apps.courses.models import Course
    from apps.wahub import shadow_tools

    wanted_id = _uuid(course_id)
    if course_id and not wanted_id:
        return {'found': False, 'hint': 'course_id לא תקין — לקחת אותו מ-find_courses.'}
    if wanted_id:
        course = Course.objects.filter(id=wanted_id, is_active=True, branch__is_active=True).select_related('branch').first()
        matches = [course] if course is not None else []
    elif name.strip():
        matches = _courses_by_name(ctx, name=name, branch=branch, city=city)
    else:
        return {'found': False, 'hint': 'צריך course_id (מתוך find_courses), או שם חוג וסניף.'}
    if not matches:
        return {'found': False, 'hint': 'לא נמצא חוג כזה במערכת. אפשר לחפש אותו ב-find_courses לפי עיר, גיל ותחום.'}
    if len(matches) > 1:
        return {
            'found': False,
            'matches': [_match_row(course) for course in matches[:shadow_tools.MAX_COURSES]],
            'hint': 'יש כמה חוגים בשם הזה — לבחור אחד ולקרוא שוב עם ה-course_id שלו.',
        }
    course = with_lessons(Course.objects.filter(id=matches[0].id)).get()
    if course.branch.is_external:
        return _external_answer(course)
    payload = course_payload(course, Seats([course]), detail=True)
    return {
        'found': True,
        'course': payload,
        'branch': shadow_tools._branch_payload(course.branch),
        'discounts_available': discounts_available(today_of(ctx)),
        'note': TRIAL_PER_COURSE,
    }


# --- "how much if I register now" ---------------------------------------------------------------------

def _parse_day(value, fallback: date) -> date:
    text = str(value or '').strip()
    for pattern in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return fallback


def _contact_children(ctx) -> list:
    """One child per family behind the contact — enough to ask the family's own questions."""
    contact = getattr(ctx, 'contact', None)
    ids = list(getattr(contact, 'kogo_child_ids', None) or []) if contact is not None else []
    if not ids:
        return []
    from apps.customers.models import Child

    seen, children = set(), []
    for child in Child.objects.filter(id__in=ids).select_related('family'):
        if child.family_id and child.family_id not in seen:
            seen.add(child.family_id)
            children.append(child)
    return children


def _sibling_on_a_team(children, *, besides_the_child: bool) -> bool:
    """
    Whether the "ילד שני" discount would apply to a child of the contact's
    family, by the registration's own check. When the child asked about is
    one who is already on a team (a second class for the same child), that
    child is not their own sibling: a second child on a team is needed.
    """
    from apps.customers.discount_service import DiscountService
    from apps.enrollments.models import LessonEnrollment

    for child in children:
        if DiscountService().check_second_child_discount(str(child.family_id)) is None:
            continue
        if not besides_the_child:
            return True
        on_teams = LessonEnrollment.objects.filter(
            child__family_id=child.family_id, status='active', trial_lesson_date__isnull=True,
        ).values('child_id').distinct().count()
        if on_teams >= 2:
            return True
    return False


def quote_registration(
    ctx, *, course_id: str = '', lesson_id: str = '', track_id: str = '', start_date: str = '',
    child_age=None, grade: str = '', siblings_registered=None, took_paid_trial=None,
    another_class_same_child=None,
) -> dict:
    """
    The first charge and the monthly price after it, were the child registered
    on `start_date` (today when empty) — worked out by apps/core/registration_quote.py
    from the pieces the registration itself charges with.
    """
    from apps.core import registration_quote
    from apps.core.payment_service import registration_fee_amount
    from apps.courses.bundles import catalog_bundles_for_course, resolve_registration_bundle
    from apps.courses.models import Course
    from apps.customers.trial_credit import TRIAL_CREDIT_WINDOW_DAYS, quote_trial_credit
    from apps.wahub import shadow_tools

    wanted_id = _uuid(course_id)
    if not wanted_id:
        return {'can_quote': False, 'reason': 'צריך course_id של החוג (מתוך find_courses).'}
    course = with_lessons(Course.objects.filter(id=wanted_id, is_active=True, branch__is_active=True)).first()
    if course is None:
        return {'can_quote': False, 'reason': 'חוג לא נמצא'}
    if course.branch.is_external:
        return {'can_quote': False, 'is_external': True, 'reason': shadow_tools.EXTERNAL_NOTE,
                'external_link': course.external_link or course.branch.external_link or ''}
    if not course.show_in_widget:
        return {'can_quote': False, 'reason': NOT_OPEN, 'note': HIDDEN_NOTE}
    lessons = list(course.lessons.all())
    if not lessons:
        return {'can_quote': False, 'reason': 'לא נמצאו שיעורים לחוג זה'}

    # The lesson and the track, chosen as the registration chooses them.
    lesson = lessons[0]
    if str(lesson_id or '').strip():
        lesson = next((row for row in lessons if str(row.id) == _uuid(lesson_id)), None)
        if lesson is None:
            return {'can_quote': False, 'reason': 'שיעור לא נמצא'}
    bundle = None
    if str(track_id or '').strip():
        bundle = resolve_registration_bundle(course=course, bundle_id=_uuid(track_id) or str(track_id))
        if bundle is None:
            return {'can_quote': False, 'reason': 'מסלול משולב לא נמצא'}
    elif course.must_attend_all_lessons:
        tracks = catalog_bundles_for_course(course)
        bundle = next((row for row in tracks if lesson in list(row.lessons.all())), None) or (tracks[0] if tracks else None)
    if bundle is not None:
        members = list(bundle.lessons.all())
        lesson = lesson if lesson in members else members[0]

    notes = []
    today = today_of(ctx)
    as_of = max(_parse_day(start_date, today), today)

    # What is known about the family: told by the caller, or read from the contact's own card.
    children = _contact_children(ctx)
    another_class = bool(another_class_same_child)
    if siblings_registered is None:
        sibling = _sibling_on_a_team(children, besides_the_child=another_class)
        if sibling:
            notes.append('לפי כרטיס הלקוח יש במשפחה ילד שרשום לחוג — חושבה הנחת ילד שני')
    else:
        sibling = bool(siblings_registered)

    trial_reason = {}
    credit_for, paid_trial = None, None
    if took_paid_trial is not False and children:
        def credit_for(first_charge):
            for child in children:
                found = quote_trial_credit(child, branch_id=course.branch_id, first_charge=first_charge, today=as_of)
                if found['amount'] > 0:
                    trial_reason['text'] = found['reason']
                    return found['amount']
            return Decimal('0.00')
    elif took_paid_trial:
        priced = course.trial_lesson_is_paid and course.trial_lesson_price is not None and course.trial_lesson_price > 0
        if priced:
            paid_trial = course.trial_lesson_price
        else:
            notes.append('שיעור הניסיון של החוג הזה אינו בתשלום, ולכן אין מה לקזז')

    try:
        figures = registration_quote.quote(
            lesson, today=as_of, bundle=bundle, credit_for=credit_for,
            facts=registration_quote.Facts(sibling_registered=sibling, another_class=another_class, paid_trial=paid_trial),
        )
    except ValueError:
        return {'can_quote': False, 'reason': 'לחוג הזה לא מוגדר מחיר במערכת — לבדוק במשרד'}

    credit = figures['trial_credit_amount']
    if credit > 0:
        if trial_reason.get('text'):
            notes.append(trial_reason['text'])
        else:
            notes.append(
                f'הקיזוז מחושב בהנחה ששולם {_shekels(paid_trial)} על שיעור ניסיון באותו סניף ב-{TRIAL_CREDIT_WINDOW_DAYS} '
                'הימים האחרונים ושהוא עוד לא קוזז; הסכום הסופי מוצג באתר לפני התשלום'
            )
    elif took_paid_trial and children:
        notes.append(
            f'לא נמצא בכרטיס הלקוח שיעור ניסיון בתשלום שעוד אפשר לקזז (אותו סניף, {TRIAL_CREDIT_WINDOW_DAYS} יום, לא קוזז כבר) — לבדוק במשרד'
        )

    # A fact that changed nothing is said, so that "you have a brother here" is not heard as a discount.
    applied = {row['type'] for row in figures['discounts_applied']}
    if sibling and not applied & {'second_child', 'fixed_final_price'}:
        notes.append('במערכת לא מוגדרת כרגע הנחת ילד שני שחלה כאן — לא מבטיחים הנחה כזאת')
    if another_class and 'additional_lesson' not in applied and figures['base_amount'] == course.price and bundle is None:
        notes.append('במערכת לא מוגדרים כרגע הנחה או מחיר מיוחד לחוג נוסף שחלים כאן — לא מבטיחים הנחה כזאת')

    breakdown = []
    remaining, total = figures['prorate_lessons_remaining'], figures['total_lessons_this_month']
    month = MONTH_NAMES[as_of.month - 1]
    if figures['deferred_charge_date']:
        notes.append(f'החיוב החודשי מתחיל ב-{_day(figures["deferred_charge_date"])}; בהרשמה נגבים דמי הרישום בלבד')
    elif figures['monthly_amount'] <= 0:
        pass
    elif total and remaining >= total:
        breakdown.append({'label': f'חודש {month} — חודש מלא', 'amount': _money(figures['prorated_amount'])})
    else:
        label = f'חודש {month} — חלק יחסי: {remaining} מתוך {total} השיעורים של החודש עוד לפנינו'
        if remaining == 0:
            label = f'חודש {month} — לא נשארו שיעורים החודש (חיוב מינימלי)'
        breakdown.append({'label': label, 'amount': _money(figures['prorated_amount'])})
    if figures['registration_fee'] > 0:
        breakdown.append({'label': 'דמי רישום (פעם אחת לכל ילד)', 'amount': _money(figures['registration_fee'])})
    elif another_class and registration_fee_amount(course) > 0:
        notes.append('דמי רישום לא נגבים שוב מילד שכבר משלם על חוג')
    if credit > 0:
        breakdown.append({'label': 'קיזוז שיעור ניסיון ששולם', 'amount': _money(-credit)})

    seats = Seats([course])
    members = list(bundle.lessons.all()) if bundle is not None else [lesson]
    lesson_rows = [lesson_payload(row, course, seats, dates=1) for row in members]
    if any(row['is_full'] for row in lesson_rows):
        notes.append('השיעור מלא כרגע — אי אפשר להירשם אליו באתר; אפשר להציע מועד אחר או להפנות למשרד')
    grade_codes = age_groups.codes_for_grade(grade)
    if grade_codes or child_age not in (None, ''):
        codes = grade_codes or age_groups.codes_for_age(child_age)
        if not age_groups.fits(course.min_age, course.max_age, codes):
            notes.append(f'החוג מיועד ל{age_groups.range_label(course.min_age, course.max_age)} — הגיל שנמסר לא בטווח')
    if as_of != today:
        notes.append(f'החישוב הוא להרשמה ב-{_day(as_of)}; החלק היחסי וההנחות נקבעים לפי יום ההרשמה בפועל')
    if course.charge_standing_order_immediately:
        notes.append('בחוג הזה הוראת הקבע מחויבת מיד')
    notes.append('ההרשמה והתשלום באתר; הסכום הסופי מוצג שם לפני התשלום')

    return {
        'can_quote': True,
        'course': {
            'id': str(course.id), 'name': course.name, 'branch': course.branch.name,
            'ages': age_groups.range_label(course.min_age, course.max_age),
        },
        'track': ({'id': str(bundle.id), 'name': bundle.name or 'מסלול משולב'} if bundle is not None else None),
        'lessons': [{'id': row['id'], 'day': row['day'], 'start': row['start'], 'end': row['end']} for row in lesson_rows],
        'as_of': _day(as_of),
        'based_on': {
            'siblings_registered': sibling,
            'another_class_same_child': another_class,
            'took_paid_trial': credit > 0,
        },
        'first_charge': {'amount': _money(figures['final_amount']), 'breakdown': breakdown},
        'monthly_after': {
            'amount': _money(figures['monthly_amount']),
            'from': _day(figures['next_billing_date']),
            'price_before_discounts': _money(figures['base_amount']),
            'discounts': [
                {'name': row['name'], 'amount': _money(row['value']), 'reason': row['reason']}
                for row in figures['discounts_applied']
            ],
        },
        'notes': notes,
    }


# --- an instructor ---------------------------------------------------------------------------------

def instructor_info(ctx, *, name: str = '', branch: str = '') -> dict:
    """
    Who teaches: the fields, the branches and the lessons, with the picture the
    form shows. Nothing personal — no phone, no address, and nothing of pay.
    """
    from apps.courses.models import Lesson
    from apps.wahub import shadow_tools

    lessons = Lesson.objects.filter(
        instructor__isnull=False, instructor__is_active=True,
        course__is_active=True, course__show_in_widget=True,
        course__branch__is_active=True, course__branch__is_external=False,
    ).select_related('instructor', 'course', 'course__course_type', 'course__branch').order_by('day_of_week', 'start_time')
    if branch.strip():
        branches = shadow_tools.resolve_branches(ctx, branch=branch) or shadow_tools.resolve_branches(ctx, city=branch)
        if not branches:
            return {'instructors': [], 'hint': 'לא נמצא סניף כזה במערכת.'}
        lessons = lessons.filter(course__branch_id__in=[row.id for row in branches])

    wanted = [shadow_tools._norm(part) for part in re.split(r'\s+', name.strip()) if len(part) >= 2]
    by_instructor: dict = {}
    for lesson in lessons:
        instructor = lesson.instructor
        full_name = shadow_tools._norm(instructor.full_name)
        if wanted and not all(part in full_name for part in wanted):
            continue
        by_instructor.setdefault(instructor.id, (instructor, []))[1].append(lesson)
    if not by_instructor:
        return {'instructors': [], 'hint': 'לא נמצא מדריך כזה בחוגים הפעילים. לא ממציאים; אפשר לבדוק במשרד.'}

    rows = []
    for instructor, taught in sorted(by_instructor.values(), key=lambda pair: pair[0].full_name):
        fields = sorted({lesson.course.course_type.name for lesson in taught if lesson.course.course_type_id})
        row = {
            'name': instructor.full_name,
            'specialization': instructor.specialization or '',
            'course_types': fields,
            'branches': sorted({lesson.course.branch.name for lesson in taught}),
            'photo_url': instructor.photo_url or '',
        }
        if wanted:
            row['lessons'] = [
                {
                    'course_id': str(lesson.course_id),
                    'course': lesson.course.name,
                    'branch': lesson.course.branch.name,
                    'ages': age_groups.range_label(lesson.course.min_age, lesson.course.max_age),
                    'day': DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
                    'start': str(lesson.start_time)[:5],
                    'end': str(lesson.end_time)[:5],
                }
                for lesson in taught
            ]
        rows.append(row)
    shown = rows[:MAX_INSTRUCTORS] if wanted else rows[:MAX_INSTRUCTOR_NAMES]
    result = {'instructors': shown, 'instructors_total': len(rows)}
    if not wanted:
        result['hint'] = 'רשימת המדריכים בלי מועדים; למועדים של מדריך אחד לקרוא שוב עם השם שלו.'
    return result


# --- what a branch has -----------------------------------------------------------------------------

def branch_extras(branch) -> dict:
    """The fields taught in a branch and the days its classes run — from the timetable the form shows."""
    from apps.courses.models import Lesson
    from apps.customers.widget_views import _widget_catalog_courses

    courses = _widget_catalog_courses().filter(branch=branch)
    fields = sorted({
        name for name in courses.filter(course_type__is_active=True).values_list('course_type__name', flat=True) if name
    })
    days: dict = {}
    for day, start, end in Lesson.objects.filter(course__in=courses).values_list('day_of_week', 'start_time', 'end_time'):
        first, last = days.get(day, (start, end))
        days[day] = (min(first, start), max(last, end))
    return {
        'course_types': fields,
        # When classes run here. Not reception hours: the system holds none for a branch.
        'class_days': [
            {'day': DAY_NAMES[day], 'from': str(first)[:5], 'to': str(last)[:5]}
            for day, (first, last) in sorted(days.items()) if 0 <= day < 7
        ],
    }
