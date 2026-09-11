"""Where a link we hand to a parent should point.

`crm_frontend_url()` falls back to `http://localhost:3000` when CRM_FRONTEND_URL
is not configured. On a deployment without that variable every card link the
office created pointed at localhost — the parent tapped it and got "refused to
connect". The office always creates and sends links from the CRM itself, so the
origin of that request is exactly the host the parent needs.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from django.conf import settings

from apps.core.password_reset_email import crm_frontend_url


def _origin_of(url: str) -> str:
    parts = urlsplit((url or '').strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return ''
    return f'{parts.scheme}://{parts.netloc}'


def _is_allowed_origin(origin: str) -> bool:
    """Only an origin the API already trusts for CORS may become a link host."""
    allowed = {o.rstrip('/') for o in getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or []}
    if origin in allowed:
        return True
    for pattern in getattr(settings, 'CORS_ALLOWED_ORIGIN_REGEXES', []) or []:
        if re.match(pattern, origin):
            return True
    return False


def public_frontend_url(request=None) -> str:
    """
    Base URL for parent-facing links, in order of trust:
    an explicit CRM_FRONTEND_URL, then the CRM origin the request came from,
    then the legacy fallback.
    """
    explicit = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip()
    if explicit:
        return explicit.rstrip('/')
    if request is not None:
        headers = getattr(request, 'headers', {}) or {}
        for candidate in (headers.get('Origin'), headers.get('Referer')):
            origin = _origin_of(candidate or '')
            if origin and _is_allowed_origin(origin):
                return origin
    return crm_frontend_url()
