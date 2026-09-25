"""
Each device signs in with a key of its own (owner, 25.9.2026: "מפתח נפרד לכל מכשיר").

The old DRF Token is one per user and shared by every device, so signing out on
one device signed out all of them: on 24.9.2026 a phone had 23 attendance marks
refused mid-lesson because the same account had signed out elsewhere minutes
before. What these tests hold: signing out ends this device only; a new
password ends every device; a key is stored only as a hash; a deactivated user
is locked out; and the keys handed out before this change keep working.
"""
from datetime import timedelta

from django.contrib.auth.tokens import default_token_generator
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import LoginSession, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory

LOGIN = '/api/v1/core/auth/login/'
LOGOUT = '/api/v1/core/auth/logout/'
ME = '/api/v1/core/auth/me/'
PASSWORD = 'testpass123!'


class _Devices(TestCase):
    def setUp(self):
        self.user = TestDataFactory.create_user(username='instructor@x.com', role=UserProfile.ROLE_WORKER)

    def _sign_in(self, agent='phone'):
        res = APIClient().post(LOGIN, {'email': self.user.email, 'password': PASSWORD}, HTTP_USER_AGENT=agent)
        self.assertEqual(res.status_code, 200, res.content)
        return res.data['token']

    @staticmethod
    def _device(key):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {key}')
        return client


class OneKeyPerDeviceTests(_Devices):
    def test_every_sign_in_gets_a_key_of_its_own(self):
        phone, laptop = self._sign_in('phone'), self._sign_in('laptop')
        self.assertNotEqual(phone, laptop)
        self.assertTrue(phone.startswith(LoginSession.KEY_PREFIX))
        self.assertEqual(self._device(phone).get(ME).status_code, 200)
        self.assertEqual(self._device(laptop).get(ME).status_code, 200)

    def test_signing_out_ends_this_device_only(self):
        phone, laptop = self._sign_in('phone'), self._sign_in('laptop')
        self.assertEqual(self._device(laptop).post(LOGOUT).status_code, 200)
        self.assertEqual(self._device(laptop).get(ME).status_code, 401)
        self.assertEqual(self._device(phone).get(ME).status_code, 200)

    def test_the_key_is_kept_only_as_a_hash(self):
        key = self._sign_in()
        session = LoginSession.objects.get(user=self.user)
        self.assertNotEqual(session.key_hash, key)
        self.assertNotIn(key, session.key_hash)
        self.assertEqual(session.key_hash, LoginSession.hash_key(key))

    def test_the_device_is_remembered_by_its_browser(self):
        self._sign_in('Mozilla/5.0 (iPhone)')
        self.assertEqual(LoginSession.objects.get(user=self.user).user_agent, 'Mozilla/5.0 (iPhone)')

    def test_the_cookie_carries_the_same_key(self):
        key = self._sign_in()
        client = APIClient()
        client.cookies['auth_token'] = key
        self.assertEqual(client.get(ME).status_code, 200)

    def test_a_key_nobody_issued_is_refused(self):
        self.assertEqual(self._device(f'{LoginSession.KEY_PREFIX}made-up').get(ME).status_code, 401)

    def test_a_deactivated_user_is_locked_out_on_every_device(self):
        key = self._sign_in()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self._device(key).get(ME).status_code, 401)

    def test_last_use_is_recorded_without_a_write_on_every_request(self):
        key = self._sign_in()
        self._device(key).get(ME)
        first = LoginSession.objects.get(user=self.user).last_used_at
        self.assertIsNotNone(first)
        self._device(key).get(ME)
        self.assertEqual(LoginSession.objects.get(user=self.user).last_used_at, first)

        LoginSession.objects.filter(user=self.user).update(last_used_at=timezone.now() - timedelta(hours=1))
        self._device(key).get(ME)
        self.assertGreater(LoginSession.objects.get(user=self.user).last_used_at, first)


@override_settings(CRM_FRONTEND_URL='https://crm.test', RESEND_API_KEY='re_test', EMAIL_HOST='')
class NewPasswordEndsEveryDeviceTests(_Devices):
    def test_a_password_reset_signs_out_every_device(self):
        phone, laptop = self._sign_in('phone'), self._sign_in('laptop')
        legacy = Token.objects.create(user=self.user)
        res = APIClient().post('/api/v1/core/auth/reset-password/', {
            'uid': urlsafe_base64_encode(force_bytes(self.user.pk)),
            'token': default_token_generator.make_token(self.user),
            'password': 'NewSecurePass123!',
        })
        self.assertEqual(res.status_code, 200, res.content)
        for key in (phone, laptop, legacy.key):
            self.assertEqual(self._device(key).get(ME).status_code, 401)


class KeysFromBeforeTests(_Devices):
    """Devices signed in before this change hold the old shared key."""

    def test_an_old_shared_key_still_works(self):
        legacy = Token.objects.create(user=self.user)
        self.assertEqual(self._device(legacy.key).get(ME).status_code, 200)

    def test_signing_out_with_the_old_key_ends_the_old_key_and_leaves_new_devices_signed_in(self):
        legacy = Token.objects.create(user=self.user)
        phone = self._sign_in('phone')
        self.assertEqual(self._device(legacy.key).post(LOGOUT).status_code, 200)
        self.assertEqual(self._device(legacy.key).get(ME).status_code, 401)
        self.assertEqual(self._device(phone).get(ME).status_code, 200)


class StaffRecognisedOnPublicPagesTests(_Devices):
    def test_a_rental_link_opened_by_staff_with_a_device_key_is_not_the_tenant(self):
        from apps.rentals.public_views import _opened_by_staff

        key = self._sign_in()
        request = RequestFactory().get('/', HTTP_AUTHORIZATION=f'Token {key}')
        self.assertTrue(_opened_by_staff(request))
        self.assertFalse(_opened_by_staff(RequestFactory().get('/')))
