"""
The hard checks of the bot's self-checks — what code can decide about an answer.

Two families:

    GENERAL   held against every answer of every conversation. Grounding: an
              amount, an hour, a day, a date, a class, a field, an instructor,
              a branch, a phone or a link the bot wrote must have come from
              the tools or the knowledge of that conversation. The order of
              the conversation: when `find_courses` answered with a question,
              the bot asked it and offered nothing. And the rules of form.
    specific  what one turn of one script expects, built by the small
              functions at the bottom (`asks`, `says_amount`, `called`, …).

A check answers '' when it holds, and otherwise one plain Hebrew sentence of
what was expected and what the bot wrote — the owner reads these on the
screen, so no check name, tool name or regular expression is in them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import unquote

DAYS = ('ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת')
_DAY = '|'.join(DAYS)
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_URL = re.compile(r'https?://\S+')
_BARE_SITE = re.compile(r'(?<![/\w.])(?:www\.)?[a-z0-9-]+\.(?:co\.il|com|app)\b', re.I)
_N = r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?'
# "17:00, ₪260" holds one amount: the minutes of an hour beside a ₪ are not money.
_AMOUNT = re.compile(rf'₪\s?({_N})|(?<![\d:.,])({_N})\s?(?:₪|ש"ח|ש״ח|שקלים|שקל)')
_SHEKEL_IN_TEXT = re.compile(rf'₪\s?({_N})')
_TIME = re.compile(r'(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])')
_DATE = re.compile(r'(?<![\d.₪])(\d{1,2})\.(\d{2})(?:\.(\d{2,4}))?(?![\d.])')
_PHONE = re.compile(r'(?<!\d)0\d{1,2}[-\s]?\d{3}[-\s]?\d{4}(?!\d)')
# A weekday is a day only where it reads as one: "יום שלישי", "בימי ראשון ורביעי", "שלישי 17:00".
# "ילד שני" and "התשלום הראשון" are not days.
_DAY_AFTER_WORD = re.compile(rf'(?:יום|ימי|בימי|בימים|ימים|ביום|וביום|ובימי)\s+((?:ה|ב)?(?:{_DAY})(?:\s*(?:,|ו|עד|-|–|ו-)\s*(?:ה|ב|ו)?(?:{_DAY}))*)')
_DAY_BEFORE_TIME = re.compile(rf'(?<![א-ת])(?:ב|ו|וב)?({_DAY})(?![א-ת])[^\n\d]{{0,8}}\d{{1,2}}:\d{{2}}')
_ONE_DAY = re.compile(rf'(?<![א-ת])(?:ה|ב|ו|וב)?({_DAY})(?![א-ת])')
_LATIN = re.compile(r'[A-Za-z]{2,}')
_RANGE_END = re.compile(r'\d{1,2}:\d{2}\s*(?:-|–|—|עד)\s*(\d{1,2}:\d{2})')
# "We have none" said outright — not "אין לי את הפרט", which is the line for a detail the system lacks.
NONE = r'אין לנו|אין אצלנו|אין כרגע|אין חוג|אין סניף|אין קבוצה|אין את התחום|אין כזה|לא קיים|לא מתאים לגיל|אי אפשר'


MONEY_KEYS = {'price_monthly', 'registration_fee', 'price', 'amount', 'trial_price', 'price_before_discounts', 'price_as_second_class'}
# What no tool may hand over (docs/WAHUB-CONTRACT-BOT-TOOLS.md): a key that names it.
FORBIDDEN_KEYS = ('salary', 'bonus', 'payment', 'invoice', 'card', 'token', 'iban', 'wifi')
CLASS_TOOLS = ('find_courses', 'course_card', 'quote_registration', 'instructor_info', 'branch_info')
# Activities a bot might name from its own head; one of them in an answer has to have come from somewhere.
ACTIVITY_WORDS = (
    'קפוארה', 'היפ הופ', 'היפהופ', 'מחול', 'בלט', 'ג׳אז', "ג'אז", 'ברייקדאנס', 'אקרובטיקה', 'יוגה', 'התעמלות',
    'ג׳ודו', "ג'ודו", 'קרטה', 'שחייה', 'שחמט', 'כדורגל', 'כדורסל', 'קרמיקה', 'רובוטיקה', 'זומבה', 'פילאטיס',
)
# A phrasing written for one situation, by how an answer that uses it reads. Said elsewhere, it is out of place
# (the owner's live test: "סליחה על העיכוב בימים האחרונים" to a customer nobody had kept waiting).
SITUATIONAL = {
    'campaign_not_answered': (r'סליחה על העיכוב|עומס בימים האחרונים', 'התנצלות על עיכוב (הנוסח של "לא עניתם לי" אחרי פנייה יזומה)'),
    # The whole line, with the word that opens it: "באיזו עיר אתם מחפשים חוג" alone is a question anybody may be asked.
    'campaign_yes': (r'מעולה,?\s+באיזו עיר אתם מחפשים חוג', 'הנוסח של תשובה לפנייה יזומה'),
    'campaign_decline': (r'שיהיה שנה טובה', 'הנוסח של סירוב לפנייה יזומה'),
    'waiting_for_agent': (r'עומס חריג|הפנייה שלך אצל הצוות', 'הנוסח של "ממתינים לנציג"'),
    'trial_reminder_yes': (r'נתראה בשיעור', 'אישור הגעה לשיעור ניסיון'),
    'trial_reminder_no': (r'נשמח לתאם מועד חלופי', 'אישור ביטול הגעה לשיעור ניסיון'),
    'birthday_party_gilad': (r'מנהל האירועים הפרטיים', 'ההפניה לגלעד בנושא ימי הולדת'),
    'birthday_greeting_name': (r'כתבו לנו מה השם', 'תסריט הברכה האישית'),
    'cancel_form': (r'studio-cancellation-form', 'טופס הביטול'),
    'cancel_after_deadline': (r'אין אפשרות לבטל', 'הנוסח של ביטול אחרי 1.4'),
    'voice_message': (r'הודעות קוליות|הודעה קולית|את ההקלטה', 'הנוסח של הודעה קולית'),
    'shows': (r'לוח ההופעות', 'ההפניה ללוח ההופעות'),
    'whatsapp_group': (r'קבוצות הוואטסאפ מנוהלות', 'הנוסח של קבוצת וואטסאפ'),
    'fixed_track': (r'מסלול קבוע', 'הנוסח של מסלול קבוע'),
    'instructor_request': (r'אני פה לכל דבר', 'הנוסח של בקשה לדבר עם מדריך'),
    'cancel_policy_info': (r'בסוף חודש הפעילות שבו נמסרה', 'הנוסח של מדיניות הביטול'),
}
# The phrasings that are long, or carry their own emoji and "רשמנו", by the vendor's wording: said word for word where they belong.
WORD_FOR_WORD = (
    'birthday_party_gilad', 'birthday_greeting_name', 'cancel_form', 'shows', 'trial_reminder_yes', 'trial_reminder_no',
)
_GENERALISED = re.compile(
    r'בכל (?:ה)?(?:אפשרויות|חוגים|קבוצות|סניפים|שיעורים)[^\n]{0,40}(?:ניסיון|מחיר|עלות|עולה)'
    r'|(?:ניסיון|המחיר|העלות)[^\n]{0,40}(?:בכל|לכל) (?:ה)?(?:אפשרויות|חוגים|קבוצות|סניפים)'
    r'|בסניף הזה (?:שיעור )?(?:ה)?ניסיון'
)
_TRIAL_COST = re.compile(r'ניסיון[^\n]{0,40}(?:ללא עלות|חינם|בעלות|בתשלום|₪)|(?:ללא עלות|חינם)[^\n]{0,30}ניסיון')
_ASKED_ABOUT_COST = re.compile(r'ניסיון|נסיון|עלות|מחיר|כמה|עולה|תשלום|חינם|להירשם|הרשמה|לרשום|פרטים')
_PRESSED = re.compile(r'מה יש|תגיד|הכול|הכל|כל החוגים|רשימה|תראו|כל מה ש')
_COURTESY = re.compile(r'עוד משהו|עזרה נוספת|משהו נוסף שאוכל|צריכים עוד|אשמח לעזור|בוודאי!|מצוין!')
# The line for a detail the system lacks: today's ("בודקת מול המשרד") and the one before it.
_MISSING_INFO = re.compile(r'בודק(?:ת|ים)? מול המשרד|אין לי את הפרט')
# Something said to have been done. The bot registers nobody, holds no seat and updates nothing: it marks for the office.
_DONE = re.compile(
    r'רשמנו|רשמתי|שריינו|שריינתי|שמרנו לכם|שמרתי לכם|שמרתי לך|עדכנתי|עדכנו אצלנו|מעודכן אצלנו|הזזתי|הזזנו|ביטלתי|ביטלנו את|הוספתי אות|צירפתי אות'
)
# What belongs to the system and not to the customer: a note in brackets, a failure line, the bot speaking of itself.
_INTERNAL = re.compile(
    r'לא הצלחתי לעבד|נתקלתי בבעיה|בעיה טכנית|תקלה טכנית|השיחה נסגרה|הבוט (שלנו )?חזר|\[[^\]\n]{3,}\]|שלח(?:י|ו)? ["\'״]?נציג["\'״]?|כבוט|אני בוט'
)
# The end of a line, as `shadow.polish` enforces it: the vendor's own word-for-word phrasings carry periods inside a line.
_SENTENCE_PERIOD = re.compile(r'[א-ת)"״\']\.\s*$', re.M)
# Faces, hearts, hands. Not the marks a phrasing or a list is built with (🔹 📞 🔗 ➡ 👇).
_EMOJI = re.compile(r'[\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U0001F900-\U0001FAFF\u2600-\u27BF\u2B50]')
_MARKS = set('🔹📞🔗➡👇🔸▪✔✅')

ASKS = {
    'city': (r'עיר|סניף|איפה|אזור|היכן|מאיפה', 'באיזו עיר או סניף מדובר'),
    'branch': (r'סניף', 'באיזה סניף'),
    'field': (r'תחום|איזה חוג|איזה סוג|סוג החוג|סוג של חוג|במה .{0,12}מתעניי', 'באיזה תחום מדובר'),
    'age': (r'בן כמה|בת כמה|בני כמה|גיל|כיתה|גן ', 'בן כמה הילד או באיזו כיתה'),
    'course': (r'איזה חוג|באיזה חוג|לאיזה חוג|תחום|איזה שיעור|באיזו עיר|סניף', 'באיזה חוג מדובר'),
}
NEED_ASKS = {'city': 'city', 'branch': 'branch', 'course_type': 'field', 'age': 'age', 'grade': 'age'}


@dataclass
class Moment:
    """One answer of the bot, with the conversation around it — what a check is given."""
    reply: str
    index: int = 0
    customer: list = field(default_factory=list)        # what the customer wrote in this turn
    customer_all: str = ''                              # everything the customer wrote, earlier conversations included
    customer_now: str = ''                              # what the customer wrote in the conversation on the table, this turn included
    calls: list = field(default_factory=list)           # this turn's tool calls: [{name, input, result}]
    earlier_calls: list = field(default_factory=list)   # the tool calls of the turns before it
    previous_reply: str = ''
    request_human: bool = False
    tags: list = field(default_factory=list)
    world: dict = field(default_factory=dict)
    knowledge_text: str = ''                            # what the knowledge lets the bot say (bodies and data; no examples)
    office_days: tuple = ()                             # the weekdays the office is open on
    catalog: object = None
    allow: tuple = ()                                   # the situational phrasings this turn may use

    @property
    def all_calls(self) -> list:
        return list(self.earlier_calls) + list(self.calls)


@dataclass(frozen=True)
class Check:
    id: str
    title: str                          # what is checked, in plain Hebrew
    test: Callable[[Moment], str]       # '' when it holds; otherwise what went wrong, in plain Hebrew


# --- reading an answer --------------------------------------------------------------------------------------

def _norm(text) -> str:
    return re.sub(r'[\s\-–"״\'׳.,:;!?*()]', '', str(text or '').replace('תקוה', 'תקווה')).casefold()


def _plain(reply: str) -> str:
    """The answer without its links: a link's letters and digits are not the bot's words."""
    return _URL.sub(' ', reply or '')


def _number(text) -> Decimal | None:
    try:
        return Decimal(str(text).replace(',', '')).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def amounts_in(text: str) -> list:
    found = []
    for match in _AMOUNT.finditer(_plain(text)):
        value = _number(match.group(1) or match.group(2))
        if value is not None:
            found.append(value)
    return found


def times_in(text: str) -> list:
    return [f'{int(hour):02d}:{minute}' for hour, minute in _TIME.findall(_plain(text))]


def days_in(text: str) -> list:
    found = []
    plain = _plain(text)
    for match in _DAY_AFTER_WORD.finditer(plain):
        found += _ONE_DAY.findall(match.group(1))
    found += _DAY_BEFORE_TIME.findall(plain)
    return list(dict.fromkeys(found))


def dates_in(text: str) -> list:
    found = []
    for day, month, _year in _DATE.findall(_plain(text)):
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            found.append((int(day), int(month)))
    return found


def phones_in(text: str) -> list:
    return [re.sub(r'\D', '', match) for match in _PHONE.findall(_plain(text))]


def links_in(text: str) -> list:
    return [unquote(url.rstrip('.,;)!?*"\'')) for url in _URL.findall(text or '')]


def _shekels(value: Decimal) -> str:
    text = f'{value}'
    return '₪' + (text[:-3] if text.endswith('.00') else text)


def _quote(text: str, limit: int = 160) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


# --- reading what the tools returned ---------------------------------------------------------------------------

def _leaves(value, key=''):
    if isinstance(value, dict):
        for name, inner in value.items():
            yield from _leaves(inner, str(name))
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _leaves(inner, key)
    else:
        yield key, value


def _keys(value):
    if isinstance(value, dict):
        for name, inner in value.items():
            yield str(name)
            yield from _keys(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _keys(inner)


def _dump(calls: list) -> str:
    """Everything the tools returned, as text, without ids: a random id may hold any digits."""
    return _UUID.sub('', json.dumps([call.get('result') for call in calls], ensure_ascii=False, default=str))


def money_evidence(calls: list, knowledge_text: str = '') -> set:
    found = set()
    for call in calls:
        for key, value in _leaves(call.get('result')):
            if key in MONEY_KEYS and value not in (None, '') and not isinstance(value, bool):
                number = _number(str(value).lstrip('-'))
                if number is not None:
                    found.add(number)
            if isinstance(value, str):
                found.update(number for number in (_number(text) for text in _SHEKEL_IN_TEXT.findall(value)) if number is not None)
    found.update(number for number in (_number(text) for text in _SHEKEL_IN_TEXT.findall(knowledge_text)) if number is not None)
    return found


def day_evidence(calls: list, office_days=()) -> set:
    found = set(office_days)
    for call in calls:
        for key, value in _leaves(call.get('result')):
            if not isinstance(value, str):
                continue
            if key in ('day', 'day_label') and value in DAYS:
                found.add(value)
            elif key == 'lessons':                       # a track's lessons: "ראשון 16:00–17:00"
                found.update(_ONE_DAY.findall(value.split(' ')[0]))
    return found


def lessons_evidence(calls: list) -> list:
    """Every lesson the tools returned: [{course, day, start, end, instructor, amounts}] — what belongs to what."""
    rows = []

    def of_course(course: dict) -> None:
        if not isinstance(course, dict) or not isinstance(course.get('lessons'), list):
            return
        amounts = {
            number for number in (
                _number(str(value).lstrip('-')) for key, value in _leaves(course)
                if key in MONEY_KEYS and value not in (None, '') and not isinstance(value, bool)
            ) if number is not None
        }
        for text in (value for _key, value in _leaves(course) if isinstance(value, str)):
            amounts.update(number for number in (_number(found) for found in _SHEKEL_IN_TEXT.findall(text)) if number is not None)
        for lesson in course['lessons']:
            if isinstance(lesson, dict) and lesson.get('start'):
                rows.append({
                    'course': course.get('name') or '', 'day': lesson.get('day') or '', 'start': str(lesson.get('start'))[:5],
                    'end': str(lesson.get('end') or '')[:5], 'instructor': lesson.get('instructor') or lesson.get('instructor_name') or '',
                    'amounts': amounts,
                })

    quotes: dict = {}
    for call in calls:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        for course in result.get('courses') or []:
            of_course(course)
        if isinstance(result.get('course'), dict):
            of_course(result['course'])
        if call.get('name') == 'quote_registration' and result.get('can_quote'):
            name = (result.get('course') or {}).get('name') or ''
            quotes.setdefault(name, set()).update(money_evidence([call]))
    for row in rows:
        row['amounts'] = set(row['amounts']) | quotes.get(row['course'], set())
    return rows


def _gated(moment: Moment) -> dict | None:
    """The question `find_courses` said to ask in this turn, when no later call of the turn returned a class."""
    need = None
    for call in moment.calls:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        if call.get('name') == 'find_courses':
            need = result if result.get('need') else None
        elif call.get('name') in ('course_card', 'quote_registration') and (result.get('found') or result.get('can_quote')):
            need = None
    return need


def _class_details(reply: str) -> list:
    """What an answer says of a class: hours, and amounts of money."""
    return times_in(reply) + [_shekels(value) for value in amounts_in(reply)]


# --- the general checks ---------------------------------------------------------------------------------------------

def _grounded_amounts(m: Moment) -> str:
    known = money_evidence(m.all_calls, m.knowledge_text)
    strange = [value for value in amounts_in(m.reply) if value not in known]
    if not strange:
        return ''
    shown = ', '.join(_shekels(value) for value in dict.fromkeys(strange))
    return f'הבוט כתב סכום שלא הגיע מהמערכת: {shown}. סכום נאמר רק כפי שהמערכת החזירה אותו בשיחה הזאת'


def _grounded_times(m: Moment) -> str:
    known = set(times_in(_dump(m.all_calls))) | set(times_in(m.knowledge_text))
    strange = [value for value in times_in(m.reply) if value not in known]
    if not strange:
        return ''
    return f'הבוט כתב שעה שלא הגיעה מהמערכת: {", ".join(dict.fromkeys(strange))}'


def _grounded_days(m: Moment) -> str:
    # The days the office is open ground a day only in an answer about the office: a lesson's day comes from the tools.
    known = day_evidence(m.all_calls, m.office_days if re.search(r'משרד|שעות הפעילות', m.reply) else ())
    strange = [value for value in days_in(m.reply) if value not in known]
    if not strange:
        return ''
    return f'הבוט כתב יום בשבוע שלא הגיע מהמערכת: {", ".join("יום " + day for day in dict.fromkeys(strange))}'


def _grounded_dates(m: Moment) -> str:
    known = set(dates_in(_dump(m.all_calls))) | set(dates_in(m.knowledge_text))
    strange = [value for value in dates_in(m.reply) if value not in known]
    if not strange:
        return ''
    return f'הבוט כתב תאריך שלא הגיע מהמערכת או מהידע: {", ".join(f"{day:02d}.{month:02d}" for day, month in dict.fromkeys(strange))}'


def _grounded_names(m: Moment) -> str:
    catalog = m.catalog
    reply = _norm(_plain(m.reply))
    from_tools = _norm(_dump(m.all_calls))
    allowed = from_tools + _norm(m.knowledge_text) + _norm(m.customer_all)
    problems = []
    if catalog is not None:
        for label, names, source in (
            ('חוג', catalog.courses, allowed), ('תחום', catalog.fields, allowed),
            ('סניף', catalog.branches, allowed), ('עיר', catalog.cities, allowed),
            # An instructor's name comes from the tools and from nowhere else.
            ('מדריך', catalog.instructors, from_tools + _norm(m.customer_all)),
        ):
            for name in sorted(names):
                short = _norm(name)
                if len(short) >= 4 and short in reply and short not in source:
                    problems.append(f'{label} "{name}"')
    for word in ACTIVITY_WORDS:
        short = _norm(word)
        if short in reply and short not in allowed:
            problems.append(f'התחום "{word}"')
    if not problems:
        return ''
    return f'הבוט הזכיר {", ".join(dict.fromkeys(problems))} — שם שלא חזר מהמערכת בשיחה הזאת והלקוח לא כתב אותו'


def _grounded_phones(m: Moment) -> str:
    known = ' '.join((re.sub(r'\D', '', _dump(m.all_calls)), re.sub(r'[-\s]', '', m.knowledge_text), re.sub(r'[-\s]', '', m.customer_all)))
    strange = [phone for phone in phones_in(m.reply) if phone not in known]
    return f'הבוט כתב מספר טלפון שלא הגיע מהמערכת או מהידע: {", ".join(strange)}' if strange else ''


def _grounded_links(m: Moment) -> str:
    known = unquote(_dump(m.all_calls) + ' ' + m.knowledge_text)
    strange = [url for url in links_in(m.reply) if url.rstrip('/') not in known]
    return f'הבוט שלח קישור שלא מופיע במערכת או בידע: {", ".join(strange)}' if strange else ''


def _own_details(m: Moment) -> str:
    """An hour stands beside its own day, its own instructor and its own price — not beside another class's."""
    lessons = lessons_evidence(m.all_calls)
    if not lessons:
        return ''
    starts = {row['start'] for row in lessons}
    instructors = {row['instructor'] for row in lessons if row['instructor']}
    for line in m.reply.split('\n'):
        # "16:00–17:00": the second hour is when that lesson ends, not a lesson that begins at five.
        ends = [f'{int(value.split(":")[0]):02d}:{value.split(":")[1]}' for value in _RANGE_END.findall(_plain(line))]
        hours = [value for value in times_in(line) if value in starts and value not in ends]
        if not hours:
            continue
        days = days_in(line)
        for hour in hours:
            owners = [row for row in lessons if row['start'] == hour]
            if days and not any(row['day'] in days for row in owners):
                real = ', '.join(dict.fromkeys(f'יום {row["day"]}' for row in owners))
                return f'הבוט כתב "{_quote(line)}" — השעה {hour} שייכת ל{real}, לא ליום שנכתב לידה'
            named = [name for name in instructors if _norm(name) in _norm(line)]
            if named and not any(row['instructor'] in named for row in owners if not days or row['day'] in days):
                return f'הבוט כתב "{_quote(line)}" — המדריך שנכתב ליד השעה {hour} לא מלמד בשעה הזאת'
        owners = [row for row in lessons if row['start'] in hours and (not days or row['day'] in days)]
        own_amounts = set().union(*(row['amounts'] for row in owners)) if owners else set()
        for value in amounts_in(line):
            if own_amounts and value not in own_amounts:
                return f'הבוט כתב "{_quote(line)}" — הסכום {_shekels(value)} שייך לחוג אחר, לא לחוג שהשעה שלו כתובה באותה שורה'
    return ''


def _order_kept(m: Moment) -> str:
    asked = _gated(m)
    if asked is None:
        return ''
    need = asked.get('need')
    what = ASKS[NEED_ASKS.get(need, 'city')]
    said = _class_details(m.reply)
    if said:
        return (
            f'המערכת אמרה שקודם צריך לשאול {what[1]}, והבוט כבר נתן פרטים של חוג ({", ".join(dict.fromkeys(said))}). '
            'קודם מבררים, אחר כך מציעים'
        )
    choice = [str(name) for name in (asked.get('course_types') or asked.get('between') or [])]
    named = sum(1 for name in choice if _norm(name) in _norm(m.reply))
    if not re.search(what[0], m.reply) and named < 2:
        return f'המערכת אמרה שקודם צריך לשאול {what[1]}, והבוט לא שאל את זה'
    return ''


def _show_all_only_when_pressed(m: Moment) -> str:
    wanted = any(call.get('name') == 'find_courses' and (call.get('input') or {}).get('show_all') is True for call in m.calls)
    if wanted and not _PRESSED.search(m.customer_now):
        return 'הבוט ביקש מהמערכת את כל החוגים, בלי שהלקוח ביקש לראות הכול. קודם מבררים עיר, תחום וגיל'
    return ''


def _fresh_details(m: Moment) -> str:
    # The hours of the office are the knowledge's, not a class's.
    office = set(times_in(m.knowledge_text))
    said = [value for value in _class_details(m.reply) if value not in office]
    if said and not any(call.get('name') in CLASS_TOOLS for call in m.calls):
        return f'הבוט נתן פרטים של חוג ({", ".join(dict.fromkeys(said))}) בלי לבדוק אותם במערכת בתשובה הזאת'
    return ''


def _no_generalisation(m: Moment) -> str:
    found = _GENERALISED.search(m.reply)
    if found:
        return f'הבוט הכליל על כל החוגים או על הסניף: "{_quote(found.group(0))}". עלות נאמרת על חוג מסוים בלבד'
    return ''


def _classes_returned(calls: list) -> int:
    """How many classes this turn's tools spelled out."""
    names = set()
    for call in calls:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        names.update(course.get('name') for course in result.get('courses') or [] if isinstance(course, dict))
        if isinstance(result.get('course'), dict) and result['course'].get('name'):
            names.add(result['course']['name'])
    return len(names)


def _cost_only_when_asked(m: Moment) -> str:
    found = _TRIAL_COST.search(m.reply)
    # One class was found and is being offered: its own trial is part of the offer. Anything wider waits to be asked.
    if found and _classes_returned(m.calls) == 1:
        return ''
    if found and not _ASKED_ABOUT_COST.search(m.customer_now):
        return f'הבוט אמר מעצמו כמה עולה שיעור הניסיון ("{_quote(found.group(0))}") בלי שהלקוח שאל על עלות, ניסיון או הרשמה'
    return ''


def _phrasing_in_place(m: Moment) -> str:
    for key, (pattern, label) in SITUATIONAL.items():
        if key in m.allow:
            continue
        found = re.search(pattern, _plain(m.reply))
        if found:
            return f'הבוט השתמש ב{label} ("{_quote(found.group(0))}") במצב שהוא לא מיועד לו'
    return ''


def _phrasing_lines(world: dict) -> list:
    """The fixed lines of the knowledge's phrasings, up to their first variable: a line the bot is told to say word for word."""
    lines = []
    for body in ((world.get('facts') or {}).get('phrasings') or {}).values():
        for line in str(body).split('\n'):
            head = line.split('{')[0].strip()
            if len(head) >= 10:
                lines.append(head)
    return lines


def _no_repeat(m: Moment) -> str:
    if not m.previous_reply:
        return ''
    fixed = _phrasing_lines(m.world)
    before = {line.strip() for line in m.previous_reply.split('\n') if len(line.strip()) >= 15}
    # A phrasing said twice ("אין לי את הפרט הזה") is the same answer to another question, not a repeated answer.
    again = [
        line.strip() for line in m.reply.split('\n')
        if line.strip() in before and not any(line.strip().startswith(head) for head in fixed)
    ]
    if len(again) >= 2:
        return f'הבוט חזר על מה שכבר כתב בתשובה הקודמת: "{_quote(again[0])}" ועוד {len(again) - 1} שורות'
    return ''


def _form(m: Moment) -> str:
    reply = m.reply
    problems = []
    if _SENTENCE_PERIOD.search(_plain(reply)):
        problems.append('נקודה בסוף משפט')
    if '**' in reply:
        problems.append('כוכבית כפולה')
    if re.search(r'\d\s?₪', reply):
        problems.append('סימן ₪ אחרי המספר')
    if _BARE_SITE.search(_plain(reply)):
        problems.append('כתובת אתר בלי https://')
    return f'בעיית עיצוב בתשובה: {", ".join(problems)}' if problems else ''


def _hebrew_only(m: Moment) -> str:
    found = _LATIN.findall(_plain(m.reply))
    return f'הבוט כתב באותיות לועזיות: {", ".join(dict.fromkeys(found))}. עונים בעברית בלבד' if found else ''


def _word_for_word(m: Moment) -> bool:
    """This turn may say one of the vendor's long phrasings as it is written."""
    return any(key in WORD_FOR_WORD for key in m.allow)


def _short(m: Moment) -> str:
    lines = [line for line in m.reply.split('\n') if line.strip()]
    prose = [line for line in lines if not line.lstrip().startswith('🔹')]
    limit = 9 if _word_for_word(m) else 4
    if len(prose) > limit or len(lines) > 9 or len(_plain(m.reply)) > 700:
        return (
            f'התשובה ארוכה מדי: {len(lines)} שורות ({len(prose)} מהן לא פריטים של רשימה), {len(_plain(m.reply))} תווים. '
            'הכלל הוא 1–3 שורות'
        )
    return ''


_BULLET = '🔹'
# The subjects an answer can carry about a class. More than two at once, unasked, is everything in one message.
_SUBJECTS = (
    ('ימים ושעות', re.compile(r'\b\d{1,2}:\d{2}\b'), re.compile(r'מתי|איזה יום|באיזה יום|באילו ימים|שעה|שעות|ימים|מערכת')),
    ('מחיר', re.compile(r'₪\s*\d|\d\s*₪'), re.compile(r'מחיר|עולה|עלות|כמה|תשלום|לשלם|יקר|דמי')),
    ('מה להביא', re.compile(r'להביא|ללבוש|לבוש|תלבושת'), re.compile(r'להביא|ללבוש|לבוש|תלבושת|ציוד')),
    ('קישור', re.compile(r'https?://'), re.compile(r'קישור|לינק|להירשם|הרשמה|לרשום|ניסיון')),
)


def _list_of_four(m: Moment) -> str:
    if _word_for_word(m) or any((call.get('input') or {}).get('show_all') is True for call in m.calls):
        return ''                     # a line said word for word, or a customer who asked to see everything
    items = [line for line in m.reply.split('\n') if line.lstrip().startswith(_BULLET)]
    if len(items) > 4:
        return f'רשימה של {len(items)} פריטים בתשובה אחת. עד 4 — ויותר מזה שואלים שאלה שמצמצמת, או כותבים את השמות בשורה אחת'
    return ''


def _one_subject(m: Moment) -> str:
    if _word_for_word(m):
        return ''
    said = [(name, asked) for name, shows, asked in _SUBJECTS if shows.search(m.reply)]
    unasked = [name for name, asked in said if not asked.search(m.customer_now)]
    if len(said) >= 3 and len(unasked) >= 2:
        return (
            f'הבוט העמיס על תשובה אחת: {", ".join(name for name, _ in said)}. '
            f'הלקוח לא שאל על: {", ".join(unasked)}. נושא אחד בכל הודעה, והשאר כששואלים'
        )
    return ''


def _one_question(m: Moment) -> str:
    count = _plain(m.reply).count('?')
    return f'הבוט שאל {count} שאלות בתשובה אחת. שואלים שאלה אחת בכל פעם: "{_quote(m.reply)}"' if count > 1 else ''


def _one_emoji(m: Moment) -> str:
    if _word_for_word(m):
        return ''
    found = [char for char in _EMOJI.findall(m.reply) if char not in _MARKS]
    return f'יותר מאימוג׳י אחד בתשובה: {" ".join(found)}' if len(found) > 1 else ''


def _nothing_claimed_done(m: Moment) -> str:
    found = _DONE.search(m.reply)
    if found:
        return f'הבוט כתב שנעשה משהו ("{found.group(0)}") — והוא לא רושם, לא משריין ולא מעדכן דבר. הוא רק מסמן לנציג'
    return ''


def _no_internal_text(m: Moment) -> str:
    found = _INTERNAL.search(_plain(m.reply))
    return f'הבוט כתב ללקוח טקסט פנימי של המערכת: "{_quote(found.group(0))}"' if found else ''


def _no_courtesy(m: Moment) -> str:
    found = _COURTESY.search(m.reply)
    return f'הבוט כתב משפט נימוס שאסור ("{found.group(0)}")' if found else ''


def _no_internal_money(m: Moment) -> str:
    for call in m.calls:
        for key in _keys(call.get('result')):
            if any(word in key.lower() for word in FORBIDDEN_KEYS):
                return f'כלי של הבוט החזיר שדה שאסור לו לראות ("{key}")'
    return ''


# Second-person verbs that address one man, or one woman. "לך" and "את" are spelled alike for both and are left out.
_TO_A_MAN = re.compile(r'(?<![א-ת])(?:תוכל|תרצה|תגיד|תכתוב|תשלח|תבדוק|תבחר|תספר|אתה|מעוניין|מוזמן)(?![א-ת])')
_TO_A_WOMAN = re.compile(r'(?<![א-ת])(?:תוכלי|תרצי|תגידי|תכתבי|תשלחי|תבדקי|תבחרי|תספרי|מעוניינת|מוזמנת)(?![א-ת])')
# What tells who is writing: how she speaks of herself, or who she says she is.
_WRITES_AS_A_WOMAN = re.compile(r'(?<![א-ת])(?:מעוניינת|מחפשת|שואלת|מתעניינת|רציתי לשאול כאמא|אמא של|אני אמא|הסבתא|סבתא של|אני סבתא)(?![א-ת])')
_WRITES_AS_A_MAN = re.compile(r'(?<![א-ת])(?:מעוניין|מחפש|שואל|מתעניין|אבא של|אני אבא|הסבא|סבא של|אני סבא)(?![א-ת])')


def _neutral_address(m: Moment) -> str:
    """
    Nobody is addressed as a man or as a woman by a guess: until the customer's
    own words say which, the answer speaks in the plural or without addressing.
    Once they do, the answer must not address the other one.
    """
    woman, man = _WRITES_AS_A_WOMAN.search(m.customer_all), _WRITES_AS_A_MAN.search(m.customer_all)
    to_man, to_woman = _TO_A_MAN.search(m.reply), _TO_A_WOMAN.search(m.reply)
    if woman and not man:
        return f'הלקוחה כותבת על עצמה בלשון נקבה, והבוט פנה אליה בלשון זכר ("{to_man.group(0)}")' if to_man else ''
    if man and not woman:
        return f'הלקוח כותב על עצמו בלשון זכר, והבוט פנה אליו בלשון נקבה ("{to_woman.group(0)}")' if to_woman else ''
    found = to_man or to_woman
    if found:
        return f'עוד לא ברור אם כותב גבר או אישה, והבוט פנה בלשון {"זכר" if to_man else "נקבה"} ("{found.group(0)}"). עד שברור — לשון ניטרלית'
    return ''


GENERAL = (
    Check('grounded_amounts', 'כל סכום בתשובה הגיע מהמערכת', _grounded_amounts),
    Check('grounded_times', 'כל שעה בתשובה הגיעה מהמערכת', _grounded_times),
    Check('grounded_days', 'כל יום בשבוע בתשובה הגיע מהמערכת', _grounded_days),
    Check('grounded_dates', 'כל תאריך בתשובה הגיע מהמערכת או מהידע', _grounded_dates),
    Check('grounded_names', 'כל חוג, תחום, מדריך, סניף ועיר בתשובה הגיעו מהמערכת', _grounded_names),
    Check('grounded_phones', 'כל מספר טלפון בתשובה הגיע מהמערכת או מהידע', _grounded_phones),
    Check('grounded_links', 'כל קישור בתשובה מופיע במערכת או בידע', _grounded_links),
    Check('own_details', 'פרט של חוג אחד לא נכתב ליד חוג אחר', _own_details),
    Check('order_kept', 'קודם מבררים, אחר כך מציעים', _order_kept),
    Check('show_all_only_when_pressed', 'רשימה מלאה רק למי שביקש לראות הכול', _show_all_only_when_pressed),
    Check('fresh_details', 'פרטים של חוג נבדקים במערכת לפני שנאמרים', _fresh_details),
    Check('no_generalisation', 'לא מכלילים עלות על כל החוגים או על הסניף', _no_generalisation),
    Check('cost_only_when_asked', 'עלות ניסיון נאמרת רק כששאלו', _cost_only_when_asked),
    Check('phrasing_in_place', 'נוסח נאמר רק במצב שהוא מיועד לו', _phrasing_in_place),
    Check('no_repeat', 'לא חוזרים על מה שכבר נכתב', _no_repeat),
    Check('form', 'עיצוב: בלי נקודה בסוף, ₪ לפני המספר, כוכבית אחת, קישור מלא', _form),
    Check('hebrew_only', 'עברית בלבד', _hebrew_only),
    Check('short', 'תשובה קצרה', _short),
    Check('one_question', 'שאלה אחת בכל פעם', _one_question),
    Check('list_of_four', 'רשימה — עד 4 פריטים', _list_of_four),
    Check('one_subject', 'נושא אחד בכל הודעה, לא הכול בבת אחת', _one_subject),
    Check('one_emoji', 'אימוג׳י אחד לכל היותר', _one_emoji),
    Check('nothing_claimed_done', 'לא אומרים שנעשה משהו שלא נעשה', _nothing_claimed_done),
    Check('no_internal_text', 'שום טקסט פנימי של המערכת לא נכתב ללקוח', _no_internal_text),
    Check('no_courtesy', 'בלי שאלת נימוס בסוף ובלי "אשמח לעזור"', _no_courtesy),
    Check('neutral_address', 'לא פונים בלשון זכר או נקבה בניחוש', _neutral_address),
    Check('no_internal_money', 'הכלים לא מחזירים כספים פנימיים', _no_internal_money),
)
GENERAL_IDS = tuple(check.id for check in GENERAL)


# --- what one turn of one script expects ---------------------------------------------------------------------------

def _fact(world: dict, path: str):
    value = world.get('facts') or {}
    for part in path.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
        if value is None:
            return None
    return value


def asks(what: str) -> Check:
    pattern, label = ASKS[what]

    def test(m: Moment) -> str:
        if re.search(pattern, m.reply):
            return ''
        if what == 'field':
            fields = ((m.world.get('facts') or {}).get('stage') or {}).get('fields') or []
            if sum(1 for name in fields if _norm(name) in _norm(m.reply)) >= 2:
                return ''
        return f'ציפינו שהבוט ישאל {label}. הוא ענה: "{_quote(m.reply)}"'

    return Check(f'asks_{what}', f'הבוט שואל {label}', test)


def offers_nothing() -> Check:
    def test(m: Moment) -> str:
        said = _class_details(m.reply)
        if said:
            return f'ציפינו שהבוט לא יציע חוג בשלב הזה. הוא כבר כתב: {", ".join(dict.fromkeys(said))}'
        catalog = m.catalog
        fields = {_norm(name) for name in (catalog.fields if catalog else ())}
        # A field the bot names to choose from is not a class it offers — and a class may be named like
        # part of a field ("היפהופ" inside the field "היפהופ - מחול"), which the bot may say in short.
        listed = [field for field in fields if field]
        named = [
            name for name in sorted(catalog.courses if catalog else ())
            if len(_norm(name)) >= 6 and _norm(name) in _norm(m.reply) and _norm(name) not in fields
            and not any(_norm(name) in field for field in listed) and _norm(name) not in _norm(m.customer_all)
        ]
        if named:
            return f'ציפינו שהבוט לא יציע חוג בשלב הזה. הוא כבר הציע: {", ".join(named)}'
        return ''

    return Check('offers_nothing', 'הבוט לא מציע חוג, יום, שעה או מחיר בשלב הזה', test)


def tells_lesson(path: str) -> Check:
    def test(m: Moment) -> str:
        entry = _fact(m.world, path) or {}
        lessons = [lesson for course in (entry.get('same_question') or [entry]) for lesson in course.get('lessons') or []]
        if any(lesson['start'] in times_in(m.reply) and (not lesson['day'] or lesson['day'] in m.reply) for lesson in lessons):
            return ''
        wanted = ', '.join(dict.fromkeys(f'יום {lesson["day"]} {lesson["start"]}' for lesson in lessons)) or 'אין מועד במערכת'
        return f'ציפינו שהבוט יאמר מתי החוג ({wanted}). הוא ענה: "{_quote(m.reply)}"'

    return Check(f'tells_lesson:{path}', 'הבוט אומר את היום והשעה של החוג שמתאים', test)


def only_lessons_of(path: str) -> Check:
    """Every hour in the answer is an hour of the classes the question was about — no other age group, no other field."""
    def test(m: Moment) -> str:
        entry = _fact(m.world, path) or {}
        courses = entry.get('same_question') or [entry]
        allowed = {value for course in courses for lesson in course.get('lessons') or [] for value in (lesson['start'], lesson['end'])}
        strange = [value for value in times_in(m.reply) if value not in allowed]
        if strange:
            return (
                f'ציפינו רק לחוג שמתאים ל{entry.get("field", "")} {entry.get("child", "")}. '
                f'הבוט כתב גם שעות של חוגים אחרים: {", ".join(dict.fromkeys(strange))}'
            )
        return ''

    return Check(f'only_lessons_of:{path}', 'הבוט מציג רק את החוג שמתאים לגיל ולתחום שנאמרו', test)


def says_amount(path: str, what: str) -> Check:
    def test(m: Moment) -> str:
        wanted = _number(_fact(m.world, path))
        if wanted is None:
            return f'במערכת לא מוגדר {what} לחוג הזה, ולכן אי אפשר לבדוק את התשובה'
        if wanted in amounts_in(m.reply):
            return ''
        return f'ציפינו ל{what} שבמערכת: {_shekels(wanted)}. הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'says_amount:{path}', f'הבוט אומר את {what} כפי שהוא במערכת', test)


def not_amount(path: str, what: str) -> Check:
    def test(m: Moment) -> str:
        unwanted = _number(_fact(m.world, path))
        if unwanted is not None and unwanted in amounts_in(m.reply):
            return f'הבוט כתב {_shekels(unwanted)} — זה {what}, ולא של החוג שנשאל עליו'
        return ''

    return Check(f'not_amount:{path}', f'הבוט לא אומר את {what}', test)


def _amounts_of_class(calls: list, name) -> set:
    """Every sum the tools of this answer returned inside the record of the class of that name."""
    found = set()
    wanted = _norm(name)
    if not wanted:
        return found
    for call in calls:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        records = [row for row in result.get('courses') or [] if isinstance(row, dict)]
        if isinstance(result.get('course'), dict):
            records.append(result['course'])
        for record in records:
            if _norm(record.get('name')) != wanted:
                continue
            for key, value in _leaves(record):
                if key in MONEY_KEYS and value not in (None, '') and not isinstance(value, bool):
                    number = _number(str(value).lstrip('-'))
                    if number is not None:
                        found.add(number)
    return found


def not_amount_of_other(unwanted: str, wanted: str, what: str) -> Check:
    """The other class's amount is not in the answer — checked when the two classes really differ in it."""
    def test(m: Moment) -> str:
        other, own = _number(_fact(m.world, unwanted)), _number(_fact(m.world, wanted))
        # The same sum may belong to the class asked about now as well (its twice-a-week price happens to be
        # the first class's monthly one): then it stands in that class's own record, and it is nobody else's.
        if other is not None and other in _amounts_of_class(m.calls, _fact(m.world, f"{wanted.split('.')[0]}.name")):
            return ''
        if other is not None and other != own and other in amounts_in(m.reply):
            return f'הבוט כתב {_shekels(other)} — זה {what}, ולא של החוג שנשאל עליו עכשיו. פרט של חוג אחד נכנס לתשובה על חוג אחר'
        return ''

    return Check(f'not_amount_of_other:{unwanted}', f'הבוט לא מערבב את {what} בתשובה על החוג שנשאל', test)


def says_word(path: str, label: str) -> Check:
    def test(m: Moment) -> str:
        wanted = str(_fact(m.world, path) or '').strip()
        if not wanted:
            return f'במערכת לא רשום {label}, ולכן אי אפשר לבדוק את התשובה'
        return '' if _norm(wanted) in _norm(m.reply) else f'ציפינו ל{label} שבמערכת: {wanted}. הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'says_word:{path}', f'הבוט אומר את {label} כפי שהוא במערכת', test)


def not_listing_fields() -> Check:
    def test(m: Moment) -> str:
        fields = ((m.world.get('facts') or {}).get('stage') or {}).get('fields') or []
        named = [name for name in fields if _norm(name) in _norm(m.reply)]
        return f'ציפינו לבקשה קצרה בלבד. הבוט חזר על רשימת התחומים: {", ".join(named)}' if len(named) >= 2 else ''

    return Check('not_listing_fields', 'הבוט לא חוזר על רשימת התחומים', test)


def no_amounts() -> Check:
    def test(m: Moment) -> str:
        said = amounts_in(m.reply)
        return f'ציפינו שלא ייאמר שום סכום. הבוט כתב: {", ".join(_shekels(value) for value in said)}' if said else ''

    return Check('no_amounts', 'הבוט לא אומר שום סכום', test)


def called(tool: str, label: str) -> Check:
    def test(m: Moment) -> str:
        return '' if any(call.get('name') == tool for call in m.calls) else f'ציפינו שהבוט {label} לפני שהוא עונה. הוא לא עשה את זה'

    return Check(f'called:{tool}', f'הבוט {label}', test)


def not_called(tool: str, label: str) -> Check:
    def test(m: Moment) -> str:
        return f'הבוט {label}, ולא היה צריך' if any(call.get('name') == tool for call in m.calls) else ''

    return Check(f'not_called:{tool}', f'הבוט לא {label}', test)


def called_with(tool: str, argument: str, expected, label: str) -> Check:
    def test(m: Moment) -> str:
        for call in m.calls:
            if call.get('name') == tool and (call.get('input') or {}).get(argument) == expected:
                return ''
        return f'ציפינו שהבוט {label}. הוא לא עשה את זה'

    return Check(f'called_with:{tool}:{argument}', f'הבוט {label}', test)


def human(wanted: bool = True) -> Check:
    def test(m: Moment) -> str:
        if m.request_human == wanted:
            return ''
        return 'ציפינו שהבוט יסמן שהשיחה צריכה נציג. הוא לא סימן' if wanted else 'הבוט סימן שהשיחה צריכה נציג, בלי שהיה צורך'

    return Check(f'human:{wanted}', 'הבוט מעביר לנציג' if wanted else 'הבוט לא מעביר לנציג', test)


def tagged(tag: str) -> Check:
    def test(m: Moment) -> str:
        return '' if any(row.get('tag') == tag for row in m.tags) else f'ציפינו שהבוט יסמן על השיחה את התגית "{tag}". הוא לא סימן'

    return Check(f'tagged:{tag}', f'הבוט מסמן את התגית "{tag}"', test)


def has(pattern: str, label: str) -> Check:
    def test(m: Moment) -> str:
        return '' if re.search(pattern, m.reply) else f'ציפינו ש{label}. הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'has:{label}', label, test)


def has_not(pattern: str, label: str) -> Check:
    def test(m: Moment) -> str:
        found = re.search(pattern, m.reply)
        return f'ציפינו ש{label}. הבוט כתב: "{_quote(found.group(0))}"' if found else ''

    return Check(f'has_not:{label}', label, test)


def link(key: str, label: str) -> Check:
    def test(m: Moment) -> str:
        url = ((m.world.get('facts') or {}).get('links') or {}).get(key) or ''
        if not url:
            return f'בידע הבוט אין קישור {label}, ולכן אי אפשר לבדוק את התשובה'
        return '' if url.rstrip('/') in unquote(m.reply) else f'ציפינו לקישור {label}. הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'link:{key}', f'הבוט שולח את הקישור {label}', test)


def no_link(key: str, label: str) -> Check:
    def test(m: Moment) -> str:
        url = ((m.world.get('facts') or {}).get('links') or {}).get(key) or ''
        return f'הבוט שלח את הקישור {label}, ולא היה צריך בשלב הזה' if url and url.rstrip('/') in unquote(m.reply) else ''

    return Check(f'no_link:{key}', f'הבוט לא שולח את הקישור {label}', test)


def says_fact(path: str, label: str) -> Check:
    """The text at `path` of the facts (what to bring to a trial) is in the answer, as the system holds it."""
    def test(m: Moment) -> str:
        wanted = str(_fact(m.world, path) or '').strip()
        if not wanted:
            return f'במערכת לא כתוב {label}, ולכן אי אפשר לבדוק את התשובה'
        words = [word for word in re.findall(r'[א-ת]{3,}', wanted)]
        found = sum(1 for word in words if word in m.reply)
        if words and found >= max(1, (len(words) + 1) // 2):
            return ''
        return f'ציפינו ל{label} כפי שכתוב במערכת ("{_quote(wanted, 80)}"). הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'says_fact:{path}', f'הבוט אומר את {label} כפי שכתוב במערכת', test)


def lines_at_most(count: int) -> Check:
    def test(m: Moment) -> str:
        lines = [line for line in m.reply.split('\n') if line.strip()]
        return f'ציפינו לתשובה של עד {count} שורות. הבוט כתב {len(lines)}: "{_quote(m.reply)}"' if len(lines) > count else ''

    return Check(f'lines_at_most:{count}', f'תשובה של עד {count} שורות', test)


def not_the_same_again() -> Check:
    def test(m: Moment) -> str:
        if m.previous_reply and ' '.join(m.reply.split()) == ' '.join(m.previous_reply.split()):
            return f'הבוט שלח שוב בדיוק את אותה הודעה: "{_quote(m.reply)}"'
        return ''

    return Check('not_the_same_again', 'הבוט לא שולח שוב את אותה הודעה', test)


def not_missing_info() -> Check:
    def test(m: Moment) -> str:
        if _MISSING_INFO.search(m.reply):
            return 'הבוט ענה "אין לי את הפרט הזה" לפני שבירר באיזה חוג מדובר ולפני שבדק במערכת'
        return ''

    return Check('not_missing_info', '"אין לי את הפרט" לא נאמר לפני שבודקים', test)


def run(checks, moment: Moment, *, turn: int) -> list:
    """The verdicts of a turn: [{id, turn, kind, title, status, explain}]. A check that breaks is a failed check that says so."""
    rows = []
    for check in checks:
        try:
            explain = check.test(moment)
        except Exception as exc:  # a check must not take the run down with it
            explain = f'הבדיקה עצמה נכשלה ({type(exc).__name__}) — לדווח למפתח'
        rows.append({
            'id': check.id, 'turn': turn, 'kind': 'hard', 'title': check.title,
            'status': 'failed' if explain else 'passed', 'explain': explain,
        })
    return rows


# --- trial dates: only the ones the site offers now (the owner, 12.10.2026) ---------------------------------------------
#
# "The dates one can register on for a trial lesson — by the system, exactly." The tools hand the bot the dates the
# registration form offers at that moment (`trial.dates`), the ones it shows as full (`trial.full_dates`) and the ones
# it skips (`trial.not_offered`) — apps/wahub/trial_offer.py. `grounded_dates` above is content with any date a tool
# returned; these hold an answer to the dates that can really be booked.

# A date said to be taken or closed is the truth about it, not an offer of it.
_UNAVAILABLE = re.compile(
    r'מלא|אין מקום|לא נשאר|תפוס|לא מתקיים|לא יתקיים|בוטל|מבוטל|לא פתוח|סגור|אי אפשר|לא ניתן|לא אפשרי|אין שיעור|אין ניסיון|לא זמין|חופש|כבר התחיל'
)
_NOT = re.compile(r'לא|אין|אי אפשר')


def _date_text(value: tuple) -> str:
    return f'{value[0]:02d}.{value[1]:02d}'


def trial_dates_evidence(calls: list) -> tuple:
    """
    (open, closed): the dates the tools returned as ones a trial can be booked
    on, and the ones they returned as full or not offered — (day, month) each.
    A date open in one class and closed in another counts as open: which class
    the answer meant is not for a hard check to guess.
    """
    open_, closed = set(), set()

    def walk(value) -> None:
        if isinstance(value, dict):
            trial = value.get('trial')
            if isinstance(trial, dict):
                for row in trial.get('dates') or []:
                    open_.update(dates_in(str(row.get('date') if isinstance(row, dict) else row)))
                for key in ('full_dates', 'not_offered'):
                    for row in trial.get(key) or []:
                        closed.update(dates_in(str(row.get('date') if isinstance(row, dict) else row)))
            for inner in value.values():
                walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                walk(inner)

    for call in calls:
        walk(call.get('result'))
    return open_, closed - open_


def _open_trial_dates(m: Moment) -> str:
    _open, closed = trial_dates_evidence(m.all_calls)
    for line in m.reply.split('\n'):
        offered = [value for value in dates_in(line) if value in closed]
        if offered and not _UNAVAILABLE.search(line):
            return (
                f'הבוט הציע את התאריך {", ".join(_date_text(value) for value in dict.fromkeys(offered))} — באתר התאריך הזה מלא או לא פתוח לשיעור ניסיון. '
                'מציעים רק תאריך שהמערכת החזירה כפנוי'
            )
    return ''


def only_open_trial_dates() -> Check:
    """Every date in the answer is one the tools returned as open for a trial — or is said to be taken or closed."""
    def test(m: Moment) -> str:
        open_, _closed = trial_dates_evidence(m.all_calls)
        for line in m.reply.split('\n'):
            strange = [value for value in dates_in(line) if value not in open_]
            if strange and not _UNAVAILABLE.search(line):
                known = ', '.join(_date_text(value) for value in sorted(open_, key=lambda value: (value[1], value[0]))) or 'אין כרגע תאריך פנוי'
                return (
                    f'הבוט הציע את התאריך {", ".join(_date_text(value) for value in dict.fromkeys(strange))} — זה לא אחד מהתאריכים '
                    f'שהמערכת החזירה כפנויים לשיעור ניסיון ({known})'
                )
        return ''

    return Check('only_open_trial_dates', 'כל תאריך שהבוט מציע לשיעור ניסיון הוא תאריך שהאתר מציע עכשיו', test)


def says_open_trial_date(path: str) -> Check:
    """
    The answer names a date the site offers now for the class at `path` — by
    the facts read from the system, not by the tool. When the site offers none,
    the answer says that there is none, and offers no date.
    """
    def test(m: Moment) -> str:
        wanted = [str(value) for value in (_fact(m.world, f'{path}.trial_dates.open') or [])]
        said = {_date_text(value) for line in m.reply.split('\n') if not _UNAVAILABLE.search(line) for value in dates_in(line)}
        if not wanted:
            if said:
                return f'באתר אין כרגע תאריך פנוי לשיעור ניסיון בחוג הזה, והבוט הציע: {", ".join(sorted(said))}'
            if not re.search(r'אין|מלא|סגור|אי אפשר|לא ניתן', m.reply):
                return f'באתר אין כרגע תאריך פנוי לשיעור ניסיון בחוג הזה, וציפינו שהבוט יאמר זאת. הוא ענה: "{_quote(m.reply)}"'
            return ''
        if said & set(wanted):
            return ''
        return f'ציפינו לתאריך שהאתר מציע עכשיו לשיעור ניסיון ({", ".join(wanted[:3])}). הבוט ענה: "{_quote(m.reply)}"'

    return Check(f'says_open_trial_date:{path}', 'הבוט אומר תאריך שהאתר מציע עכשיו לשיעור ניסיון — או שאין כרגע', test)


def not_closed_trial_date(path: str) -> Check:
    """No date the site shows as full, or skips, for the class at `path` is offered — by the facts read from the system."""
    def test(m: Moment) -> str:
        facts = _fact(m.world, f'{path}.trial_dates') or {}
        closed = (set(facts.get('full') or []) | set(facts.get('skipped') or [])) - set(facts.get('open') or [])
        for line in m.reply.split('\n'):
            offered = [_date_text(value) for value in dates_in(line) if _date_text(value) in closed]
            if offered and not _UNAVAILABLE.search(line):
                what = 'מלא' if set(offered) & set(facts.get('full') or []) else 'לא פתוח לשיעור ניסיון (חופשה או שיעור שבוטל)'
                return f'הבוט הציע את התאריך {", ".join(dict.fromkeys(offered))} — באתר התאריך הזה {what}'
        return ''

    return Check(f'not_closed_trial_date:{path}', 'הבוט לא מציע תאריך מלא, חופשה או שיעור שבוטל', test)


def _said_without_no(reply: str, word: str) -> str:
    """The line that says `word` ("היום", "השבוע") with no "לא" or "אין" beside it."""
    return next((line for line in reply.split('\n') if word in line and not _NOT.search(line)), '')


def today_as_the_system_has_it(path: str) -> Check:
    """When no trial can be booked today for the class at `path`, the answer does not say that one can."""
    def test(m: Moment) -> str:
        facts = _fact(m.world, f'{path}.trial_dates') or {}
        line = _said_without_no(m.reply, 'היום')
        if not facts.get('today_open') and line:
            return f'לפי המערכת אי אפשר להירשם היום לשיעור ניסיון בחוג הזה, והבוט כתב: "{_quote(line)}"'
        return ''

    return Check(f'today_as_the_system_has_it:{path}', '"היום" נאמר רק כשהאתר מציע שיעור ניסיון היום', test)


def this_week_as_the_system_has_it(path: str) -> Check:
    """When no trial can be booked this week for the class at `path`, the answer does not say that one can."""
    def test(m: Moment) -> str:
        facts = _fact(m.world, f'{path}.trial_dates') or {}
        line = _said_without_no(m.reply, 'השבוע')
        if not facts.get('this_week_open') and line:
            return f'לפי המערכת אין השבוע תאריך פנוי לשיעור ניסיון בחוג הזה, והבוט כתב: "{_quote(line)}"'
        return ''

    return Check(f'this_week_as_the_system_has_it:{path}', '"השבוע" נאמר רק כשהאתר מציע שיעור ניסיון השבוע', test)


GENERAL = GENERAL + (
    Check('open_trial_dates', 'תאריך מלא או סגור לא מוצע לשיעור ניסיון', _open_trial_dates),
)
GENERAL_IDS = tuple(check.id for check in GENERAL)
