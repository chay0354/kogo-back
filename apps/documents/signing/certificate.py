"""
The certificate the documents are signed under — self-issued, public.

A secured electronic signature needs no certificate authority (חוק חתימה
אלקטרונית, ס' 1, 3): the certificate here only names who holds the key, for
whoever opens the PDF. It is built once, by `make_signing_certificate`, and
signed by the signing key itself, so the private key never has to leave the
HSM even for that. Its public PEM is committed as
apps/documents/assets/signing_cert.pem (or given in SIGNING_CERT_PEM) and is
published with its fingerprint at /api/v1/documents/signing/certificate/.

Acrobat shows such a signature as "the document has not been modified since
it was signed" beside "the signer's identity is unknown" — the owner knew that
when choosing a secured signature over an approved one. Moving to an approved
signature later means another key and certificate; nothing else here changes.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from asn1crypto import pem, x509
from django.conf import settings

from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_NAME
from apps.documents.signing import SigningUnavailable

CERT_PATH = Path(__file__).resolve().parent.parent / 'assets' / 'signing_cert.pem'
VALIDITY_DAYS = 3650


def pem_text(value: str) -> str:
    """A PEM from an environment variable, whose newlines may have arrived as the two characters \\n."""
    text = (value or '').strip()
    if '\n' not in text and '\\n' in text:
        text = text.replace('\\n', '\n')
    return text


def _certificate_pem() -> bytes | None:
    configured = pem_text(getattr(settings, 'SIGNING_CERT_PEM', '') or '')
    if configured:
        return configured.encode()
    if CERT_PATH.exists():
        return CERT_PATH.read_bytes()
    return None


def load_certificate() -> x509.Certificate | None:
    """The configured certificate, or None when there is none (nothing is signed then)."""
    data = _certificate_pem()
    if not data:
        return None
    try:
        if pem.detect(data):
            _type, _headers, data = pem.unarmor(data)
        certificate = x509.Certificate.load(data)
        certificate.native  # parse it all now, not halfway through a signature
    except Exception as exc:
        raise SigningUnavailable('The signing certificate is not a readable X.509 certificate') from exc
    return certificate


def fingerprint_sha256(certificate: x509.Certificate) -> str:
    return hashlib.sha256(certificate.dump()).hexdigest()


def subject_text(certificate: x509.Certificate) -> str:
    return certificate.subject.human_friendly


def certificate_pem(certificate: x509.Certificate) -> str:
    return pem.armor('CERTIFICATE', certificate.dump()).decode('ascii')


def issuer_subject() -> x509.Name:
    """O and CN the company's name, its ח.פ. as serialNumber, and IL."""
    return x509.Name.build({
        'country_name': 'IL',
        'organization_name': ISSUER_NAME,
        'common_name': ISSUER_NAME,
        'serial_number': ISSUER_COMPANY_NUMBER,
    })


def build_self_issued_certificate(backend, *, days: int = VALIDITY_DAYS, serial: int | None = None,
                                  now: datetime | None = None) -> x509.Certificate:
    """
    An X.509 v3 certificate for the backend's public key, signed by the backend itself.

    keyUsage digitalSignature + nonRepudiation (critical): what a document
    signature is, and what pyHanko's validator asks of a signer by default.
    Not a CA. Ten years by default, well past the seven the originals are kept.
    """
    public_key = backend.public_key_info()
    algorithm = backend.signature_algorithm()
    name = issuer_subject()
    now = (now or datetime.now(dt_timezone.utc)).replace(microsecond=0)
    not_before = now - timedelta(minutes=5)
    not_after = not_before + timedelta(days=days)
    key_identifier = hashlib.sha1(public_key['public_key'].contents).digest()

    tbs = x509.TbsCertificate({
        'version': 'v3',
        'serial_number': serial or secrets.randbits(63) + 1,
        'signature': {'algorithm': algorithm},
        'issuer': name,
        'validity': {
            # UTCTime runs to 2049 (RFC 5280 §4.1.2.5); ten years from now is inside it.
            'not_before': x509.Time(name='utc_time', value=not_before),
            'not_after': x509.Time(name='utc_time', value=not_after),
        },
        'subject': name,
        'subject_public_key_info': public_key,
        'extensions': [
            {'extn_id': 'basic_constraints', 'critical': True, 'extn_value': {'ca': False}},
            {'extn_id': 'key_usage', 'critical': True,
             'extn_value': {'digital_signature', 'non_repudiation'}},
            {'extn_id': 'key_identifier', 'critical': False, 'extn_value': key_identifier},
            {'extn_id': 'authority_key_identifier', 'critical': False,
             'extn_value': {'key_identifier': key_identifier}},
        ],
    })
    signature = backend.sign_digest(hashlib.sha256(tbs.dump()).digest(), 'sha256')
    return x509.Certificate({
        'tbs_certificate': tbs,
        'signature_algorithm': {'algorithm': algorithm},
        'signature_value': signature,
    })


def certificate_matches_backend(certificate: x509.Certificate, backend) -> bool:
    return certificate.public_key.dump() == backend.public_key_info().dump()


def describe(certificate: x509.Certificate | None) -> dict:
    """What the public certificate page shows."""
    if certificate is None:
        return {
            'configured': False, 'pem': '', 'fingerprint_sha256': '', 'subject': '',
            'not_before': None, 'not_after': None,
        }
    validity = certificate['tbs_certificate']['validity']
    return {
        'configured': True,
        'pem': certificate_pem(certificate),
        'fingerprint_sha256': fingerprint_sha256(certificate),
        'subject': subject_text(certificate),
        'not_before': validity['not_before'].native.isoformat(),
        'not_after': validity['not_after'].native.isoformat(),
    }
