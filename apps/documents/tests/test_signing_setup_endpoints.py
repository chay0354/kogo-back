"""The two setup calls: issue the certificate and run the self-test against the deployment's own key."""
from __future__ import annotations

import base64

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.documents.tests.signing_support import local_cert_pem, signing_settings, signing_on
from django.test import override_settings

SELFTEST = '/api/v1/documents/signing/selftest/'
ISSUE = '/api/v1/documents/signing/certificate/issue/'


def make_user(username, role):
    user = get_user_model().objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return get_user_model().objects.get(pk=user.pk)


@signing_on(SIGNING_ADMIN_TOKEN='setup-token-for-tests')
class SetupEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_the_self_test_signs_a_sample_that_validates(self):
        response = self.client.post(SELFTEST, HTTP_X_SIGNING_ADMIN_TOKEN='setup-token-for-tests')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['backend'], 'local')
        self.assertTrue(base64.b64decode(body['pdf_base64']).startswith(b'%PDF'))

    def test_a_manager_may_run_it_without_the_token(self):
        self.client.force_authenticate(make_user('manager-setup@test', UserProfile.ROLE_MANAGER))
        self.assertEqual(self.client.post(SELFTEST).status_code, 200)

    def test_anyone_else_is_refused(self):
        self.assertEqual(self.client.post(SELFTEST).status_code, 403)
        self.assertEqual(self.client.post(SELFTEST, HTTP_X_SIGNING_ADMIN_TOKEN='wrong').status_code, 403)
        self.client.force_authenticate(make_user('worker-setup@test', UserProfile.ROLE_WORKER))
        self.assertEqual(self.client.post(ISSUE).status_code, 403)

    def test_the_certificate_is_built_for_the_key_and_not_stored(self):
        response = self.client.post(ISSUE, HTTP_X_SIGNING_ADMIN_TOKEN='setup-token-for-tests')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn('BEGIN CERTIFICATE', body['pem'])
        self.assertIn('516504412', body['subject'])
        self.assertNotEqual(body['pem'], local_cert_pem())  # a fresh one, the configured one untouched

    def test_without_a_certificate_the_self_test_says_what_is_missing(self):
        with override_settings(**signing_settings(SIGNING_CERT_PEM='', SIGNING_ADMIN_TOKEN='setup-token-for-tests')):
            response = self.client.post(SELFTEST, HTTP_X_SIGNING_ADMIN_TOKEN='setup-token-for-tests')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])


class NoTokenConfiguredTests(TestCase):
    def test_an_empty_token_setting_opens_nothing(self):
        with override_settings(**signing_settings(SIGNING_ADMIN_TOKEN='')):
            response = APIClient().post(SELFTEST, HTTP_X_SIGNING_ADMIN_TOKEN='')
        self.assertEqual(response.status_code, 403)
