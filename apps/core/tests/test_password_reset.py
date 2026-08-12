from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.password_reset_email import build_password_reset_link, send_password_reset_email
from apps.core.tests.test_fixtures import TestDataFactory

User = get_user_model()


@override_settings(CRM_FRONTEND_URL='https://crm.test', RESEND_API_KEY='re_test', EMAIL_HOST='')
class PasswordResetEmailTests(TestCase):
    def setUp(self):
        self.user = TestDataFactory.create_user(username='worker@example.com', role=UserProfile.ROLE_WORKER)

    def test_build_link_contains_uid_and_token(self):
        link = build_password_reset_link(self.user)
        self.assertTrue(link.startswith('https://crm.test/reset-password?'))
        self.assertIn('uid=', link)
        self.assertIn('token=', link)

    @patch('apps.core.password_reset_email.send_resend_email')
    def test_send_via_resend(self, mock_resend):
        ok = send_password_reset_email(self.user)
        self.assertTrue(ok)
        mock_resend.assert_called_once()
        self.assertEqual(mock_resend.call_args.kwargs['to'], ['worker@example.com'])
        self.assertIn('איפוס סיסמה', mock_resend.call_args.kwargs['subject'])


@override_settings(CRM_FRONTEND_URL='https://crm.test', RESEND_API_KEY='re_test', EMAIL_HOST='')
class PasswordResetApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = TestDataFactory.create_user(username='manager@example.com', role=UserProfile.ROLE_MANAGER)

    @patch('apps.core.password_reset_email.send_password_reset_email')
    def test_forgot_password_sends_email(self, mock_send):
        res = self.client.post('/api/v1/core/auth/forgot-password/', {'email': 'manager@example.com'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['ok'])
        mock_send.assert_called_once()

    @patch('apps.core.password_reset_email.send_password_reset_email')
    def test_forgot_password_unknown_email_still_ok(self, mock_send):
        res = self.client.post('/api/v1/core/auth/forgot-password/', {'email': 'missing@example.com'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['ok'])
        mock_send.assert_not_called()

    def test_reset_password_success(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        res = self.client.post('/api/v1/core/auth/reset-password/', {
            'uid': uid,
            'token': token,
            'password': 'NewSecurePass123!',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['ok'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewSecurePass123!'))

    def test_reset_password_invalid_token(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        res = self.client.post('/api/v1/core/auth/reset-password/', {
            'uid': uid,
            'token': 'bad-token',
            'password': 'NewSecurePass123!',
        })
        self.assertEqual(res.status_code, 400)
