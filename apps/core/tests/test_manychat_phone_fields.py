"""
Finding a parent ManyChat only knows by WhatsApp number.

4,196 contacts were imported from the previous system with a WhatsApp number
and a name and nothing else. ManyChat's API cannot search by WhatsApp number,
so none of them could be reached from Kogo. A ManyChat rule now copies every
contact's number into kogo_whatsapp_phone, and the WhatsApp bot already keeps it
in Client_Phone; these pin how Kogo searches those fields — and that searching
more never means sending to the wrong family.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.manychat_service import ManyChatError, ManyChatService

PHONE = '0502268616'
E164 = '972502268616'

FIELDS = [
    {'id': 14803972, 'name': 'Client_Phone', 'type': 'text'},
    {'id': 14813096, 'name': 'Client_Phone (2026-07-26 07:28:15)', 'type': 'text'},
    {'id': 15000001, 'name': 'kogo_whatsapp_phone', 'type': 'text'},
    {'id': 14917399, 'name': 'bp_phone', 'type': 'text'},
    {'id': 15000002, 'name': 'kogo_whatsapp_phone_count', 'type': 'number'},
]


class _Fake:
    """ManyChat, as far as these lookups go. Anything else is a test failure."""

    def __init__(self, fields=FIELDS, contacts=None):
        self.fields = fields
        # {(field_id, value): [rows]}
        self.contacts = contacts or {}
        self.searches: list[tuple[str, str]] = []

    def __call__(self, method, path, *, params=None, json_body=None, timeout=30):
        if path == '/fb/page/getCustomFields':
            if isinstance(self.fields, Exception):
                raise self.fields
            return {'data': self.fields}
        if path == '/fb/subscriber/findByCustomField':
            key = (str(params['field_id']), params['field_value'])
            self.searches.append(key)
            return {'data': self.contacts.get(key, [])}
        raise AssertionError(f'unexpected ManyChat call {method} {path}')


def _service(fake, field_id=''):
    with override_settings(MANYCHAT_PHONE_FIELD_ID=field_id):
        svc = ManyChatService(api_key='test-key')
    svc._request = fake
    return svc


class NoNetworkMixin:
    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.manychat_service.requests.request', side_effect=AssertionError('network call'))
        patcher.start()
        self.addCleanup(patcher.stop)


class FieldChoiceTests(NoNetworkMixin, SimpleTestCase):
    def test_the_named_fields_are_found_without_any_setting(self):
        svc = _service(_Fake())
        self.assertEqual(svc.phone_lookup_field_ids(), ['15000001', '14803972'])

    def test_the_configured_field_still_comes_first_and_is_not_repeated(self):
        svc = _service(_Fake(), field_id='14803972')
        self.assertEqual(svc.phone_lookup_field_ids(), ['14803972', '15000001'])

    def test_another_integrations_field_and_import_copies_are_not_searched(self):
        ids = _service(_Fake()).phone_lookup_field_ids()
        self.assertNotIn('14917399', ids)  # bp_phone
        self.assertNotIn('14813096', ids)  # Client_Phone (timestamp)

    def test_a_number_field_of_a_similar_name_is_ignored(self):
        self.assertNotIn('15000002', _service(_Fake()).phone_lookup_field_ids())

    def test_a_page_without_the_fields_searches_what_it_has(self):
        svc = _service(_Fake(fields=[{'id': 1, 'name': 'Other', 'type': 'text'}]), field_id='777')
        self.assertEqual(svc.phone_lookup_field_ids(), ['777'])

    def test_when_the_field_list_cannot_be_read_the_configured_field_still_works(self):
        svc = _service(_Fake(fields=ManyChatError('boom')), field_id='777')
        self.assertEqual(svc.phone_lookup_field_ids(), ['777'])


class SearchTests(NoNetworkMixin, SimpleTestCase):
    def test_an_imported_contact_is_found_through_the_rule_field(self):
        row = {'id': 1783389370, 'whatsapp_phone': E164}
        fake = _Fake(contacts={('15000001', E164): [row]})
        self.assertEqual(_service(fake).find_by_custom_phone_field(PHONE), [row])

    def test_a_contact_the_bot_saw_is_found_through_client_phone(self):
        row = {'id': 309493049, 'whatsapp_phone': E164}
        fake = _Fake(contacts={('14803972', E164): [row]})
        self.assertEqual(_service(fake).find_by_custom_phone_field(PHONE), [row])

    def test_the_search_stops_at_the_first_match(self):
        row = {'id': 1, 'whatsapp_phone': E164}
        fake = _Fake(contacts={('15000001', PHONE): [row]})
        _service(fake).find_by_custom_phone_field(PHONE)
        self.assertEqual(fake.searches, [('15000001', PHONE)])

    def test_every_format_is_tried_in_every_field_before_giving_up(self):
        fake = _Fake()
        self.assertEqual(_service(fake).find_by_custom_phone_field(PHONE), [])
        fields = {f for f, _ in fake.searches}
        values = {v for _, v in fake.searches}
        self.assertEqual(fields, {'15000001', '14803972'})
        self.assertEqual(values, {PHONE, E164, f'+{E164}'})

    def test_an_invalid_phone_asks_manychat_nothing(self):
        fake = _Fake()
        self.assertEqual(_service(fake).find_by_custom_phone_field(''), [])
        self.assertEqual(fake.searches, [])


class WrongFamilyTests(NoNetworkMixin, SimpleTestCase):
    """The bot's field can hold a number a parent typed. That is never a match."""

    def test_a_contact_whose_whatsapp_is_another_number_is_not_chosen(self):
        other = {'id': 5, 'whatsapp_phone': '972541111111'}
        fake = _Fake(contacts={('14803972', E164): [other]})
        svc = _service(fake)
        svc.get_subscriber = MagicMock(return_value=other)
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        svc.find_by_phone = MagicMock(return_value=[])
        svc.find_by_name_for_phone = MagicMock(return_value=[])
        self.assertIsNone(svc._resolve_subscriber(PHONE, 'הורה'))

    def test_of_two_contacts_under_the_number_the_one_on_it_is_chosen(self):
        other = {'id': 5, 'whatsapp_phone': '972541111111'}
        right = {'id': 6, 'whatsapp_phone': E164}
        fake = _Fake(contacts={('14803972', E164): [other, right]})
        svc = _service(fake)
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        self.assertEqual(svc._resolve_subscriber(PHONE, 'הורה')['id'], 6)


class LookupEndpointTests(NoNetworkMixin, TestCase):
    """The WhatsApp page's read-only lookup reaches imported contacts too."""

    def test_lookup_falls_back_to_the_phone_fields(self):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        from apps.core.models import UserProfile

        user = get_user_model().objects.create_user(username='m@x.com', email='m@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

        row = {'id': 1783389370, 'whatsapp_phone': E164}
        with patch.object(ManyChatService, 'find_by_phone', return_value=[]), \
                patch.object(ManyChatService, 'find_by_custom_phone_field', return_value=[row]) as custom:
            res = client.get('/api/v1/core/whatsapp/lookup/', {'phone': PHONE})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['subscribers'], [row])
        custom.assert_called_once_with(PHONE)
