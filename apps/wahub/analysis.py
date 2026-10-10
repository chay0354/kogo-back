"""
"מה ידוע" — what a conversation says, worked out without a person reading it:
what they asked about, which class and where, the child's age, how interested
they are, when they said to come back, and one or two short sentences.

Two ways, one result:

    rules  keyword rules, always available. Ported from the one-off check of
           6.10.2026 (scripts/whatsapp-leads/analyze.py): topic, branch names
           and nicknames, and the exclusion of studio rentals and birthdays.
    ai     Claude, when ANTHROPIC_API_KEY is set (environment, or the row a
           manager stored). A failure, a timeout or a refusal falls back to the
           rules and never raises.

The result is written to the `known_*` fields only. The follow-up marks belong
to a person and are not touched here.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import (
    ANALYSIS_AI,
    ANALYSIS_RULES,
    DIRECTION_IN,
    EVENT_ANALYZED,
    FLAG_LABELS,
    INTEREST_CHOICES,
    SENDER_BOT,
    SENDER_CUSTOMER,
    SENDER_OFFICE,
    STATUS_FAILED,
    TOPIC_CHOICES,
    TYPE_TEMPLATE,
    Contact,
    Message,
)

logger = logging.getLogger(__name__)

TOPICS = [value for value, _ in TOPIC_CHOICES]
INTERESTS = [value for value, _ in INTEREST_CHOICES]
FLAGS = list(FLAG_LABELS)
TOPIC_LABELS = dict(TOPIC_CHOICES)

MAX_MESSAGES = 40
SUMMARY_MAX_CHARS = 300

ANTHROPIC_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_AI_MODEL = 'claude-sonnet-5-5'
DEFAULT_AI_TIMEOUT = 12


@dataclass
class Known:
    topic: str = ''
    course_type: str = ''
    city: str = ''
    branch_id: object = None
    branch_name: str = ''
    child_age: str = ''
    interest: str = ''
    callback_on: date | None = None
    flags: list = field(default_factory=list)
    summary: str = ''
    source: str = ANALYSIS_RULES


@dataclass
class Line:
    """One message, as the analysis reads it."""
    who: str          # customer / bot / office
    text: str
    sent_at: object
    is_template: bool = False


@dataclass
class Place:
    """A branch as the business has it: the real row, and its city."""
    id: object
    name: str
    city: str


# --- keyword rules ----------------------------------------------------------------

P_TRIAL = re.compile(r'ניסיון|נסיון|ניסיונ|נסיונ|התנסות|להתנסות|לנסות|שיעור היכרות|שיעור הכרות')
P_REG = re.compile(
    r'להירש|להרש|הרשמ|רישו|לרשו|ארשו|נירש|נרשמ|נרשם|רשמתי|רשמנו|רושמ|תרשמ|ירשמ|להצטרף|הצטרפות|לצרף|מצטרפ'
)
P_AD = re.compile(r'מידע נוסף על זה|more info on this|узнать об этом', re.I)
P_INFO = re.compile(
    r'פרטים|מידע|מחיר|עלות|כמה עולה|כמה זה עולה|מערכת שעות|באילו ימים|באיזה ימים|איזה ימים|באיזה יום|איזה יום'
    r'|מתי החוג|יש מקום|מקום פנוי|נשאר מקום'
)
P_CLASS = re.compile(r'חוג|קפוארה|קפואירה|היפ הופ|היפהופ|היפ-הופ|מחול|ריקוד|בלט|אקרו|שיעור|גיל|בן \d|בת \d|כיתה|גן ')
P_ASK = re.compile(
    r'מתי נפתח|מתי מתחיל|יש מסלול|יש חוג|יש לכם|יש אצלכם|איפה|מעוניי|מתעניי|להגיע לשיעור|לבוא לשיעור|האם יש|אפשר לדעת'
)
P_COURSE = re.compile(r'חוג|קפוארה|קפואירה|היפ הופ|היפהופ|היפ-הופ|מחול|ריקוד|בלט|אקרו|מסלול')
# Renting a studio and birthday parties are another business, not a class lead.
P_RENT = re.compile(r'השכרה|השכרת|להשכיר|לשכור|יום הולדת|ימי הולדת|יומולדת')

# How parents name the branches: the city, the street, the mall, the school.
BRANCH_ALIASES = [
    ('ראש העין', 'ראש העין', r'ראש העין|ראש-העין|ראשהעין|רה"ע|רה״ע|קרל וגרטי|גרטי קורי|פסגות אפק'),
    ('כפר סבא', 'כפר סבא', r'כפר סבא|כפר-סבא|כפ"ס|כפ״ס|כפס|דמרי'),
    ('פתח תקווה – אם המושבות', 'פתח תקווה', r'אם המושבות|אם-המושבות|רפאל איתן|קניון ספיר|מרכז ספיר'),
    ('פתח תקווה – מינץ (מרכז העיר)', 'פתח תקווה', r'מינץ|מרכז העיר'),
    ('פתח תקווה – כפר גנים / מרכז זמיר', 'פתח תקווה', r'כפר גנים|זמיר'),
    ('פתח תקווה (לא צוין סניף)', 'פתח תקווה', r'פתח תקווה|פתח תקוה|פתח-תקווה|פ"ת|פ״ת|בפת\b'),
    ('שוהם', 'שוהם', r'שוהם|ניצנים'),
    ('אור יהודה', 'אור יהודה', r'אור יהודה|בית בפארק'),
    ('יהוד', 'יהוד', r'יהוד(?!ה)|יהודה הלוי'),
    ('רמת גן', 'רמת גן', r'רמת גן|רמת-גן|גאולים|רמת חן'),
    ('הוד השרון', 'הוד השרון', r'הוד השרון|מרכז דור'),
    ('רמלה', 'רמלה', r'רמלה'),
    ('עפולה', 'עפולה', r'עפולה'),
]
BRANCH_ALIASES = [(label, city, re.compile(pattern)) for label, city, pattern in BRANCH_ALIASES]
ALIAS_BY_LABEL = {label: (city, pattern) for label, city, pattern in BRANCH_ALIASES}
PT_GENERIC = 'פתח תקווה (לא צוין סניף)'
PT_SPECIFIC = {'פתח תקווה – אם המושבות', 'פתח תקווה – מינץ (מרכז העיר)', 'פתח תקווה – כפר גנים / מרכז זמיר'}
# Cities people ask about where there is no branch.
OTHER_CITY = re.compile(
    r'חולון|נתניה|הרצליה|רעננה|אריאל|תל אביב|אופקים|ירושלים|חיפה|באר שבע|אשדוד|ראשון לציון|רחובות|מודיעין'
    r'|בני ברק|גבעת שמואל|קרית אונו|אלעד'
)

COURSE_WORDS = [
    ('קפוארה', re.compile(r'קפוארה|קפואירה|קפוירה')),
    ('היפ הופ', re.compile(r'היפ הופ|היפהופ|היפ-הופ')),
    ('בלט', re.compile(r'בלט')),
    ('אקרובטיקה', re.compile(r'אקרו')),
    ('מחול', re.compile(r'מחול|ריקוד')),
]
P_AGE = re.compile(r'(?:בן|בת|בגיל|גיל|בני|בנות)\s*(\d{1,2})(\s*וחצי|\.5)?')
P_GRADE = re.compile(r'כיתה\s*([א-ו])[\'׳]?')

P_COLD = re.compile(r'לא מעוניינ|לא רלוונטי|לא מתאים לנו|לא כרגע|ויתרנו|נוותר|לא תודה')
FLAG_RULES = [
    ('price', re.compile(r'יקר|מחיר גבוה|הרבה כסף|יותר מדי כסף')),
    ('class_full', re.compile(r'אין מקום|החוג מלא|הקבוצה מלאה|רשימת המתנה')),
    ('lives_far', re.compile(r'רחוק')),
    ('complaint', re.compile(r'תלונה|מאוכזב|לא מרוצ|מתלונ|גרוע|חוצפה')),
    ('says_registered', re.compile(r'כבר נרשמ|כבר רשומ|כבר רשמ|נרשמנו|רשמתי|רשמנו')),
    ('child_too_young', re.compile(r'קטן מדי|קטנה מדי|צעיר מדי|צעירה מדי')),
]
# "I'll get back to you in two weeks" — counted from the day it was written.
P_CALLBACK_WORD = re.compile(r'אחזור|נחזור|אעדכן|נעדכן|נדבר|אחשוב|נחשוב|תחזרו|לחזור אלי|אתן תשובה|ניתן תשובה')
CALLBACK_SPANS = [
    (re.compile(r'בעוד שבועיים|עוד שבועיים'), 14),
    (re.compile(r'בעוד שבוע|עוד שבוע|שבוע הבא|בשבוע הבא'), 7),
    (re.compile(r'בעוד חודשיים|עוד חודשיים'), 60),
    (re.compile(r'בעוד חודש|עוד חודש|חודש הבא|בחודש הבא'), 30),
    (re.compile(r'מחרתיים'), 2),
    (re.compile(r'מחר'), 1),
    (re.compile(r'בעוד כמה ימים|עוד כמה ימים'), 3),
]


def branches_in(text: str) -> list[str]:
    found = [label for label, _city, pattern in BRANCH_ALIASES if pattern.search(text or '')]
    if any(label in PT_SPECIFIC for label in found) and PT_GENERIC in found:
        found.remove(PT_GENERIC)
    return found


def discussed_branches(lines: list[Line]) -> list[str]:
    """
    The branches named in the conversation: the parent's words first, then the
    office's, then the bot's free answers. A template the system sent never
    counts, nor a message that lists every city, nor the office's own address.
    """
    for who in (SENDER_CUSTOMER, SENDER_OFFICE, SENDER_BOT):
        counted: Counter = Counter()
        for line in lines:
            if line.who != who or not line.text or line.is_template:
                continue
            found = branches_in(line.text)
            if who != SENDER_CUSTOMER and len({label.split(' ')[0] for label in found}) >= 3:
                continue
            if who == SENDER_BOT and 'המשרד שלנו' in line.text:
                continue
            counted.update(found)
        if any(label in PT_SPECIFIC for label in counted) and PT_GENERIC in counted:
            del counted[PT_GENERIC]
        if counted:
            return [label for label, _ in counted.most_common()]
    return []


def resolve_branch(label: str, places: list[Place], said: str = '') -> Place | None:
    """
    The real branch behind a name from a conversation, or None when the name
    does not settle on exactly one. Matched on the branches' own names; `said`
    is what was written, and picks between two branches of one city when the
    words name one of them ("פסגות אפק").
    """
    wanted = (label or '').strip()
    if not wanted or not places:
        return None
    exact = [place for place in places if place.name.strip().casefold() == wanted.casefold()]
    if len(exact) == 1:
        return exact[0]
    city, pattern = ALIAS_BY_LABEL.get(wanted, (None, None))
    if pattern is None:
        # A free name (from the AI): the nickname group it belongs to, if any.
        for alias_label, alias_city, alias_pattern in BRANCH_ALIASES:
            if alias_pattern.search(wanted):
                city, pattern = alias_city, alias_pattern
                wanted = alias_label
                break
    if pattern is None:
        partial = [place for place in places if wanted.casefold() in place.name.casefold()]
        return partial[0] if len(partial) == 1 else None
    if wanted == PT_GENERIC:
        named = []
    else:
        named = [place for place in places if pattern.search(place.name)]
    if len(named) == 1:
        return named[0]
    if named:
        spoken = [
            place for place in named
            if any(re.search(part, said) and re.search(part, place.name) for part in pattern.pattern.split('|'))
        ] if said else []
        return spoken[0] if len(spoken) == 1 else None
    in_city = [place for place in places if _same_city(place.city, city)]
    return in_city[0] if len(in_city) == 1 else None


def _same_city(a: str, b: str) -> bool:
    def norm(value):
        return re.sub(r'[\s\-"״]', '', (value or '').replace('תקוה', 'תקווה'))
    return bool(a and b) and norm(a) == norm(b)


def analyze_rules(lines: list[Line], places: list[Place]) -> Known:
    known = Known(source=ANALYSIS_RULES)
    incoming = [line for line in lines if line.who == SENDER_CUSTOMER and line.text]
    all_in = '\n'.join(line.text for line in incoming)
    if not incoming:
        return known

    # --- topic ---
    topics = set()
    for line in incoming:
        text = line.text
        if P_TRIAL.search(text):
            topics.add('trial')
        elif P_REG.search(text):
            topics.add('registration')
        elif P_AD.search(text):
            topics.add('info')
        elif P_INFO.search(text) and P_CLASS.search(all_in) and not (P_RENT.search(text) and 'חוג' not in text):
            topics.add('info')
        elif P_ASK.search(text) and P_COURSE.search(text) and not P_RENT.search(text):
            topics.add('info')
    rental_only = P_RENT.search(all_in) and 'חוג' not in all_in and topics <= {'info'}
    if rental_only or not topics:
        known.topic = 'other'
    elif 'trial' in topics:
        known.topic = 'trial'
    elif 'registration' in topics:
        known.topic = 'registration'
    else:
        known.topic = 'info'

    # --- class, age ---
    known.course_type = next((name for name, pattern in COURSE_WORDS if pattern.search(all_in)), '')
    age = P_AGE.search(all_in)
    if age and 1 <= int(age.group(1)) <= 18:
        known.child_age = age.group(1) + ('.5' if age.group(2) else '')
    else:
        grade = P_GRADE.search(all_in)
        if grade:
            known.child_age = f'כיתה {grade.group(1)}'

    # --- where ---
    labels = discussed_branches(lines)
    other_cities = list(dict.fromkeys(OTHER_CITY.findall(all_in)))
    if labels:
        label = labels[0]
        place = resolve_branch(label, places, said='\n'.join(line.text for line in lines if not line.is_template))
        if place is not None:
            known.branch_id, known.branch_name = place.id, place.name
            known.city = place.city or ALIAS_BY_LABEL[label][0]
        else:
            known.branch_name = '' if label == PT_GENERIC else label
            known.city = ALIAS_BY_LABEL[label][0]
    elif other_cities:
        known.city = other_cities[0]

    # --- flags ---
    flags = [flag for flag, pattern in FLAG_RULES if pattern.search(all_in)]
    if other_cities and not labels:
        flags.append('no_branch_nearby')
    known.flags = flags

    # --- interest ---
    if known.topic == 'other':
        known.interest = 'none'
    elif P_COLD.search(incoming[-1].text) or (len(incoming) <= 2 and P_COLD.search(all_in)):
        known.interest = 'cold'
    elif known.topic in ('trial', 'registration'):
        known.interest = 'hot'
    else:
        known.interest = 'warm'

    # --- "I'll get back to you" ---
    for line in reversed(incoming):
        if not P_CALLBACK_WORD.search(line.text):
            continue
        days = next((span for pattern, span in CALLBACK_SPANS if pattern.search(line.text)), None)
        if days is not None:
            written = timezone.localtime(line.sent_at).date() if line.sent_at else state.now_israel_date()
            known.callback_on = written + timedelta(days=days)
            break

    known.summary = _rules_summary(known, rental_only)
    return known


def _rules_summary(known: Known, rental_only: bool) -> str:
    if rental_only:
        return 'פנייה על השכרת סטודיו או יום הולדת, לא על חוג.'
    if known.topic == 'other':
        opening = 'פנייה כללית'
    else:
        opening = {
            'trial': 'מתעניינים בשיעור ניסיון', 'registration': 'רוצים להירשם לחוג', 'info': 'ביקשו פרטים',
        }[known.topic]
    if known.course_type:
        opening += {'trial': f' ב{known.course_type}', 'registration': f' {known.course_type}'}.get(
            known.topic, f' על {known.course_type}',
        )
    where = known.branch_name or known.city
    if where:
        opening += f', {where}'
    sentences = [opening + '.']
    extra = []
    if known.child_age:
        extra.append(f'גיל הילד: {known.child_age}')
    if known.callback_on:
        extra.append(f'אמרו שיחזרו ב-{known.callback_on.day}.{known.callback_on.month}')
    if extra:
        sentences.append(', '.join(extra) + '.')
    return ' '.join(sentences)[:SUMMARY_MAX_CHARS]


# --- Claude -------------------------------------------------------------------------

AI_SCHEMA = {
    'type': 'object',
    'properties': {
        'topic': {'type': 'string', 'enum': TOPICS + ['']},
        'course_type': {'type': 'string'},
        'city': {'type': 'string'},
        'branch_name': {'type': 'string'},
        'child_age': {'type': 'string'},
        'interest': {'type': 'string', 'enum': INTERESTS + ['']},
        'callback_on': {'type': 'string'},
        'flags': {'type': 'array', 'items': {'type': 'string', 'enum': FLAGS}},
        'summary': {'type': 'string'},
    },
    'required': [
        'topic', 'course_type', 'city', 'branch_name', 'child_age', 'interest', 'callback_on', 'flags', 'summary',
    ],
    'additionalProperties': False,
}

AI_SYSTEM = """\
אתה קורא שיחת וואטסאפ בין הורה לבין סטודיו לחוגי ילדים (קפוארה, מחול, היפ הופ, אקרובטיקה), וממלא כרטיס קצר על הפנייה עבור צוות המשרד. \
מי שקורא את הכרטיס לא יקרא את השיחה, ולכן כתוב רק מה שנאמר בה בפועל. שדה שלא נאמר עליו דבר נשאר מחרוזת ריקה; אל תנחש.

השדות:
- topic: על מה הפנייה. trial = שיעור ניסיון; registration = רישום לחוג; info = בקשת פרטים על חוג (מחיר, ימים, מקום פנוי); other = כל דבר אחר, כולל השכרת סטודיו וימי הולדת, שאינם פנייה לחוג.
- course_type: סוג החוג במילים של העסק, למשל "קפוארה".
- city: העיר שההורה שאל עליה או גר בה.
- branch_name: שם הסניף, בדיוק כפי שהוא כתוב ברשימת הסניפים שתקבל. אם ההורה ציין רק עיר ויש בה יותר מסניף אחד, או שאין סניף מתאים, השאר ריק.
- child_age: גיל הילד כפי שנאמר, למשל "5" או "כיתה ב". כמה ילדים: "5, 8".
- interest: hot = רוצה להירשם או לקבוע ניסיון עכשיו; warm = מתעניין ושואל; cold = אמר שלא מתאים או לא עכשיו; none = אי אפשר לדעת.
- callback_on: אם ההורה אמר מתי לחזור אליו או מתי יחזור ("אחזור בעוד שבועיים", "דברו איתי אחרי החגים"), התאריך בצורה YYYY-MM-DD. חשב אותו מהתאריך של ההודעה שבה זה נאמר, לא מהיום. אחרת ריק.
- flags: רק מה שעולה בבירור מהשיחה. price = המחיר עצר אותו; class_full = אין מקום בחוג; lives_far = גר רחוק מהסניף; no_branch_nearby = אין סניף בעיר שלו; complaint = תלונה; says_registered = אומר שכבר נרשם; child_too_young = הילד צעיר מהגיל של החוג; difficult = שיחה טעונה או לא ברורה שכדאי שאדם יקרא.
- summary: עד שני משפטים קצרים בעברית פשוטה: מה ההורה רוצה ואיפה זה עומד. בלי פתיח, בלי שמות שדות ובלי ציטוטים ארוכים.

בשיחה, "לקוח" הוא ההורה, "בוט" הוא המענה האוטומטי ו"משרד" הוא נציג. תבנית שהמערכת שלחה אינה דברי ההורה."""


def ai_configured() -> bool:
    from apps.core.scoping import integration_credential

    return bool(integration_credential('ANTHROPIC_API_KEY'))


def _ai_input(lines: list[Line], places: list[Place], today: date) -> str:
    cities = sorted({place.city for place in places if place.city})
    parts = [
        f'היום: {today.isoformat()} (שעון ישראל).',
        'הסניפים של העסק (שם הסניף — עיר):',
        *(f'- {place.name} — {place.city or "לא צוינה עיר"}' for place in places),
        'הערים שיש בהן סניף: ' + (', '.join(cities) if cities else 'אין רשימה'),
        '',
        'השיחה, מהישנה לחדשה:',
    ]
    who_label = {SENDER_CUSTOMER: 'לקוח', SENDER_BOT: 'בוט', SENDER_OFFICE: 'משרד'}
    for line in lines:
        written = timezone.localtime(line.sent_at).strftime('%Y-%m-%d %H:%M') if line.sent_at else ''
        tag = who_label.get(line.who, 'מערכת') + (' (תבנית)' if line.is_template else '')
        parts.append(f'[{written}] {tag}: {line.text}')
    return '\n'.join(parts)


def analyze_ai(lines: list[Line], places: list[Place], *, timeout: float | None = None) -> Known | None:
    """
    Ask Claude. None for anything that is not a usable answer — no key, a
    network fault, a timeout, an error status, a refusal, an answer cut short.
    Never raises.

    Plain HTTPS to the Messages API: no SDK, by decision — no new dependency
    for one call. The answer is constrained to AI_SCHEMA (output_config.format),
    which is how current models return JSON: they reject a forced tool call.
    """
    from apps.core.scoping import integration_credential

    try:
        api_key = integration_credential('ANTHROPIC_API_KEY')
    except Exception:
        logger.exception('wahub analysis: could not read the Anthropic key')
        return None
    if not api_key or not lines:
        return None

    body = {
        'model': (getattr(settings, 'WAHUB_AI_MODEL', '') or DEFAULT_AI_MODEL).strip(),
        # A ceiling, not a spend: thinking counts toward it, so it sits far
        # above the few hundred tokens the card itself takes.
        'max_tokens': 8000,
        'system': AI_SYSTEM,
        'messages': [{'role': 'user', 'content': _ai_input(lines, places, state.now_israel_date())}],
        'output_config': {
            'effort': 'low',
            'format': {'type': 'json_schema', 'schema': AI_SCHEMA},
        },
    }
    seconds = getattr(settings, 'WAHUB_AI_TIMEOUT_SECONDS', DEFAULT_AI_TIMEOUT) or DEFAULT_AI_TIMEOUT
    if timeout:
        seconds = min(seconds, timeout)
    try:
        response = requests.post(
            ANTHROPIC_URL,
            headers={
                'x-api-key': api_key,
                'anthropic-version': ANTHROPIC_VERSION,
                'content-type': 'application/json',
            },
            json=body,
            timeout=(min(5, seconds), seconds),
        )
    except requests.RequestException as exc:
        logger.warning('wahub analysis: Claude did not answer (%s); using the rules', type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning('wahub analysis: Claude answered %s; using the rules', response.status_code)
        return None
    try:
        payload = response.json()
        # Checked before the content: a refusal or an answer cut at the token
        # limit does not carry the card.
        if payload.get('stop_reason') in ('refusal', 'max_tokens'):
            logger.warning('wahub analysis: Claude stopped with %s; using the rules', payload.get('stop_reason'))
            return None
        text = next(
            block.get('text', '') for block in payload.get('content') or []
            if isinstance(block, dict) and block.get('type') == 'text'
        )
        data = json.loads(text)
    except (ValueError, StopIteration, AttributeError, TypeError):
        logger.warning('wahub analysis: could not read Claude\'s answer; using the rules')
        return None
    if not isinstance(data, dict):
        return None
    return _known_from_ai(data, places)


def _known_from_ai(data: dict, places: list[Place]) -> Known:
    def text(key, limit):
        value = data.get(key)
        return re.sub(r'\s+', ' ', value).strip()[:limit] if isinstance(value, str) else ''

    known = Known(source=ANALYSIS_AI)
    known.topic = data.get('topic') if data.get('topic') in TOPICS else ''
    known.interest = data.get('interest') if data.get('interest') in INTERESTS else ''
    known.course_type = text('course_type', 100)
    known.city = text('city', 100)
    known.child_age = text('child_age', 40)
    known.summary = text('summary', SUMMARY_MAX_CHARS)
    known.flags = [flag for flag in dict.fromkeys(data.get('flags') or []) if flag in FLAGS]

    raw_date = text('callback_on', 10)
    if raw_date:
        try:
            known.callback_on = date.fromisoformat(raw_date)
        except ValueError:
            known.callback_on = None

    branch = text('branch_name', 200)
    if branch:
        place = resolve_branch(branch, places)
        if place is not None:
            known.branch_id, known.branch_name = place.id, place.name
            known.city = known.city or place.city
        else:
            known.branch_name = branch
    return known


# --- reading a contact, and keeping the result ------------------------------------------

def load_places() -> list[Place]:
    from apps.core.models import Branch

    return [
        Place(id=branch_id, name=name or '', city=city or '')
        for branch_id, name, city in Branch.objects.filter(is_active=True, is_external=False)
        .values_list('id', 'name', 'city__name')
    ]


def conversation_lines(contact: Contact) -> list[Line]:
    """The last MAX_MESSAGES messages that reached the customer or came from them, oldest first."""
    rows = list(
        Message.objects.filter(contact=contact).exclude(status=STATUS_FAILED).order_by('-id')
        .values_list('direction', 'sender', 'text', 'sent_at', 'message_type')[:MAX_MESSAGES]
    )
    rows.reverse()
    return [
        Line(
            who=SENDER_CUSTOMER if direction == DIRECTION_IN else sender,
            text=text or '',
            sent_at=sent_at,
            is_template=message_type == TYPE_TEMPLATE,
        )
        for direction, sender, text, sent_at, message_type in rows
    ]


def analyze_contact(
    contact: Contact, *, places: list[Place] | None = None, timeout: float | None = None, use_ai: bool = True,
) -> Known:
    """Summarise one contact and keep it. Only the known_* fields are written."""
    places = load_places() if places is None else places
    lines = conversation_lines(contact)
    known = (analyze_ai(lines, places, timeout=timeout) if use_ai else None) or analyze_rules(lines, places)

    before = (
        contact.known_topic, contact.known_course_type, contact.known_city, contact.known_branch_id,
        contact.known_branch_name, contact.known_child_age, contact.known_interest, contact.known_callback_on,
        list(contact.known_flags or []), contact.known_summary, contact.analysis_source,
    )
    after = (
        known.topic, known.course_type, known.city, known.branch_id, known.branch_name, known.child_age,
        known.interest, known.callback_on, list(known.flags), known.summary, known.source,
    )
    now = timezone.now()
    fields = dict(
        known_topic=known.topic,
        known_course_type=known.course_type,
        known_city=known.city,
        known_branch_id=known.branch_id,
        known_branch_name=known.branch_name,
        known_child_age=known.child_age,
        known_interest=known.interest,
        known_callback_on=known.callback_on,
        known_flags=list(known.flags),
        known_summary=known.summary,
        needs_analysis=False,
        analyzed_at=now,
        analysis_source=known.source,
    )
    if before == after:
        # Nothing the screen shows moved: no need to wake it up.
        Contact.objects.filter(pk=contact.pk).update(needs_analysis=False, analyzed_at=now)
    else:
        state.touch(contact.pk, **fields)
        label = 'AI' if known.source == ANALYSIS_AI else 'כללים'
        state.log_event(contact.pk, EVENT_ANALYZED, f'סיכום אוטומטי ({label}): {known.summary}'.strip(': '))
    for name, value in fields.items():
        setattr(contact, name, value)
    return known
