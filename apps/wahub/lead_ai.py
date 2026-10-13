"""
What the bot understands about a lead (docs/WAHUB-CONTRACT-LEADS-V2.md, א).

It is worked out in the summary pass — the very call to Claude that fills "מה
ידוע" (analysis.py), never a second one — and by keyword rules when there is no
key or Claude did not answer. The result is one dictionary kept on the contact
(`Contact.lead_ai`, written by analysis.py alone):

    intent          what the conversation is about, one of INTENTS — which decides whether
                    this is a lead for a class at all (LEAD_INTENTS); '' when nothing says
    empty_opening   they wrote nothing of their own: the ad's ready-made line, or "היי"
    interest        'yes' / 'unsure' / 'no' / 'registered' ("we already signed up");
                    '' when nothing says
    quote, quote_at the customer's own words behind a 'no' or a 'registered', and when
                    they wrote them
    blocker         what holds them back, one of BLOCKERS; '' when nothing does
    blocker_text    one sentence in Hebrew
    branch_id, branch_name, city
    external        the branch is an external one: its registration is the municipality's
    course_type, course_type_id, course_id
    child           {name, age_years, age_months, grade, birth_date}
    come_back       what was said about "when", as it was said
    come_back_on    the day itself — worked out HERE, never by the model
    come_back_why   one sentence
    link_sent       a registration link went out in a message Kogo saw
    summary         one sentence: why this is a lead
    source          'ai' / 'rules'

Three things hold whatever the model answered: a field it was not sure of stays
empty; "not interested" and "already registered" stand only with the customer's
own words, found in a message they themselves wrote; and every date is
arithmetic done in this module (`come_back_day`) from what was said and the day
it was said on.

Nothing here writes to the database or calls another company.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from urllib.parse import quote as url_quote, unquote as url_unquote

from django.utils import timezone

from apps.wahub.models import (
    ANALYSIS_AI,
    ANALYSIS_RULES,
    DELIVERED_STATUSES,
    DIRECTION_OUT,
    KIND_LINK,
    KIND_SPECIAL_DAY,
    SCOPE_BUSINESS,
    SENDER_CUSTOMER,
    KnowledgeItem,
    Message,
)

VERSION = 1

# What the conversation is about (docs/WAHUB-CONTRACT-LEADS-V2.md, ח.1). Only the first three
# are leads for a class; the rest get no lead stage and no follow-up.
INTENT_CLASS = 'class'
INTENT_TRIAL = 'trial'
INTENT_ADD_CLASS = 'add_class'
INTENT_LABELS = {
    INTENT_CLASS: 'מתעניין בחוג',
    INTENT_TRIAL: 'רוצה שיעור ניסיון',
    INTENT_ADD_CLASS: 'לקוח קיים — רוצה להוסיף',
    'birthday_greeting': 'ברכת יום הולדת',
    'birthday_party': 'הפעלה ליום הולדת',
    'rental': 'השכרת סטודיו',
    'supplier': 'ספק שמציע שירות',
    'store': 'קנייה בחנות',
    'auto_reply': 'מענה אוטומטי של עסק',
    'existing_question': 'לקוח קיים — שאלה תפעולית',
    'other': 'פנייה שאינה על חוג',
}
INTENTS = tuple(INTENT_LABELS)
LEAD_INTENTS = (INTENT_CLASS, INTENT_TRIAL, INTENT_ADD_CLASS)

INTEREST_YES = 'yes'
INTEREST_UNSURE = 'unsure'
INTEREST_NO = 'no'
# "כבר נרשמנו": the lead is closed as registered on the customer's word, with no argument (ח.7).
INTEREST_REGISTERED = 'registered'
INTERESTS = (INTEREST_YES, INTEREST_UNSURE, INTEREST_NO, INTEREST_REGISTERED)
# What is never taken from the model without the customer's own words beside it.
QUOTED_INTERESTS = (INTEREST_NO, INTEREST_REGISTERED)

BLOCKER_WILL_CHECK = 'will_check'
BLOCKER_ASK_SOMEONE = 'ask_someone'
BLOCKER_TOO_YOUNG = 'too_young'
BLOCKER_LATER = 'later'
BLOCKER_NO_BRANCH = 'no_branch'
BLOCKER_NO_CLASS = 'no_class'
BLOCKER_PRICE = 'price'
BLOCKER_SCHEDULE = 'schedule'
BLOCKER_FULL = 'full'
BLOCKER_COMMITMENT = 'commitment'
BLOCKER_GROUP = 'group'
BLOCKER_RESCHEDULE_TRIAL = 'reschedule_trial'
BLOCKER_CHILD_NOT_READY = 'child_not_ready'
BLOCKER_INSTRUCTOR = 'instructor'
BLOCKER_SITE_PROBLEM = 'site_problem'
BLOCKER_OPEN_QUESTION = 'open_question'
BLOCKER_CALL_SOMEONE = 'call_someone'
BLOCKER_OTHER = 'other'
BLOCKER_LABELS = {
    BLOCKER_WILL_CHECK: 'אמר שיבדוק ויחזור',
    BLOCKER_ASK_SOMEONE: 'צריך לבדוק עם מישהו אחר',
    BLOCKER_TOO_YOUNG: 'הילד צעיר מדי',
    BLOCKER_LATER: 'לא עכשיו, בזמן אחר',
    BLOCKER_NO_BRANCH: 'אין סניף באזור שלו',
    BLOCKER_NO_CLASS: 'אין חוג שמתאים לו',
    BLOCKER_PRICE: 'המחיר עצר אותו',
    BLOCKER_SCHEDULE: 'הימים או השעות לא מתאימים',
    BLOCKER_FULL: 'החוג מלא',
    BLOCKER_COMMITMENT: 'חושש מהתחייבות',
    BLOCKER_GROUP: 'שואל על הרכב הקבוצה',
    BLOCKER_RESCHEDULE_TRIAL: 'רוצה מועד אחר לשיעור הניסיון',
    BLOCKER_CHILD_NOT_READY: 'הילד לא רצה — אולי בשנה הבאה',
    BLOCKER_INSTRUCTOR: 'מחפש מדריך מסוים',
    BLOCKER_SITE_PROBLEM: 'ניסה להירשם ונתקע באתר',
    BLOCKER_OPEN_QUESTION: 'שאל ולא קיבל תשובה',
    BLOCKER_CALL_SOMEONE: 'ביקש שנדבר עם מישהו אחר',
    BLOCKER_OTHER: 'סיבה אחרת',
}
BLOCKERS = tuple(BLOCKER_LABELS)
# Said, and waiting for the customer to come back by themselves.
CHECKING_BLOCKERS = (BLOCKER_WILL_CHECK, BLOCKER_ASK_SOMEONE)
# Nothing the business has fits them today.
NOTHING_TO_OFFER_BLOCKERS = (BLOCKER_NO_BRANCH, BLOCKER_NO_CLASS, BLOCKER_FULL)
# A person has to do something before anything else: the question nobody answered, a trial to
# move, somebody to call. (Stuck on the site has a stage of its own.)
OFFICE_BLOCKERS = (BLOCKER_OPEN_QUESTION, BLOCKER_RESCHEDULE_TRIAL, BLOCKER_CALL_SOMEONE)
# The bot does not come back by itself: there is nothing new to tell them (ח.10).
NO_NUDGE_BLOCKERS = (BLOCKER_PRICE, BLOCKER_SCHEDULE)

COME_BACK_AGE = 'age'
COME_BACK_OFFSET = 'offset'
COME_BACK_AFTER = 'after'
COME_BACK_DATE = 'date'
COME_BACK_KINDS = (COME_BACK_AGE, COME_BACK_OFFSET, COME_BACK_AFTER, COME_BACK_DATE)

# The youngest age a class takes (the first age group, 3–4.5: apps/courses/age_groups.py).
MIN_AGE_YEARS = 3
# "Not interested" is read only in what the customer wrote last: this many of their messages back.
QUOTE_WITHIN = 3
QUOTE_CHARS = 200
SENTENCE_CHARS = 200
SUMMARY_CHARS = 240
# Special days this close to one another are one holiday season: "after the holidays" is after the last of them.
SEASON_GAP = timedelta(days=7)
# How far ahead "after the holidays" is looked for, and the latest day anybody is asked to come back on.
HORIZON = timedelta(days=370)
FAR_AHEAD = timedelta(days=366 * 4)


# --- what Claude is asked for (nested under `lead` in analysis.AI_SCHEMA) ---------------------------

_TEXT = {'type': 'string'}
_NUMBER_OR_NOTHING = {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}


def _object(properties: dict) -> dict:
    return {
        'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False,
    }


LEAD_SCHEMA = _object({
    'intent': {'type': 'string', 'enum': [*INTENTS, '']},
    'empty_opening': {'type': 'boolean'},
    'interest': {'type': 'string', 'enum': [*INTERESTS, '']},
    'quote': _TEXT,
    'blocker': {'type': 'string', 'enum': [*BLOCKERS, '']},
    'blocker_text': _TEXT,
    'child': _object({
        'name': _TEXT,
        'age_years': _NUMBER_OR_NOTHING,
        'age_months': _NUMBER_OR_NOTHING,
        'grade': _TEXT,
        'birth_date': _TEXT,
    }),
    'come_back': _object({
        'kind': {'type': 'string', 'enum': [*COME_BACK_KINDS, '']},
        'said_on': _TEXT,
        'years': _NUMBER_OR_NOTHING,
        'months': _NUMBER_OR_NOTHING,
        'weeks': _NUMBER_OR_NOTHING,
        'days': _NUMBER_OR_NOTHING,
        'until_years': _NUMBER_OR_NOTHING,
        'what': _TEXT,
        'iso': _TEXT,
    }),
    'come_back_why': _TEXT,
    'summary': _TEXT,
})

LEAD_PROMPT = """\
- lead: מה מבינים על הפנייה כליד, כדי לדעת מי עושה את הצעד הבא ומתי. גם כאן רק מה שנאמר בפועל; מה שלא ברור נשאר ריק.
  - intent: על מה השיחה. class = מתעניין בחוג; trial = רוצה שיעור ניסיון; add_class = לקוח שכבר רשום ורוצה חוג נוסף, לרשום אח, או קבוצה אחרת. כל השאר אינם לידים לחוג: birthday_greeting = ברכת יום הולדת מהדמות; birthday_party = הפעלה או הופעה ליום הולדת; rental = השכרת סטודיו; supplier = מישהו שמציע שירות לעסק; store = קניית חליפה או מוצר; auto_reply = המספר עונה בהודעה אוטומטית של עסק; existing_question = לקוח רשום עם שאלה תפעולית (יש שיעור היום? להחליף יום? חיוב? ביטול?); other = כל דבר אחר שאינו חוג. לא ברור — ריק.
  - empty_opening: true רק כשהלקוח לא כתב שום פרט משלו — רק את הנוסח המוכן של המודעה ("אפשר לקבל מידע נוסף על זה?") או ברכה כמו "היי".
  - interest: yes = רוצה להירשם או לקבוע ניסיון; unsure = מתעניין ועוד לא החליט; no = רק כשהלקוח אמר במפורש שאינו מעוניין ("לא רלוונטי", "תודה, לא", "טעות", "יקר לי, אני מוותרת", "מצאנו מקום אחר", "תורידו אותי"); registered = הלקוח אמר שכבר נרשם או שהוא כבר לקוח ("בסוף נרשמנו", "אנחנו כבר אצלכם"). שתיקה, "לא עכשיו", "אבדוק ואחזור", הודעה שלא נענתה, "הילד לא רצה, אולי בשנה הבאה" — אינם no. גם "לא" לבדו אינו no: "לא" כתשובה ל"האם אתם מגיעים?" הוא ביטול שיעור, ו"לא!!" אחרי תשובה של הבוט פירושו "זה לא נכון". לא ברור — ריק.
  - quote: כש-interest הוא no או registered — המילים של הלקוח עצמו שעליהן זה נשען, מועתקות בדיוק מההודעה שלו. בלי ציטוט כזה אל תסמן no או registered. בכל מקרה אחר ריק.
  - blocker: מה עוצר אותו מלהירשם עכשיו, אם משהו. site_problem = ניסה להירשם ולא הצליח ("הקישור לא עובד", "החוג לא מופיע", "כתוב מלא", "לא נותן לאשר"); open_question = שאל שאלה, קיבל "אין לי את הפרט" או הפניה למשרד, ולא קיבל תשובה; call_someone = ביקש שנדבר או נתקשר למישהו אחר ("עדיף שתדברו עם בעלי"); reschedule_trial = רשום לשיעור ניסיון ורוצה מועד אחר; will_check = אמר שיבדוק או יחשוב ויחזור; ask_someone = צריך לבדוק בעצמו עם מישהו אחר (בן זוג, הילד, סבתא); too_young = הילד צעיר מהגיל שממנו יש חוג; child_not_ready = הילד לא רצה או לא מוכן עדיין ("אולי בשנה הבאה"); later = לא עכשיו, בזמן אחר; no_branch = אין סניף באזור שלו; no_class = אין חוג שמתאים לגיל או לתחום שביקש; full = אין מקום בחוג; commitment = חושש מהתחייבות ("ומה אם זה לא יתאים? מחזירים כסף?"); price = המחיר; schedule = הימים או השעות לא מתאימים; group = שואל כמה ילדים, אילו גילים, או אם הקבוצה נפתחה; instructor = ההחלטה תלויה במדריך מסוים; other = סיבה אחרת שנאמרה. לא נאמר דבר שעוצר — ריק.
  - blocker_text: משפט אחד קצר על מה שעוצר, במילים של השיחה ("אמרה שתבדוק עם הבת שלה ותחזור"). ב-open_question — השאלה עצמה. ריק כש-blocker ריק.
  - child: מה שנאמר על הילד. name = שמו, רק אם ההורה כתב אותו; age_years ו-age_months = הגיל במספרים ("בן שנתיים ושבעה חודשים" ← 2 ו-7; "בת 5" ← 5 ו-null); grade = הכיתה כפי שנאמרה; birth_date = תאריך לידה אם נאמר, YYYY-MM-DD. מה שלא נאמר: null או ריק. כמה ילדים — הילד שעליו הפנייה.
  - come_back: אם נאמר מתי לחזור אליו, או שהילד צעיר מדי — הנתון עצמו כפי שנאמר, בלי לחשב תאריך. kind: age = הילד צעיר מדי וצריך לחכות שיגדל (years ו-months = גילו ביום שזה נאמר; until_years = הגיל שממנו אפשר, אם נאמר, אחרת null); offset = "בעוד חצי שנה", "בעוד שבועיים" (months, weeks או days; השאר null); after = אחרי מועד שנאמר בשמו, "אחרי החגים", "אחרי פסח" (what = שם המועד כפי שנאמר); date = תאריך מפורש או חודש מפורש (iso = YYYY-MM-DD; חודש בלי יום ← ה-1 בחודש). said_on = התאריך של ההודעה שבה זה נאמר, YYYY-MM-DD. לא נאמר מתי — kind ריק, והשאר null או ריק.
  - come_back_why: משפט קצר למה לחזור אז ("הילד יהיה בן 3", "ביקשה שנחזור אחרי החגים"). ריק כשאין.
  - summary: משפט אחד: למה הוא ליד ואיפה זה עומד. למשל: "אמא לילד בן 5 בראש העין, שאלה על קפוארה, קיבלה קישור ולא נרשמה"."""


# --- dates: arithmetic, never the model's ------------------------------------------------------------

def add_months(day: date, months: int) -> date:
    """`day` moved by whole months; the 31st of a month that has no 31st is its last day."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _whole(value, low: int, high: int):
    """A number the model (or a rule) gave, when it is one and inside what makes sense; else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _iso_day(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or '').strip()[:10])
    except ValueError:
        return None


def birthday(*, years: int, months: int, said_on: date, until_years: int, birth_date: date | None = None) -> date | None:
    """
    The day a child reaches `until_years`: from the birth date when it was said,
    else from the age and the day it was said on (aged 2 and 7 months on
    12.10.2026 — born about 12.3.2024, three on 12.3.2027). None when the child
    is already that old: there is nothing to wait for.
    """
    born = birth_date or add_months(said_on, -(years * 12 + months))
    reaches = add_months(born, until_years * 12)
    return reaches if reaches > said_on else None


def special_day_spans(day_from: date, day_to: date) -> list[tuple[date, date, str]]:
    """The whole business's special days that touch the range, oldest first: (first day, last day, title)."""
    spans = []
    for item in KnowledgeItem.objects.filter(kind=KIND_SPECIAL_DAY, is_active=True, scope_level=SCOPE_BUSINESS):
        data = item.data or {}
        start = _iso_day(data.get('date_from'))
        end = _iso_day(data.get('date_to') or data.get('date_from'))
        if start is None or end is None or end < day_from or start > day_to:
            continue
        spans.append((start, max(start, end), item.title or ''))
    return sorted(spans)


_NAMED_WORDS = re.compile(r'[א-ת]{3,}')
# "The holidays" in general, as against one holiday by its name.
_ANY_HOLIDAY = {'חגים', 'החגים', 'חג', 'החג', 'מועד', 'המועד', 'מועדים', 'המועדים', 'החופש', 'חופש', 'חופשה', 'החופשה'}


def after_special_day(what: str, said_on: date, spans=None) -> date | None:
    """
    "אחרי החגים": the first day after the nearest special day on the list of
    special days ("שעות ומועדים") — and after the ones that follow it closely,
    which are one holiday season. A holiday named ("אחרי פסח") is looked for by
    its name first. None when the list has nothing ahead: no date is invented.
    """
    spans = special_day_spans(said_on, said_on + HORIZON) if spans is None else spans
    ahead = [span for span in spans if span[1] >= said_on]
    if not ahead:
        return None
    words = [word for word in _NAMED_WORDS.findall(what or '') if word not in _ANY_HOLIDAY and word != 'אחרי']
    named = [
        span for span in ahead
        if any(word in span[2] or word.removeprefix('ה') in span[2] for word in words)
    ] if words else []
    if words and not named:
        # A holiday that was named and is not on the list: nothing is known about when it ends.
        return None
    first = (named or ahead)[0]
    end = first[1]
    for start, last, _title in ahead[ahead.index(first) + 1:]:
        if start - end > SEASON_GAP:
            break
        end = max(end, last)
    return end + timedelta(days=1)


def come_back_day(raw: dict, *, said_on: date, child: dict | None = None, spans=None) -> date | None:
    """
    The day to come back, out of what was said (docs/WAHUB-CONTRACT-LEADS-V2.md, א):

        {kind: 'age', years, months[, until_years]}   the day the child is old enough
        {kind: 'offset', months | weeks | days}       that long after the day it was said
        {kind: 'after', what}                         the first day after that holiday
        {kind: 'date', iso}                           the date itself

    None when what was said does not make a day that is still ahead of the day
    it was said on.
    """
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get('kind')
    child = child or {}
    day = None
    if kind == COME_BACK_AGE:
        years = _whole(raw.get('years'), 0, 17)
        months = _whole(raw.get('months'), 0, 11) or 0
        born = _iso_day(child.get('birth_date'))
        if years is None and born is None:
            years, months = _whole(child.get('age_years'), 0, 17), _whole(child.get('age_months'), 0, 11) or 0
        if years is None and born is None:
            return None
        until = _whole(raw.get('until_years'), 1, 18) or MIN_AGE_YEARS
        day = birthday(years=years or 0, months=months, said_on=said_on, until_years=until, birth_date=born)
    elif kind == COME_BACK_OFFSET:
        months = (_whole(raw.get('months'), 0, 48) or 0) + 12 * (_whole(raw.get('years'), 0, 4) or 0)
        days = (_whole(raw.get('days'), 0, 400) or 0) + 7 * (_whole(raw.get('weeks'), 0, 60) or 0)
        if months or days:
            day = add_months(said_on, months) + timedelta(days=days)
    elif kind == COME_BACK_AFTER:
        day = after_special_day(str(raw.get('what') or ''), said_on, spans)
    elif kind == COME_BACK_DATE:
        day = _iso_day(raw.get('iso'))
    if day is None or day <= said_on or day > said_on + FAR_AHEAD:
        return None
    return day


def next_school_year(said_on: date) -> date:
    """The first of September that follows the day it was said on."""
    first = date(said_on.year, 9, 1)
    return first if first > said_on else date(said_on.year + 1, 9, 1)


def _why(raw: dict, child: dict, day: date) -> str:
    """One sentence for a day nobody explained."""
    kind = raw.get('kind')
    if kind == COME_BACK_AGE:
        until = _whole(raw.get('until_years'), 1, 18) or MIN_AGE_YEARS
        name = (child.get('name') or '').strip()
        return f'ימלאו ל{name} {until}' if name else f'הילד יהיה בן {until}'
    if kind == COME_BACK_AFTER:
        what = ' '.join(str(raw.get('what') or '').split())
        return f'ביקש לחזור אליו אחרי {what.removeprefix("אחרי ").strip()}' if what else 'ביקש לחזור אליו אחרי החגים'
    return 'ביקש לחזור אליו אז'


# --- the registration link ---------------------------------------------------------------------------

def _needle(url: str) -> str:
    """An address as it is looked for in a message: no scheme, no www, no slash at the end, lower case."""
    text = url_unquote(str(url or '').strip())
    text = re.sub(r'^https?://', '', text, flags=re.I)
    text = re.sub(r'^www\.', '', text, flags=re.I)
    return text.rstrip('/').casefold()


def registration_needles() -> list[str]:
    """
    The registration link(s) of the business as the bot's knowledge has them
    (the link whose key is "registration"), each spelled the two ways WhatsApp
    carries a Hebrew address — as it reads, and percent-encoded. Until the
    knowledge is filed, the address the old bot's texts used.
    """
    urls = [
        (item.data or {}).get('url') for item in
        KnowledgeItem.objects.filter(kind=KIND_LINK, is_active=True, key__startswith='registration')
    ]
    urls = [url for url in urls if url]
    if not urls:
        from apps.wahub import knowledge_seed

        urls = [knowledge_seed.VARIABLES['{קישור_הרשמה}']]
    needles = []
    for url in urls:
        plain = _needle(url)
        if not plain:
            continue
        host, _, path = plain.partition('/')
        encoded = f'{host}/{url_quote(path, safe="/-_.~")}'.casefold() if path else host
        needles += [plain, encoded]
    return list(dict.fromkeys(needles))


def has_registration_link(text: str, needles: list[str] | None = None) -> bool:
    """The message carries the registration link — an external branch's own link is another matter."""
    text = (text or '').casefold()
    if '.' not in text:
        # No address in it at all: the knowledge is not asked.
        return False
    needles = registration_needles() if needles is None else needles
    return any(needle in text for needle in needles) or any(needle in url_unquote(text) for needle in needles)


def link_sent_at(contact):
    """
    When a registration link last went out to this contact in a message Kogo
    kept — the old bot's (copied by ManyChat), the new bot's, the office's.
    None when none did. One query.
    """
    from django.db.models import Q

    needles = registration_needles()
    if not needles:
        return None
    carries = Q()
    for needle in needles:
        carries |= Q(text__icontains=needle)
    return (
        Message.objects.filter(contact=contact, direction=DIRECTION_OUT, status__in=DELIVERED_STATUSES)
        .filter(carries).order_by('-sent_at').values_list('sent_at', flat=True).first()
    )


# --- the customer's own words ------------------------------------------------------------------------

_LOOSE = re.compile(r'[\s"\'`׳״.,!?:;()\-–—…]+')


def _loose(text: str) -> str:
    return _LOOSE.sub(' ', text or '').strip().casefold()


def find_quote(quote: str, lines) -> object | None:
    """
    The customer's message the quote was taken from, among the last QUOTE_WITHIN
    they wrote; None when the words are not theirs, or are older than that — a
    customer who said "no" and wrote again since is not closed on the old words.
    """
    wanted = _loose(quote)
    if len(wanted) < 3:
        return None
    theirs = [line for line in lines if line.who == SENDER_CUSTOMER and line.text]
    for line in reversed(theirs[-QUOTE_WITHIN:]):
        if wanted in _loose(line.text):
            return line
    return None


def _moment(value) -> str | None:
    return value.isoformat() if value is not None else None


def _short(value, limit: int) -> str:
    return re.sub(r'\s+', ' ', value).strip()[:limit] if isinstance(value, str) else ''


def _said_on(raw: dict, lines, today: date, notes=()) -> date:
    """
    The day it was said on: the model's, when it is a day of the conversation
    (or of a note the office wrote about it); else the customer's last message.
    """
    days = [timezone.localtime(line.sent_at).date() for line in lines if line.sent_at is not None]
    said = _iso_day(raw.get('said_on')) if isinstance(raw, dict) else None
    spoken = days + [timezone.localtime(note.sent_at).date() for note in notes if note.sent_at is not None]
    if said is not None and spoken and min(spoken) <= said <= max(spoken):
        return said
    theirs = [
        timezone.localtime(line.sent_at).date() for line in lines
        if line.who == SENDER_CUSTOMER and line.sent_at is not None
    ]
    return theirs[-1] if theirs else (days[-1] if days else today)


def _blank(source: str) -> dict:
    return {
        'v': VERSION, 'source': source,
        'intent': '', 'empty_opening': False,
        'interest': '', 'quote': '', 'quote_at': None,
        'blocker': '', 'blocker_text': '',
        'branch_id': None, 'branch_name': '', 'city': '', 'external': False,
        'course_type': '', 'course_type_id': None, 'course_id': None,
        'child': {'name': '', 'age_years': None, 'age_months': None, 'grade': '', 'birth_date': ''},
        'come_back': {}, 'come_back_on': None, 'come_back_why': '',
        'link_sent': False, 'summary': '',
    }


def _place(lead: dict, known, course_types, external_places=()) -> None:
    """
    Where and what, from the card the same pass filled — only what it settled
    on; nothing is guessed. A place that is none of the business's own branches
    is looked for among the external ones: their registration goes through the
    municipality, so nobody can know from Kogo whether the customer signed up.
    """
    lead['branch_id'] = str(known.branch_id) if known.branch_id else None
    lead['branch_name'] = known.branch_name or ''
    lead['city'] = known.city or ''
    if not known.branch_id and external_places and (known.branch_name or known.city):
        from apps.wahub import analysis

        outside = analysis.resolve_branch(known.branch_name or known.city, list(external_places))
        if outside is not None:
            lead.update(branch_id=str(outside.id), branch_name=outside.name, external=True)
            lead['city'] = lead['city'] or outside.city
    lead['course_type'] = known.course_type or ''
    wanted = (known.course_type or '').strip().casefold()
    if wanted:
        exact = [type_id for type_id, name in course_types if (name or '').strip().casefold() == wanted]
        near = exact or [
            type_id for type_id, name in course_types
            if (name or '').strip() and ((name or '').strip().casefold() in wanted or wanted in (name or '').strip().casefold())
        ]
        if len(near) == 1:
            lead['course_type_id'] = str(near[0])


def _settle_day(lead: dict, raw: dict, known, lines, today: date, notes=()) -> None:
    """The day to come back and why, onto the lead; the card's own "they said when" answers when nothing else does."""
    said_on = _said_on(raw, lines, today, notes)
    kind = raw.get('kind') if isinstance(raw, dict) else ''
    if kind in COME_BACK_KINDS:
        day = come_back_day(raw, said_on=said_on, child=lead['child'])
        if day is not None:
            lead['come_back'] = {
                'kind': kind, 'said_on': said_on.isoformat(),
                **{name: raw.get(name) for name in ('years', 'months', 'weeks', 'days', 'until_years') if _whole(raw.get(name), 0, 400) is not None},
                **{name: _short(raw.get(name), 60) for name in ('what', 'iso') if _short(raw.get(name), 60)},
            }
            lead['come_back_on'] = day.isoformat()
            lead['come_back_why'] = lead['come_back_why'] or _why(raw, lead['child'], day)
            return
    lead['come_back_why'] = ''
    if known.callback_on is not None:
        lead['come_back'] = {'kind': COME_BACK_DATE, 'iso': known.callback_on.isoformat()}
        lead['come_back_on'] = known.callback_on.isoformat()
        lead['come_back_why'] = 'אמר מתי לחזור אליו'
    elif lead['blocker'] == BLOCKER_CHILD_NOT_READY:
        # "אולי בשנה הבאה": a lead with a day — the start of the next year of classes — not a "no".
        day = next_school_year(said_on)
        lead['come_back'] = {'kind': COME_BACK_DATE, 'iso': day.isoformat(), 'said_on': said_on.isoformat()}
        lead['come_back_on'] = day.isoformat()
        lead['come_back_why'] = 'הילד לא רצה — לנסות שוב בתחילת השנה הבאה'


def from_model(data, *, lines, known, course_types=(), external_places=(), today: date, link_sent: bool = False,
               notes=()) -> dict:
    """Claude's `lead` object, checked field by field. Anything outside the contract is dropped, not kept."""
    data = data if isinstance(data, dict) else {}
    lead = _blank(ANALYSIS_AI)
    lead['link_sent'] = bool(link_sent)
    _place(lead, known, course_types, external_places)
    lead['intent'] = data.get('intent') if data.get('intent') in INTENTS else ''
    lead['empty_opening'] = data.get('empty_opening') is True

    interest = data.get('interest') if data.get('interest') in INTERESTS else ''
    if interest in QUOTED_INTERESTS:
        line = find_quote(data.get('quote'), lines)
        if line is None:
            # No words of the customer's own behind it: the lead stays open.
            interest = INTEREST_UNSURE
        else:
            lead['quote'] = _short(data.get('quote'), QUOTE_CHARS)
            lead['quote_at'] = _moment(line.sent_at)
    if interest not in QUOTED_INTERESTS:
        # "לא" to our own "כתבו 'לא' ולא אשלח יותר" is kept by the words themselves, whatever the reader
        # made of so short an answer: it is a promise we made, not a matter of understanding.
        stop = asked_to_stop(lines)
        if stop is not None:
            interest = INTEREST_NO
            lead['quote'], lead['quote_at'] = _short(stop.text, QUOTE_CHARS), _moment(stop.sent_at)
    lead['interest'] = interest

    lead['blocker'] = data.get('blocker') if data.get('blocker') in BLOCKERS else ''
    if lead['blocker']:
        lead['blocker_text'] = _short(data.get('blocker_text'), SENTENCE_CHARS) or BLOCKER_LABELS[lead['blocker']]

    child = data.get('child') if isinstance(data.get('child'), dict) else {}
    born = _iso_day(child.get('birth_date'))
    lead['child'] = {
        'name': _short(child.get('name'), 60),
        'age_years': _whole(child.get('age_years'), 0, 18),
        'age_months': _whole(child.get('age_months'), 0, 11),
        'grade': _short(child.get('grade'), 30),
        'birth_date': born.isoformat() if born is not None and born <= today else '',
    }

    lead['come_back_why'] = _short(data.get('come_back_why'), SENTENCE_CHARS)
    _settle_day(lead, data.get('come_back') if isinstance(data.get('come_back'), dict) else {}, known, lines, today, notes)
    lead['summary'] = _short(data.get('summary'), SUMMARY_CHARS)
    return lead


# --- the keyword rules, for a day with no key ------------------------------------------------------------

# Said outright. A question in the same message ("לא מעוניינת בקפוארה, יש מחול?") is not a goodbye,
# and a bare "לא" never is: it answers whatever was asked last (ח.8).
P_NO = re.compile(
    r'לא מעוניינ|לא מעונין|מצאנו (?:כבר )?(?:מקום|חוג|מסגרת|סטודיו) אחר|נרשמנו (?:כבר )?(?:ל|ב)מקום אחר'
    r'|תורידו אותי|תסירו אותי|הסירו אותי|להסיר אותי|אל תשלחו|לא לפנות אלי|ויתרנו|החלטנו לוותר'
    # Asked, in so many words, that the messages stop — the bot writes by itself now, and this is how it is told to.
    r'|תפסיקו לשלוח|תפסיקו לכתוב|הפסיקו לשלוח|להפסיק לשלוח|לא לשלוח לי|אל תכתבו לי|די לשלוח|די להטריד'
    r'|אני מוותרת|אני מוותר\b|אנחנו מוותרים|נוותר'
)
# The whole message, and nothing else in it.
P_NO_WHOLE = re.compile(r'^[\s.,!]*(?:תודה,?\s*לא|לא,?\s*תודה|טעות|זו טעות|זאת טעות)[\s.,!🙏]*$')
# The one bare "לא" that is a goodbye: the answer to our own last reminder, which asks for that very
# word and promises to stop ("אם זה כבר לא רלוונטי תכתבו לי לא ואני מפסיקה", lead_followup_seed.LAST_TIME —
# and, as it was first filed, "כתבו 'לא' ולא אשלח יותר"). Both halves are looked for: "ענו לא" alone
# may be about a lesson, not about hearing from us.
P_INVITED_NO = re.compile(
    r'(?:כתבו|תכתבו|השיבו|ענו|שלחו)(?:\s+(?:לי|לנו))?\s*[\'"׳״‘’“”]?לא[\'"׳״‘’“”]?\s*,?\s*'
    r'ו(?:לא\s+(?:אשלח|נשלח|אכתוב|נכתוב|אפנה|נפנה|אזכיר|נזכיר)'
    r'|(?:אני\s+|אנחנו\s+)?(?:אפסיק|נפסיק|מפסיקה|מפסיק|מפסיקים))'
)
P_BARE_NO = re.compile(r'^[\s.,!\'"׳״‘’“”]*לא[\s.,!\'"׳״‘’“”🙏]*$')
# "We already signed up" — not about a trial lesson, which is no registration.
P_SAYS_REGISTERED = re.compile(
    r'כבר נרשמנו|בסוף נרשמנו|בסוף רשמנו|נרשמנו כבר|כבר רשמנו|כבר רשמתי|כבר רשומ|כבר אצלכם|כבר לקוח'
)
P_TRIAL_WORD = re.compile(r'ניסיון|נסיון')

# What the conversation is about, when it is not a class (ח.1).
P_AD_LINE = re.compile(r'מידע נוסף על זה|more info on this|узнать об этом', re.I)
P_GREETING = re.compile(
    r'^[\s!.,?🙂👋🙏]*(?:היי+|הי|שלום(?: רב)?|אהלן|בוקר טוב|ערב טוב|צהריים טובים|מה נשמע|מה שלומכם)?[\s!.,?🙂👋🙏]*$'
)
INTENT_RULES = [
    ('birthday_greeting', re.compile(r'ברכה אישית|ברכה ליום|ברכה מקוגומלו|ברכת יום הולדת')),
    ('birthday_party', re.compile(r'יום הולדת|ימי הולדת|יומולדת|הפעלה ל|הופעה ל')),
    ('rental', re.compile(r'השכרה|השכרת|להשכיר|לשכור')),
    ('store', re.compile(r'חליפה|חליפות|תחפושת|מידות')),
]
P_AUTO_REPLY = re.compile(
    r'הודעה אוטומטית|מענה אוטומטי|תודה שפנית|תודה על פנייתך|אחזור אליך בהקדם|נחזור אליך בהקדם|נחזור אליכם בהקדם'
)
P_ADD_CLASS = re.compile(
    r'להוסיף (?:לו |לה |להם )?(?:גם )?(?:עוד )?חוג|חוג נוסף|חוג שני|לרשום גם את (?:האח|האחות|אח|אחות)'
)

# What only a person can settle (ח.2–ח.4).
P_SITE_PROBLEM = re.compile(
    r'הקישור לא (?:עובד|נפתח)|האתר לא (?:עובד|נותן|מאפשר)|לא (?:מצליח|מצליחה|מצליחים|הצלחתי|הצלחנו) (?:להירשם|לרשום|לשלם)'
    r'|לא נותן (?:לי |לנו )?(?:לאשר|להירשם|להמשיך|לבחור)|לא מופיע (?:באתר|ברשימה|בהרשמה)|החוג לא מופיע'
    r'|כתוב (?:לי )?(?:ש)?(?:ה(?:חוג|קבוצה) )?מלא|אי אפשר להירשם|נתקע(?:תי|נו)? ב'
)
P_CALL_SOMEONE = re.compile(
    r'(?:ש?תדברו|דברו|ש?תתקשרו|התקשרו|ש?תפנו) (?:עם|אל|ל)\s?' + r'(?:בעלי|אשתי|ה?בעל\b|ה?אישה|בן הזוג|בת הזוג|אבא|אמא|ה?סבא|ה?סבתא)'
)
P_RESCHEDULE_TRIAL = re.compile(
    r'(?:לשנות|להזיז|לדחות|להחליף) את (?:ה)?(?:מועד|ניסיון|שיעור הניסיון|תאריך)|מועד אחר ל(?:שיעור ה)?ניסיון'
    r'|לבוא (?:ל(?:שיעור ה)?ניסיון )?ביום אחר'
)
P_NO_INFO_TOLD = re.compile(
    r'אין לי את הפרט|אין לי (?:את ה)?מידע|לבדוק (?:מול|עם|ב)\s?המשרד|לפנות למשרד|לברר (?:מול|עם|ב)\s?המשרד'
)
P_COMMITMENT = re.compile(
    r'מה (?:קורה )?אם (?:זה )?לא (?:י|ת)תאים|אם (?:הוא|היא) לא (?:י|ת)רצה|מחזירים כסף|החזר כספי|התחייבות|לעצור את התשלום'
)
P_GROUP = re.compile(r'כמה ילדים|כמה משתתפים|אילו גילים|איזה גילים|הקבוצה (?:נפתחה|התגבשה|נפתחת)')
P_CHILD_NOT_READY = re.compile(
    r'(?:הוא|היא|הילד|הילדה) לא (?:רצה|רצתה|התחבר|התחברה|אהב|אהבה)|עוד לא (?:מוכן|מוכנה|בשל|בשלה)|אולי בשנה הבאה'
)
P_INSTRUCTOR = re.compile(r'איפה (?:הוא|היא) (?:מלמד|מלמדת)|רק (?:אצל|עם) ה?מדריכ|תלוי (?:ב|מי )ה?מדריכ')
# "לא רלוונטי" closes a lead only when it is not about one day, hour or branch that was offered.
P_NO_RELEVANT = re.compile(r'לא רלוונטי|לא רלבנטי')
P_ABOUT_AN_OPTION = re.compile(r'יום|ימים|שעה|שעות|סניף|מחיר|קבוצה')

_SOMEONE = (
    r'(?:ה?בעל\b|בעלי|אשתי|ה?אישה|ה?בת\b|ה?בן\b|ה?ילד\b|ה?ילדה|ה?ילדים|אבא|אמא|ה?סבא|ה?סבתא|בן הזוג|בת הזוג'
    r'|ה?נכד\b|ה?נכדה|ה?הורים|ה?גננת|ה?מורה)'
)
P_ASK_SOMEONE = re.compile(
    r'(?:אבדוק|נבדוק|אשאל|נשאל|אדבר|נדבר|אתייעץ|נתייעץ|אברר|נברר|לבדוק|לשאול|לדבר|להתייעץ)\s+(?:עם|את|מול)\s+(?:ה)?' + _SOMEONE
)
P_WILL_CHECK = re.compile(
    r'אבדוק|נבדוק|אחשוב|נחשוב|אתייעץ|נתייעץ|אחזור אלי|נחזור אלי|אעדכן|נעדכן|אברר|נברר|אחליט|נחליט'
    r'|צריכה לחשוב|צריך לחשוב|צריכים לחשוב|אתן תשובה|ניתן תשובה'
)
P_LATER = re.compile(r'לא כרגע|לא עכשיו|אולי בהמשך|אולי בעתיד|שנה הבאה|בזמן אחר|מאוחר יותר|עוד מוקדם|בינתיים לא')
P_SCHEDULE = re.compile(
    r'(?:הימים|השעות|היום|השעה|המועד)\s+(?:הזה\s+|הזאת\s+|האלה\s+)?לא\s+(?:מתאי|נוח|מסתדר)'
    r'|לא\s+(?:מתאי\S*|נוח\S*|מסתדר\S*)\s+(?:לנו\s+|לי\s+)?ב?(?:יום|ימים|שעה|שעות)|מתנגש'
)
P_NO_CLASS_ASKED = re.compile(r'אין לכם (?:חוג|קבוצה|שיעור)')
P_NO_CLASS_TOLD = re.compile(r'אין (?:לנו|אצלנו)\s+(?:כרגע\s+|כעת\s+|עדיין\s+)?(?:חוג|קבוצה|שיעור)')

# Words that make a stretch of time an answer to "when shall we talk again", not a fact about the class.
P_RETURN = re.compile(
    r'אחזור|נחזור|אעדכן|נעדכן|נדבר|אחשוב|נחשוב|תחזרו|לחזור אלי|אתן תשובה|ניתן תשובה|אולי|רלוונטי|לא עכשיו|לא כרגע'
    r'|נראה|נשקול|תפנו|פנו אלי|דברו איתי|תדברו איתי|נתחיל|נרשם|נירשם'
)

_YEAR_WORDS = {'שנה': 1, 'שנתיים': 2, 'שלוש': 3, 'ארבע': 4, 'חמש': 5}
_MONTH_WORDS = {
    'חודש': 1, 'חודשיים': 2, 'שלושה': 3, 'ארבעה': 4, 'חמישה': 5, 'שישה': 6, 'שבעה': 7, 'שמונה': 8, 'תשעה': 9,
    'עשרה': 10, 'אחד עשר': 11, 'אחד-עשר': 11,
}
_MONTHS_ALT = '|'.join(sorted(_MONTH_WORDS, key=len, reverse=True))
P_AGE_SPOKEN = re.compile(
    r'(?:בן|בת|בגיל|גיל)\s+(?P<years>שנתיים|שנה|שלוש|ארבע|חמש|\d{1,2})'
    r'(?:(?P<dot>[.,]5)|\s*ו[-־]?\s*(?:(?P<half>חצי)|(?P<months>\d{1,2}|' + _MONTHS_ALT + r')(?P<unit>\s*חודשים|\s*חודש)?))?'
)
P_AGE_MONTHS_ONLY = re.compile(r'(?:בן|בת)\s+(?P<months>\d{1,2}|' + _MONTHS_ALT + r')\s+חודשים')

_COUNT = r'(\d{1,2}|' + _MONTHS_ALT + r')'
OFFSET_RULES = [
    (re.compile(r'עוד כ?חצי שנה'), lambda m: {'months': 6}),
    (re.compile(r'עוד שנה\b|שנה הבאה'), lambda m: {'months': 12}),
    (re.compile(r'עוד ' + _COUNT + r' חודשים'), lambda m: {'months': _count(m.group(1))}),
    (re.compile(r'עוד חודשיים'), lambda m: {'months': 2}),
    (re.compile(r'עוד ' + _COUNT + r' שבועות'), lambda m: {'weeks': _count(m.group(1))}),
]
P_AFTER = re.compile(
    r'אחרי (ה?חגים|ה?חג\b|ה?מועדים|פסח|סוכות|חנוכה|פורים|ראש השנה|שבועות|יום כיפור|ה?חופש הגדול|ה?חופשה?\b|ה?קיץ)'
)
P_DAY_AND_MONTH = re.compile(r'(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)')


def _count(word: str) -> int | None:
    return int(word) if word.isdigit() else _MONTH_WORDS.get(word)


def spoken_age(text: str) -> tuple[int, int] | None:
    """(years, months) of an age said in words or digits: "בן שנתיים ושבעה חודשים", "בת 2.5", "בן 8 חודשים"."""
    months_only = P_AGE_MONTHS_ONLY.search(text or '')
    if months_only:
        months = _count(months_only.group('months'))
        if months is not None and months <= 36:
            return divmod(months, 12)
    found = P_AGE_SPOKEN.search(text or '')
    if not found:
        return None
    raw = found.group('years')
    years = int(raw) if raw.isdigit() else _YEAR_WORDS.get(raw)
    if years is None or years > 18:
        return None
    months = 0
    if found.group('half') or found.group('dot'):
        months = 6
    elif found.group('months'):
        said = _count(found.group('months'))
        # "בן 5 ו-7" is two children; "בת שנה ו-8" is one, a year and eight months old.
        if said is not None and said <= 11 and (found.group('unit') or (years <= 2 and found.group('months').isdigit())
                                               or not found.group('months').isdigit()):
            months = said
    return years, months


def _come_back_by_rules(lines, today: date) -> tuple[dict, str]:
    """(what was said about "when", why) from the customer's messages, newest first; ({}, '') when nothing was."""
    for line in reversed([line for line in lines if line.who == SENDER_CUSTOMER and line.text]):
        text = line.text
        said_on = (timezone.localtime(line.sent_at).date() if line.sent_at else today).isoformat()
        after = P_AFTER.search(text)
        if after:
            return {'kind': COME_BACK_AFTER, 'what': after.group(1), 'said_on': said_on}, f'ביקש לחזור אליו אחרי {after.group(1)}'
        if not P_RETURN.search(text):
            continue
        for pattern, build in OFFSET_RULES:
            found = pattern.search(text)
            amounts = build(found) if found else None
            if amounts and all(value is not None for value in amounts.values()):
                return {'kind': COME_BACK_OFFSET, 'said_on': said_on, **amounts}, f'ביקש לחזור אליו ב{found.group(0)}'
        named = P_DAY_AND_MONTH.search(text)
        if named:
            written = _iso_day(said_on)
            day, month, year = int(named.group(1)), int(named.group(2)), named.group(3)
            year = (int(year) + 2000 if len(year) == 2 else int(year)) if year else written.year
            try:
                wanted = date(year, month, day)
                if wanted <= written and not named.group(3):
                    wanted = date(year + 1, month, day)
            except ValueError:
                continue
            return {'kind': COME_BACK_DATE, 'iso': wanted.isoformat(), 'said_on': said_on}, f'ביקש לחזור אליו ב-{day}.{month}'
    return {}, ''


class _as_said:
    """A note of the office, shaped like a line the rules about "when" can read. Used for nothing else."""

    def __init__(self, note):
        self.who, self.text, self.sent_at, self.is_template = SENDER_CUSTOMER, note.text, note.sent_at, False


def asked_to_stop(lines):
    """
    The customer's bare "לא" that answers our own last reminder — the message
    that says "כתבו 'לא' ולא אשלח יותר". Anywhere else a bare "לא" answers
    whatever was asked last and closes nothing (ח.8); here it is the word we
    asked for, and a promise: nobody writes to them again. None unless it is
    the last thing they wrote, and the last thing we wrote before it asked for it.
    """
    spoken = [line for line in lines if line.text]
    last = next((index for index in range(len(spoken) - 1, -1, -1) if spoken[index].who == SENDER_CUSTOMER), None)
    if last is None or not P_BARE_NO.match(spoken[last].text):
        return None
    ours = next((line for line in reversed(spoken[:last]) if line.who != SENDER_CUSTOMER), None)
    return spoken[last] if ours is not None and P_INVITED_NO.search(ours.text) else None


def _says_no(lines):
    """The customer's message that says, outright, that they are not interested — among their last two."""
    stop = asked_to_stop(lines)
    if stop is not None:
        return stop
    theirs = [line for line in lines if line.who == SENDER_CUSTOMER and line.text]
    for line in reversed(theirs[-2:]):
        text = line.text
        if '?' in text:
            continue
        if P_NO.search(text) or P_NO_WHOLE.match(text) or (
            P_NO_RELEVANT.search(text) and not P_ABOUT_AN_OPTION.search(text)
        ):
            return line
    return None


def _says_registered(lines):
    """The customer's message that says they have already signed up — among their last two."""
    theirs = [line for line in lines if line.who == SENDER_CUSTOMER and line.text]
    for line in reversed(theirs[-2:]):
        if '?' not in line.text and P_SAYS_REGISTERED.search(line.text) and not P_TRIAL_WORD.search(line.text):
            return line
    return None


def _only_an_opening(theirs) -> bool:
    """Nothing of their own: every message is the ad's ready-made line or a greeting."""
    return bool(theirs) and all(P_AD_LINE.search(line.text) or P_GREETING.match(line.text) for line in theirs)


def _intent_by_rules(theirs, known) -> str:
    """What the conversation is about, as far as keywords tell; '' when they do not."""
    everything = '\n'.join(line.text for line in theirs)
    if P_AUTO_REPLY.search(theirs[-1].text):
        return 'auto_reply'
    if known.topic in ('trial', 'registration', 'info'):
        if P_ADD_CLASS.search(everything):
            return INTENT_ADD_CLASS
        return INTENT_TRIAL if known.topic == 'trial' else INTENT_CLASS
    for intent, pattern in INTENT_RULES:
        if pattern.search(everything):
            return intent
    return INTENT_CLASS if _only_an_opening(theirs) else ''


def _open_question(lines):
    """
    The customer's question that got "I do not have that detail" for an answer
    and nothing since: (the question, True); (None, False) otherwise. Seen only
    where Kogo sees the answers.
    """
    spoken = [line for line in lines if line.text and not line.is_template]
    for index in range(len(spoken) - 1, -1, -1):
        line = spoken[index]
        if line.who == SENDER_CUSTOMER:
            continue
        if not P_NO_INFO_TOLD.search(line.text):
            return None
        asked = next((earlier for earlier in reversed(spoken[:index]) if earlier.who == SENDER_CUSTOMER and '?' in earlier.text), None)
        return asked
    return None


def _quoted(label: str, line) -> str:
    said = _short(line.text, 120) if line is not None else ''
    return f'{label}: "{said}"'[:SENTENCE_CHARS] if said else label


def from_rules(lines, known, *, course_types=(), external_places=(), today: date, link_sent: bool = False,
               notes=()) -> dict:
    """
    The same dictionary without Claude. The rules read the customer's own
    messages and say less than a reader would: what they cannot tell stays empty.
    Of the notes the office wrote about the customer (`notes`) they read two
    things only — the child's age and when to come back — which is what a person
    writes down after a phone call; a note is never the customer's own word.
    """
    lead = _blank(ANALYSIS_RULES)
    lead['link_sent'] = bool(link_sent)
    _place(lead, known, course_types, external_places)
    theirs = [line for line in lines if line.who == SENDER_CUSTOMER and line.text]
    noted = [note for note in notes if note.text]
    if not theirs:
        return lead
    everything = '\n'.join(line.text for line in theirs)
    recent = theirs[-QUOTE_WITHIN:]
    lead['intent'] = _intent_by_rules(theirs, known)
    lead['empty_opening'] = _only_an_opening(theirs)

    # --- the child ---
    age = spoken_age(everything)
    if age is None:
        age = next((found for found in (spoken_age(note.text) for note in reversed(noted)) if found is not None), None)
    if age is not None:
        lead['child']['age_years'], lead['child']['age_months'] = age[0], (age[1] or None)
    if (known.child_age or '').startswith('כיתה'):
        lead['child']['grade'] = known.child_age

    # --- interested? ---
    no = _says_no(lines)
    registered = _says_registered(lines) if no is None else None
    if no is not None:
        lead.update(interest=INTEREST_NO, quote=_short(no.text, QUOTE_CHARS), quote_at=_moment(no.sent_at))
    elif registered is not None:
        lead.update(
            interest=INTEREST_REGISTERED, quote=_short(registered.text, QUOTE_CHARS), quote_at=_moment(registered.sent_at),
        )
    elif known.interest == 'hot':
        lead['interest'] = INTEREST_YES
    elif known.topic in ('trial', 'registration', 'info'):
        lead['interest'] = INTEREST_UNSURE

    # --- what holds them back: the strongest thing said ---
    flags = set(known.flags or [])
    too_young = 'child_too_young' in flags or (age is not None and age[0] < MIN_AGE_YEARS)

    def said(pattern):
        return next((line for line in reversed(recent) if pattern.search(line.text)), None)

    told_no_class = next(
        (line for line in reversed(lines) if line.who != SENDER_CUSTOMER and not line.is_template and P_NO_CLASS_TOLD.search(line.text or '')),
        None,
    )
    question = _open_question(lines)
    found = None
    if said(P_SITE_PROBLEM):
        found = (BLOCKER_SITE_PROBLEM, said(P_SITE_PROBLEM))
    elif said(P_CALL_SOMEONE):
        found = (BLOCKER_CALL_SOMEONE, said(P_CALL_SOMEONE))
    elif said(P_RESCHEDULE_TRIAL):
        found = (BLOCKER_RESCHEDULE_TRIAL, said(P_RESCHEDULE_TRIAL))
    elif question is not None:
        found = (BLOCKER_OPEN_QUESTION, question)
    elif 'no_branch_nearby' in flags:
        found = (BLOCKER_NO_BRANCH, None)
    elif said(P_NO_CLASS_ASKED) or told_no_class:
        found = (BLOCKER_NO_CLASS, said(P_NO_CLASS_ASKED))
    elif 'class_full' in flags:
        found = (BLOCKER_FULL, None)
    elif too_young:
        found = (BLOCKER_TOO_YOUNG, None)
    elif said(P_CHILD_NOT_READY):
        found = (BLOCKER_CHILD_NOT_READY, said(P_CHILD_NOT_READY))
    elif said(P_ASK_SOMEONE):
        found = (BLOCKER_ASK_SOMEONE, said(P_ASK_SOMEONE))
    elif said(P_WILL_CHECK):
        found = (BLOCKER_WILL_CHECK, said(P_WILL_CHECK))
    elif said(P_LATER):
        found = (BLOCKER_LATER, said(P_LATER))
    elif said(P_COMMITMENT):
        found = (BLOCKER_COMMITMENT, said(P_COMMITMENT))
    elif said(P_SCHEDULE):
        found = (BLOCKER_SCHEDULE, said(P_SCHEDULE))
    elif 'price' in flags:
        found = (BLOCKER_PRICE, None)
    elif said(P_GROUP):
        found = (BLOCKER_GROUP, said(P_GROUP))
    elif said(P_INSTRUCTOR):
        found = (BLOCKER_INSTRUCTOR, said(P_INSTRUCTOR))
    if found is not None and no is None and registered is None:
        lead['blocker'] = found[0]
        lead['blocker_text'] = (
            _short(found[1].text, SENTENCE_CHARS) if found[0] == BLOCKER_OPEN_QUESTION
            else _quoted(BLOCKER_LABELS[found[0]], found[1])
        )

    # --- when to come back ---
    raw, why = {}, ''
    if too_young and age is not None and age[0] < MIN_AGE_YEARS:
        said_on = next((line.sent_at for line in reversed(theirs + noted) if spoken_age(line.text) is not None), None)
        raw = {
            'kind': COME_BACK_AGE, 'years': age[0], 'months': age[1],
            'said_on': (timezone.localtime(said_on).date() if said_on else today).isoformat(),
        }
    elif known.callback_on is None:
        raw, why = _come_back_by_rules(lines, today)
        if not raw and noted:
            # What the office wrote down ("לחזור בעוד חודשיים", "אחרי החגים"), read as the words about "when" that they are.
            raw, why = _come_back_by_rules([_as_said(note) for note in noted], today)
            why = why.replace('ביקש לחזור אליו', 'לפי פתק של המשרד: לחזור')
    lead['come_back_why'] = why
    _settle_day(lead, raw, known, lines, today, noted)

    lead['summary'] = _rules_summary(lead, known)
    return lead


def _rules_summary(lead: dict, known) -> str:
    """One sentence out of what the rules found: what was asked, where, for whom, and where it stands."""
    if lead['intent'] not in LEAD_INTENTS or (known.topic in ('', 'other') and not known.course_type):
        return ''
    asked = f'שאלו על {known.course_type}' if known.course_type else 'שאלו על חוגים'
    where = known.branch_name or known.city
    if where:
        asked += f' ב{where}'
    child = lead['child']
    if child['age_years'] is not None:
        asked += f', גיל {child["age_years"]}' + (f' ו-{child["age_months"]} חודשים' if child['age_months'] else '')
    elif known.child_age:
        asked += f', {known.child_age}' if not known.child_age[0].isdigit() else f', גיל {known.child_age}'
    if lead['interest'] == INTEREST_NO:
        tail = 'אמרו שאינם מעוניינים'
    elif lead['interest'] == INTEREST_REGISTERED:
        tail = 'אמרו שכבר נרשמו'
    elif lead['blocker']:
        tail = BLOCKER_LABELS[lead['blocker']]
    elif lead['link_sent']:
        tail = 'קיבלו קישור ולא נרשמו'
    else:
        tail = ''
    return (f'{asked}; {tail}' if tail else asked)[:SUMMARY_CHARS]


# --- reading it back -----------------------------------------------------------------------------------

def child_words(child: dict) -> str:
    """The child as a chip says it: "נועה, בת 5" is not known — "נועה, גיל 5"; "גיל שנתיים ו-7 חודשים"; "כיתה ב"."""
    child = child if isinstance(child, dict) else {}
    parts = []
    name = (child.get('name') or '').strip()
    if name:
        parts.append(name)
    years, months = child.get('age_years'), child.get('age_months')
    if isinstance(years, int):
        age = {0: '', 1: 'שנה', 2: 'שנתיים'}.get(years, str(years))
        if months:
            age = f'{age} ו-{months} חודשים' if age else f'{months} חודשים'
        if age:
            parts.append(f'גיל {age}')
    elif (child.get('grade') or '').strip():
        parts.append(child['grade'].strip())
    return ', '.join(parts)
