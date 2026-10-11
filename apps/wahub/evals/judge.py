"""
The rubric of the bot's self-checks: short Hebrew sentences about an answer
("the bot asked for the field before it offered a class") that code cannot
decide, judged by a second call to Claude — only where there is a key.

One call per conversation: the judge reads the whole conversation as it was
played (what the customer wrote, what the bot answered, which tools it called
and what one line they returned) and gives each sentence a verdict and one
plain line of why. It is told to judge the sentence and nothing else, and to
fail a sentence it cannot confirm from the conversation.

Plain HTTPS, as analysis.py and shadow.py do, with the same server-side
fallback as the shadow's own call (a request the model declines is re-run on
another inside the same call). A failure of the judge is never a failed check:
the sentences stay "not judged", with the reason.
"""
from __future__ import annotations

import json
import logging
import time as time_module

import requests
from django.conf import settings

from apps.wahub.analysis import ANTHROPIC_URL, ANTHROPIC_VERSION

logger = logging.getLogger(__name__)

# The same beta the shadow's own call sends (shadow.FALLBACK_BETA).
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
DEFAULT_TIMEOUT = 30
NO_KEY = 'לא נשפט: אין מפתח של Claude במערכת הזאת'

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


def _conversation(turns: list) -> str:
    parts = []
    for turn in turns:
        parts.append(f'--- תור {turn["turn"]} ---')
        if turn.get('last_outbound'):
            parts.append(f'(ההודעה האחרונה שהמערכת שלחה ללקוח לפני התור הזה: "{turn["last_outbound"]}")')
        for message in turn.get('customer') or []:
            kind = {'voice': '[הקלטה שלא תומללה] ', 'image': '[תמונה שלא ניתן לפתוח] '}.get(message.get('kind'), '')
            parts.append(f'לקוח: {kind}{message.get("text") or ""}'.rstrip())
        for tool in turn.get('tools') or []:
            parts.append(f'(הבוט בדק במערכת — {tool.get("name")}: {tool.get("summary") or ""})')
        if turn.get('request_human'):
            parts.append('(הבוט סימן שהשיחה צריכה נציג)')
        parts.append(f'הבוט: {turn.get("reply") or ""}')
    return '\n'.join(parts)


def judge(turns: list, sentences: list, *, deadline: float | None = None) -> list:
    """
    `sentences`: [{turn, text}]. Returns one {status, explain} per sentence, in
    order: passed / failed with the judge's line, or not_judged with the reason.
    Never raises.
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
        'messages': [{'role': 'user', 'content': f'השיחה:\n{_conversation(turns)}\n\nהמשפטים:\n{listed}'}],
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
