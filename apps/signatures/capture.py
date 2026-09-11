"""
Keep the signature a parent gives in the registration widget.

The widget makes the parent accept the health declaration and the terms and
sign with a finger; the signature arrives as a PNG data URL in the register
payload. This module turns that into a Signature row: the image, the terms as
they read at that moment and their hash, the consents, who signed and from
where.

Capture is evidence, not a step of registering. It sits next to card charging
in a customer-facing flow, so it must never make a registration fail:
record_registration_signature() catches everything, logs it with context and
returns None. The views call it only after the registration has committed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.registration_terms_service import get_registration_terms

logger = logging.getLogger(__name__)

PNG_DATA_URL_PREFIX = 'data:image/png;base64,'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
MAX_SIGNATURE_BYTES = 300 * 1024
# Base64 is 4 characters per 3 bytes: anything longer than this cannot decode
# to an allowed image, so it is refused before being decoded at all.
_MAX_ENCODED_LENGTH = 4 * ((MAX_SIGNATURE_BYTES + 2) // 3)

# The widget sends one register call per child (and per extra lesson) with the
# same signature. The same family and the same image within this window are one
# signing, so they become one row with several children.
SAME_SIGNING_WINDOW = timedelta(minutes=30)

REGISTRATION_TERMS_TITLE = 'תקנון הרשמה'


def decode_signature_png(value) -> tuple[bytes | None, str]:
    """
    The PNG bytes of a `data:image/png;base64,` URL, or (None, reason).

    Only a real PNG (magic bytes) of at most MAX_SIGNATURE_BYTES is accepted:
    the column holds evidence, not whatever a hand-made request put there.
    """
    if not isinstance(value, str) or not value:
        return None, 'missing'
    if not value.startswith(PNG_DATA_URL_PREFIX):
        return None, 'not a data:image/png;base64 URL'
    encoded = value[len(PNG_DATA_URL_PREFIX):].strip()
    if len(encoded) > _MAX_ENCODED_LENGTH:
        return None, 'larger than the limit'
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None, 'not valid base64'
    if len(raw) > MAX_SIGNATURE_BYTES:
        return None, 'larger than the limit'
    if not raw.startswith(PNG_MAGIC):
        return None, 'not a PNG'
    return raw, ''


def client_ip(request) -> str | None:
    """
    The first X-Forwarded-For entry, else REMOTE_ADDR.

    Behind Vercel the first entry is the client the platform saw. A value that
    is not an IP address is skipped rather than failing the insert.
    """
    meta = getattr(request, 'META', {}) or {}
    forwarded = (meta.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    for candidate in (forwarded, (meta.get('REMOTE_ADDR') or '').strip()):
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def _truthy(value) -> bool:
    """JSON true or its form spelling — never bool('false')."""
    return str(value if value is not None else '').strip().lower() in ('true', '1', 'yes', 'on')


def _text(value, max_length: int) -> str:
    return (str(value).strip() if value is not None else '')[:max_length]


def consents_from_payload(data) -> dict:
    """
    What the parent ticked, as sent.

    computerized_documents follows the terms: the terms carry the paragraph on
    receiving tax documents by email (core migration 0020), so accepting them
    is that consent. A payload from a widget build that predates terms_consent
    still says the terms were accepted through computerized_docs_consent — the
    field whose meaning was exactly that — so it stands in when terms_consent
    is absent. Nothing is inferred for the health declaration.
    """
    if 'terms_consent' in data:
        terms = _truthy(data.get('terms_consent'))
    else:
        terms = _truthy(data.get('computerized_docs_consent'))
    return {
        'health': _truthy(data.get('health_consent')),
        'terms': terms,
        'computerized_documents': terms,
    }


def _json_safe_refs(refs) -> dict:
    """UUIDs and dates as strings; empty entries dropped from lists."""
    if not isinstance(refs, dict):
        return {}
    cleaned = json.loads(json.dumps(refs, default=str))
    for key, value in list(cleaned.items()):
        if isinstance(value, list):
            cleaned[key] = [item for item in value if item not in (None, '')]
    return cleaned


def merge_refs(existing, new) -> dict:
    """Lists are unioned in order, flags are OR-ed, other values are kept from the first signing."""
    merged = dict(existing or {})
    for key, value in (new or {}).items():
        current = merged.get(key)
        if isinstance(value, list):
            base = list(current) if isinstance(current, list) else ([] if current in (None, '') else [current])
            for item in value:
                if item not in base:
                    base.append(item)
            merged[key] = base
        elif isinstance(value, bool):
            merged[key] = bool(current) or value
        elif current in (None, ''):
            merged[key] = value
    return merged


def record_registration_signature(request, *, family, child, branch, data, refs=None):
    """
    Store the signature in a registration payload, if there is a valid one.

    Returns the Signature (a new one, or the signing this registration joined),
    or None when nothing was stored. Never raises.
    """
    try:
        return _record_registration_signature(
            request, family=family, child=child, branch=branch, data=data, refs=refs,
        )
    except Exception:
        logger.exception(
            'signature capture failed — nothing stored, the registration is unaffected '
            '(family %s, child %s)',
            getattr(family, 'pk', None), getattr(child, 'pk', None),
        )
        return None


def _record_registration_signature(request, *, family, child, branch, data, refs):
    from apps.signatures.models import Signature

    family_id = getattr(family, 'pk', None)
    child_id = getattr(child, 'pk', None)
    value = data.get('signature') if hasattr(data, 'get') else None
    if not value:
        logger.info(
            'signature capture: no signature in the payload (family %s, child %s)',
            family_id, child_id,
        )
        return None

    png, problem = decode_signature_png(value)
    if png is None:
        logger.warning(
            'signature capture: %s — nothing stored (family %s, child %s, %s characters)',
            problem, family_id, child_id, len(value) if isinstance(value, str) else type(value).__name__,
        )
        return None

    signature_sha256 = hashlib.sha256(png).hexdigest()
    new_refs = _json_safe_refs(refs)
    now = timezone.now()

    # Its own atomic block: a transaction of its own in autocommit, a savepoint
    # inside any transaction that happens to be open. A database error here
    # rolls back this block alone, never the registration around it.
    with transaction.atomic():
        existing = None
        if family is not None:
            existing = (
                Signature.objects.select_for_update()
                .filter(
                    kind=Signature.KIND_REGISTRATION_TERMS,
                    family=family,
                    signature_sha256=signature_sha256,
                    signed_at__gte=now - SAME_SIGNING_WINDOW,
                )
                .order_by('-signed_at')
                .first()
            )
        if existing is not None:
            if child is not None:
                existing.children.add(child)
            merged = merge_refs(existing.refs, new_refs)
            if merged != existing.refs:
                existing.refs = merged
                existing.save(update_fields=['refs'])
            return existing

        # The terms as they read at this moment — the office may edit them
        # tomorrow, and the row must still show what was signed today.
        document_html = get_registration_terms().content or ''
        meta = getattr(request, 'META', {}) or {}
        signer_name = ' '.join(
            part for part in (
                _text(data.get('parent_first_name'), 100),
                _text(data.get('parent_last_name'), 100),
            ) if part
        )
        signature = Signature.objects.create(
            kind=Signature.KIND_REGISTRATION_TERMS,
            signed_at=now,
            signer_name=signer_name[:200],
            signer_id_number=_text(data.get('parent_id_number'), 20),
            signer_phone=_text(data.get('parent_phone'), 20),
            signer_email=_text(data.get('parent_email'), 254),
            family=family,
            branch=branch or getattr(family, 'branch', None),
            document_title=REGISTRATION_TERMS_TITLE,
            document_html=document_html,
            document_sha256=hashlib.sha256(document_html.encode('utf-8')).hexdigest(),
            consents=consents_from_payload(data),
            signature_png=png,
            signature_sha256=signature_sha256,
            ip_address=client_ip(request),
            user_agent=_text(meta.get('HTTP_USER_AGENT'), 500),
            source=Signature.SOURCE_WIDGET,
            refs=new_refs,
        )
        if child is not None:
            signature.children.add(child)
        return signature
