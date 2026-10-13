"""
The rubric of the bot's self-checks: short Hebrew sentences about an answer
("the bot asked for the field before it offered a class") that code cannot
decide, judged by a second call to Claude — only where there is a key.

One call per conversation: the judge reads the whole conversation as it was
played and gives each sentence a verdict and one plain line of why. It is told
to judge the sentence and nothing else, and to fail a sentence it cannot
confirm from the conversation.

It reads what the bot itself read (the full run of 13.10.2026: eleven answers
that were right by the knowledge or by a tool failed, because the judge was
shown one line of summary and wrote "no such check is seen in the
conversation" — the ₪60 of the ceremony, cancelling until 01.04, the way to a
branch, how many are registered, the dates of a trial):

    * what was said before the script begins, and the card of the writer;
    * the clock of every turn — "tomorrow" is a date;
    * what every tool returned, not only its line: whole when it is short,
      and when it is long — the fields the answer quotes first, then the rest
      for as long as there is room (`_returned`);
    * the items of the knowledge the answer rests on, title and body.

Plain HTTPS, as analysis.py and shadow.py do, with the same server-side
fallback as the shadow's own call (a request the model declines is re-run on
another inside the same call). A failure of the judge is never a failed check:
the sentences stay "not judged", with the reason.
"""
from __future__ import annotations

import json
import logging
import re
import time as time_module

import requests
from django.conf import settings

from apps.wahub.analysis import ANTHROPIC_URL, ANTHROPIC_VERSION

logger = logging.getLogger(__name__)

# The same beta the shadow's own call sends (shadow.FALLBACK_BETA).
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
DEFAULT_TIMEOUT = 30
NO_KEY = 'לא נשפט: אין מפתח של Claude במערכת הזאת'
# How much of the system's own words the judge is shown: one tool's answer, one item of the knowledge, the card of
# the writer — and the whole conversation, which halves the first of these until it fits.
MAX_RESULT_CHARS = 3000
MAX_ITEM_CHARS = 700
MAX_CARD_CHARS = 1500
MAX_CONVERSATION_CHARS = 60000
LEAST_RESULT_CHARS = 400
# What a tool returns that says nothing of the world: its own ids, and its instructions to the bot.
_NOISE_KEYS = frozenset({'id', 'course_id', 'lesson_id', 'track_id', 'branch_id', 'child_id', 'ns', 'image', 'photo', 'note', 'hint'})
# What says which class, lesson or branch a quoted field belongs to: kept beside it when an answer is cut.
_WHOSE_KEYS = ('name', 'day', 'start', 'date', 'branch', 'city')
_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_WHO = {'customer': 'לקוח', 'bot': 'הבוט', 'office': 'נציג מהמשרד', 'system': 'מערכת'}

SCHEMA = {
    'type': 'object',
    'properties': {
        'verdicts': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'number': {'type': 'integer', 'description': 'מספר המשפט, כפי שניתן'},
                    'holds': {'type': 'boolean', 'description': 'האם המשפט נכון לגבי התשובה של הבוט'},
                    'why': {'type': 'string', 'description': 'שורה אחת בעברית פשוטה: מה בתשובה מראה את זה'},
                },
                'required': ['number', 'holds', 'why'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['verdicts'],
    'additionalProperties': False,
}

SYSTEM = """\
את בודקת תשובות של בוט וואטסאפ של סטודיו לחוגים. מקבלים שיחה שלמה (מה הלקוח כתב, מה הבוט ענה, ואילו בדיקות הבוט עשה במערכת) \
ורשימת משפטים ממוספרים, כל אחד על תשובה מסוימת של הבוט.
לכל משפט: holds=true רק אם המשפט נכון לגבי התשובה הזאת, לפי מה שכתוב בשיחה. אם אי אפשר לאשר אותו מהשיחה — holds=false.
מה שכתוב בסוגריים הוא חלק מהשיחה, והוא מה שהבוט עצמו קרא: מה נאמר לפני השיחה, מה המערכת יודעת על הכותב, השעה של כל תור, \
מה כל בדיקה במערכת החזירה ("חזר:"), והידע שהתשובה נשענת עליו ("מהידע של הבוט"). \
עובדה שהבוט אמר — סכום, תאריך, כתובת, הוראות הגעה, כמה רשומים — ומופיעה שם, הגיעה מהמערכת או מהידע: זו לא המצאה, וזו כן בדיקה שנעשתה. \
עובדה שלא מופיעה שם ולא בדברי הלקוח — לא הגיעה מהמערכת. מה שחזר ארוך מקוצר, והשדות שהתשובה מצטטת מופיעים ראשונים.
שופטים את המשפט בלבד: לא את הסגנון, לא את מה שהיה אפשר לענות טוב יותר, ולא כללים שלא כתובים בו.
why — שורה אחת בעברית פשוטה, בלי מונחים טכניים, שמצטטת מהתשובה את מה שהכריע.
הפלט: JSON בלבד לפי הסכמה.
"""


def configured() -> bool:
    from apps.wahub import analysis

    return analysis.ai_configured()


def model_name() -> str:
    from apps.wahub import shadow

    return (getattr(settings, 'WAHUB_EVAL_JUDGE_MODEL', '') or shadow.model_name()).strip()


def _clean(value):
    """A tool's answer without its ids, its instructions to the bot and its empty fields."""
    if isinstance(value, dict):
        kept = {key: _clean(inner) for key, inner in value.items() if key not in _NOISE_KEYS}
        return {key: inner for key, inner in kept.items() if inner not in (None, '', [], {})}
    if isinstance(value, (list, tuple)):
        return [inner for inner in (_clean(inner) for inner in value) if inner not in (None, '', [], {})]
    if isinstance(value, str) and _ID.match(value):
        return None
    return value


def _flat(value, path=()):
    """Every field of a tool's answer with where it stands: (('course', 'lessons', 0, 'seats_text'), 'רשומים 9 מתוך 14')."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _flat(inner, path + (key,))
    elif isinstance(value, (list, tuple)):
        for index, inner in enumerate(value):
            yield from _flat(inner, path + (index,))
    else:
        yield path, value


def _squeezed(text) -> str:
    return re.sub(r'[\s\-–"״\'׳.,:;!?*()₪]', '', str(text or '')).casefold()


def _quoted(value, reply: str) -> bool:
    """Is this field of a tool's answer said in the bot's reply — as it is, as the number it is, or by most of its words."""
    if isinstance(value, bool) or value in (None, ''):
        return False
    text = str(value)
    if re.fullmatch(r'-?\d+\.\d{2}', text):
        text = text.lstrip('-')
        text = text[:-3] if text.endswith('.00') else text         # "260.00" is written ₪260
        return re.search(rf'(?<![\d.,]){re.escape(text)}(?![\d])', reply) is not None
    squeezed = _squeezed(text)
    if len(squeezed) < 2:
        return False
    if squeezed in _squeezed(reply):
        return True
    # Said in the bot's own words: most of the field's words are in the answer — and every number of it, or it is
    # another lesson's line ("רשומים 3 מתוך 14" is not "רשומים 9 מתוך 14").
    words = re.findall(r'[א-ת]{3,}', text)
    numbers = re.findall(r'\d+', text)
    return (
        len(words) >= 4 and sum(1 for word in words if word in reply) * 2 >= len(words)
        and all(re.search(rf'(?<!\d){number}(?!\d)', reply) for number in numbers)
    )


def _telling(value) -> bool:
    """A quoted field that says something of its own — a sentence, an address, a sum — and not a word every lesson shares ("שלישי")."""
    return re.fullmatch(r'-?\d+\.\d{2}', str(value)) is not None or len(_squeezed(value)) >= 6


def _path(path: tuple) -> str:
    text = ''
    for part in path:
        text += f'[{part}]' if isinstance(part, int) else (f'.{part}' if text else str(part))
    return text


def _returned(result, reply: str, limit: int = MAX_RESULT_CHARS) -> str:
    """
    What a tool returned, as the judge is shown it. Whole when it fits in
    `limit`. Cut otherwise, field by field: first the fields the bot's reply
    quotes — the address, the way there, how many are registered, a trial's
    dates, a price — each with what says whose it is (the class, the day), then
    the rest in their order while there is room. Never raises.
    """
    try:
        cleaned = _clean(result)
        whole = json.dumps(cleaned, ensure_ascii=False, default=str)
        if len(whole) <= limit:
            return whole
        fields = dict(_flat(cleaned))
        quoted = [path for path, value in fields.items() if _quoted(value, reply)]
        first = set()
        for path in quoted:
            if _telling(fields[path]):
                first.add(path)
                # Whose it is: the name of the class, the day of the lesson — the same fields of everything it stands in.
                for depth in range(len(path)):
                    first.update(path[:depth] + (key,) for key in _WHOSE_KEYS if path[:depth] + (key,) in fields)
        second = set(quoted) - first
        order = [path for path in fields if path in first] + [path for path in fields if path in second]
        order += [path for path in fields if path not in first and path not in second]
        lines, used, left_out = [], 0, 0
        for path in order:
            line = f'{_path(path)}: {fields[path]}'
            if used + len(line) + 1 > limit:
                left_out += 1
                continue
            lines.append(line)
            used += len(line) + 1
        return '\n'.join(lines) + (f'\n… (קוצר: עוד {left_out} שדות שלא נכנסו)' if left_out else '')
    except Exception:  # the judge must still be asked
        logger.exception('wahub evals: a tool result could not be shown to the judge')
        return ''


def _short_text(text, limit: int) -> str:
    text = str(text or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def _conversation(turns: list, *, before=(), card=None, result_chars: int = MAX_RESULT_CHARS) -> str:
    """
    The conversation as the judge reads it. `before` — what was said before the
    script's first turn ([{who, text, hours_ago}]); `card` — what the system
    told the bot about the writer, when the script has one.
    """
    parts = []
    if before:
        parts.append('--- לפני השיחה (נאמר קודם, והבוט קרא את זה) ---')
        for line in before:
            hours = float(line.get('hours_ago') or 0)
            ago = f'{round(hours * 60)} דקות' if hours < 1 else f'{hours:g} שעות'
            parts.append(f'(לפני {ago}) {_WHO.get(line.get("who"), "מערכת")}: {line.get("text") or ""}')
    if card:
        parts.append('(מה המערכת יודעת על הכותב, כפי שנמסר לבוט: ' + _short_text(json.dumps(_clean(card), ensure_ascii=False, default=str), MAX_CARD_CHARS) + ')')
    for turn in turns:
        parts.append(f'--- תור {turn["turn"]} ---')
        if turn.get('at_text'):
            parts.append(f'(עכשיו, בשעון של השיחה: {turn["at_text"]})')
        if turn.get('last_outbound'):
            parts.append(f'(ההודעה האחרונה שהמערכת שלחה ללקוח לפני התור הזה: "{turn["last_outbound"]}")')
        for message in turn.get('customer') or []:
            kind = {'voice': '[הקלטה שלא תומללה] ', 'image': '[תמונה שלא ניתן לפתוח] '}.get(message.get('kind'), '')
            parts.append(f'לקוח: {kind}{message.get("text") or ""}'.rstrip())
        reply = turn.get('reply') or ''
        for tool in turn.get('tools') or []:
            parts.append(f'(הבוט בדק במערכת — {tool.get("name")}: {tool.get("summary") or ""})')
            returned = _returned(tool.get('result'), reply, result_chars) if tool.get('result') is not None else ''
            if returned:
                parts.append(f'(חזר: {returned})')
        for item in turn.get('knowledge') or []:
            parts.append(f'(מהידע של הבוט — {item.get("title") or ""}: {_short_text(item.get("text"), MAX_ITEM_CHARS)})')
        if turn.get('request_human'):
            parts.append('(הבוט סימן שהשיחה צריכה נציג)')
        for tag in turn.get('tags') or []:
            parts.append(f'(הבוט סימן על השיחה את התגית "{tag.get("tag") or ""}")')
        parts.append(f'הבוט: {reply}')
    return '\n'.join(parts)


def conversation_for_judge(turns: list, *, before=(), card=None) -> str:
    """The conversation with everything the bot read — inside a size the judge reads well: a long one shows less of each tool's answer."""
    limit = MAX_RESULT_CHARS
    text = _conversation(turns, before=before, card=card, result_chars=limit)
    while len(text) > MAX_CONVERSATION_CHARS and limit > LEAST_RESULT_CHARS:
        limit = max(LEAST_RESULT_CHARS, limit // 2)
        text = _conversation(turns, before=before, card=card, result_chars=limit)
    return text


def judge(turns: list, sentences: list, *, deadline: float | None = None, before=(), card=None) -> list:
    """
    `sentences`: [{turn, text}]. Returns one {status, explain} per sentence, in
    order: passed / failed with the judge's line, or not_judged with the reason.
    `before` and `card`: what the bot read that is not in the turns (see
    `_conversation`). Never raises.
    """
    from apps.core.scoping import integration_credential

    def not_judged(reason: str) -> list:
        return [{'status': 'not_judged', 'explain': reason} for _ in sentences]

    if not sentences:
        return []
    try:
        api_key = integration_credential('ANTHROPIC_API_KEY')
    except Exception:
        logger.exception('wahub evals: could not read the Anthropic key')
        api_key = ''
    if not api_key:
        return not_judged(NO_KEY)

    seconds = float(getattr(settings, 'WAHUB_EVAL_JUDGE_TIMEOUT_SECONDS', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    if deadline is not None:
        seconds = min(seconds, deadline - time_module.monotonic())
        if seconds < 5:
            return not_judged('לא נשפט: לא נשאר זמן בבקשה הזאת')
    listed = '\n'.join(f'{index}. (על התשובה בתור {row["turn"]}) {row["text"]}' for index, row in enumerate(sentences, 1))
    body = {
        'model': model_name(),
        # A ceiling, not a spend: thinking counts toward it.
        'max_tokens': 16000,
        'system': SYSTEM,
        'messages': [{'role': 'user', 'content': f'השיחה:\n{conversation_for_judge(turns, before=before, card=card)}\n\nהמשפטים:\n{listed}'}],
        'output_config': {'effort': 'low', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
        'fallbacks': 'default',
    }
    try:
        response = requests.post(
            ANTHROPIC_URL,
            headers={
                'x-api-key': api_key, 'anthropic-version': ANTHROPIC_VERSION, 'anthropic-beta': FALLBACK_BETA,
                'content-type': 'application/json',
            },
            json=body, timeout=(min(5, seconds), seconds),
        )
    except requests.RequestException as exc:
        logger.warning('wahub evals: the judge did not answer (%s)', type(exc).__name__)
        return not_judged('לא נשפט: Claude לא ענה בזמן')
    if response.status_code != 200:
        logger.warning('wahub evals: the judge answered %s', response.status_code)
        return not_judged(f'לא נשפט: Claude ענה {response.status_code}')
    try:
        payload = response.json()
        if payload.get('stop_reason') in ('refusal', 'max_tokens'):
            return not_judged('לא נשפט: Claude לא השלים את התשובה')
        text = next(
            block.get('text', '') for block in payload.get('content') or []
            if isinstance(block, dict) and block.get('type') == 'text'
        )
        rows = json.loads(text).get('verdicts')
    except (ValueError, StopIteration, AttributeError, TypeError):
        return not_judged('לא נשפט: התשובה של Claude לא קריאה')
    by_number = {row.get('number'): row for row in rows if isinstance(row, dict)} if isinstance(rows, list) else {}
    verdicts = []
    for index in range(1, len(sentences) + 1):
        row = by_number.get(index)
        if row is None or not isinstance(row.get('holds'), bool):
            verdicts.append({'status': 'not_judged', 'explain': 'לא נשפט: Claude לא החזיר הכרעה למשפט הזה'})
        else:
            verdicts.append({'status': 'passed' if row['holds'] else 'failed', 'explain': ' '.join(str(row.get('why') or '').split())[:400]})
    return verdicts
