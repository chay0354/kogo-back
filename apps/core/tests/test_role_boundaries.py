"""
An instructor account must not reach office data, and neither must an account
whose profile was never created.

The second case is the one that bit us: every guard asked "is this a worker?"
and let everything else through, so a user with no profile — created by a
script, or by hand — was treated as staff.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import UserProfile

User = get_user_model()

# Office endpoints an instructor has no business reading.
OFFICE_ENDPOINTS = [
    '/api/v1/core/users/',
    '/api/v1/customers/children/',
    '/api/v1/customers/families/',
    '/api/v1/customers/payments/',
    '/api/v1/courses/courses/',
    '/api/v1/store/products/',
    '/api/v1/store/invoices/',
    '/api/v1/store/sales/',
]


class InstructorCannotReachOfficeDataTests(APITestCase):
    def setUp(self):
        self.worker = User.objects.create_user(username='worker@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=self.worker)
        profile.role = UserProfile.ROLE_WORKER
        profile.save(update_fields=['role'])

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_every_office_endpoint_is_refused(self):
        self.auth(self.worker)
        for url in OFFICE_ENDPOINTS:
            res = self.client.get(url)
            self.assertIn(
                res.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                f'{url} answered {res.status_code} to an instructor',
            )

    def test_charging_endpoints_are_refused(self):
        """These take money. An instructor must not be able to reach them."""
        self.auth(self.worker)
        for url in ('/api/v1/store/payment/initiate/', '/api/v1/store/payment/charge-card/'):
            res = self.client.post(url, {}, format='json')
            self.assertIn(
                res.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                f'{url} answered {res.status_code} to an instructor',
            )


class ProfilelessAccountIsNotStaffTests(APITestCase):
    """A user with no UserProfile row is not staff by omission."""

    def setUp(self):
        self.orphan = User.objects.create_user(username='orphan@test', password='pw-for-tests')
        UserProfile.objects.filter(user=self.orphan).delete()

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_has_no_profile(self):
        self.assertFalse(UserProfile.objects.filter(user=self.orphan).exists())

    def test_every_office_endpoint_is_refused(self):
        self.auth(self.orphan)
        for url in OFFICE_ENDPOINTS:
            res = self.client.get(url)
            self.assertIn(
                res.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                f'{url} answered {res.status_code} to an account with no profile',
            )

    def test_me_reports_no_role(self):
        """The client keys its own guard off this, so it must not invent one."""
        self.auth(self.orphan)
        res = self.client.get('/api/v1/core/auth/me/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data['user']['role'])
