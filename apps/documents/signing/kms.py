"""
Google Cloud KMS as the signing backend — plain HTTPS through ``requests``.

No Google SDK: three small REST calls do everything, and the SDK would bring
grpc and a tree of packages into a serverless function for them.

Credentials, in this order:

1. Vercel OIDC federation. The token Vercel gives the invocation (the
   ``x-vercel-oidc-token`` header, kept by middleware.py; else the
   VERCEL_OIDC_TOKEN variable) is exchanged at Google STS for a federated
   token of the Workload Identity pool (SIGNING_GCP_WIF_AUDIENCE), and that
   for an access token of the signing service account
   (SIGNING_GCP_SERVICE_ACCOUNT) — whose one role is
   roles/cloudkms.signerVerifier on the key version. No secret is stored.
   With no service account named, the federated token is used as it is
   (direct resource access granted to the pool's principal).
2. The same service account's JSON key (SIGNING_GCP_SA_KEY_JSON), used for a
   self-signed JWT bearer grant — for when no OIDC token reaches the function.

Either way the private key never leaves the HSM: what crosses the network is a
32-byte digest one way and a signature the other.

Every failure — no credentials, a refused exchange, a timeout, a signature
whose checksum does not match — is SigningUnavailable. The caller holds the
document and the sign-pending cron tries again. Tokens are never logged.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests
from asn1crypto import keys, pem

from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import BACKEND_KMS, SigningBackend

logger = logging.getLogger(__name__)

STS_URL = 'https://sts.googleapis.com/v1/token'
IAM_CREDENTIALS_URL = (
    'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{account}:generateAccessToken'
)
OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
KMS_URL = 'https://cloudkms.googleapis.com/v1/{name}'
CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'

# Connect, read. A signature is a few hundred milliseconds; a request that
# hangs longer than this is better held and retried by the cron than waited on.
TIMEOUT = (3.05, 8)
# An access token is used until this close to its expiry, then fetched again.
REFRESH_MARGIN_SECONDS = 120

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


# ── CRC32C (Castagnoli) — what KMS checksums its answers with. zlib's crc32 is
# a different polynomial, and google-crc32c would be a dependency for 15 lines.

def _crc32c_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


# ── HTTP ────────────────────────────────────────────────────────────────────

def _google_error(response) -> str:
    """What Google said went wrong, in a form safe for a log line (never a token)."""
    try:
        body = response.json()
    except ValueError:
        return f'HTTP {response.status_code}'
    error = body.get('error') if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"HTTP {response.status_code} {error.get('status') or ''}".strip()
    if isinstance(error, str):
        # OAuth-style answers: {"error": "invalid_grant", "error_description": "..."}
        return f'HTTP {response.status_code} {error}'
    return f'HTTP {response.status_code}'


def _post(url: str, *, what: str, **kwargs) -> dict:
    try:
        response = requests.post(url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise SigningUnavailable(f'{what}: {type(exc).__name__}') from exc
    if response.status_code != 200:
        detail = _google_error(response)
        logger.warning('Signing: %s refused (%s)', what, detail)
        raise SigningUnavailable(f'{what} refused ({detail})')
    try:
        return response.json()
    except ValueError as exc:
        raise SigningUnavailable(f'{what}: the answer was not JSON') from exc


def _get(url: str, *, what: str, headers: dict) -> dict:
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=headers)
    except requests.RequestException as exc:
        raise SigningUnavailable(f'{what}: {type(exc).__name__}') from exc
    if response.status_code != 200:
        detail = _google_error(response)
        logger.warning('Signing: %s refused (%s)', what, detail)
        raise SigningUnavailable(f'{what} refused ({detail})')
    try:
        return response.json()
    except ValueError as exc:
        raise SigningUnavailable(f'{what}: the answer was not JSON') from exc


# ── credentials ─────────────────────────────────────────────────────────────

def _oidc_subject_token() -> str:
    from apps.documents.signing.middleware import current_oidc_token

    return (current_oidc_token() or os.environ.get('VERCEL_OIDC_TOKEN') or '').strip()


def _expiry_from_rfc3339(stamp: str, fallback_seconds: int = 3000) -> float:
    try:
        moment = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
        return moment.timestamp()
    except (TypeError, ValueError):
        return time.time() + fallback_seconds


def _token_via_oidc(subject_token: str) -> tuple[str, float]:
    from django.conf import settings

    audience = (getattr(settings, 'SIGNING_GCP_WIF_AUDIENCE', '') or '').strip()
    if not audience:
        raise SigningUnavailable('SIGNING_GCP_WIF_AUDIENCE is not set')
    exchanged = _post(
        STS_URL,
        what='STS token exchange',
        data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'audience': audience,
            'scope': CLOUD_PLATFORM_SCOPE,
            'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt',
            'subject_token': subject_token,
        },
    )
    federated = exchanged.get('access_token')
    if not federated:
        raise SigningUnavailable('STS token exchange: no access token in the answer')
    federated_expiry = time.time() + int(exchanged.get('expires_in') or 3600)

    account = (getattr(settings, 'SIGNING_GCP_SERVICE_ACCOUNT', '') or '').strip()
    if not account:
        return federated, federated_expiry
    issued = _post(
        IAM_CREDENTIALS_URL.format(account=account),
        what='service account impersonation',
        headers={'Authorization': f'Bearer {federated}'},
        json={'scope': [CLOUD_PLATFORM_SCOPE], 'lifetime': '3600s'},
    )
    token = issued.get('accessToken')
    if not token:
        raise SigningUnavailable('service account impersonation: no access token in the answer')
    return token, _expiry_from_rfc3339(issued.get('expireTime'))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _token_via_service_account_json(raw: str) -> tuple[str, float]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        info = json.loads(raw)
        email = info['client_email']
        private_key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
    except Exception:
        # The message would quote the secret's content; say only that it is unreadable.
        raise SigningUnavailable('SIGNING_GCP_SA_KEY_JSON is not a readable service account key') from None
    token_uri = info.get('token_uri') or OAUTH_TOKEN_URL
    now = int(time.time())
    header = {'alg': 'RS256', 'typ': 'JWT'}
    if info.get('private_key_id'):
        header['kid'] = info['private_key_id']
    claims = {'iss': email, 'scope': CLOUD_PLATFORM_SCOPE, 'aud': token_uri, 'iat': now, 'exp': now + 3600}
    signing_input = (
        _b64url(json.dumps(header, separators=(',', ':')).encode())
        + '.'
        + _b64url(json.dumps(claims, separators=(',', ':')).encode())
    )
    try:
        signature = private_key.sign(signing_input.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise SigningUnavailable('SIGNING_GCP_SA_KEY_JSON could not sign its assertion') from None
    granted = _post(
        token_uri,
        what='service account token grant',
        data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': f'{signing_input}.{_b64url(signature)}',
        },
    )
    token = granted.get('access_token')
    if not token:
        raise SigningUnavailable('service account token grant: no access token in the answer')
    return token, time.time() + int(granted.get('expires_in') or 3600)


def access_token() -> str:
    """A Google access token for the signing service account, from the cache while it is fresh."""
    from django.conf import settings

    subject = _oidc_subject_token()
    raw_json = (getattr(settings, 'SIGNING_GCP_SA_KEY_JSON', '') or '').strip()
    if subject:
        mode = 'oidc'
    elif raw_json:
        mode = 'json'
    else:
        raise SigningUnavailable('No Google credentials: no Vercel OIDC token and no SIGNING_GCP_SA_KEY_JSON')

    with _token_lock:
        cached = _token_cache.get(mode)
        if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
    token, expires_at = _token_via_oidc(subject) if mode == 'oidc' else _token_via_service_account_json(raw_json)
    with _token_lock:
        _token_cache[mode] = (token, expires_at)
    return token


# ── the backend ─────────────────────────────────────────────────────────────

class GcpKmsBackend(SigningBackend):
    """A Cloud KMS asymmetric-sign key version (an HSM key in production)."""

    name = BACKEND_KMS

    def __init__(self, key_version: str):
        key_version = (key_version or '').strip().strip('/')
        if '/cryptoKeyVersions/' not in key_version:
            raise SigningUnavailable('SIGNING_KMS_KEY_VERSION must name a key version (…/cryptoKeyVersions/N)')
        self._name = key_version
        self._public_key: keys.PublicKeyInfo | None = None

    @property
    def key_id(self) -> str:
        return self._name

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {access_token()}'}

    def sign_digest(self, digest: bytes, algo: str = 'sha256') -> bytes:
        if algo != 'sha256' or len(digest) != 32:
            raise SigningUnavailable('Only SHA-256 digests are signed')
        answer = _post(
            KMS_URL.format(name=self._name) + ':asymmetricSign',
            what='KMS asymmetricSign',
            headers=self._headers(),
            json={
                'digest': {'sha256': base64.b64encode(digest).decode('ascii')},
                # KMS checks the digest arrived as sent and says so.
                'digestCrc32c': str(crc32c(digest)),
            },
        )
        try:
            signature = base64.b64decode(answer['signature'])
        except (KeyError, TypeError, ValueError) as exc:
            raise SigningUnavailable('KMS asymmetricSign: no signature in the answer') from exc
        if answer.get('verifiedDigestCrc32c') is False:
            raise SigningUnavailable('KMS asymmetricSign: the digest was corrupted on the way')
        if 'signatureCrc32c' in answer and int(answer['signatureCrc32c']) != crc32c(signature):
            raise SigningUnavailable('KMS asymmetricSign: the signature was corrupted on the way')
        if answer.get('name') and answer['name'] != self._name:
            raise SigningUnavailable('KMS asymmetricSign: answered for another key version')
        return signature

    def public_key_info(self) -> keys.PublicKeyInfo:
        if self._public_key is None:
            answer = _get(KMS_URL.format(name=self._name) + '/publicKey', what='KMS getPublicKey',
                          headers=self._headers())
            text = (answer.get('pem') or '').encode()
            if 'pemCrc32c' in answer and int(answer['pemCrc32c']) != crc32c(text):
                raise SigningUnavailable('KMS getPublicKey: the key was corrupted on the way')
            try:
                _type, _headers, der = pem.unarmor(text)
                self._public_key = keys.PublicKeyInfo.load(der)
            except Exception as exc:
                raise SigningUnavailable('KMS getPublicKey: not a public key') from exc
        return self._public_key
