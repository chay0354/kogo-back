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
        detail = ''
        try:
            detail = str((response.json().get('error') or {}).get('message', ''))[:200]
        except ValueError:
            pass
        logger.warning('wahub transcribe: OpenAI answered %s %s', response.status_code, detail)
        raise TranscriptionError(f'התמלול נכשל ({response.status_code})')
    try:
        text = str(response.json().get('text') or '').strip()
    except ValueError as exc:
        raise TranscriptionError('תשובה לא קריאה מהתמלול') from exc
    return text
