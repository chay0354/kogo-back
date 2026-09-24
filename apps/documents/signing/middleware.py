"""
The request's Vercel OIDC token, kept where the KMS client can find it.

With OIDC federation on, Vercel hands each function invocation a short-lived
token in the ``x-vercel-oidc-token`` request header — in the Python runtime
that header is the only place it arrives (the cron is a request too). The KMS
client exchanges it for Google credentials (apps/documents/signing/kms.py), so
no key to Google sits in an environment variable. A signature is made from
deep inside a service call or an on_commit hook, where the request is out of
reach; a context variable carries the token there for as long as the request
lasts, and is cleared with it.

Without the header this does nothing at all.
"""
from __future__ import annotations

from contextvars import ContextVar

OIDC_HEADER = 'x-vercel-oidc-token'

_request_oidc_token: ContextVar[str | None] = ContextVar('kogo_vercel_oidc_token', default=None)


def current_oidc_token() -> str | None:
    """The token of the request being served, or None outside one (or without the header)."""
    return _request_oidc_token.get()


class VercelOidcTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = (request.headers.get(OIDC_HEADER) or '').strip()
        if not token:
            return self.get_response(request)
        reset = _request_oidc_token.set(token)
        try:
            return self.get_response(request)
        finally:
            _request_oidc_token.reset(reset)
