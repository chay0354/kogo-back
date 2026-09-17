"""
A parent ManyChat has but will not find.

A broadcast came back with rows reading "This WhatsApp ID already exists": the
parent had written to the business on WhatsApp, so ManyChat knew the number,
but its API has no search by WhatsApp number and none of the lookups it does
offer reached the contact. Creating it again was refused, and every message to
that parent failed while the screen told the office to check the number was on
WhatsApp — which it plainly was.

These pin the way out: a contact is remembered once known, one ManyChat cannot
find can be linked by hand, and a link is always checked against the phone
before a message follows it.
"""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.manychat_service import (
    CONTACT_UNFINDABLE_MESSAGE,
    ContactLinkError,
    ManyChatContactUnfindable,
    ManyChatError,
    ManyChatService,
    manychat_error_detail,
    parse_contact_ref,
)
from apps.core.models import ManyChatContact, UserProfile

PHONE = '0548185646'
E164 = '972548185646'
ALREADY_EXISTS = ManyChatError(
    'Validation error',
    status_code=400,
    payload={'details': {'messages': {'wa_id': {'message': [f'This WhatsApp ID already exists: {E164}']}}}},
)


class NoNetworkMixin:
    """Nothing in these tests may reach ManyChat."""

    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.manychat_service.requests.request', side_effect=AssertionError('network call'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        svc = ManyChatService(api_key='test-key')
        svc._ensure_phone_indexed = MagicMock()
        return svc


class UnfindableContactTests(NoNetworkMixin, TestCase):
    def test_already_exists_and_not_found_says_what_is_actually_wrong(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ALREADY_EXISTS)
        with self.assertRaises(ManyChatContactUnfindable) as caught:
            svc.lookup_or_create(PHONE, 'הורה')
        text = manychat_error_detail(caught.exception)
        self.assertEqual(text, CONTACT_UNFINDABLE_MESSAGE)
        # The old advice was wrong: the number is on WhatsApp.
        self.assertNotIn('ודא שהמספר רשום', text)
        self.assertIn('קישור לאיש קשר', text)

    def test_it_is_still_a_manychat_error_for_every_other_caller(self):
        self.assertTrue(issubclass(ManyChatContactUnfindable, ManyChatError))

    def test_a_number_whatsapp_rejects_keeps_its_own_message(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ManyChatError(
            'Validation error', payload={'details': {'messages': {'whatsapp_phone': ['is not a valid whatsapp id']}}},
        ))
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        with self.assertRaises(ManyChatError) as caught:
            svc.lookup_or_create(PHONE, 'הורה')
        # It used to be caught by the "already exists" branch, whose "whatsapp
        # id" test it also matches, and never got this message.
        self.assertNotIsInstance(caught.exception, ManyChatContactUnfindable)
        self.assertIn('אינו רשום ב-WhatsApp', str(caught.exception))
        self.assertEqual(svc._resolve_subscriber.call_count, 1)


class RememberedContactTests(NoNetworkMixin, TestCase):
    def test_a_found_contact_is_remembered(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value={'id': 111, 'whatsapp_phone': E164})
        svc.lookup_or_create(PHONE, 'הורה')
        row = ManyChatContact.objects.get(phone=E164)
        self.assertEqual((row.subscriber_id, row.source), (111, ManyChatContact.SOURCE_FOUND))

    def test_a_created_contact_is_remembered(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc.create_whatsapp_subscriber = MagicMock(return_value={'id': 222})
        svc.lookup_or_create(PHONE, 'הורה')
        self.assertEqual(ManyChatContact.objects.get(phone=E164).source, ManyChatContact.SOURCE_CREATED)

    def test_a_remembered_contact_is_used_without_searching(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=333, source=ManyChatContact.SOURCE_MANUAL)
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 333, 'whatsapp_phone': E164})
        svc._resolve_subscriber = MagicMock()
        svc.create_whatsapp_subscriber = MagicMock()
        out = svc.lookup_or_create(PHONE, 'הורה')
        self.assertEqual(out['subscriber_id'], 333)
        svc._resolve_subscriber.assert_not_called()
        svc.create_whatsapp_subscriber.assert_not_called()

    def test_a_contact_now_on_another_number_is_forgotten_not_followed(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=333, source=ManyChatContact.SOURCE_FOUND)
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 333, 'whatsapp_phone': '972501111111'})
        svc._resolve_subscriber = MagicMock(return_value={'id': 444, 'whatsapp_phone': E164})
        out = svc.lookup_or_create(PHONE, 'הורה')
        self.assertEqual(out['subscriber_id'], 444)
        self.assertEqual(ManyChatContact.objects.get(phone=E164).subscriber_id, 444)

    def test_a_contact_deleted_in_manychat_is_forgotten(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=333, source=ManyChatContact.SOURCE_FOUND)
        svc = self.service()
        svc.get_subscriber = MagicMock(side_effect=ManyChatError('Subscriber does not exist', status_code=400))
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc.create_whatsapp_subscriber = MagicMock(return_value={'id': 555})
        svc.lookup_or_create(PHONE, 'הורה')
        self.assertEqual(ManyChatContact.objects.get(phone=E164).subscriber_id, 555)

    def test_a_network_hiccup_does_not_throw_a_link_away(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=333, source=ManyChatContact.SOURCE_MANUAL)
        svc = self.service()
        svc.get_subscriber = MagicMock(side_effect=ManyChatError('שגיאת רשת ב-ManyChat: timeout'))
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ALREADY_EXISTS)
        with self.assertRaises(ManyChatContactUnfindable):
            svc.lookup_or_create(PHONE, 'הורה')
        self.assertTrue(ManyChatContact.objects.filter(phone=E164, subscriber_id=333).exists())

    def test_an_automatic_find_does_not_rewrite_an_unchanged_row(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=111, source=ManyChatContact.SOURCE_MANUAL)
        svc = self.service()
        svc.get_subscriber = MagicMock(side_effect=ManyChatError('timeout'))
        svc._resolve_subscriber = MagicMock(return_value={'id': 111, 'whatsapp_phone': E164})
        svc.lookup_or_create(PHONE, 'הורה')
        self.assertEqual(ManyChatContact.objects.get(phone=E164).source, ManyChatContact.SOURCE_MANUAL)


class ManualLinkTests(NoNetworkMixin, TestCase):
    def test_the_contact_address_from_manychat_links_the_phone(self):
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 7788990011, 'whatsapp_phone': E164, 'name': 'מירב'})
        out = svc.link_contact(PHONE, 'https://app.manychat.com/fb2939190/chat/7788990011')
        svc.get_subscriber.assert_called_once_with(7788990011)
        self.assertTrue(out['phone_verified'])
        row = ManyChatContact.objects.get(phone=E164)
        self.assertEqual((row.subscriber_id, row.source), (7788990011, ManyChatContact.SOURCE_MANUAL))

    def test_after_linking_the_next_send_reaches_the_contact(self):
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 7788990011, 'whatsapp_phone': E164})
        svc.link_contact(PHONE, '7788990011')
        svc._resolve_subscriber = MagicMock(return_value=None)
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ALREADY_EXISTS)
        self.assertEqual(svc.lookup_or_create(PHONE, 'הורה')['subscriber_id'], 7788990011)
        svc.create_whatsapp_subscriber.assert_not_called()

    def test_a_contact_of_another_number_is_refused(self):
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 5, 'whatsapp_phone': '972501111111'})
        with self.assertRaises(ContactLinkError) as caught:
            svc.link_contact(PHONE, '12345')
        self.assertIn('972501111111', str(caught.exception))
        self.assertFalse(ManyChatContact.objects.exists())

    def test_pasting_the_phone_itself_is_explained(self):
        svc = self.service()
        svc.get_subscriber = MagicMock()
        with self.assertRaises(ContactLinkError) as caught:
            svc.link_contact(PHONE, '054-8185646')
        self.assertIn('זה מספר הטלפון', str(caught.exception))
        svc.get_subscriber.assert_not_called()

    def test_an_id_manychat_does_not_have_is_refused(self):
        svc = self.service()
        svc.get_subscriber = MagicMock(side_effect=ManyChatError('Subscriber does not exist', status_code=400))
        with self.assertRaises(ContactLinkError):
            svc.link_contact(PHONE, '99999')
        self.assertFalse(ManyChatContact.objects.exists())

    def test_a_contact_manychat_gives_no_number_for_is_linked_unverified(self):
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 6})
        out = svc.link_contact(PHONE, '66666')
        self.assertFalse(out['phone_verified'])
        self.assertTrue(ManyChatContact.objects.filter(phone=E164, subscriber_id=66666).exists())

    def test_relinking_replaces_the_old_contact(self):
        ManyChatContact.objects.create(phone=E164, subscriber_id=1, source=ManyChatContact.SOURCE_FOUND)
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 2, 'whatsapp_phone': E164})
        svc.link_contact(PHONE, '22222')
        row = ManyChatContact.objects.get(phone=E164)
        self.assertEqual((row.subscriber_id, row.source), (22222, ManyChatContact.SOURCE_MANUAL))


class ParseContactRefTests(TestCase):
    def test_what_the_office_might_paste(self):
        self.assertEqual(parse_contact_ref('7788990011'), 7788990011)
        self.assertEqual(parse_contact_ref(' 7788990011 '), 7788990011)
        self.assertEqual(parse_contact_ref('https://app.manychat.com/fb2939190/subscribers/7788990011'), 7788990011)
        self.assertEqual(parse_contact_ref('https://app.manychat.com/fb2939190/chat/7788990011?x=1'), 7788990011)
        self.assertEqual(parse_contact_ref('app.manychat.com/fb2939190/inbox/7788990011'), 7788990011)
        self.assertIsNone(parse_contact_ref('https://app.manychat.com/'))
        self.assertIsNone(parse_contact_ref(''))


class BroadcastRowTests(NoNetworkMixin, TestCase):
    """The broadcast row says the contact needs linking, on both roads out."""

    def test_the_kind_road(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(side_effect=ManyChatContactUnfindable(CONTACT_UNFINDABLE_MESSAGE))
        out = svc.notify_registration(
            phone=PHONE, parent_name='הורה', child_name='ילד', course_name='קפוארה',
            day_name='רביעי', start_time='17:30', end_time='18:15', branch_name='דמרי',
        )
        self.assertEqual(out['reason'], 'contact_unfindable')
        self.assertIn('ManyChat', out['error'])

    def test_the_flow_road(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(side_effect=ManyChatContactUnfindable(CONTACT_UNFINDABLE_MESSAGE))
        with self.assertRaises(ManyChatContactUnfindable) as caught:
            svc.send_automation_to_contact(automation_type='flow', automation_id='content1', phone=PHONE, name='הורה')
        self.assertTrue(str(caught.exception).startswith('איש הקשר: '))

    def test_other_contact_failures_keep_their_reason(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(side_effect=ManyChatError('Validation error'))
        out = svc.notify_registration(
            phone=PHONE, parent_name='הורה', child_name='ילד', course_name='קפוארה',
            day_name='רביעי', start_time='17:30', end_time='18:15', branch_name='דמרי',
        )
        self.assertEqual(out['reason'], 'lookup_failed')


class LinkContactEndpointTests(NoNetworkMixin, TestCase):
    url = '/api/v1/core/whatsapp/link-contact/'

    def _client(self, role):
        User = get_user_model()
        user = User.objects.create_user(username=f'{role}@x.com', email=f'{role}@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client, user

    def test_a_manager_links_a_contact(self):
        client, user = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(ManyChatService, 'get_subscriber', return_value={'id': 7788990011, 'whatsapp_phone': E164, 'first_name': 'מירב'}):
            res = client.post(self.url, {'phone': PHONE, 'contact': 'https://app.manychat.com/fb1/chat/7788990011'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['subscriber_id'], 7788990011)
        self.assertTrue(res.data['phone_verified'])
        self.assertEqual(ManyChatContact.objects.get(phone=E164).linked_by, user)

    def test_a_refused_link_answers_in_words(self):
        client, _ = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(ManyChatService, 'get_subscriber', return_value={'id': 5, 'whatsapp_phone': '972501111111'}):
            res = client.post(self.url, {'phone': PHONE, 'contact': '12345'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('שייך למספר', res.data['error'])

    def test_both_fields_are_required(self):
        client, _ = self._client(UserProfile.ROLE_MANAGER)
        self.assertEqual(client.post(self.url, {'phone': PHONE}, format='json').status_code, 400)

    def test_only_a_manager_may_link(self):
        client, _ = self._client(UserProfile.ROLE_WORKER)
        res = client.post(self.url, {'phone': PHONE, 'contact': '12345'}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(ManyChatContact.objects.exists())
