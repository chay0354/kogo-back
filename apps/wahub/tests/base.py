"""Shared set-up for the wahub tests: a manager, a few builders, and no way out to the network."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.wahub import inbound
from apps.wahub.models import Contact

User = get_user_model()
BASE = '/api/v1/wahub'
PHONE = '972505550101'


def make_user(email, role=UserProfile.ROLE_MANAGER, **names):
    user = User.objects.create_user(username=email, email=email, password='pass12345!', is_active=True, **names)
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    return user


def client_for(user) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.get_or_create(user=user)[0].key}')
    return client


class WahubTestCase(TestCase):
    """
    Every test here runs with the network cut: a request that leaves the
    process fails the test. What a test needs from ManyChat or Claude it mocks.
    """

    def setUp(self):
        super().setUp()
        cache.clear()  # the inbound throttle counts in the cache
        blocker = patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('a wahub test tried to reach the network'),
        )
        self.network = blocker.start()
        self.addCleanup(blocker.stop)
        self.manager = make_user('manager@wahub.test', first_name='דנה', last_name='מנהלת')
        self.client = client_for(self.manager)

    # --- builders ---

    def incoming(self, text='שלום', phone=PHONE, **extra):
        return inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=phone, text=text, **extra)

    def bot_reply(self, text='היי, איך אפשר לעזור?', phone=PHONE, **extra):
        return inbound.store_event(event=inbound.EVENT_BOT_REPLY, phone=phone, text=text, **extra)

    def contact(self, phone=PHONE) -> Contact:
        return Contact.objects.get(phone=phone)

    def make_contact(self, phone=PHONE, **fields) -> Contact:
        return Contact.objects.create(phone=phone, **fields)

    def age(self, contact, **minutes_by_field):
        """Move timestamps of a contact back, e.g. age(c, last_inbound_at=60*25)."""
        now = timezone.now()
        Contact.objects.filter(pk=contact.pk).update(
            **{name: now - timedelta(minutes=minutes) for name, minutes in minutes_by_field.items()}
        )
        contact.refresh_from_db()
        return contact

    def get(self, path, **params):
        return self.client.get(f'{BASE}/{path}', params)

    def post(self, path, data=None):
        return self.client.post(f'{BASE}/{path}', data or {}, format='json')
