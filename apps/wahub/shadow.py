"""
הבוט בצל — what the new bot WOULD have answered (docs/WAHUB-CONTRACT-STAGE2.md, ב).

For every customer message the cron asks `propose(contact)`: the context is
built (the last twenty messages, the contact's card, the last message the
system itself sent — so "כן" is read as an answer to it — the office hours now,
and the knowledge in scope), Claude answers with the Kogo tools of
shadow_tools.py, and the proposal is kept as a ShadowReply beside the old bot's
real answer. Nothing here sends. Nothing here calls sending.py or handoff.py.
`request_human` only marks the draft.

Without an Anthropic key the stub answers: a rule-built reply from the same
tools and phrasings, marked `model: "stub"`, so the screen works on a
developer's machine and the owner can see the shape of things.

Claude is reached over plain HTTPS with `requests`, as analysis.py does: the
SDK is not a dependency of this deployment (requirements.txt), and one request
shape does not justify one.
"""
from __future__ import annotations

import json
import logging
import re
import time as time_module
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.wahub import analysis, inbound, knowledge, shadow_tools, state
from apps.wahub.analysis import ANTHROPIC_URL, ANTHROPIC_VERSION, P_CLASS, P_COLD, P_INFO, P_REG, P_TRIAL, Line
from apps.wahub.models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    KIND_BEHAVIOR_RULE,
    KIND_LINK,
    KIND_TOPIC,
    SENDER_BOT,
    SENDER_CUSTOMER,
    SENDER_OFFICE,
    SENDER_SYSTEM,
    SHADOW_MODEL_STUB,
    STATUS_FAILED,
    TYPE_TEMPLATE,
    Contact,
    Message,
    ShadowReply,
)
from apps.wahub.shadow_tools import TOOLS, ToolContext, run_tool, summarize_result

logger = logging.getLogger(__name__)

# Several messages from one customer within this window get one answer.
MERGE_WINDOW = timedelta(seconds=20)
MAX_CONTEXT_MESSAGES = 20
MAX_TOOL_ROUNDS = 6
LAST_OUTBOUND_WITHIN = timedelta(days=7)
DEFAULT_MODEL = 'claude-opus-5-5'
DEFAULT_EFFORT = 'medium'
DEFAULT_TIMEOUT = 40
# Claude Opus 5.5 declines some requests through its safety classifiers; the
# server-side fallback re-runs the request on another model inside the same call.
FALLBACK_BETA = 'server-side-fallback-2026-07-01'

REPLY_SCHEMA = {
    'type': 'object',
    'properties': {
        'text': {'type': 'string', 'description': 'ההודעה ללקוח, מוכנה לשליחה בוואטסאפ'},
        'reasoning': {'type': 'string', 'description': 'למה ככה: 2–4 שורות בעברית פשוטה, עם [#id] של רשומות הידע שהשתמשת בהן'},
        'knowledge_ids': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'מזהי רשומות הידע שהתשובה נשענת עליהן'},
    },
    'required': ['text', 'reasoning', 'knowledge_ids'],
    'additionalProperties': False,
}

SYSTEM_TASK = """\
את הבוט של וואטסאפ של סטודיו קוגומלו (חוגי ילדים: קפוארה, מחול, היפ הופ, אקרובטיקה אווירית). \
המערכת מראה לבעל העסק מה *היית* עונה ללקוח; התשובה לא נשלחת. כתבי אותה בדיוק כפי שהיא צריכה להגיע ללקוח.

מה חובה:
1. כל פרט על חוג, יום, שעה, מדריך, מחיר, שיעור ניסיון, תפוסה, כתובת או סניף — רק ממה שהכלים מחזירים עכשיו. לא מהזיכרון, לא מהשיחה. \
לפני תשובה כזאת קראי ל-find_courses (או branch_info). פרט שלא חזר מהכלי — אין אותו, ואומרים את הנוסח missing_info.
2. סניף שחוזר מהכלי כ-is_external — אומרים שצריך לבדוק מול העירייה/המתנ"ס ושולחים את קישור ההרשמה. בלי מחיר, בלי ניסיון, בלי כתובת.
3. כמה הודעות רצופות של הלקוח — תשובה אחת לכולן.
4. תשובה קצרה ("כן", "לא", "מאשר") מתפרשת לפי ההודעה האחרונה שהמערכת שלחה ללקוח, אם יש כזאת בהקשר.
5. כשצריך נציג (לפי הכללים והנושאים) קראי ל-request_human ואמרי ללקוח שנציג יחזור; מחוץ לשעות — את הודעת "סגור".
6. הכללים, הנוסחים (מילה במילה כשמסומן) והעובדות למטה מחייבים. עובדה שמסומנת "רק אם שואלים" לא נאמרת מיוזמתך.
7. בלי נקודה בסוף משפט, ₪ לפני המספר, כוכבית אחת להדגשה עם רווח לפניה, 🔹 לכל פריט ברשימה, קישורים עם https:// מלא, עברית בלבד, 3–4 שורות.

הפלט הסופי: JSON בלבד לפי הסכמה — text (ההודעה ללקוח), reasoning (למה ככה, בעברית פשוטה, 2–4 שורות, עם [#id] של הרשומות ששימשו), knowledge_ids.
"""

_GREETING = re.compile(r'^(היי+|הי+|שלום|הלו|בוקר טוב|ערב טוב|צהריים טובים|שבת שלום|hi|hello)[\s!?.,🙂👋]*$', re.I)
_YES = re.compile(r'^(כן|כן כן|מגיעים|נגיע|מאשר|מאשרת|בכיף|נגיע בכיף|בטח|אוקיי|אוקי|סבבה|עדיין מחפשים|כן, מה יש לכם להציע\??)[\s!.]*$')
_NO = re.compile(r'^(לא|לא נגיע|לא מגיעים|נבטל|לא יכולים|לא נוכל|לא נספיק)[\s!.]*$')
_REMINDER = re.compile(r'תזכורת|מגיעים|ניסיון|שיעור')
_CAMPAIGN = re.compile(r'רלוונטי|התענייתם|חזרה ללידים|תפוצה|קמפיין|מה קורה')
_NOT_ANSWERED = re.compile(r'לא עניתם|לא ענית|אף אחד לא חזר|לא חזרתם')
_BIRTHDAY_GREETING = re.compile(r'ברכה')
_BIRTHDAY_PARTY = re.compile(r'יום הולדת|יומולדת|הפעלה|הפעלת|געגע')
_CANCEL = re.compile(r'לבטל|ביטול|להפסיק את|מבטל')
_CANCEL_REQUEST = re.compile(r'רוצה לבטל|רוצים לבטל|אני מבטל|לבטל את|תשלחו|טופס|להפסיק את')
_RENT = re.compile(r'להשכיר|השכרה|השכרת|לשכור')
_GROUP = re.compile(r'קבוצת וואטסאפ|קבוצה של החוג|קבוצת החוג|להצטרף לקבוצה|קבוצת ווצאפ')
_SHOWS = re.compile(r'הופע|הצגה|הצגות|מופע|לוח הופעות')
_INSTRUCTOR = re.compile(r'(לדבר|לשוחח|להתקשר).*(מדריך|מדריכה|מאמן|מאמנת)|(מדריך|מדריכה|מאמן).*(לדבר|טלפון)')
_WHERE = re.compile(r'איפה|כתובת|איך מגיעים|חניה|חנייה')
_PRICE = re.compile(r'מחיר|עולה|עלות|כמה זה|תעריף')
_DONE = re.compile(r'עזבי|לא משנה|נשאיר את זה ככה|הסתדרתי|הסתדרנו|תודה רבה|תודה,|^תודה')
_SHEKEL_AFTER = re.compile(r'(\d[\d,.]*)\s*₪')
_TRAILING_DOT = re.compile(r'(?<!\.)\.(?=\s*$)', re.M)


class ShadowModelError(Exception):
    """Claude did not give a usable answer. The stub takes over; the reason is kept in the reasoning."""


# --- settings -----------------------------------------------------------------------------------

def shadow_configured() -> bool:
    return analysis.ai_configured()


def model_name() -> str:
    return (getattr(settings, 'WAHUB_SHADOW_MODEL', '') or DEFAULT_MODEL).strip()


def _effort() -> str:
    value = (getattr(settings, 'WAHUB_SHADOW_EFFORT', '') or DEFAULT_EFFORT).strip()
    return value if value in ('low', 'medium', 'high', 'xhigh', 'max') else DEFAULT_EFFORT


def _timeout_seconds() -> float:
    return float(getattr(settings, 'WAHUB_SHADOW_TIMEOUT_SECONDS', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)


# --- the context ------------------------------------------------------------------------------------

@dataclass
class Target:
    """A customer message the draft answers (or the question typed in "נסה שאלה")."""
    id: int | None
    text: str
    message_type: str = 'text'
    sent_at: object = None


@dataclass
class Context:
    now: object
    today: date
    contact: Contact | None
    lines: list                      # [{id, who, text, sent_at, message_type, is_template}] oldest first
    targets: list                    # [Target] — what is being answered now
    last_outbound: dict | None       # the last template / system message, or the one given to "נסה שאלה"
    hours: dict
    items: list                      # KnowledgeItem rows in scope
    customer: dict | None
    places: list = field(default_factory=list)
    tool_ctx: ToolContext = None


@dataclass
class Draft:
    text: str
    reasoning: str
    tools_used: list = field(default_factory=list)
    knowledge_used: list = field(default_factory=list)
    model: str = SHADOW_MODEL_STUB
    took_ms: int = 0
    request_human: bool = False
    request_human_reason: str = ''

    def as_dict(self) -> dict:
        return {
            'text': self.text, 'reasoning': self.reasoning, 'tools_used': self.tools_used,
            'knowledge_used': list(self.knowledge_used), 'model': self.model, 'took_ms': self.took_ms,
            'request_human': self.request_human, 'request_human_reason': self.request_human_reason,
        }


def _conversation(contact: Contact, before_id: int | None = None) -> list[dict]:
    rows = Message.objects.filter(contact=contact).exclude(status=STATUS_FAILED)
    if before_id:
        rows = rows.filter(id__lte=before_id)
    rows = list(rows.order_by('-id').values('id', 'direction', 'sender', 'text', 'sent_at', 'message_type')[:MAX_CONTEXT_MESSAGES])
    rows.reverse()
    return [{
        'id': row['id'],
        'who': SENDER_CUSTOMER if row['direction'] == DIRECTION_IN else row['sender'],
        'text': row['text'] or '',
        'sent_at': row['sent_at'],
        'message_type': row['message_type'],
        'is_template': row['message_type'] == TYPE_TEMPLATE,
    } for row in rows]


def _last_system_outbound(contact: Contact, before_id: int | None, now) -> dict | None:
    """The last template or system message that went out before the message being answered."""
    rows = Message.objects.filter(contact=contact, direction=DIRECTION_OUT).exclude(status=STATUS_FAILED).filter(
        Q(message_type=TYPE_TEMPLATE) | Q(sender=SENDER_SYSTEM)
    ).filter(sent_at__gte=now - LAST_OUTBOUND_WITHIN)
    if before_id:
        rows = rows.filter(id__lt=before_id)
    row = rows.order_by('-id').values('id', 'text', 'sent_at', 'message_type').first()
    if not row:
        return None
    return {'id': row['id'], 'text': row['text'], 'sent_at': row['sent_at'], 'kind': row['message_type']}


def _scope_ids(contact: Contact | None) -> dict:
    if contact is None:
        return {}
    ids = {'branch_id': contact.known_branch_id}
    if contact.known_city:
        from apps.core.models import City
        ids['city_id'] = City.objects.filter(name__iexact=contact.known_city).values_list('id', flat=True).first()
    if contact.known_course_type:
        from apps.courses.models import CourseType
        ids['course_type_id'] = CourseType.objects.filter(name__icontains=contact.known_course_type, is_active=True).values_list('id', flat=True).first()
    return ids


def build_context(contact: Contact | None, *, targets: list, now=None, last_outbound: str | None = None) -> Context:
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    first_target_id = next((target.id for target in targets if target.id), None)
    lines = _conversation(contact) if contact is not None else []
    if last_outbound is not None and str(last_outbound).strip():
        outbound = {'id': None, 'text': str(last_outbound).strip(), 'sent_at': None, 'kind': 'given'}
    else:
        outbound = _last_system_outbound(contact, first_target_id, now) if contact is not None else None
    items = knowledge.relevant_items(today=today, **_scope_ids(contact))
    tool_ctx = ToolContext(contact, now=now, items=items)
    customer = shadow_tools.customer_card(tool_ctx) if contact is not None else None
    return Context(
        now=now, today=today, contact=contact, lines=lines, targets=list(targets), last_outbound=outbound,
        hours=knowledge.office_hours_now(now), items=items, customer=customer, places=analysis.load_places(), tool_ctx=tool_ctx,
    )


# --- style, enforced in code ------------------------------------------------------------------------

def polish(text: str) -> str:
    """The style rules nobody should have to remember: ₪ first, one asterisk, no final period."""
    text = (text or '').replace('**', '*')
    text = _SHEKEL_AFTER.sub(r'₪\1', text)
    text = _space_before_opening_asterisk(text)
    text = _TRAILING_DOT.sub('', text)
    return text.strip()


def _space_before_opening_asterisk(text: str) -> str:
    """ב*03.09* → ב *03.09*. Asterisks alternate open/close per line; only an opening one glued to a letter moves."""
    out = []
    for line in text.split('\n'):
        chars, opening = [], True
        for index, char in enumerate(line):
            if char == '*':
                if opening and index > 0 and (line[index - 1].isalnum()) and index + 1 < len(line) and not line[index + 1].isspace():
                    chars.append(' ')
                opening = not opening
            chars.append(char)
        out.append(''.join(chars))
    return '\n'.join(out)


# --- the stub ----------------------------------------------------------------------------------------

def _season_cancel_deadline(today: date) -> date:
    year = today.year + 1 if today.month >= 9 else today.year
    return date(year, 4, 1)


def _stub(ctx: Context) -> Draft:
    """
    A reply from the tools and the phrasings alone, by fixed rules. Not clever
    on purpose: it exists so the screen works without a key, and so the owner
    sees which rule fired (the reasoning names it).
    """
    tctx = ctx.tool_ctx
    phrasings = knowledge.phrasings_by_key(ctx.items)
    links = {item.key: item for item in ctx.items if item.kind == KIND_LINK and item.key}
    rules = {item.title: item for item in ctx.items if item.kind == KIND_BEHAVIOR_RULE}
    topics = {item.title: item for item in ctx.items if item.kind == KIND_TOPIC}
    reasons: list[str] = []
    tools_used: list[dict] = []
    office = next((item for item in ctx.items if item.kind == 'contact' and (item.data or {}).get('phone') and 'משרד' in (item.title or '')), None)
    office_phone = (office.data or {}).get('phone') if office else ''
    if office is not None:
        tctx.used(office)

    def call(name: str, **arguments) -> dict:
        result = run_tool(name, arguments, tctx)
        tools_used.append({'name': name, 'input': arguments, 'summary': summarize_result(name, result)})
        reasons.append(f'קראתי לכלי {name}: {summarize_result(name, result)}')
        return result

    def say(key: str, **variables) -> str:
        item = phrasings.get(key)
        if item is None:
            return ''
        tctx.used(item)
        reasons.append(f'השתמשתי בנוסח "{key}" [#{item.id}]')
        variables.setdefault('טלפון_משרד', office_phone or '')
        for link_key, item_link in links.items():
            variables.setdefault({'registration': 'קישור_הרשמה', 'shows': 'קישור_לוח_הופעות', 'cancellation_form': 'קישור_טופס_ביטול', 'shop': 'קישור_חנות'}.get(link_key, link_key), (item_link.data or {}).get('url'))
        return knowledge.render_phrasing(item, **variables)

    def link(key: str) -> str:
        item = links.get(key)
        if item is None:
            return ''
        tctx.used(item)
        reasons.append(f'צירפתי את הקישור "{key}" [#{item.id}]')
        return (item.data or {}).get('url') or ''

    def rule(title_part: str):
        for title, item in rules.items():
            if title_part in title:
                tctx.used(item)
                reasons.append(f'לפי הכלל "{title}" [#{item.id}]')
                return item
        return None

    def topic(title_part: str):
        for title, item in topics.items():
            if title_part in title:
                tctx.used(item)
                reasons.append(f'לפי הנושא "{title}" [#{item.id}]')
                return item
        return None

    def missing() -> str:
        return say('missing_info') or f'אין לי את הפרט הזה כרגע, אפשר לבדוק במשרד 📞 {office_phone}'.rstrip(' 📞')

    name = (ctx.contact.name.split(' ')[0] if ctx.contact and ctx.contact.name else '')
    merged = '\n'.join(target.text for target in ctx.targets if target.text).strip()
    short = merged.replace('\n', ' ').strip()
    if len(ctx.targets) > 1:
        rule('כמה הודעות ברצף')
        reasons.append(f'{len(ctx.targets)} הודעות רצופות — תשובה אחת לכולן')
    request_human = ''

    def handoff(reason: str) -> str:
        nonlocal request_human
        call('request_human', reason=reason)
        request_human = reason
        hours = call('office_hours_now')
        if not hours.get('open') and hours.get('message_if_closed'):
            reasons.append('מחוץ לשעות המשרד — הודעת "סגור"')
            return hours['message_if_closed'].strip()
        return say('handoff_notice') or 'הפנייה הועברה לנציג מהמשרד, נחזור אליך בהקדם'

    text = ''
    # 1. a voice message we cannot read
    if any(target.message_type == 'voice' for target in ctx.targets) and not merged:
        reasons.append('הודעה קולית בלי טקסט')
        text = say('voice_message') or 'לא ניתן לשמוע הודעות קוליות, בבקשה להשאיר הודעה כתובה'
    # 2. asks for a person
    elif inbound.asks_for_human(merged):
        reasons.append('הלקוח מבקש נציג במפורש')
        text = handoff('הלקוח ביקש נציג')
    # 3. a short answer to the last message the system sent
    elif ctx.last_outbound and len(short) <= 40 and (_YES.match(short) or _NO.match(short) or _NOT_ANSWERED.search(short)):
        outbound_text = ctx.last_outbound.get('text') or ''
        reasons.append(f'תשובה קצרה; ההודעה האחרונה שיצאה: "{state.preview(outbound_text)[:60]}"')
        if _CAMPAIGN.search(outbound_text) and not _REMINDER.search(outbound_text):
            topic('קמפיין')
            if _NOT_ANSWERED.search(short):
                text = say('campaign_not_answered')
            elif _YES.match(short):
                text = say('campaign_yes')
            else:
                text = say('campaign_decline') or 'תודה רבה על העדכון'
        else:
            topic('תזכורת')
            text = say('trial_reminder_yes') if _YES.match(short) else say('trial_reminder_no')
    elif _NOT_ANSWERED.search(merged):
        topic('קמפיין')
        text = say('campaign_not_answered')
    # 4. birthdays, cancellations, rentals, groups, shows, instructor
    elif _BIRTHDAY_GREETING.search(merged) and not _BIRTHDAY_PARTY.search(merged):
        topic('ברכה אישית')
        text = say('birthday_greeting_name')
    elif _BIRTHDAY_PARTY.search(merged) and not _RENT.search(merged):
        topic('הפעלת יום הולדת')
        text = say('birthday_party_gilad')
    elif _CANCEL.search(merged):
        topic('ביטול')
        if _CANCEL_REQUEST.search(merged):
            if ctx.today > _season_cancel_deadline(ctx.today) and ctx.today.month < 9:
                reasons.append('הבקשה אחרי 1.4 של שנת הפעילות')
                text = say('cancel_after_deadline')
            else:
                text = say('cancel_form')
        else:
            text = say('cancel_policy_info')
    elif _RENT.search(merged):
        topic('השכרת סטודיו')
        text = handoff('השכרת סטודיו')
        text = 'ההשכרות מטופלות מול נציג מהמשרד\n' + text
    elif _GROUP.search(merged):
        topic('קבוצת וואטסאפ')
        text = say('whatsapp_group')
        text = (text + '\n' if text else '') + handoff('קבוצת וואטסאפ')
    elif _SHOWS.search(merged):
        topic('הופעות')
        text = say('shows')
    elif _INSTRUCTOR.search(merged):
        topic('מדריך')
        text = say('instructor_request')
    # 5. just a greeting
    elif _GREETING.match(short):
        reasons.append('ברכת פתיחה בלבד')
        text = say('greeting', שם=name) if name else say('greeting_no_name')
    elif _DONE.search(short) and len(short) <= 30:
        rule('זיהוי סיום')
        text = 'בשמחה, יום טוב'
    else:
        # 6. a question about classes — through the same rules the summary uses, then the tools
        lines = [Line(who=row['who'], text=row['text'], sent_at=row['sent_at'], is_template=row['is_template']) for row in ctx.lines]
        if ctx.contact is None or not any(row.get('id') == target.id for row in ctx.lines for target in ctx.targets):
            lines += [Line(who=SENDER_CUSTOMER, text=target.text, sent_at=target.sent_at or ctx.now) for target in ctx.targets]
        known = analysis.analyze_rules(lines, ctx.places)
        about_class = bool(P_CLASS.search(merged) or P_INFO.search(merged) or P_TRIAL.search(merged) or P_REG.search(merged) or known.topic in ('trial', 'registration', 'info'))
        if not about_class:
            reasons.append('לא זיהיתי נושא מוכר')
            text = missing()
        else:
            topic('מסע הרשמה')
            branch_name = known.branch_name if known.branch_id else ''
            city = known.city
            if not branch_name and not city:
                if _PRICE.search(merged):
                    item = rule('קודם סניף וחוג')
                    text = (item.example_good if item and item.example_good else 'באיזה סניף ואיזה חוג מדובר?')
                else:
                    reasons.append('אין עיר ואין סניף בשיחה — שואלים עיר')
                    text = 'באיזו עיר אתם?'
            else:
                age = None
                if known.child_age and re.match(r'^\d', known.child_age):
                    age = float(re.match(r'^\d+(\.\d)?', known.child_age).group(0))
                found = call('find_courses', city=city, branch=branch_name, age=age, course_type=known.course_type)
                branches = found.get('branches') or []
                courses = found.get('courses') or []
                if not branches:
                    reasons.append(f'אין סניף ב{city or branch_name}')
                    text = f'כרגע אין לנו סניף ב{city or branch_name}' if (city or branch_name) else missing()
                elif all(row.get('is_external') for row in branches):
                    rule('סניף חיצוני')
                    text = f'הסניף ב{branches[0]["city"] or branches[0]["name"]} מנוהל מול העירייה, את הפרטים המדויקים כדאי לבדוק איתם\nההרשמה דרך הקישור: {link("registration")}'.strip()
                elif not courses:
                    reasons.append('הסניף קיים אבל אין חוג שמתאים לסינון')
                    text = missing()
                else:
                    if city and len(branches) > 1 and not branch_name and len({row['id'] for row in branches}) > 1:
                        reasons.append('בעיר יש כמה סניפים — שואלים סניף')
                        text = 'באיזה סניף נוח לכם?\n' + '\n'.join(f'🔹 {row["name"]}' for row in branches if not row.get('is_external'))
                    else:
                        bullets = []
                        for course in courses[:3]:
                            label = course['course_type'] or course['name']
                            for lesson in course['lessons'][:2]:
                                line = f'🔹 {label} — יום {lesson["day"]} {lesson["start"]}–{lesson["end"]}'
                                if lesson['instructor']:
                                    line += f' עם {lesson["instructor"]}'
                                if lesson['is_full']:
                                    line += ' (מלא)'
                                bullets.append(line)
                            if not course['lessons']:
                                bullets.append(f'🔹 {label}')
                        text = f'ב{courses[0]["branch"]} יש:\n' + '\n'.join(bullets[:5])
                        if _PRICE.search(merged) or known.topic == 'info' and P_INFO.search(merged) and _PRICE.search(merged):
                            prices = {course['price_monthly'] for course in courses[:3] if course['price_monthly']}
                            if len(prices) == 1:
                                text += f'\nהמחיר ₪{next(iter(prices)).split(".")[0]} לחודש'
                        if known.topic == 'trial' or P_TRIAL.search(merged):
                            trial = courses[0]['trial']
                            text += '\nשיעור ניסיון ' + (f'בעלות ₪{trial["price"].split(".")[0]}' if trial['is_paid'] and trial['price'] else 'ללא עלות')
                            text += f', נרשמים מראש כאן: {link("registration")}'
                        elif known.topic == 'registration':
                            text += f'\nלהרשמה: {link("registration")}'
                        if _WHERE.search(merged):
                            info = call('branch_info', branch=courses[0]['branch'])
                            address = (info.get('branches') or [{}])[0].get('address')
                            text += f'\nהכתובת: {address}' if address else '\n' + missing()
    text = polish(text)
    reasons.insert(0, 'מודל: stub — בלי מפתח Anthropic; התשובה נבנתה מהכלים ומהנוסחים לפי כללים קבועים')
    return Draft(
        text=text, reasoning='\n'.join(f'• {line}' for line in reasons), tools_used=tools_used,
        knowledge_used=list(tctx.knowledge_used), model=SHADOW_MODEL_STUB,
        request_human=bool(request_human or tctx.request_human_reason), request_human_reason=request_human or tctx.request_human_reason or '',
    )


# --- Claude -------------------------------------------------------------------------------------------

def _transcript(ctx: Context) -> str:
    who_label = {SENDER_CUSTOMER: 'לקוח', SENDER_BOT: 'הבוט הישן', SENDER_OFFICE: 'נציג מהמשרד', SENDER_SYSTEM: 'מערכת'}
    target_ids = {target.id for target in ctx.targets if target.id}
    parts = []
    for row in ctx.lines:
        if row['id'] in target_ids:
            continue
        when = timezone.localtime(row['sent_at']).strftime('%d.%m %H:%M') if row['sent_at'] else ''
        tag = who_label.get(row['who'], 'מערכת') + (' (תבנית ששלחה המערכת)' if row['is_template'] else '')
        parts.append(f'[{when}] {tag}: {row["text"]}')
    return '\n'.join(parts) if parts else '(אין היסטוריה)'


def _user_message(ctx: Context) -> str:
    now_text = timezone.localtime(ctx.now).strftime('%A %d.%m.%Y %H:%M')
    parts = [f'עכשיו (שעון ישראל): {now_text}']
    if ctx.customer:
        parts.append('מה המערכת יודעת על הכותב:\n' + json.dumps(ctx.customer, ensure_ascii=False))
    else:
        parts.append('הכותב: לא ידוע (שאלת ניסיון בלי איש קשר)')
    if ctx.last_outbound:
        parts.append(f'ההודעה האחרונה שהמערכת שלחה ללקוח (תשובה קצרה מתפרשת ביחס אליה):\n"""{ctx.last_outbound["text"]}"""')
    else:
        parts.append('ההודעה האחרונה שהמערכת שלחה ללקוח: אין (לא תבנית ולא תפוצה בשבוע האחרון)')
    parts.append('השיחה עד עכשיו (מהישנה לחדשה):\n' + _transcript(ctx))
    kinds = {'voice': 'הודעה קולית', 'image': 'תמונה'}
    targets = '\n'.join(
        f'- {kinds.get(target.message_type, "")}{": " if target.message_type in kinds else ""}{target.text or "(בלי טקסט)"}'
        for target in ctx.targets
    )
    parts.append(f'ההודעה/ות של הלקוח שעליהן צריך לענות עכשיו ({len(ctx.targets)}):\n{targets}')
    return '\n\n'.join(parts)


def _claude(ctx: Context, deadline: float | None) -> Draft:
    from apps.core.scoping import integration_credential

    api_key = integration_credential('ANTHROPIC_API_KEY')
    if not api_key:
        raise ShadowModelError('אין מפתח Anthropic')
    tctx = ctx.tool_ctx
    tools_used: list[dict] = []
    system = SYSTEM_TASK + '\n\n# הידע של הבוט\n\n' + knowledge.describe_for_prompt(ctx.items, ctx.hours)
    messages = [{'role': 'user', 'content': _user_message(ctx)}]
    body = {
        'model': model_name(),
        # A ceiling, not a spend: thinking counts toward it.
        'max_tokens': 16000,
        'system': system,
        'tools': TOOLS,
        'tool_choice': {'type': 'auto'},
        'messages': messages,
        'output_config': {'effort': _effort(), 'format': {'type': 'json_schema', 'schema': REPLY_SCHEMA}},
        'fallbacks': 'default',
    }
    headers = {
        'x-api-key': api_key,
        'anthropic-version': ANTHROPIC_VERSION,
        'anthropic-beta': FALLBACK_BETA,
        'content-type': 'application/json',
    }
    served_model = model_name()
    for _round in range(MAX_TOOL_ROUNDS + 1):
        remaining = _timeout_seconds()
        if deadline is not None:
            remaining = min(remaining, deadline - time_module.monotonic())
            if remaining < 3:
                raise ShadowModelError('נגמר הזמן של הקריאה')
        try:
            response = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=(min(5, remaining), remaining))
        except requests.RequestException as exc:
            raise ShadowModelError(f'Claude לא ענה ({type(exc).__name__})') from exc
        if response.status_code != 200:
            detail = ''
            try:
                detail = (response.json().get('error') or {}).get('message', '')[:200]
            except ValueError:
                pass
            logger.warning('wahub shadow: Claude answered %s %s', response.status_code, detail)
            raise ShadowModelError(f'Claude ענה {response.status_code}')
        try:
            payload = response.json()
        except ValueError as exc:
            raise ShadowModelError('תשובה לא קריאה מ-Claude') from exc
        served_model = payload.get('model') or served_model
        stop = payload.get('stop_reason')
        content = payload.get('content') or []
        if stop == 'refusal':
            raise ShadowModelError('Claude סירב לענות')
        if stop == 'max_tokens':
            raise ShadowModelError('התשובה נקטעה (max_tokens)')
        tool_uses = [block for block in content if isinstance(block, dict) and block.get('type') == 'tool_use']
        if stop == 'tool_use' and tool_uses:
            # The whole assistant turn goes back as it came (thinking blocks included).
            messages.append({'role': 'assistant', 'content': content})
            results = []
            for block in tool_uses:
                arguments = block.get('input') if isinstance(block.get('input'), dict) else {}
                result = run_tool(block.get('name', ''), arguments, tctx)
                tools_used.append({'name': block.get('name', ''), 'input': arguments, 'summary': summarize_result(block.get('name', ''), result)})
                results.append({
                    'type': 'tool_result', 'tool_use_id': block.get('id'),
                    'content': json.dumps(result, ensure_ascii=False, default=str),
                    **({'is_error': True} if 'error' in result else {}),
                })
            messages.append({'role': 'user', 'content': results})
            continue
        text = ''.join(block.get('text', '') for block in content if isinstance(block, dict) and block.get('type') == 'text')
        return _draft_from_answer(text, ctx, tools_used, served_model)
    raise ShadowModelError('יותר מדי סבבי כלים')


def _draft_from_answer(text: str, ctx: Context, tools_used: list, model: str) -> Draft:
    tctx = ctx.tool_ctx
    reply, reasoning, ids = text.strip(), '', []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            reply = str(data.get('text') or '').strip()
            reasoning = str(data.get('reasoning') or '').strip()
            ids = [int(value) for value in data.get('knowledge_ids') or [] if str(value).lstrip('-').isdigit()]
    except (ValueError, TypeError):
        reasoning = ''
    if not reply:
        raise ShadowModelError('Claude החזיר תשובה ריקה')
    known_ids = {item.id for item in ctx.items}
    used = [item_id for item_id in dict.fromkeys(ids) if item_id in known_ids]
    for item_id in tctx.knowledge_used:
        if item_id not in used:
            used.append(item_id)
    for match in re.findall(r'#(\d+)', reasoning):
        item_id = int(match)
        if item_id in known_ids and item_id not in used:
            used.append(item_id)
    return Draft(
        text=polish(reply), reasoning=reasoning or 'Claude לא הסביר', tools_used=tools_used, knowledge_used=used,
        model=model, request_human=bool(tctx.request_human_reason), request_human_reason=tctx.request_human_reason or '',
    )


# --- drafting, proposing, trying ----------------------------------------------------------------------------

def draft(ctx: Context, *, deadline: float | None = None) -> Draft:
    """Claude when there is a key, the stub otherwise — and the stub when Claude fails, saying why."""
    started = time_module.monotonic()
    result = None
    if shadow_configured():
        try:
            result = _claude(ctx, deadline)
        except ShadowModelError as exc:
            logger.warning('wahub shadow: %s; the stub answers', exc)
            result = _stub(ctx)
            result.reasoning = f'• Claude לא נתן תשובה ({exc}); התשובה נבנתה מהכלים ומהנוסחים\n' + result.reasoning
        except Exception:
            logger.exception('wahub shadow: Claude call failed unexpectedly; the stub answers')
            result = _stub(ctx)
            result.reasoning = '• תקלה בקריאה ל-Claude; התשובה נבנתה מהכלים ומהנוסחים\n' + result.reasoning
    else:
        result = _stub(ctx)
    result.took_ms = int((time_module.monotonic() - started) * 1000)
    return result


def pending(now=None):
    """Contacts with a customer message the shadow has not answered, once the burst has settled."""
    now = now or timezone.now()
    return Contact.objects.filter(needs_shadow=True).filter(
        Q(last_inbound_at__lte=now - MERGE_WINDOW) | Q(last_inbound_at__isnull=True)
    )


def unanswered_messages(contact: Contact) -> list[Message]:
    """The customer messages since the last shadow reply, oldest first."""
    last = ShadowReply.objects.filter(contact=contact).exclude(after_message__isnull=True).order_by('-id').values_list('after_message_id', flat=True).first()
    rows = Message.objects.filter(contact=contact, direction=DIRECTION_IN)
    if last:
        rows = rows.filter(id__gt=last)
    return list(rows.order_by('id'))


def propose(contact: Contact, *, now=None, deadline: float | None = None) -> ShadowReply | None:
    """
    The new bot's answer to what the customer wrote since the last one, kept as
    a ShadowReply. None when there is nothing to answer. Never sends anything.
    """
    now = now or timezone.now()
    messages = unanswered_messages(contact)
    if not messages:
        state.touch(contact.pk, needs_shadow=False)
        contact.needs_shadow = False
        return None
    # One answer per burst: the messages within the merge window of the last one.
    last_at = messages[-1].sent_at
    burst = [row for row in messages if last_at - row.sent_at <= MERGE_WINDOW * 3] or messages[-1:]
    targets = [Target(id=row.id, text=row.text, message_type=row.message_type, sent_at=row.sent_at) for row in burst]
    ctx = build_context(contact, targets=targets, now=now)
    result = draft(ctx, deadline=deadline)
    with transaction.atomic():
        reply = ShadowReply.objects.create(
            contact=contact, after_message=burst[-1], covers_message_ids=[row.id for row in burst],
            text=result.text, reasoning=result.reasoning, tools_used=result.tools_used, knowledge_used=result.knowledge_used,
            took_ms=result.took_ms, model=result.model, request_human=result.request_human, request_human_reason=result.request_human_reason,
        )
        newer = Message.objects.filter(contact=contact, direction=DIRECTION_IN, id__gt=burst[-1].id).exists()
        state.touch(contact.pk, needs_shadow=newer, last_shadow_at=now)
    contact.needs_shadow, contact.last_shadow_at = newer, now
    return reply


def answer(question: str, *, contact: Contact | None = None, pretend_now=None, last_outbound: str | None = None,
           deadline: float | None = None) -> Draft:
    """"נסה שאלה": the same draft for a typed question, with a pretend clock and a pretend last message."""
    now = pretend_now or timezone.now()
    targets = [Target(id=None, text=question.strip(), sent_at=now)]
    ctx = build_context(contact, targets=targets, now=now, last_outbound=last_outbound)
    return draft(ctx, deadline=deadline)


def old_bot_reply_for(reply: ShadowReply) -> dict | None:
    """The old bot's real answer to the same message: the first bot message after it, before the customer wrote again."""
    if not reply.after_message_id:
        return None
    next_in = Message.objects.filter(contact_id=reply.contact_id, direction=DIRECTION_IN, id__gt=reply.after_message_id).order_by('id').values_list('id', flat=True).first()
    rows = Message.objects.filter(contact_id=reply.contact_id, direction=DIRECTION_OUT, sender=SENDER_BOT, id__gt=reply.after_message_id).exclude(status=STATUS_FAILED)
    if next_in:
        rows = rows.filter(id__lt=next_in)
    row = rows.order_by('id').values('text', 'sent_at').first()
    return {'text': row['text'], 'sent_at': row['sent_at']} if row else None


def summary(now=None) -> dict:
    from django.db.models import Avg, Count, Q as _Q

    now = now or timezone.now()
    week = now - timedelta(days=7)
    totals = ShadowReply.objects.aggregate(
        proposed_7d=Count('id', filter=_Q(created_at__gte=week)),
        judged_good=Count('id', filter=_Q(verdict='good')),
        judged_bad=Count('id', filter=_Q(verdict='bad')),
        awaiting_verdict=Count('id', filter=_Q(verdict='')),
        avg_ms=Avg('took_ms', filter=_Q(created_at__gte=week)),
        stub_7d=Count('id', filter=_Q(created_at__gte=week, model=SHADOW_MODEL_STUB)),
    )
    return {
        'proposed_7d': totals['proposed_7d'],
        'judged_good': totals['judged_good'],
        'judged_bad': totals['judged_bad'],
        'awaiting_verdict': totals['awaiting_verdict'],
        'avg_ms': int(totals['avg_ms'] or 0),
        'stub_7d': totals['stub_7d'],
        'shadow_configured': shadow_configured(),
        'model': model_name() if shadow_configured() else SHADOW_MODEL_STUB,
        'pending': pending(now).count(),
    }


# --- the JSON of the contract ---------------------------------------------------------------------------------

def reply_payload(reply: ShadowReply, *, items: dict | None = None, with_contact: bool = False) -> dict:
    """GET contacts/{id}/shadow/ — one proposal beside the old bot's real answer."""
    from apps.wahub.models import KnowledgeItem

    ids = [item_id for item_id in (reply.knowledge_used or []) if isinstance(item_id, int)]
    if items is None:
        items = {item.id: item for item in KnowledgeItem.objects.filter(id__in=ids)} if ids else {}
    payload = {
        'id': reply.id,
        'after_message_id': reply.after_message_id,
        'covers_message_ids': list(reply.covers_message_ids or []),
        'text': reply.text,
        'reasoning': reply.reasoning,
        'tools_used': reply.tools_used or [],
        'knowledge_used': [knowledge.item_ref(items[item_id]) for item_id in ids if item_id in items],
        'model': reply.model,
        'took_ms': reply.took_ms,
        'request_human': reply.request_human,
        'request_human_reason': reply.request_human_reason,
        'created_at': reply.created_at.isoformat() if reply.created_at else None,
        'old_bot_reply': None,
        'verdict': reply.verdict or None,
        'verdict_note': reply.verdict_note,
        'verdict_by_name': state.user_display_name(reply.verdict_by) if reply.verdict_by_id else None,
        'verdict_at': reply.verdict_at.isoformat() if reply.verdict_at else None,
    }
    old = old_bot_reply_for(reply)
    if old:
        payload['old_bot_reply'] = {'text': old['text'], 'sent_at': old['sent_at'].isoformat() if old['sent_at'] else None}
    if with_contact:
        payload['contact_id'] = reply.contact_id
        payload['contact_name'] = reply.contact.name if reply.contact else ''
        payload['customer_text'] = reply.after_message.text if reply.after_message_id and reply.after_message else ''
    return payload


def draft_payload(result: Draft) -> dict:
    from apps.wahub.models import KnowledgeItem

    ids = [item_id for item_id in result.knowledge_used if isinstance(item_id, int)]
    items = {item.id: item for item in KnowledgeItem.objects.filter(id__in=ids)} if ids else {}
    payload = result.as_dict()
    payload['knowledge_used'] = [knowledge.item_ref(items[item_id]) for item_id in ids if item_id in items]
    return payload
