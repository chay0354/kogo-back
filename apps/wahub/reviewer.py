"""
ביקורת ושיפור — "סוכן שעובר על השיחות ומתקן את הבוט לפי הביקורות" (docs/WAHUB-CONTRACT-STAGE2.md, ד).

Four places a proposal is born:

    human_override  the office answered over the bot (or over the shadow) — what
                    the person wrote is what the bot should have known
    bad_verdict     the owner marked a shadow reply 👎 with a note
    service_note    the office wrote "the bot did such and such here"
    reviewer        the cron's sweep over yesterday's conversations finds a
                    known failure pattern (two bot replies in a row, internal
                    text, "I'll check and get back", a customer who repeats
                    himself or writes "you never answered")

Every proposal is a KnowledgeProposal that WAITS. Nothing here writes a
KnowledgeItem until approve() is called by a manager — the owner's rule
("בינתיים ככפתור לאישור"). The pattern detection is rules; the wording of a
proposal is Claude's when there is a key and a plain template otherwise.
Nothing here sends anything, and nothing here calls sending.py or handoff.py.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.wahub import knowledge, state
from apps.wahub.analysis import ANTHROPIC_URL, ANTHROPIC_VERSION, ai_configured
from apps.wahub.models import (
    DELIVERED_STATUSES,
    DIRECTION_IN,
    DIRECTION_OUT,
    EVENT_NEEDS_HUMAN,
    KIND_ALIAS,
    KIND_BEHAVIOR_RULE,
    KIND_FACT,
    KIND_PHRASING,
    KIND_SPECIAL_DAY,
    KNOWLEDGE_KIND_CHOICES,
    PROPOSAL_APPLIED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    PROPOSAL_SOURCE_CHOICES,
    PROPOSAL_STATUS_CHOICES,
    SENDER_BOT,
    SENDER_CUSTOMER,
    SENDER_OFFICE,
    SOURCE_BAD_VERDICT,
    SOURCE_HUMAN_OVERRIDE,
    SOURCE_REVIEWER,
    SOURCE_SERVICE_NOTE,
    STATUS_FAILED,
    WHEN_PROACTIVE,
    Contact,
    KnowledgeItem,
    KnowledgeProposal,
    Message,
    ServiceNote,
    ShadowReply,
)

logger = logging.getLogger(__name__)

STATUS_LABELS = dict(PROPOSAL_STATUS_CHOICES)
SOURCE_LABELS = dict(PROPOSAL_SOURCE_CHOICES)
KIND_LABELS = dict(KNOWLEDGE_KIND_CHOICES)

LOOKBACK = timedelta(hours=24)
MAX_CONTACTS = 200
MAX_MESSAGES = 40
MIN_NOTE_CHARS = 8
NEEDS_HUMAN_BY_REVIEW = 'סימון של סורק השיחות'

# What a proposal may create or change; the rest of the knowledge is the owner's by hand.
PROPOSAL_KINDS = (KIND_PHRASING, KIND_FACT, KIND_BEHAVIOR_RULE, KIND_ALIAS, KIND_SPECIAL_DAY, 'office_hours')

# --- the patterns of the sweep ------------------------------------------------------------------------

PATTERN_INTERNAL_TEXT = 'internal_text'
PATTERN_BOT_TWICE = 'bot_twice'
PATTERN_LOOP = 'loop_question'
PATTERN_WILL_CHECK = 'will_check'
PATTERN_VOICE = 'voice_not_heard'
PATTERN_NOT_ANSWERED = 'not_answered'
PATTERN_REPEATED = 'customer_repeated'
PATTERN_FAKE_REGISTRATION = 'fake_registration'
PATTERN_NICOLE = 'nicole'
PATTERN_UNAVAILABLE = 'number_unavailable'

PATTERN_LABELS = {
    PATTERN_INTERNAL_TEXT: 'טקסט פנימי של הבוט הגיע ללקוח',
    PATTERN_BOT_TWICE: 'הבוט ענה פעמיים ברצף',
    PATTERN_LOOP: 'הבוט שאל שוב אותה שאלה',
    PATTERN_WILL_CHECK: '"אבדוק ואחזור" בלי המשך',
    PATTERN_VOICE: '"לא ניתן לשמוע" להודעה קולית',
    PATTERN_NOT_ANSWERED: 'הלקוח כתב "לא עניתם"',
    PATTERN_REPEATED: 'הלקוח חזר על אותה שאלה',
    PATTERN_FAKE_REGISTRATION: 'הבוט "רשם" לקוח בלי יכולת לרשום',
    PATTERN_NICOLE: 'הפניה לניקול',
    PATTERN_UNAVAILABLE: 'הבוט אמר שהמספר לא זמין',
}
# Which patterns also mean "a person has to look at this" — the contract's "מסמן 'דורש בן אדם'".
NEEDS_HUMAN_PATTERNS = (PATTERN_WILL_CHECK, PATTERN_NOT_ANSWERED, PATTERN_REPEATED, PATTERN_FAKE_REGISTRATION)

_INTERNAL = re.compile(r'<invoke|</invoke|<function|<tool|</tool|<parameter|\{"name"\s*:|Request_Human_Agent|Course_Manager')
_WILL_CHECK = re.compile(r'אבדוק ואחזור|אבדוק ואעדכן|אבדוק עם הצוות|אבדוק מול המשרד|נבדוק ונחזור|אברר ואחזור')
_VOICE = re.compile(r'לא ניתן לשמוע|לא יכולה לשמוע|לא יכול לשמוע|אי אפשר לשמוע')
_NOT_ANSWERED = re.compile(r'לא עניתם|לא ענית לי|אף אחד לא חזר|לא חזרתם אלי')
_FAKE_REG = re.compile(r'רשמתי (אותך|אותכם|אתכם|את ה|שתגיע|שתגיעו|לשיעור)|נרשמת(ם)? לשיעור|קבעתי לכם|קבעתי לך|שריינתי')
_NICOLE = re.compile(r'ניקול')
_UNAVAILABLE = re.compile(r'המספר לא זמין|המספר אינו זמין|לא זמין כרגע')

# The fix each pattern proposes when nobody (Claude) words a better one.
PATTERN_FIXES = {
    PATTERN_INTERNAL_TEXT: ('אסור טקסט פנימי בתשובה ללקוח', 'ללקוח מגיע רק טקסט מוכן בעברית. שום תגית, שם כלי, JSON או פרמטר פנימי לא נכתב בהודעה. אם משהו כזה נוצר — התשובה נפסלת ונכתבת מחדש.'),
    PATTERN_BOT_TWICE: ('תשובה אחת לכמה הודעות רצופות', 'כשהלקוח שולח כמה הודעות בתוך שניות — ממתינים ועונים פעם אחת על כולן. לא שתי תשובות לשתי הודעות.'),
    PATTERN_LOOP: ('לא שואלים שוב שאלה שנענתה', 'שאלה שהלקוח כבר ענה עליה (עיר, גיל, סניף) לא נשאלת שוב. הפרט נעול וממשיכים לשלב הבא.'),
    PATTERN_WILL_CHECK: ('לא מבטיחים "אבדוק ואחזור"', 'הבוט לא יכול לבדוק ולחזור. כשחסר מידע — נוסח missing_info עם טלפון המשרד; כשצריך אדם — request_human ואמירה שנציג יחזור.'),
    PATTERN_VOICE: ('הודעה קולית: לא "לא ניתן לשמוע"', 'להודעה קולית עונים בעדינות שעדיף בכתב, או מעבירים לנציג אם ההקשר דורש; לא משפט טכני.'),
    PATTERN_NOT_ANSWERED: ('"לא עניתם" — להתנצל בקצרה ולהתקדם', 'כשהלקוח כותב שלא ענו לו: הנוסח campaign_not_answered (התנצלות קצרה ושאלת עיר), בלי לסגור את השיחה. השיחה מסומנת לנציג.'),
    PATTERN_REPEATED: ('לקוח שחוזר על שאלה', 'אם הלקוח חוזר על אותה שאלה — התשובה הקודמת לא ענתה לו. עונים ישירות על השאלה, ואם אין תשובה מהכלים — request_human.'),
    PATTERN_FAKE_REGISTRATION: ('הבוט לא רושם ולא קובע', 'הבוט לא רושם לשיעור ולא קובע מועד. הרשמה וניסיון — רק דרך קישור ההרשמה. אסור "רשמתי אותך" או "קבעתי לך".'),
    PATTERN_NICOLE: ('אין ניקול במשרד', 'לא מפנים לניקול. כשצריך נציגה — שלי.'),
    PATTERN_UNAVAILABLE: ('לא אומרים "המספר לא זמין"', 'אין מספר שאינו זמין. כשצריך את המשרד — מוסרים את מספר המשרד מרשומת איש הקשר.'),
}


class ReviewError(ValueError):
    """A review action refused, with the reason in Hebrew."""


# --- payloads ----------------------------------------------------------------------------------------

def _iso(moment):
    return moment.isoformat() if moment else None


def proposal_payload(proposal: KnowledgeProposal) -> dict:
    return {
        'id': proposal.id,
        'status': proposal.status,
        'status_label': STATUS_LABELS.get(proposal.status, proposal.status),
        'source': proposal.source,
        'source_label': SOURCE_LABELS.get(proposal.source, proposal.source),
        'pattern': proposal.pattern,
        'pattern_label': PATTERN_LABELS.get(proposal.pattern, ''),
        'contact_id': proposal.contact_id,
        'contact_name': proposal.contact.name if proposal.contact_id and proposal.contact else '',
        'message_id': proposal.message_id,
        'shadow_id': proposal.shadow_id,
        'title': proposal.title,
        'explanation': proposal.explanation,
        'change': proposal.change,
        'change_kind_label': KIND_LABELS.get((proposal.change or {}).get('kind', ''), ''),
        'evidence': proposal.evidence,
        'created_at': _iso(proposal.created_at),
        'decided_at': _iso(proposal.decided_at),
        'decided_by_name': state.user_display_name(proposal.decided_by) if proposal.decided_by_id else None,
        'decision_note': proposal.decision_note,
        'applied_item_id': proposal.applied_item_id,
    }


def _evidence(message: Message | None = None, *, who: str = '', text: str = '', message_id=None) -> dict:
    if message is not None:
        who = SENDER_CUSTOMER if message.direction == DIRECTION_IN else message.sender
        return {'message_id': message.id, 'who': who, 'text': state.preview(message.text)[:300], 'sent_at': _iso(message.sent_at)}
    return {'message_id': message_id, 'who': who, 'text': state.preview(text)[:300]}


def _day(now) -> str:
    return timezone.localtime(now).date().isoformat()


def _dedup_key(source: str, contact_id, now, extra: str = '') -> str:
    key = f'{source}:{contact_id or "none"}:{_day(now)}'
    return f'{key}:{extra}' if extra else key


def _existing(key: str) -> KnowledgeProposal | None:
    return KnowledgeProposal.objects.filter(dedup_key=key).order_by('-id').first()


def _create(*, source: str, title: str, explanation: str, change: dict, evidence: list, contact=None, message=None,
            shadow=None, pattern: str = '', now=None, dedup_extra: str = '') -> KnowledgeProposal | None:
    """One proposal, unless the same cause already made one today. None when it did."""
    now = now or timezone.now()
    key = _dedup_key(source, contact.id if contact else None, now, dedup_extra)
    if _existing(key) is not None:
        return None
    return KnowledgeProposal.objects.create(
        source=source, pattern=pattern, contact=contact, message=message, shadow=shadow,
        title=title[:200], explanation=explanation, change=change, evidence=evidence, dedup_key=key,
    )


def _change_create(kind: str, title: str, body: str, **extra) -> dict:
    after = {'kind': kind, 'title': title[:200], 'body': body, 'when_to_say': WHEN_PROACTIVE, **extra}
    return {'action': 'create', 'item_id': None, 'kind': kind, 'before': None, 'after': after}


def _change_update(item: KnowledgeItem, **after) -> dict:
    before = {name: getattr(item, name) for name in ('title', 'body', 'when_to_say', 'example_good', 'example_bad')}
    return {'action': 'update', 'item_id': item.id, 'kind': item.kind, 'before': before, 'after': after}


# --- Claude words the proposal (when there is a key) -----------------------------------------------------------

FORMULATE_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'explanation': {'type': 'string'},
        'kind': {'type': 'string', 'enum': list(PROPOSAL_KINDS)},
        'after': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'}, 'body': {'type': 'string'}, 'key': {'type': 'string'},
                'when_to_say': {'type': 'string', 'enum': ['proactive', 'if_asked', 'internal']},
                'example_good': {'type': 'string'}, 'example_bad': {'type': 'string'},
                'what_customer_writes': {'type': 'string'}, 'means': {'type': 'string'},
            },
            'required': ['title', 'body', 'key', 'when_to_say', 'example_good', 'example_bad', 'what_customer_writes', 'means'],
            'additionalProperties': False,
        },
        'understood': {'type': 'boolean'},
    },
    'required': ['title', 'explanation', 'kind', 'after', 'understood'],
    'additionalProperties': False,
}

FORMULATE_SYSTEM = """\
אתה עורך את בסיס הידע של בוט וואטסאפ של סטודיו לחוגי ילדים. מקבלים ראיות משיחה (מה הלקוח שאל, מה הבוט ענה, מה נציג אנושי ענה או מה בעל העסק הֵעיר), \
ומנסחים הצעת עדכון אחת לידע: נוסח (phrasing), עובדה (fact), כלל התנהגות (behavior_rule) או כינוי (alias). \
כותרת קצרה בעברית, הסבר של 2–3 שורות ("הבוט ענה X; הנציגה ענתה Y; ההבדל: …"), והרשומה המוצעת. \
מחיר, הנחה, כתובת, מועד או תפוסה אינם נכנסים לידע — הם ב-Kogo; אם ההערה עליהם, הצע כלל התנהגות ("לקרוא מהכלי") ולא עובדה עם מספר. \
אם הראיות לא מספיקות כדי להבין מה לתקן — understood=false ובהסבר "לא הבנתי, פרט". שדות שאינם רלוונטיים לסוג — מחרוזת ריקה."""


def formulate(draft: dict, evidence: list) -> dict | None:
    """Ask Claude to word the proposal. None when there is no key or no usable answer; never raises."""
    from apps.core.scoping import integration_credential

    try:
        api_key = integration_credential('ANTHROPIC_API_KEY')
    except Exception:
        return None
    if not api_key:
        return None
    body = {
        'model': (getattr(settings, 'WAHUB_SHADOW_MODEL', '') or 'claude-opus-5-5').strip(),
        'max_tokens': 8000,
        'system': FORMULATE_SYSTEM,
        'messages': [{'role': 'user', 'content': (
            f'מקור ההצעה: {draft.get("source_label", "")}\n'
            f'הצעה גולמית לפי הכללים: {json.dumps({k: draft.get(k) for k in ("title", "explanation", "change")}, ensure_ascii=False)}\n\n'
            f'הראיות:\n{json.dumps(evidence, ensure_ascii=False, default=str)}'
        )}],
        'output_config': {'effort': 'low', 'format': {'type': 'json_schema', 'schema': FORMULATE_SCHEMA}},
    }
    try:
        response = requests.post(
            ANTHROPIC_URL, json=body, timeout=(5, 25),
            headers={'x-api-key': api_key, 'anthropic-version': ANTHROPIC_VERSION, 'content-type': 'application/json'},
        )
        if response.status_code != 200:
            logger.warning('wahub reviewer: Claude answered %s; keeping the rule-made wording', response.status_code)
            return None
        payload = response.json()
        if payload.get('stop_reason') in ('refusal', 'max_tokens'):
            return None
        text = next(block.get('text', '') for block in payload.get('content') or [] if isinstance(block, dict) and block.get('type') == 'text')
        data = json.loads(text)
    except (requests.RequestException, ValueError, StopIteration, AttributeError, TypeError):
        logger.warning('wahub reviewer: could not read Claude\'s wording; keeping the rule-made one')
        return None
    if not isinstance(data, dict) or not data.get('understood'):
        return {'understood': False}
    after = {name: value for name, value in (data.get('after') or {}).items() if value not in ('', None)}
    kind = data.get('kind') if data.get('kind') in PROPOSAL_KINDS else draft['change'].get('kind')
    change = dict(draft['change'])
    change['kind'] = kind
    change['after'] = {**(change.get('after') or {}), **after, 'kind': kind}
    return {'title': str(data.get('title') or draft['title'])[:200], 'explanation': str(data.get('explanation') or draft['explanation']), 'change': change, 'understood': True}


def _worded(draft: dict, evidence: list) -> dict:
    """The rule-made draft, improved by Claude when there is a key. Always returns something usable."""
    better = formulate(draft, evidence) if ai_configured() else None
    if better and better.get('understood'):
        return {**draft, **{k: better[k] for k in ('title', 'explanation', 'change')}}
    if better and better.get('understood') is False:
        return {**draft, 'explanation': draft['explanation'] + '\nהסוכן: לא הבנתי עד הסוף, פרט בהערה.'}
    return draft


# --- the four sources -------------------------------------------------------------------------------------------

def propose_from_verdict(shadow: ShadowReply, note: str, user=None) -> KnowledgeProposal | None:
    """👎 with a note: the owner's words become the proposal."""
    note = ' '.join((note or '').split())
    if len(note) < MIN_NOTE_CHARS:
        return None
    customer = shadow.after_message
    evidence = []
    if customer is not None:
        evidence.append(_evidence(customer))
    evidence.append({'message_id': None, 'who': 'shadow', 'text': state.preview(shadow.text)[:300]})
    evidence.append({'message_id': None, 'who': 'office', 'text': f'הערת בעל המערכת: {note[:300]}'})
    draft = {
        'source_label': SOURCE_LABELS[SOURCE_BAD_VERDICT],
        'title': f'לתקן לפי ההערה: {note[:60]}',
        'explanation': f'הבוט בצל ענה: "{state.preview(shadow.text)[:120]}". בעל המערכת סימן שהתשובה לא טובה: "{note}".',
        'change': _change_create(KIND_BEHAVIOR_RULE, note[:60], note, example_bad=state.preview(shadow.text)[:300]),
    }
    draft = _worded(draft, evidence)
    return _create(
        source=SOURCE_BAD_VERDICT, title=draft['title'], explanation=draft['explanation'], change=draft['change'],
        evidence=evidence, contact=shadow.contact, message=customer, shadow=shadow,
    )


def propose_from_note(note: ServiceNote) -> KnowledgeProposal | None:
    """A service note. Too short to act on → None (the view answers "לא הבנתי, פרט")."""
    text = ' '.join((note.text or '').split())
    if len(text) < MIN_NOTE_CHARS:
        return None
    evidence = []
    if note.message_id and note.message:
        evidence.append(_evidence(note.message))
        # The bot's answer right after it, if there was one.
        after = Message.objects.filter(contact_id=note.message.contact_id, id__gt=note.message_id, direction=DIRECTION_OUT, sender=SENDER_BOT).order_by('id').first()
        if after is not None:
            evidence.append(_evidence(after))
    evidence.append({'message_id': None, 'who': 'office', 'text': f'הערת שירות: {text[:300]}'})
    draft = {
        'source_label': SOURCE_LABELS[SOURCE_SERVICE_NOTE],
        'title': f'הערת שירות: {text[:60]}',
        'explanation': f'שירות הלקוחות כתב: "{text}".',
        'change': _change_create(KIND_BEHAVIOR_RULE, text[:60], text),
    }
    draft = _worded(draft, evidence)
    proposal = _create(
        source=SOURCE_SERVICE_NOTE, title=draft['title'], explanation=draft['explanation'], change=draft['change'],
        evidence=evidence, contact=note.contact, message=note.message, dedup_extra='' if note.contact_id else f'note{note.id}',
    )
    if proposal is None and note.contact_id:
        proposal = _existing(_dedup_key(SOURCE_SERVICE_NOTE, note.contact_id, timezone.now()))
    return proposal


def _mark_needs_human(contact: Contact, reason: str) -> None:
    if contact.needs_human:
        return
    state.touch(contact.pk, needs_human=True, needs_human_reason=reason[:200], needs_human_at=timezone.now())
    state.log_event(contact.pk, EVENT_NEEDS_HUMAN, f'{NEEDS_HUMAN_BY_REVIEW}: {reason}')
    contact.needs_human = True


def _messages_of(contact: Contact) -> list[Message]:
    rows = list(Message.objects.filter(contact=contact).exclude(status=STATUS_FAILED).order_by('-id')[:MAX_MESSAGES])
    rows.reverse()
    return rows


def _human_override(contact: Contact, rows: list[Message], since, now) -> KnowledgeProposal | None:
    """An office message after a bot reply (or a shadow reply) to the same customer message."""
    for index, row in enumerate(rows):
        if row.direction != DIRECTION_OUT or row.sender != SENDER_OFFICE or row.status not in DELIVERED_STATUSES or row.sent_at < since:
            continue
        customer = next((prev for prev in reversed(rows[:index]) if prev.direction == DIRECTION_IN), None)
        if customer is None:
            continue
        bot = next((mid for mid in rows[rows.index(customer) + 1:index] if mid.direction == DIRECTION_OUT and mid.sender == SENDER_BOT), None)
        shadow = ShadowReply.objects.filter(contact=contact, after_message=customer).order_by('-id').first() if bot is None else None
        if bot is None and shadow is None:
            continue
        answered_by = 'הבוט הישן' if bot is not None else 'הבוט בצל'
        bot_text = bot.text if bot is not None else shadow.text
        if ' '.join(bot_text.split()) == ' '.join(row.text.split()):
            continue
        evidence = [_evidence(customer)]
        evidence.append(_evidence(bot) if bot is not None else {'message_id': None, 'who': 'shadow', 'text': state.preview(bot_text)[:300]})
        evidence.append(_evidence(row))
        who = row.sender_name or 'נציג'
        draft = {
            'source_label': SOURCE_LABELS[SOURCE_HUMAN_OVERRIDE],
            'title': f'להוסיף נוסח: {state.preview(row.text)[:50]}',
            'explanation': (
                f'הלקוח כתב: "{state.preview(customer.text)[:120]}". {answered_by} ענה: "{state.preview(bot_text)[:120]}". '
                f'{who} ענה/תה מעליו: "{state.preview(row.text)[:160]}". ההבדל: התשובה של הנציג היא מה שהבוט היה צריך לדעת.'
            ),
            'change': _change_create(
                KIND_PHRASING, f'מהמשרד: {state.preview(row.text)[:40]}', row.text.strip(),
                key=f'office_{row.id}', when=f'כשלקוח כותב: {state.preview(customer.text)[:80]}', verbatim=False,
            ),
        }
        draft = _worded(draft, evidence)
        return _create(
            source=SOURCE_HUMAN_OVERRIDE, title=draft['title'], explanation=draft['explanation'], change=draft['change'],
            evidence=evidence, contact=contact, message=customer, shadow=shadow, now=now,
        )
    return None


def _norm(text: str) -> str:
    return re.sub(r'[\s?!.,]+', ' ', (text or '')).strip().casefold()


def detect_patterns(rows: list[Message], since) -> list[dict]:
    """The failure patterns in one conversation: [{pattern, evidence: [Message]}]. Rules only."""
    found = []
    recent = [row for row in rows if row.sent_at >= since]
    bot_rows = [row for row in recent if row.direction == DIRECTION_OUT and row.sender == SENDER_BOT]
    customer_rows = [row for row in recent if row.direction == DIRECTION_IN]

    for row in bot_rows:
        if _INTERNAL.search(row.text or ''):
            found.append({'pattern': PATTERN_INTERNAL_TEXT, 'evidence': [row]})
        if _VOICE.search(row.text or ''):
            found.append({'pattern': PATTERN_VOICE, 'evidence': [row]})
        if _FAKE_REG.search(row.text or ''):
            found.append({'pattern': PATTERN_FAKE_REGISTRATION, 'evidence': [row]})
        if _NICOLE.search(row.text or ''):
            found.append({'pattern': PATTERN_NICOLE, 'evidence': [row]})
        if _UNAVAILABLE.search(row.text or ''):
            found.append({'pattern': PATTERN_UNAVAILABLE, 'evidence': [row]})
        if _WILL_CHECK.search(row.text or ''):
            later = [after for after in rows if after.id > row.id and after.direction == DIRECTION_OUT and after.sender == SENDER_OFFICE]
            if not later and timezone.now() - row.sent_at >= timedelta(hours=1):
                found.append({'pattern': PATTERN_WILL_CHECK, 'evidence': [row]})

    # two bot replies in a row with no customer message between them
    for earlier, later in zip(rows, rows[1:]):
        if (earlier.direction == DIRECTION_OUT and earlier.sender == SENDER_BOT and later.direction == DIRECTION_OUT
                and later.sender == SENDER_BOT and later.sent_at >= since and later.sent_at - earlier.sent_at <= timedelta(minutes=3)):
            found.append({'pattern': PATTERN_BOT_TWICE, 'evidence': [earlier, later]})
            break

    seen_bot: dict = {}
    for row in bot_rows:
        key = _norm(row.text)
        if len(key) >= 10 and '?' in (row.text or '') and key in seen_bot:
            found.append({'pattern': PATTERN_LOOP, 'evidence': [seen_bot[key], row]})
            break
        seen_bot.setdefault(key, row)

    for row in customer_rows:
        if _NOT_ANSWERED.search(row.text or ''):
            found.append({'pattern': PATTERN_NOT_ANSWERED, 'evidence': [row]})
            break
    seen_customer: dict = {}
    for row in [r for r in rows if r.direction == DIRECTION_IN]:
        key = _norm(row.text)
        if len(key) >= 8 and key in seen_customer and row.sent_at >= since:
            found.append({'pattern': PATTERN_REPEATED, 'evidence': [seen_customer[key], row]})
            break
        seen_customer.setdefault(key, row)

    unique, result = set(), []
    for item in found:
        if item['pattern'] not in unique:
            unique.add(item['pattern'])
            result.append(item)
    return result


def _pattern_proposal(contact: Contact, pattern: str, evidence_rows: list[Message], now) -> KnowledgeProposal | None:
    if KnowledgeProposal.objects.filter(source=SOURCE_REVIEWER, pattern=pattern, status=PROPOSAL_PENDING).exists():
        # One open proposal per pattern is enough; the next conversation adds nothing new.
        return None
    title, body = PATTERN_FIXES[pattern]
    existing_rule = KnowledgeItem.objects.filter(kind=KIND_BEHAVIOR_RULE, is_active=True, title=title).first()
    if existing_rule is not None and pattern in (PATTERN_NICOLE, PATTERN_FAKE_REGISTRATION, PATTERN_INTERNAL_TEXT):
        return None
    evidence = [_evidence(row) for row in evidence_rows]
    label = PATTERN_LABELS[pattern]
    draft = {
        'source_label': SOURCE_LABELS[SOURCE_REVIEWER],
        'title': f'{label}: {title}',
        'explanation': f'בסריקת השיחות נמצא: {label}. דוגמה: "{state.preview(evidence_rows[-1].text)[:140]}". ההצעה: {body}',
        'change': _change_create(KIND_BEHAVIOR_RULE, title, body, example_bad=state.preview(evidence_rows[-1].text)[:300]),
    }
    if existing_rule is not None:
        draft['change'] = _change_update(existing_rule, example_bad=state.preview(evidence_rows[-1].text)[:300])
        draft['title'] = f'{label}: להוסיף דוגמה שגויה לכלל "{title}"'
    draft = _worded(draft, evidence)
    return _create(
        source=SOURCE_REVIEWER, pattern=pattern, title=draft['title'], explanation=draft['explanation'], change=draft['change'],
        evidence=evidence, contact=contact, message=evidence_rows[-1], now=now,
    )


def review_contact(contact: Contact, *, now=None) -> dict:
    """
    One conversation: an office override and the failure patterns of the last
    day become proposals; the patterns that need a person mark the contact.
    """
    now = now or timezone.now()
    since = now - LOOKBACK
    counts = {'proposals': 0, 'patterns': {}, 'needs_human_marked': 0, 'overrides': 0}
    rows = _messages_of(contact)
    proposal = _human_override(contact, rows, since, now)
    if proposal is not None:
        counts['proposals'] += 1
        counts['overrides'] += 1
    for item in detect_patterns(rows, since):
        proposal = _pattern_proposal(contact, item['pattern'], item['evidence'], now)
        if proposal is not None:
            counts['proposals'] += 1
            counts['patterns'][item['pattern']] = counts['patterns'].get(item['pattern'], 0) + 1
        if item['pattern'] in NEEDS_HUMAN_PATTERNS and not contact.needs_human:
            _mark_needs_human(contact, PATTERN_LABELS[item['pattern']])
            counts['needs_human_marked'] += 1
    return counts


def scan(*, now=None, deadline: float | None = None, limit: int = MAX_CONTACTS) -> dict:
    """
    The cron's sweep: the conversations with a message in the last day, each
    looked at once for an office override and for the failure patterns.
    """
    import time as time_module

    now = now or timezone.now()
    since = now - LOOKBACK
    counts = {'contacts_scanned': 0, 'proposals': 0, 'patterns': {}, 'needs_human_marked': 0, 'overrides': 0}
    contacts = Contact.objects.filter(last_message_at__gte=since).order_by('-last_message_at', '-id')[:limit]
    for contact in contacts:
        if deadline is not None and time_module.monotonic() >= deadline:
            break
        counts['contacts_scanned'] += 1
        try:
            one = review_contact(contact, now=now)
        except Exception:
            logger.exception('wahub reviewer: contact %s failed', contact.pk)
            continue
        counts['proposals'] += one['proposals']
        counts['overrides'] += one['overrides']
        counts['needs_human_marked'] += one['needs_human_marked']
        for pattern, number in one['patterns'].items():
            counts['patterns'][pattern] = counts['patterns'].get(pattern, 0) + number
    counts['pending'] = KnowledgeProposal.objects.filter(status=PROPOSAL_PENDING).count()
    return counts


# --- the owner decides ------------------------------------------------------------------------------------------

@transaction.atomic
def approve(proposal: KnowledgeProposal, user, note: str = '') -> KnowledgeItem:
    proposal = KnowledgeProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != PROPOSAL_PENDING:
        raise ReviewError('ההצעה כבר הוחלטה.')
    where = f' מתוך שיחה {proposal.contact_id}' if proposal.contact_id else ''
    try:
        item = knowledge.apply_change(proposal.change, user, note=f'לפי הצעה #{proposal.id}{where}')
    except knowledge.KnowledgeError as exc:
        raise ReviewError(f'אי אפשר להחיל את ההצעה: {exc}') from exc
    proposal.status = PROPOSAL_APPLIED
    proposal.decided_at = timezone.now()
    proposal.decided_by = user if getattr(user, 'is_authenticated', False) else None
    proposal.decision_note = (note or '').strip()[:2000]
    proposal.applied_item = item
    proposal.save(update_fields=['status', 'decided_at', 'decided_by', 'decision_note', 'applied_item'])
    return item


@transaction.atomic
def reject(proposal: KnowledgeProposal, user, note: str = '') -> KnowledgeProposal:
    proposal = KnowledgeProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != PROPOSAL_PENDING:
        raise ReviewError('ההצעה כבר הוחלטה.')
    proposal.status = PROPOSAL_REJECTED
    proposal.decided_at = timezone.now()
    proposal.decided_by = user if getattr(user, 'is_authenticated', False) else None
    proposal.decision_note = (note or '').strip()[:2000]
    proposal.save(update_fields=['status', 'decided_at', 'decided_by', 'decision_note'])
    return proposal


def summary(now=None) -> dict:
    from django.db.models import Count, Q as _Q

    now = now or timezone.now()
    week = now - timedelta(days=7)
    totals = KnowledgeProposal.objects.aggregate(
        pending=Count('id', filter=_Q(status=PROPOSAL_PENDING)),
        applied_7d=Count('id', filter=_Q(status=PROPOSAL_APPLIED, decided_at__gte=week)),
        rejected_7d=Count('id', filter=_Q(status=PROPOSAL_REJECTED, decided_at__gte=week)),
    )
    return {**totals, 'auto_mode': False, 'notes_7d': ServiceNote.objects.filter(created_at__gte=week).count()}
