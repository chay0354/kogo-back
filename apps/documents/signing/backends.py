"""
Who holds the signing key.

A backend signs a digest and nothing else: the PDF, the certificate and the
CMS structure around the signature are pyHanko's (signer.py), so the key's
holder can change — a local key in tests, a Cloud KMS HSM in production, a
certificate authority's token one day — without the rest noticing.

The signature comes back DER-encoded for EC keys and as PKCS#1 v1.5 for RSA,
which is what Cloud KMS returns for EC_SIGN_P256_SHA256 and
RSA_SIGN_PKCS1_*_SHA256 keys, and what CMS carries.
"""
from __future__ import annotations

import hashlib
import os

from asn1crypto import keys
from django.conf import settings

from apps.documents.signing import SigningUnavailable

BACKEND_KMS = 'gcp_kms'
BACKEND_LOCAL = 'local'
BACKEND_NONE = 'none'


class SigningBackend:
    """Signs SHA-256 digests with a key it never hands out."""

    name = BACKEND_NONE

    @property
    def key_id(self) -> str:
        """Which key signed — kept on every signed original."""
        raise NotImplementedError

    def sign_digest(self, digest: bytes, algo: str = 'sha256') -> bytes:
        raise NotImplementedError

    def public_key_info(self) -> keys.PublicKeyInfo:
        """The key's public half, for building its certificate."""
        raise NotImplementedError

    def signature_algorithm(self) -> str:
        """The X.509/CMS name of what sign_digest produces."""
        algorithm = self.public_key_info().algorithm
        if algorithm == 'ec':
            return 'sha256_ecdsa'
        if algorithm == 'rsa':
            return 'sha256_rsa'
        raise SigningUnavailable(f'Unsupported signing key type: {algorithm}')

    def certificate(self):
        """The certificate the documents are signed under (asn1crypto), or None when none is configured."""
        from apps.documents.signing.certificate import load_certificate

        return load_certificate()


class LocalKeyBackend(SigningBackend):
    """
    A private key held in this process, from SIGNING_LOCAL_KEY_PEM.

    For tests and local development only. The whole point of the production
    key is that no one — the server included — can read it; a key in an
    environment variable is the opposite, so this backend refuses to load on
    Vercel whatever the settings say.
    """

    name = BACKEND_LOCAL

    def __init__(self, pem: str | bytes):
        if os.environ.get('VERCEL'):
            raise SigningUnavailable('A local signing key is never used on Vercel')
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        data = pem.encode() if isinstance(pem, str) else pem
        try:
            self._key = serialization.load_pem_private_key(data, password=None)
        except Exception as exc:
            raise SigningUnavailable('SIGNING_LOCAL_KEY_PEM is not a readable private key') from exc
        if not isinstance(self._key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
            raise SigningUnavailable('SIGNING_LOCAL_KEY_PEM must be an EC or RSA key')
        self._public_der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @property
    def key_id(self) -> str:
        return 'local:' + hashlib.sha256(self._public_der).hexdigest()[:16]

    def sign_digest(self, digest: bytes, algo: str = 'sha256') -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

        if algo != 'sha256' or len(digest) != 32:
            raise SigningUnavailable('Only SHA-256 digests are signed')
        prehashed = Prehashed(hashes.SHA256())
        if isinstance(self._key, ec.EllipticCurvePrivateKey):
            return self._key.sign(digest, ec.ECDSA(prehashed))
        return self._key.sign(digest, padding.PKCS1v15(), prehashed)

    def public_key_info(self) -> keys.PublicKeyInfo:
        return keys.PublicKeyInfo.load(self._public_der)


def backend_name() -> str:
    """Which backend the settings select, without building it (no network, no key)."""
    if (getattr(settings, 'SIGNING_KMS_KEY_VERSION', '') or '').strip():
        return BACKEND_KMS
    if (getattr(settings, 'SIGNING_LOCAL_KEY_PEM', '') or '').strip():
        return BACKEND_LOCAL
    return BACKEND_NONE


def configured_key_id() -> str:
    """The key the settings name, for the status screen — without touching the key."""
    name = backend_name()
    if name == BACKEND_KMS:
        return settings.SIGNING_KMS_KEY_VERSION.strip()
    if name == BACKEND_LOCAL:
        try:
            return get_backend().key_id
        except SigningUnavailable:
            return ''
    return ''


def get_backend() -> SigningBackend:
    """The configured backend. SigningUnavailable when none is configured or it cannot load."""
    name = backend_name()
    if name == BACKEND_KMS:
        from apps.documents.signing.kms import GcpKmsBackend

        return GcpKmsBackend(settings.SIGNING_KMS_KEY_VERSION.strip())
    if name == BACKEND_LOCAL:
        from apps.documents.signing.certificate import pem_text

        return LocalKeyBackend(pem_text(settings.SIGNING_LOCAL_KEY_PEM))
    raise SigningUnavailable('No signing key is configured')
