"""
Cloud KMS as the signing backend, with the HTTP layer mocked — no request leaves the test.

The two ways to Google credentials (Vercel OIDC → STS → service account, and
the service account's JSON key), the digest-only sign call and its CRC32C
checks, and that every failure is SigningUnavailable with no token in it.
"""
import base64
import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import requests
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import BACKEND_KMS, backend_name, get_backend
from apps.documents.signing.certificate import build_self_issued_certificate, certificate_pem
from apps.documents.signing.kms import (
    IAM_CREDENTIALS_URL, OAUTH_TOKEN_URL, STS_URL, GcpKmsBackend, clear_token_cache, crc32c,
)
from apps.documents.signing.middleware import VercelOidcTokenMiddleware, current_oidc_token
from apps.documents.signing.signer import check_signed_pdf, sign_pdf
from apps.documents.tests.signing_support import local_key_pem

KEY_VERSION = 'projects/kogo-signing/locations/me-west1/keyRings/documents/cryptoKeys/fiscal/cryptoKeyVersions/1'
AUDIENCE = '//iam.googleapis.com/projects/123456/locations/global/workloadIdentityPools/vercel/providers/vercel'
ACCOUNT = 'document-signer@kogo-signing.iam.gserviceaccount.com'


def answer(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    return response


def the_key():
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(local_key_pem().encode(), password=None)


def hsm_signature(digest: bytes) -> bytes:
    """What the HSM would return for the digest: a DER ECDSA signature by the key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    return the_key().sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))


def public_pem() -> str:
    from cryptography.hazmat.primitives import serialization

    return the_key().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('ascii')


class FakeGoogle:
    """Answers STS, IAM credentials, OAuth and KMS the way Google does; keeps every call."""

    def __init__(self, *, signature_crc=None, verified_digest=True, kms_status=200):
        self.calls = []
        self.signature_crc = signature_crc
        self.verified_digest = verified_digest
        self.kms_status = kms_status

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == STS_URL:
            return answer({'access_token': 'federated-token', 'issued_token_type': 'x', 'expires_in': 3600})
        if url == IAM_CREDENTIALS_URL.format(account=ACCOUNT):
            return answer({'accessToken': 'service-account-token', 'expireTime': '2099-01-01T00:00:00Z'})
        if url == OAUTH_TOKEN_URL:
            return answer({'access_token': 'json-key-token', 'expires_in': 3600, 'token_type': 'Bearer'})
        if url.endswith(':asymmetricSign'):
            if self.kms_status != 200:
                return answer({'error': {'code': self.kms_status, 'status': 'PERMISSION_DENIED',
                                         'message': 'Permission denied on resource'}}, self.kms_status)
            digest = base64.b64decode(kwargs['json']['digest']['sha256'])
            signature = hsm_signature(digest)
            return answer({
                'signature': base64.b64encode(signature).decode(),
                'signatureCrc32c': str(self.signature_crc if self.signature_crc is not None else crc32c(signature)),
                'verifiedDigestCrc32c': self.verified_digest,
                'name': KEY_VERSION,
                'protectionLevel': 'HSM',
            })
        raise AssertionError(f'unexpected POST {url}')

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/publicKey'):
            text = public_pem()
            return answer({'pem': text, 'pemCrc32c': str(crc32c(text.encode())), 'algorithm': 'EC_SIGN_P256_SHA256'})
        raise AssertionError(f'unexpected GET {url}')

    def urls(self):
        return [url for url, _kwargs in self.calls]

    def call(self, url):
        return next(kwargs for called, kwargs in self.calls if called == url)


class KmsTestCase(SimpleTestCase):
    def setUp(self):
        clear_token_cache()
        self.addCleanup(clear_token_cache)
        # No OIDC token from the environment unless a test puts one there.
        environ = patch.dict(os.environ, {}, clear=False)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('VERCEL_OIDC_TOKEN', None)

    def google(self, **kwargs):
        fake = FakeGoogle(**kwargs)
        post = patch('apps.documents.signing.kms.requests.post', side_effect=fake.post)
        get = patch('apps.documents.signing.kms.requests.get', side_effect=fake.get)
        post.start()
        get.start()
        self.addCleanup(post.stop)
        self.addCleanup(get.stop)
        return fake

    def verify(self, signature: bytes, digest: bytes):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

        the_key().public_key().verify(signature, digest, ec.ECDSA(Prehashed(hashes.SHA256())))


@override_settings(SIGNING_GCP_WIF_AUDIENCE=AUDIENCE, SIGNING_GCP_SERVICE_ACCOUNT=ACCOUNT, SIGNING_GCP_SA_KEY_JSON='')
class OidcFederationTests(KmsTestCase):
    def test_the_vercel_token_is_exchanged_and_only_the_digest_is_signed(self):
        google = self.google()
        digest = hashlib.sha256(b'the signed attributes').digest()
        os.environ['VERCEL_OIDC_TOKEN'] = 'vercel-oidc-jwt'

        signature = GcpKmsBackend(KEY_VERSION).sign_digest(digest)

        self.verify(signature, digest)
        self.assertEqual(google.urls(), [
            STS_URL, IAM_CREDENTIALS_URL.format(account=ACCOUNT),
            f'https://cloudkms.googleapis.com/v1/{KEY_VERSION}:asymmetricSign',
        ])
        exchange = google.call(STS_URL)
        self.assertEqual(exchange['data'], {
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'audience': AUDIENCE,
            'scope': 'https://www.googleapis.com/auth/cloud-platform',
            'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt',
            'subject_token': 'vercel-oidc-jwt',
        })
        impersonate = google.call(IAM_CREDENTIALS_URL.format(account=ACCOUNT))
        self.assertEqual(impersonate['headers'], {'Authorization': 'Bearer federated-token'})
        self.assertEqual(impersonate['json'], {'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                                               'lifetime': '3600s'})
        sign = google.call(f'https://cloudkms.googleapis.com/v1/{KEY_VERSION}:asymmetricSign')
        self.assertEqual(sign['headers'], {'Authorization': 'Bearer service-account-token'})
        self.assertEqual(sign['json'], {
            'digest': {'sha256': base64.b64encode(digest).decode()},
            'digestCrc32c': str(crc32c(digest)),
        })
        for _url, kwargs in google.calls:
            self.assertLessEqual(max(kwargs['timeout']), 10)  # short timeouts, always

    def test_the_access_token_is_reused_until_it_nears_expiry(self):
        google = self.google()
        os.environ['VERCEL_OIDC_TOKEN'] = 'vercel-oidc-jwt'
        backend = GcpKmsBackend(KEY_VERSION)
        backend.sign_digest(hashlib.sha256(b'one').digest())
        backend.sign_digest(hashlib.sha256(b'two').digest())
        self.assertEqual(google.urls().count(STS_URL), 1)
        self.assertEqual(sum(url.endswith(':asymmetricSign') for url in google.urls()), 2)

    def test_the_request_header_is_the_token_first(self):
        google = self.google()
        os.environ['VERCEL_OIDC_TOKEN'] = 'build-time-token'
        seen = {}

        def view(request):
            seen['token'] = current_oidc_token()
            GcpKmsBackend(KEY_VERSION).sign_digest(hashlib.sha256(b'x').digest())
            return 'response'

        request = RequestFactory().get('/api/v1/documents/cron/sign-pending/', HTTP_X_VERCEL_OIDC_TOKEN='request-token')
        self.assertEqual(VercelOidcTokenMiddleware(view)(request), 'response')
        self.assertEqual(seen['token'], 'request-token')
        self.assertEqual(google.call(STS_URL)['data']['subject_token'], 'request-token')
        self.assertIsNone(current_oidc_token())  # cleared with the request

    def test_the_middleware_does_nothing_without_the_header(self):
        seen = {}

        def view(request):
            seen['token'] = current_oidc_token()
            return 'response'

        self.assertEqual(VercelOidcTokenMiddleware(view)(RequestFactory().get('/')), 'response')
        self.assertIsNone(seen['token'])

    @override_settings(SIGNING_GCP_SERVICE_ACCOUNT='')
    def test_without_a_service_account_the_federated_token_signs_directly(self):
        google = self.google()
        os.environ['VERCEL_OIDC_TOKEN'] = 'vercel-oidc-jwt'
        GcpKmsBackend(KEY_VERSION).sign_digest(hashlib.sha256(b'x').digest())
        self.assertNotIn(IAM_CREDENTIALS_URL.format(account=ACCOUNT), google.urls())
        sign = google.call(f'https://cloudkms.googleapis.com/v1/{KEY_VERSION}:asymmetricSign')
        self.assertEqual(sign['headers'], {'Authorization': 'Bearer federated-token'})


class ServiceAccountJsonTests(KmsTestCase):
    def service_account(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        info = {
            'type': 'service_account', 'project_id': 'kogo-signing', 'private_key_id': 'key-1',
            'private_key': key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ).decode(),
            'client_email': ACCOUNT, 'token_uri': OAUTH_TOKEN_URL,
        }
        return key, json.dumps(info)

    def test_the_json_key_signs_a_jwt_bearer_grant(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        key, raw = self.service_account()
        google = self.google()
        digest = hashlib.sha256(b'x').digest()
        with override_settings(SIGNING_GCP_SA_KEY_JSON=raw, SIGNING_GCP_WIF_AUDIENCE=''):
            signature = GcpKmsBackend(KEY_VERSION).sign_digest(digest)
        self.verify(signature, digest)
        self.assertNotIn(STS_URL, google.urls())

        grant = google.call(OAUTH_TOKEN_URL)['data']
        self.assertEqual(grant['grant_type'], 'urn:ietf:params:oauth:grant-type:jwt-bearer')
        header_b64, claims_b64, signature_b64 = grant['assertion'].split('.')

        def decode(part):
            return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))

        key.public_key().verify(decode(signature_b64), f'{header_b64}.{claims_b64}'.encode(),
                                padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(json.loads(decode(header_b64)), {'alg': 'RS256', 'typ': 'JWT', 'kid': 'key-1'})
        claims = json.loads(decode(claims_b64))
        self.assertEqual((claims['iss'], claims['aud'], claims['scope']),
                         (ACCOUNT, OAUTH_TOKEN_URL, 'https://www.googleapis.com/auth/cloud-platform'))
        self.assertEqual(claims['exp'] - claims['iat'], 3600)
        sign = google.call(f'https://cloudkms.googleapis.com/v1/{KEY_VERSION}:asymmetricSign')
        self.assertEqual(sign['headers'], {'Authorization': 'Bearer json-key-token'})

    def test_an_unreadable_json_key_says_so_without_quoting_it(self):
        self.google()
        with override_settings(SIGNING_GCP_SA_KEY_JSON='{"client_email": "x", "private_key": "SECRET-MATERIAL"}'):
            with self.assertRaises(SigningUnavailable) as caught:
                GcpKmsBackend(KEY_VERSION).sign_digest(hashlib.sha256(b'x').digest())
        self.assertNotIn('SECRET-MATERIAL', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


@override_settings(SIGNING_GCP_WIF_AUDIENCE=AUDIENCE, SIGNING_GCP_SERVICE_ACCOUNT=ACCOUNT, SIGNING_GCP_SA_KEY_JSON='')
class KmsFailuresTests(KmsTestCase):
    def setUp(self):
        super().setUp()
        os.environ['VERCEL_OIDC_TOKEN'] = 'vercel-oidc-jwt'
        self.digest = hashlib.sha256(b'x').digest()

    def test_a_signature_whose_checksum_does_not_match_is_refused(self):
        self.google(signature_crc=12345)
        with self.assertRaisesMessage(SigningUnavailable, 'corrupted'):
            GcpKmsBackend(KEY_VERSION).sign_digest(self.digest)

    def test_a_digest_kms_did_not_receive_intact_is_refused(self):
        self.google(verified_digest=False)
        with self.assertRaisesMessage(SigningUnavailable, 'digest was corrupted'):
            GcpKmsBackend(KEY_VERSION).sign_digest(self.digest)

    def test_a_refusal_is_unavailable_and_neither_it_nor_the_log_carries_a_token(self):
        self.google(kms_status=403)
        with self.assertLogs('apps.documents.signing.kms', 'WARNING') as logs, \
                self.assertRaises(SigningUnavailable) as caught:
            GcpKmsBackend(KEY_VERSION).sign_digest(self.digest)
        self.assertIn('HTTP 403 PERMISSION_DENIED', str(caught.exception))
        for text in [str(caught.exception), *logs.output]:
            for secret in ('service-account-token', 'federated-token', 'vercel-oidc-jwt'):
                self.assertNotIn(secret, text)

    def test_a_timeout_is_unavailable(self):
        with patch('apps.documents.signing.kms.requests.post', side_effect=requests.ReadTimeout('slow')):
            with self.assertRaisesMessage(SigningUnavailable, 'ReadTimeout'):
                GcpKmsBackend(KEY_VERSION).sign_digest(self.digest)

    def test_no_credentials_at_all(self):
        os.environ.pop('VERCEL_OIDC_TOKEN', None)
        with patch('apps.documents.signing.kms.requests.post', side_effect=AssertionError('no request')):
            with self.assertRaisesMessage(SigningUnavailable, 'No Google credentials'):
                GcpKmsBackend(KEY_VERSION).sign_digest(self.digest)

    def test_a_key_that_is_not_a_key_version(self):
        with self.assertRaises(SigningUnavailable):
            GcpKmsBackend('projects/p/locations/l/keyRings/r/cryptoKeys/k')

    def test_only_sha256_digests(self):
        with self.assertRaises(SigningUnavailable):
            GcpKmsBackend(KEY_VERSION).sign_digest(b'short')

    def test_crc32c_is_castagnoli(self):
        self.assertEqual(crc32c(b'123456789'), 0xE3069283)


@override_settings(
    SIGNING_KMS_KEY_VERSION=KEY_VERSION, SIGNING_LOCAL_KEY_PEM='', SIGNING_GCP_WIF_AUDIENCE=AUDIENCE,
    SIGNING_GCP_SERVICE_ACCOUNT=ACCOUNT, SIGNING_GCP_SA_KEY_JSON='',
)
class KmsEndToEndTests(KmsTestCase):
    def test_a_certificate_built_through_kms_and_a_pdf_signed_through_it_validate(self):
        from apps.documents.signing.selftest import sample_pdf

        self.google()
        os.environ['VERCEL_OIDC_TOKEN'] = 'vercel-oidc-jwt'
        self.assertEqual(backend_name(), BACKEND_KMS)
        backend = get_backend()
        self.assertIsInstance(backend, GcpKmsBackend)
        self.assertEqual(backend.key_id, KEY_VERSION)

        certificate = build_self_issued_certificate(backend)
        with override_settings(SIGNING_CERT_PEM=certificate_pem(certificate)):
            signed = sign_pdf(sample_pdf(), backend=backend)
        check_signed_pdf(signed, certificate)


@override_settings(
    DOCUMENT_SIGNING_ENABLED=True, SIGNING_KMS_KEY_VERSION=KEY_VERSION, SIGNING_LOCAL_KEY_PEM='',
)
class StatusMakesNoCallsTests(TestCase):
    def test_the_status_panel_reads_the_settings_only(self):
        from apps.core.models import UserProfile
        from apps.documents.tests.test_register import make_user
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(make_user('manager-kms@test', UserProfile.ROLE_MANAGER))
        with patch('apps.documents.signing.kms.requests.post', side_effect=AssertionError('network')), \
                patch('apps.documents.signing.kms.requests.get', side_effect=AssertionError('network')):
            body = client.get('/api/v1/documents/signing/status/').json()
        self.assertEqual((body['backend'], body['key_id']), ('gcp_kms', KEY_VERSION))
