"""What the signing tests share: a local EC key, its certificate, and the settings that switch signing on.

No network anywhere: LocalKeyBackend signs, and the KMS tests mock the HTTP layer.
"""
from __future__ import annotations

import base64
import io
from functools import lru_cache

from django.test import override_settings
from pypdf import PdfReader


@lru_cache(maxsize=None)
def local_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode('ascii')


@lru_cache(maxsize=None)
def local_cert_pem() -> str:
    from apps.documents.signing.backends import LocalKeyBackend
    from apps.documents.signing.certificate import build_self_issued_certificate, certificate_pem

    return certificate_pem(build_self_issued_certificate(LocalKeyBackend(local_key_pem())))


def signing_settings(**extra) -> dict:
    return {
        'DOCUMENT_SIGNING_ENABLED': True,
        'COMPUTERIZED_CONSENT_ENFORCED': False,
        'SIGNING_KMS_KEY_VERSION': '',
        'SIGNING_LOCAL_KEY_PEM': local_key_pem(),
        'SIGNING_CERT_PEM': local_cert_pem(),
        'SIGNING_GCP_SA_KEY_JSON': '',
        # A mail provider, so the exits get as far as sending (Resend is patched in each test).
        'RESEND_API_KEY': 'test-resend-key',
        'TRANZILA_BILLING_TERMINAL': '',
        **extra,
    }


def signing_on(**extra):
    """override_settings for a class or a test: signing on, with the local key and its certificate."""
    return override_settings(**signing_settings(**extra))


def archive_settings(**extra) -> dict:
    """The archive switch on, the customer-facing one off — the order the owner turns them on in."""
    return signing_settings(**{'DOCUMENT_SIGNING_ENABLED': False, 'SIGNING_ARCHIVE_ENABLED': True, **extra})


def archive_on(**extra):
    """override_settings: archive signing on (SIGNING_ARCHIVE_ENABLED), DOCUMENT_SIGNING_ENABLED off, local key."""
    return override_settings(**archive_settings(**extra))


def attachment_bytes(resend_mock) -> bytes:
    """The single PDF a patched send_resend_email was handed, decoded."""
    attachments = resend_mock.call_args.kwargs['attachments']
    assert len(attachments) == 1, attachments
    return base64.b64decode(attachments[0]['content'])


def pdf_text(pdf_bytes: bytes) -> str:
    return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf_bytes)).pages)
