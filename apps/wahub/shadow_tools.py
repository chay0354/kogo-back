"""
The tools the shadow bot may call (docs/WAHUB-CONTRACT-STAGE2.md, ב.2;
docs/WAHUB-CONTRACT-BOT-TOOLS.md for everything about a class).

Every tool reads Kogo in process — the same functions the public widget uses
for classes, dates, prices and seats (apps/customers/widget_views.py), and the
registration's own pieces for "how much if I register now"
(apps/core/registration_quote.py) — so the bot's answer about a class is the
widget's answer. Nothing here writes, and nothing here sends: `request_human`
only marks the draft. What a class costs the business — an instructor's pay, a
bonus — and what a family paid are read by no tool.

The same tools serve Claude (as tool definitions) and the stub (called
directly), so a stub answer is built from the very data Claude would get.
The payloads of a class are built in course_tools.py.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.courses import age_groups
from apps.wahub import analysis, course_tools, knowledge, matching
from apps.wahub.analysis import _same_city
from apps.wahub.course_tools import DAY_NAMES, trial_line  # noqa: F401 — both are read from here too
from apps.wahub.models import KIND_ALIAS, KIND_CONTACT, SCOPE_BRANCH, Contact, KnowledgeItem

logger = logging.getLogger(__name__)

MAX_COURSES = 12
EXTERNAL_NOTE = 'סניף חיצוני (עירייה / מתנ"ס): ההרשמה, המחירים והפרטים מולם. לא לנחש — "לבדוק מול העירייה" + קישור ההרשמה.'
# One line for every tool that speaks of a class: where the truth is, and where it is fixed.
FROM_KOGO_ONLY = (
    'מחיר, שיעור ניסיון, דמי רישום, הנחות, קיזוז, תפוסה, מועדים ומדריך נאמרים רק ממה שהכלי מחזיר עכשיו — '
    'אף פעם לא מהזיכרון, מהשיחה או מהידע. מה שחוזר הוא מה שהאתר מציג; אם הוא לא נכון, התיקון הוא בכרטיס החוג ב-Kogo, לא בבוט. '
)

TOOLS = [
    {
        'name': 'find_courses',
        'description': (
            'החוגים והמועדים כפי שהם במערכת Kogo: חוג, קבוצת גיל, יום, שעה, מדריך, מחיר חודשי, דמי רישום, שיעור ניסיון, '
            'כמות רשומים, מקומות פנויים ותאריכים קרובים. מסננים לפי עיר, סניף, גיל הילד בשנים או כיתה, ותחום. '
            'חובה לקרוא לפני כל תשובה שמכילה פרט על חוג. ' + FROM_KOGO_ONLY +
            'שיעור ניסיון נקבע לכל חוג בנפרד (trial.is_paid, trial.price): באותו סניף יש חוגים שהניסיון בהם בתשלום וחוגים שלא, '
            'ולכן לא אומרים "הניסיון בסניף הזה ללא עלות" — עונים על החוג שמתאים לגיל ולתחום של הילד, וכשלא ברור איזה חוג שואלים גיל או כיתה ותחום. '
            'trial.is_paid=false פירושו ללא עלות, וזה מה שאומרים; is_paid=true — אומרים את trial.price ואת trial.credit_rule. '
            'קבוצת הגיל של חוג היא ages (במילים); גיל על הגבול בין שתי קבוצות (בן 6: גן חובה או כיתה א) מחזיר את שתיהן, ואז שואלים באיזו כיתה הילד. '
            '"מה להביא לשיעור ניסיון" הוא trial.bring של החוג — מצטטים אותו; ריק = "אין לי את הפרט הזה, אפשר לבדוק במשרד". '
            'כשיש הרבה חוגים, 12 מפורטים והשאר ב-more_courses בשורה אחת כל אחד — מצמצמים לפי תחום וגיל, או קוראים ל-course_card. '
            'סניף חיצוני חוזר עם is_external=true ובלי חוגים — אז אומרים "לבדוק מול העירייה" ושולחים את קישור ההרשמה.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'city': {'type': 'string', 'description': 'עיר, כפי שהלקוח כתב'},
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו'},
                'age': {'type': 'number', 'description': 'גיל הילד בשנים (4.5 = ארבע וחצי)'},
                'grade': {'type': 'string', 'description': 'הכיתה או הגן, כשהלקוח אמר אותם: "ג", "כיתה ז", "גן חובה", "בוגרים"'},
                'course_type': {'type': 'string', 'description': 'תחום: קפוארה, מחול, היפ הופ, אקרובטיקה…'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'course_card',
        'description': (
            'כל מה שיש במערכת על חוג אחד: תיאור, קבוצת גיל, מחיר חודשי, דמי הרישום שייגבו בפועל לחוג הזה, שיעור ניסיון '
            '(בתשלום או לא, מחיר, כלל הקיזוז, מה להביא, פתוח או סגור), כל המועדים עם מדריך, חדר, כמות רשומים, מגבלת המקומות, '
            'מקומות פנויים ושלושת התאריכים הקרובים, מסלולים משולבים (פעמיים בשבוע) ומחירם, וההנחות שמוגדרות במערכת. '
            'קוראים עם course_id מתוך find_courses (או שם החוג וסניף). ' + FROM_KOGO_ONLY
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'course_id': {'type': 'string', 'description': 'ה-id של החוג מתוך find_courses'},
                'name': {'type': 'string', 'description': 'שם החוג, כשאין id'},
                'branch': {'type': 'string', 'description': 'שם הסניף או כינוי שלו (עם name)'},
                'city': {'type': 'string', 'description': 'עיר (עם name)'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'quote_registration',
        'description': (
            '"כמה יעלה אם נירשם עכשיו": התשלום הראשון והמחיר החודשי שאחריו, מחושבים באותו קוד שמחייב בהרשמה באתר — '
            'החלק היחסי של החודש לפי השיעורים שעוד נשארו בו, דמי רישום, קיזוז שיעור ניסיון ששולם, וההנחות (ילד שני, חוג נוסף, רישום מוקדם). '
            'חובה לקרוא כששואלים כמה יעלה להירשם, כמה משלמים עכשיו או כמה יוצא החודש — לא מחשבים לבד ולא מעריכים. '
            'בתשובה מפרטים את first_charge.breakdown בשתיים–שלוש שורות ואז את monthly_after. '
            'מה שלא ידוע על המשפחה לא מניחים: אח רשום או ניסיון ששולם נמסרים רק אם הלקוח אמר או שזה בכרטיס הלקוח; '
            'אחרת משאירים ריק (ללקוח מזוהה המערכת בודקת לבד), ואומרים על מה החישוב מבוסס (based_on).'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'course_id': {'type': 'string', 'description': 'ה-id של החוג מתוך find_courses'},
                'lesson_id': {'type': 'string', 'description': 'ה-id של המועד שנבחר, כשיש לחוג כמה ימים. ריק = המועד הראשון'},
                'track_id': {'type': 'string', 'description': 'ה-id של מסלול משולב (tracks), כשנרשמים לפעמיים בשבוע'},
                'start_date': {'type': 'string', 'description': 'תאריך ההרשמה, YYYY-MM-DD. ריק = היום'},
                'child_age': {'type': 'number', 'description': 'גיל הילד בשנים, אם ידוע'},
                'grade': {'type': 'string', 'description': 'הכיתה או הגן, אם הלקוח אמר'},
                'siblings_registered': {'type': 'boolean', 'description': 'אח או אחות כבר רשומים לחוג אצלנו. ריק כשלא ידוע'},
                'took_paid_trial': {'type': 'boolean', 'description': 'הילד היה בשיעור ניסיון בתשלום בסניף הזה. ריק כשלא ידוע'},
                'another_class_same_child': {'type': 'boolean', 'description': 'זה חוג נוסף לילד שכבר רשום ומשלם על חוג אחר'},
            },
            'required': ['course_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'instructor_info',
        'description': (
            'מדריך לפי שם (אפשר גם לפי סניף): התחומים שהוא מלמד, הסניפים, המועדים שלו ותמונה. '
            'אין בכלי טלפון או פרט אישי של מדריך — את אלה לא מוסרים ללקוח; למי שמבקש לדבר עם מדריך יש נוסח משלו.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'שם המדריך או חלק ממנו. ריק = כל המדריכים של הסניף'},
                'branch': {'type': 'string', 'description': 'שם הסניף, כינוי או עיר'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'branch_info',
        'description': (
            'סניף: כתובת, עיר, הוראות הגעה, טלפון הסניף, טלפון המשרד, מנהל, התחומים שיש בו, הימים והשעות שבהם יש בו חוגים, '
            'והאם הוא סניף חיצוני. שדה ריק = אין את המידע; לא להמציא.'
        ),
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

def _office_phone(ctx: ToolContext) -> str:
    """The office's phone as the knowledge holds it (the contact item of the office)."""
    for item in ctx.items:
        phone = (item.data or {}).get('phone')
        if item.kind == KIND_CONTACT and phone and 'משרד' in (item.title or ''):
            ctx.used(item)
            return str(phone)
    return ''


def find_courses(ctx: ToolContext, *, city: str = '', branch: str = '', age=None, grade: str = '', course_type: str = '') -> dict:
    from django.db.models import Q

    from apps.customers.widget_views import _widget_catalog_courses

    registration_fee = getattr(settings, 'REGISTRATION_FEE_ILS', None)
    branches = resolve_branches(ctx, city=city, branch=branch)
    if not branches:
        hint = 'לא נמצא סניף כזה במערכת.' if (city or branch) else 'צריך עיר או סניף כדי לחפש חוגים.'
        if city and not branch:
            hint = f'אין לנו סניף ב{city}.'
        return {'branches': [], 'courses': [], 'hint': hint, 'registration_fee': registration_fee}

    try:
        age_value = float(age) if age not in (None, '') else None
    except (TypeError, ValueError):
        age_value = None
    # The child's place on the age-group scale; a course's min/max are places on it, not years.
    grade_codes = age_groups.codes_for_grade(grade)
    age_codes = grade_codes or (age_groups.codes_for_age(age_value) if age_value is not None else set())
    by_age = bool(grade_codes) or age_value is not None

    internal = [row for row in branches if not row.is_external]
    courses = list(
        course_tools.with_lessons(
            _widget_catalog_courses()
            .filter(branch_id__in=[row.id for row in internal])
            .filter(Q(course_type__is_active=True) | Q(course_type__isnull=True))
        ).order_by('branch__name', 'course_type__name', 'name')
    ) if internal else []
    courses = [course for course in courses if _course_type_matches(course, course_type, ctx)]
    if by_age:
        courses = [course for course in courses if age_groups.fits(course.min_age, course.max_age, age_codes)]

    shown, rest = courses[:MAX_COURSES], courses[MAX_COURSES:]
    seats = course_tools.Seats(shown)
    result_courses = [course_tools.course_payload(course, seats) for course in shown]

    hints = []
    if internal and not result_courses:
        hints.append('בסניפים האלה אין חוג שמתאים לסינון (גיל/תחום). אפשר להציע תחום אחר מאותו סניף, או להפנות למשרד.')
    if by_age and not age_codes:
        hints.append('אין קבוצת גיל לגיל הזה: החוגים מגיל 3.')
    if len({course['ages'] for course in result_courses}) > 1 and len(age_codes) > 1:
        hints.append('הגיל יושב על הגבול בין שתי קבוצות גיל, ולכן חזרו חוגים משתיהן — לשאול באיזו כיתה או גן הילד לפני שמציעים חוג.')
    if rest:
        hints.append(
            f'נמצאו {len(courses)} חוגים; {len(shown)} מפורטים והשאר ב-more_courses בקיצור. '
            'לצמצם לפי תחום וגיל או כיתה, או לקרוא ל-course_card לחוג מסוים.'
        )
    return {
        'branches': [_branch_payload(row) for row in branches],
        'courses': result_courses,
        'more_courses': [course_tools.brief_course(course) for course in rest],
        'courses_total': len(courses),
        # Over every class found, not only the ones spelled out: one class must never be read as the branch.
        'trial_summary': course_tools.trial_summary(courses),
        # The general fee; each class carries the fee it will actually charge (courses[].registration_fee).
        'registration_fee': registration_fee,
        'hint': ' '.join(hints),
    }


def branch_info(ctx: ToolContext, *, branch: str = '') -> dict:
    rows = resolve_branches(ctx, branch=branch) or resolve_branches(ctx, city=branch)
    if not rows:
        return {'branches': [], 'hint': 'לא נמצא סניף כזה במערכת.'}
    office_phone = _office_phone(ctx)
    return {
        'branches': [{**_branch_payload(row), **course_tools.branch_extras(row)} for row in rows],
        'office_phone': office_phone,
        'note': 'class_days הם הימים והשעות שבהם יש חוגים בסניף, לא שעות קבלה. שעות המשרד — בכלי office_hours_now.',
    }


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


def _flag(value):
    """True, False, or None when the caller did not say."""
    return value if isinstance(value, bool) else None


def run_tool(name: str, arguments: dict, ctx: ToolContext) -> dict:
    """One tool call. A broken tool answers with an error line, never raises."""
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        if name == 'find_courses':
            return find_courses(
                ctx, city=str(arguments.get('city') or ''), branch=str(arguments.get('branch') or ''),
                age=arguments.get('age'), grade=str(arguments.get('grade') or ''),
                course_type=str(arguments.get('course_type') or ''),
            )
        if name == 'course_card':
            return course_tools.course_card(
                ctx, course_id=str(arguments.get('course_id') or ''), name=str(arguments.get('name') or ''),
                branch=str(arguments.get('branch') or ''), city=str(arguments.get('city') or ''),
            )
        if name == 'quote_registration':
            return course_tools.quote_registration(
                ctx, course_id=str(arguments.get('course_id') or ''), lesson_id=str(arguments.get('lesson_id') or ''),
                track_id=str(arguments.get('track_id') or ''), start_date=str(arguments.get('start_date') or ''),
                child_age=arguments.get('child_age'), grade=str(arguments.get('grade') or ''),
                siblings_registered=_flag(arguments.get('siblings_registered')),
                took_paid_trial=_flag(arguments.get('took_paid_trial')),
                another_class_same_child=_flag(arguments.get('another_class_same_child')),
            )
        if name == 'instructor_info':
            return course_tools.instructor_info(
                ctx, name=str(arguments.get('name') or ''), branch=str(arguments.get('branch') or ''),
            )
        if name == 'branch_info':
            return branch_info(ctx, branch=str(arguments.get('branch') or ''))
        if name == 'office_hours_now':
            return office_hours_now(ctx)
        if name == 'customer_card':
            return customer_card(ctx)
        if name == 'request_human':
            return request_human(ctx, reason=str(arguments.get('reason') or ''))
    except Exception as exc:  # the answer must still come, and the fault is not lost
        logger.exception('wahub tool %s failed', name)
        return {'error': f'הכלי נכשל: {type(exc).__name__}'}
    return {'error': f'כלי לא מוכר: {name}'}


def _shekel_text(amount) -> str:
    """ "260.00" → ₪260, "187.50" → ₪187.50."""
    text = str(amount or '0')
    return '₪' + (text[:-3] if text.endswith('.00') else text)


def summarize_result(name: str, result: dict) -> str:
    """One Hebrew line about what a tool returned — what the screen shows under "אילו כלים נקראו"."""
    if 'error' in result:
        return result['error']
    if name == 'find_courses':
        branches = result.get('branches') or []
        if not branches:
            return result.get('hint') or 'לא נמצא סניף'
        external = [row['name'] for row in branches if row.get('is_external')]
        shown, total = len(result.get('courses') or []), result.get('courses_total') or 0
        text = f'{len(branches)} סניפים, {shown} חוגים'
        if total > shown:
            text += f' מפורטים מתוך {total}'
        trial = result.get('trial_summary') or {}
        if trial.get('paid_courses'):
            text += f'; ניסיון בתשלום ב-{trial["paid_courses"]}, ללא עלות ב-{trial.get("free_courses", 0)}'
        if external:
            text += f'; חיצוניים: {", ".join(external)}'
        return text
    if name == 'course_card':
        if not result.get('found'):
            return result.get('hint') or 'החוג לא נמצא'
        if result.get('is_external'):
            return f'{result["course"]["name"]}: סניף חיצוני — לבדוק מול העירייה'
        course = result['course']
        trial = course['trial']
        return (
            f'{course["name"]} ({course["branch"]}): {_shekel_text(course["price_monthly"])} לחודש, '
            f'דמי רישום {_shekel_text(course["registration_fee"])}, '
            f'{"ניסיון " + _shekel_text(trial["price"]) if trial["price"] else ("ניסיון בתשלום, בלי מחיר" if trial["is_paid"] else "ניסיון ללא עלות")}, '
            f'{len(course["lessons"])} מועדים'
        )
    if name == 'quote_registration':
        if not result.get('can_quote'):
            return f'אין חישוב: {result.get("reason") or "חסר מידע"}'
        parts = ' '.join(
            ('− ' if row['amount'].startswith('-') else '+ ') + f'{row["label"].split(" — ")[0].split(" (")[0]} {_shekel_text(row["amount"].lstrip("-"))}'
            for row in result['first_charge']['breakdown']
        ).lstrip('+ ')
        return (
            f'הרשמה ב-{result["as_of"]} ל{result["course"]["name"]}: תשלום ראשון {_shekel_text(result["first_charge"]["amount"])}'
            f'{" (" + parts + ")" if parts else ""}, אחר כך {_shekel_text(result["monthly_after"]["amount"])} לחודש'
        )
    if name == 'instructor_info':
        rows = result.get('instructors') or []
        if not rows:
            return result.get('hint') or 'לא נמצא מדריך'
        names = ', '.join(f'{row["name"]} ({", ".join(row["course_types"]) or "בלי תחום"})' for row in rows[:3])
        return names + (f' ועוד {len(rows) - 3}' if len(rows) > 3 else '')
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
