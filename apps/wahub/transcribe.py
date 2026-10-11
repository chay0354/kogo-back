"""
What a recording in a demo conversation said (docs/WAHUB-CONTRACT-REAL-DEMO.md, ב).

OpenAI's transcription (`gpt-4o-mini-transcribe`, Hebrew) over plain HTTPS with
`requests`, like every other call this app makes. Claude never receives the
audio: the shadow bot answers the text that comes back, kept on the message as
`Message.transcript`. Without a key nothing is called and the bot answers by
its "הודעה קולית" rule, saying why.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

TRANSCRIPTIONS_URL = 'https://api.openai.com/v1/audio/transcriptions'
MODEL = 'gpt-4o-mini-transcribe'
LANGUAGE = 'he'
DEFAULT_TIMEOUT = 30
NO_KEY_REASON = 'אין תמלול: חסר מפתח OPENAI_API_KEY'
# OpenAI's 429 for an account with nothing left to spend — not a rate limit: waiting does not help, paying does.
NO_CREDIT_REASON = 'אין יתרה בחשבון OpenAI — התמלול לא עובד עד שמטעינים'
BAD_KEY_REASON = 'מפתח OpenAI לא תקין או בוטל'
_NO_CREDIT_CODES = ('insufficient_quota', 'billing_not_active', 'billing_hard_limit_reached')
_NO_CREDIT_WORDS = ('credit', 'quota', 'billing')
# Where the last result is kept for the settings screen (settings_store): {'ok', 'reason', 'at'}.
HEALTH_KEY = 'transcribe_health'


class TranscriptionError(Exception):
    """The recording was not transcribed. The message is for the screen, in Hebrew."""


def _key() -> str:
    from apps.core.scoping import integration_credential

    return integration_credential('OPENAI_API_KEY')


def configured() -> bool:
    return bool(_key())


def transcribe(audio: bytes, *, filename: str, media_type: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """The Hebrew text of the recording. Raises TranscriptionError."""
    key = _key()
    if not key:
        raise TranscriptionError(NO_KEY_REASON)
    try:
        response = requests.post(
            TRANSCRIPTIONS_URL,
            headers={'Authorization': f'Bearer {key}'},
            files={'file': (filename or 'voice.webm', audio, media_type or 'application/octet-stream')},
            data={'model': MODEL, 'language': LANGUAGE, 'response_format': 'json'},
            timeout=(min(5, timeout), timeout),
        )
    except requests.RequestException as exc:
        raise TranscriptionError(f'התמלול לא ענה ({type(exc).__name__})') from exc
    if response.status_code != 200:
        detail, code = '', ''
        try:
            error = response.json().get('error') or {}
            detail, code = str(error.get('message', ''))[:200], f"{error.get('code', '')} {error.get('type', '')}"
        except (ValueError, AttributeError):
            pass
        logger.warning('wahub transcribe: OpenAI answered %s %s', response.status_code, detail)
        reason = f'התמלול נכשל ({response.status_code})'
        if response.status_code == 429 and (
            any(word in code for word in _NO_CREDIT_CODES) or any(word in detail.lower() for word in _NO_CREDIT_WORDS)
        ):
            reason = NO_CREDIT_REASON
        elif response.status_code == 401:
            reason = BAD_KEY_REASON
        _remember(False, reason)
        raise TranscriptionError(reason)
    try:
        text = str(response.json().get('text') or '').strip()
    except ValueError as exc:
        raise TranscriptionError('תשובה לא קריאה מהתמלול') from exc
    _remember(True)
    return text


def _remember(ok: bool, reason: str = '') -> None:
    """
    Whether OpenAI took the last recording, for the settings screen. Written
    only when it changes (it worked and now does not, or the other way, or the
    reason is another one) — and never allowed to fail a transcription.
    """
    try:
        from django.utils import timezone

        from apps.wahub import settings_store

        last = settings_store.get(HEALTH_KEY) or {}
        if last.get('ok') is ok and last.get('reason', '') == reason:
            return
        settings_store.put(HEALTH_KEY, {'ok': ok, 'reason': reason, 'at': timezone.now().isoformat()})
    except Exception:
        logger.exception('wahub transcribe: the result could not be written down')


def health() -> dict:
    """{'configured', 'ok', 'reason', 'at'} — `ok` is None until a recording was tried."""
    from apps.wahub import settings_store

    last = settings_store.get(HEALTH_KEY) or {}
    if not configured():
        return {'configured': False, 'ok': False, 'reason': NO_KEY_REASON, 'at': None}
    return {'configured': True, 'ok': last.get('ok'), 'reason': last.get('reason', ''), 'at': last.get('at')}
