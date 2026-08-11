"""Send transactional email via Resend HTTP API."""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


def resend_configured() -> bool:
    return bool((getattr(settings, 'RESEND_API_KEY', '') or '').strip())


def resend_from_address() -> str:
    explicit = (getattr(settings, 'RESEND_FROM_EMAIL', '') or '').strip()
    if explicit:
        return explicit
    default = (getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    if default and '@' in default and '<' not in default:
        return f'קוגומלו <{default}>'
    if default:
        return default
    return 'קוגומלו <onboarding@resend.dev>'


def send_resend_email(
    *,
    to: list[str],
    subject: str,
    text: str,
    html: str,
    attachments: list[dict] | None = None,
) -> str:
    api_key = (getattr(settings, 'RESEND_API_KEY', '') or '').strip()
    if not api_key:
        raise ValueError('RESEND_API_KEY not configured')

    body: dict = {
        'from': resend_from_address(),
        'to': to,
        'subject': subject,
        'html': html,
        'text': text,
    }
    if attachments:
        body['attachments'] = attachments

    response = requests.post(
        RESEND_API_URL,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json=body,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f'Resend failed ({response.status_code}): {response.text}')

    payload = response.json()
    message_id = payload.get('id', '')
    logger.info('Resend email sent id=%s to=%s subject=%s', message_id, to, subject)
    return message_id
