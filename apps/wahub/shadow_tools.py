"""
The tools the shadow bot may call (docs/WAHUB-CONTRACT-STAGE2.md, ב.2).

Every tool reads Kogo in process — the same functions the public widget uses
for classes, dates, prices and seats (apps/customers/widget_views.py) — so the
bot's answer about a class is the widget's answer. Nothing here writes, and
nothing here sends: `request_human` only marks the draft.

The same five tools serve Claude (as tool definitions) and the stub (called
directly), so a stub answer is built from the very data Claude would get.
"""
from __future__ import annotations

import re

from django.conf import settings

from apps.wahub import analysis, knowledge, matching
from apps.wahub.analysis import _same_city
from apps.wahub.models import KIND_ALIAS, SCOPE_BRANCH, Contact, KnowledgeItem

DAY_NAMES = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
MAX_COURSES = 12
EXTERNAL_NOTE = 'סניף חיצוני (עירייה / מתנ"ס): ההרשמה, המחירים והפרטים מולם. לא לנחש — "לבדוק מול העירייה" + קישור ההרשמה.'

TOOLS = [
    {
        'name': 'find_courses',
        'description': (
            'החוגים והמועדים כפי שהם במערכת Kogo (מקור האמת היחיד לחוג, יום, שעה, מדריך, מחיר, ניסיון ותפוסה). '
            'מסננים לפי עיר, סניף, גיל הילד ותחום. חובה לקרוא לפני כל תשובה שמכילה פרט על חוג. '
            'סניף חיצוני חוזר עם is_external=true ובלי חוגים — אז אומרים "לבדוק מול העירייה" ושולחים את קישור ההרשמה.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string', 'description': 'עיר, כפי שהלקוח כתב'},
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו'},
                'age': {'type': 'number', 'description': 'גיל הילד בשנים'},
                'course_type': {'type': 'string', 'description': 'תחום: קפוארה, מחול, היפ הופ, אקרובטיקה…'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'branch_info',
        'description': 'כתובת, הוראות הגעה, טלפון ומנהל של סניף, והאם הוא סניף חיצוני. שדה ריק = אין את המידע; לא להמציא.',
        'input_schema': {
            'type': 'object',
            'properties': {'branch': {'type': 'string', 'description': 'שם הסניף או כינוי'}},
            'required': ['branch'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'office_hours_now',
        'description': 'האם המשרד פתוח עכשיו לפי שעון ישראל, השעות של היום, יום מיוחד אם יש, והודעת "סגור".',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'customer_card',
        'description': 'מה המערכת יודעת על הכותב: אם הוא לקוח רשום, הילדים והסטטוס שלהם, ניסיון שעשה, ומה ידוע מהשיחות.',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'request_human',
        'description': (
            'מסמן שהשיחה צריכה נציג אנושי (הלקוח ביקש מפורשות, השכרת סטודיו, בעיה רפואית, או מה שהנושא קובע). '
            'רק מסמן — לא שולח דבר. אחרי הסימון התשובה ללקוח אומרת שנציג יחזור אליו.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {'reason': {'type': 'string', 'description': 'הסיבה, בקצרה ובעברית'}},
            'required': ['reason'],
            'additionalProperties': False,
        },
    },
]
TOOL_NAMES = {tool['name'] for tool in TOOLS}


class ToolContext:
    """What a tool run knows: the contact (if any), the knowledge in play, and the marks the tools leave."""

    def __init__(self, contact: Contact | None = None, *, now=None, items=None):
        self.contact = contact
        self.now = now
        self.items = list(items or [])
        self.request_human_reason: str | None = None
        self.knowledge_used: list = []

    def used(self, item: KnowledgeItem) -> None:
        if item.id not in self.knowledge_used:
            self.knowledge_used.append(item.id)


# --- resolving names -----------------------------------------------------------------------------

def _norm(text) -> str:
    return re.sub(r'[\s\-"״\'׳]', '', str(text or '').replace('תקוה', 'תקווה')).casefold()


def _alias_targets(text: str, ctx: ToolContext) -> list[str]:
    """The names a nickname from the knowledge points at, when the text contains the nickname."""
    found = []
    wanted = _norm(text)
    for item in ctx.items:
        if item.kind != KIND_ALIAS:
            continue
        data = item.data or {}
        for nick in re.split(r'\s*/\s*', str(data.get('what_customer_writes') or '')):
            if nick and _norm(nick) and _norm(nick) in wanted:
                found.append(str(data.get('means') or ''))
                ctx.used(item)
    return found


def resolve_branches(ctx: ToolContext, *, city: str = '', branch: str = ''):
    """
    The active branches a city or a branch name points at. The branch wins
    over the city. A name is matched on the branch's own name, its city, the
    conversation nicknames (analysis.BRANCH_ALIASES) and the knowledge aliases.
    """
    from apps.core.models import Branch

    rows = list(Branch.objects.filter(is_active=True).select_related('city').order_by('name'))
    if branch:
        wanted = _norm(branch)
        direct = [row for row in rows if wanted and (wanted in _norm(row.name) or _norm(row.name) in wanted)]
        if direct:
            return direct
        for label, _city, pattern in analysis.BRANCH_ALIASES:
            if pattern.search(branch):
                named = [row for row in rows if pattern.search(row.name or '')]
                if named:
                    return named
                in_city = [row for row in rows if _same_city(row.city.name if row.city_id else '', _city)]
                if in_city:
                    return in_city
        for target in _alias_targets(branch, ctx):
            hit = [row for row in rows if any(_norm(part) in _norm(row.name) for part in re.findall(r'[א-ת]{3,}', target))]
            if hit:
                return hit
    if city:
        in_city = [row for row in rows if _same_city(row.city.name if row.city_id else '', city)]
        if in_city:
            return in_city
        for label, alias_city, pattern in analysis.BRANCH_ALIASES:
            if pattern.search(city):
                hit = [row for row in rows if _same_city(row.city.name if row.city_id else '', alias_city) or pattern.search(row.name or '')]
                if hit:
                    return hit
    return []


def _course_type_matches(course, wanted: str, ctx: ToolContext) -> bool:
    if not wanted:
        return True
    names = [course.name or '', course.course_type.name if course.course_type_id else '']
    words = {_norm(wanted)} | {_norm(target) for target in _alias_targets(wanted, ctx)}
    words |= {_norm(name) for name, pattern in analysis.COURSE_WORDS if pattern.search(wanted)}
    text = _norm(' '.join(names))
    return any(word and (word in text or text in word) for word in words)


def _branch_payload(branch) -> dict:
    missing = []
    if not branch.is_external:
        for name, value in (('address', branch.address), ('phone', branch.phone), ('directions', branch.arrival_directions)):
            if not (value or '').strip():
                missing.append(name)
    return {
        'id': str(branch.id),
        'name': branch.name,
        'city': branch.city.name if branch.city_id else '',
        'is_external': branch.is_external,
        'external_link': branch.external_link or '',
        'address': branch.address or '',
        'directions': branch.arrival_directions or '',
        'phone': branch.phone or '',
        'manager_name': branch.manager_name or '',
        'missing': missing,
        'note': EXTERNAL_NOTE if branch.is_external else '',
    }


# --- the tools -----------------------------------------------------------------------------------

def find_courses(ctx: ToolContext, *, city: str = '', branch: str = '', age=None, course_type: str = '') -> dict:
    from django.db.models import Prefetch, Q

    from apps.customers.widget_views import (
        _batch_occurrence_context,
        _batch_paying_enrollment_counts,
        _batch_upcoming_trial_counts,
        _lesson_widget_capacity,
        _widget_catalog_courses,
        _widget_lesson_queryset,
    )
    from apps.enrollments.trial_policy import trial_registration_open_for, trials_open_by_default

    branches = resolve_branches(ctx, city=city, branch=branch)
    if not branches:
        hint = 'לא נמצא סניף כזה במערכת.' if (city or branch) else 'צריך עיר או סניף כדי לחפש חוגים.'
        if city and not branch:
            hint = f'אין לנו סניף ב{city}.' if not analysis.OTHER_CITY.search(city or '') else f'אין לנו סניף ב{city}.'
        return {'branches': [], 'courses': [], 'hint': hint, 'registration_fee': getattr(settings, 'REGISTRATION_FEE_ILS', None)}

    try:
        age_value = float(age) if age not in (None, '') else None
    except (TypeError, ValueError):
        age_value = None

    internal = [row for row in branches if not row.is_external]
    courses = list(
        _widget_catalog_courses()
        .filter(branch_id__in=[row.id for row in internal])
        .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
        .select_related('course_type', 'branch')
        .prefetch_related(Prefetch('lessons', queryset=_widget_lesson_queryset()))
        .order_by('branch__name', 'course_type__name', 'name')
    ) if internal else []
    courses = [course for course in courses if _course_type_matches(course, course_type, ctx)]
    if age_value is not None:
        courses = [
            course for course in courses
            if (course.min_age is None or course.min_age <= age_value) and (course.max_age is None or age_value <= course.max_age)
        ]
    lesson_ids = [lesson.id for course in courses for lesson in course.lessons.all()]
    enrolled = _batch_paying_enrollment_counts(lesson_ids)
    trials = _batch_upcoming_trial_counts(lesson_ids)
    occurrences = _batch_occurrence_context(lesson_ids)
    trials_default = trials_open_by_default()

    result_courses = []
    for course in courses[:MAX_COURSES]:
        lessons = []
        for lesson in course.lessons.all():
            cap = _lesson_widget_capacity(lesson, course, enrolled, trials, occurrences)
            lessons.append({
                'day': DAY_NAMES[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else '',
                'start': str(lesson.start_time)[:5],
                'end': str(lesson.end_time)[:5],
                'instructor': lesson.instructor.full_name if lesson.instructor_id else '',
                'is_full': cap['is_full'],
                'seats_left': cap['available_spots'],
                'trial_open': trial_registration_open_for(lesson, default=trials_default),
                'trial_is_full': cap['trial_is_full'],
                'trial_seats_left': cap['trial_spots_left'],
            })
        result_courses.append({
            'id': str(course.id),
            'name': course.name,
            'course_type': course.course_type.name if course.course_type_id else '',
            'branch': course.branch.name,
            'city': course.branch.city.name if course.branch.city_id else '',
            'price_monthly': str(course.price) if course.price is not None else None,
            'trial': {
                'is_paid': course.trial_lesson_is_paid,
                'price': str(course.trial_lesson_price) if course.trial_lesson_is_paid and course.trial_lesson_price is not None else None,
            },
            'min_age': course.min_age,
            'max_age': course.max_age,
            'is_adult': course.is_adult,
            'all_lessons_required': course.must_attend_all_lessons,
            'external_link': course.external_link or '',
            'lessons': lessons,
        })

    hint = ''
    if internal and not result_courses:
        hint = 'בסניפים האלה אין חוג שמתאים לסינון (גיל/תחום). אפשר להציע תחום אחר מאותו סניף, או להפנות למשרד.'
    return {
        'branches': [_branch_payload(row) for row in branches],
        'courses': result_courses,
        'courses_total': len(courses),
        'registration_fee': getattr(settings, 'REGISTRATION_FEE_ILS', None),
        'hint': hint,
    }


def branch_info(ctx: ToolContext, *, branch: str = '') -> dict:
    rows = resolve_branches(ctx, branch=branch) or resolve_branches(ctx, city=branch)
    if not rows:
        return {'branches': [], 'hint': 'לא נמצא סניף כזה במערכת.'}
    return {'branches': [_branch_payload(row) for row in rows]}


def office_hours_now(ctx: ToolContext) -> dict:
    hours = knowledge.office_hours_now(ctx.now)
    hours.pop('special', None) if hours.get('special') is None else None
    return hours


def customer_card(ctx: ToolContext) -> dict:
    contact = ctx.contact
    if contact is None:
        return {'known': False, 'note': 'אין איש קשר — שאלת ניסיון בלי לקוח.'}
    from apps.wahub.serializers import OUTCOME_LABELS, UNCHECKED_LABEL

    return {
        'known': True,
        'name': contact.name,
        'outcome': contact.kogo_outcome,
        'outcome_label': OUTCOME_LABELS.get(contact.kogo_outcome, UNCHECKED_LABEL),
        'detail': contact.kogo_detail,
        'children': matching.children_of(contact),
        'what_we_know': {
            'topic': contact.known_topic,
            'course_type': contact.known_course_type,
            'city': contact.known_city,
            'branch': contact.known_branch_name,
            'child_age': contact.known_child_age,
            'summary': contact.known_summary,
        },
        'handled_by': contact.handled_by,
        'needs_human': contact.needs_human,
    }


def request_human(ctx: ToolContext, *, reason: str = '') -> dict:
    ctx.request_human_reason = ' '.join(str(reason or 'הלקוח ביקש נציג').split())[:200]
    return {'ok': True, 'note': 'סומן (לא נשלח דבר). בתשובה ללקוח: נציג יחזור אליו; מחוץ לשעות — הודעת "סגור".'}


def run_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    """One tool call. A broken tool answers with an error line, never raises."""
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        if name == 'find_courses':
            return find_courses(
                ctx, city=str(arguments.get('city') or ''), branch=str(arguments.get('branch') or ''),
                age=arguments.get('age'), course_type=str(arguments.get('course_type') or ''),
            )
        if name == 'branch_info':
            return branch_info(ctx, branch=str(arguments.get('branch') or ''))
        if name == 'office_hours_now':
            return office_hours_now(ctx)
        if name == 'customer_card':
            return customer_card(ctx)
        if name == 'request_human':
            return request_human(ctx, reason=str(arguments.get('reason') or ''))
    except Exception as exc:  # the answer must still come; the fault is logged by the caller
        return {'error': f'הכלי נכשל: {type(exc).__name__}'}
    return {'error': f'כלי לא מוכר: {name}'}


def summarize_result(name: str, result: dict) -> str:
    """One Hebrew line about what a tool returned — what the screen shows under "אילו כלים נקראו"."""
    if 'error' in result:
        return result['error']
    if name == 'find_courses':
        branches = result.get('branches') or []
        if not branches:
            return result.get('hint') or 'לא נמצא סניף'
        external = [row['name'] for row in branches if row.get('is_external')]
        text = f'{len(branches)} סניפים, {len(result.get("courses") or [])} חוגים'
        if external:
            text += f'; חיצוניים: {", ".join(external)}'
        return text
    if name == 'branch_info':
        rows = result.get('branches') or []
        return ', '.join(f'{row["name"]} ({"חיצוני" if row["is_external"] else "שלנו"})' for row in rows) or 'לא נמצא'
    if name == 'office_hours_now':
        return 'המשרד פתוח' if result.get('open') else 'המשרד סגור'
    if name == 'customer_card':
        return result.get('outcome_label') or 'לא ידוע'
    if name == 'request_human':
        return 'סומן שצריך נציג'
    return ''
