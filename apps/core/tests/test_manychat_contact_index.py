"""
The ManyChat import file that makes every parent findable.

A broadcast failed for 4 of 14 parents with "already in ManyChat, but ManyChat
cannot find it by WhatsApp number": contacts imported from the previous system
never got kogo_whatsapp_phone, the only field Kogo can search them by. The
office fixes all of them at once by importing a file into ManyChat; these pin
what goes into that file — the number Kogo actually sends to, once, twice over
— and that producing it never touches ManyChat.
"""
import csv
import io
from datetime import date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_contact_index import (
    INDEX_FIELD_NAME,
    SCOPE_ALL,
    SCOPE_CURRENT,
    contact_index_csv,
    contact_index_phones,
    contact_index_rows,
)
from apps.core.manychat_service import (
    CONTACT_UNFINDABLE_MESSAGE,
    PHONE_LOOKUP_FIELD_NAMES,
    ManyChatError,
    ManyChatService,
)
from apps.core.models import Branch, ManyChatContact, UserProfile
from apps.customers.models import Child, Family, Parent


class NoNetworkMixin:
    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.manychat_service.requests.request', side_effect=AssertionError('network call'))
        patcher.start()
        self.addCleanup(patcher.stop)


def make_family(branch, *, family_phone='', parents=(), statuses=('active',)):
    family = Family.objects.create(name='משפחה', phone=family_phone, branch=branch)
    for first_name, phone, is_primary in parents:
        Parent.objects.create(family=family, first_name=first_name, last_name='x', phone=phone, is_primary=is_primary)
    for i, status in enumerate(statuses):
        Child.objects.create(
            family=family, first_name=f'ילד{i}', last_name='x',
            birth_date=date(2016, 1, 1), gender='male', status=status,
        )
    return family


class ContactIndexPhonesTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.branch = Branch.objects.create(name='Main')

    def test_the_number_a_family_is_sent_to_is_the_one_in_the_file(self):
        # Primary parent over the other parent and over the family's own phone.
        family = make_family(
            self.branch, family_phone='0521111111',
            parents=[('אבא', '0542222222', False), ('אמא', '054-333-3333', True)],
        )
        child = family.children.first()
        sent_to = ManyChatService.normalize_phone_e164(build_enrollment_whatsapp_context(child=child)['phone'])
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), [sent_to])
        self.assertEqual(sent_to, '972543333333')

    def test_no_primary_parent_means_the_first_parent_as_the_sender_picks(self):
        family = make_family(self.branch, parents=[('תמר', '0544444444', False), ('אורי', '0545555555', False)])
        child = family.children.first()
        sent_to = ManyChatService.normalize_phone_e164(build_enrollment_whatsapp_context(child=child)['phone'])
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), [sent_to])

    def test_a_parent_without_a_phone_falls_back_to_the_family_phone(self):
        make_family(self.branch, family_phone='0526666666', parents=[('רות', '', True)])
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), ['972526666666'])

    def test_one_row_per_number_even_across_families_and_siblings(self):
        make_family(self.branch, parents=[('דנה', '0547777777', True)], statuses=('active', 'trial_signed'))
        make_family(self.branch, parents=[('דנה', '+972 54-777-7777', True)])
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), ['972547777777'])

    def test_current_leaves_out_former_students_and_everyone_brings_them_in(self):
        make_family(self.branch, parents=[('פעיל', '0501000001', True)], statuses=('active',))
        make_family(self.branch, parents=[('בעיה', '0501000002', True)], statuses=('payment_problem',))
        make_family(self.branch, parents=[('ניסיון', '0501000003', True)], statuses=('trial_completed',))
        make_family(self.branch, parents=[('רישום', '0501000004', True)], statuses=('pending',))
        make_family(self.branch, parents=[('עזב', '0501000005', True)], statuses=('inactive',))
        make_family(self.branch, parents=[('רפאים', '0501000006', True)], statuses=('ghost',))
        make_family(self.branch, parents=[('בלי ילדים', '0501000007', True)], statuses=())

        self.assertEqual(
            contact_index_phones(SCOPE_CURRENT),
            ['972501000001', '972501000002', '972501000003', '972501000004'],
        )
        # A ghost is not a family anyone writes to, and nor is one without children.
        self.assertEqual(
            contact_index_phones(SCOPE_ALL),
            ['972501000001', '972501000002', '972501000003', '972501000004', '972501000005'],
        )

    def test_a_family_with_one_current_child_is_current(self):
        make_family(self.branch, parents=[('מעורב', '0501000008', True)], statuses=('inactive', 'active'))
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), ['972501000008'])

    def test_numbers_that_cannot_be_an_israeli_phone_stay_out(self):
        make_family(self.branch, parents=[('קצר', '05412', True)])
        make_family(self.branch, parents=[('זר', '+1 415 555 0100', True)])
        make_family(self.branch, parents=[('ריק', '', True)])
        make_family(self.branch, parents=[('נייח', '03-5551234', True)])
        self.assertEqual(contact_index_phones(SCOPE_CURRENT), ['97235551234'])

    def test_the_name_is_the_same_parent_the_number_came_from(self):
        family = make_family(
            self.branch, family_phone='0521111111',
            parents=[('אבא', '0542222222', False), ('נעמה', '054-333-3333', True)],
        )
        Parent.objects.filter(family=family, first_name='נעמה').update(last_name='שלמה')
        ctx = build_enrollment_whatsapp_context(child=family.children.first())
        self.assertEqual(contact_index_rows(SCOPE_CURRENT), [('972543333333', 'נעמה', 'שלמה')])
        self.assertEqual(ctx['parent_name'], 'נעמה שלמה')

    def test_a_parent_without_a_phone_still_gives_their_name(self):
        make_family(self.branch, family_phone='0526666666', parents=[('רות', '', True)])
        self.assertEqual(contact_index_rows(SCOPE_CURRENT), [('972526666666', 'רות', 'x')])

    def test_a_family_without_parents_is_named_after_the_family(self):
        family = make_family(self.branch, family_phone='0527777777', parents=())
        Family.objects.filter(pk=family.pk).update(name='משפחת לוי')
        self.assertEqual(contact_index_rows(SCOPE_CURRENT), [('972527777777', 'משפחת לוי', '')])

    def test_two_families_on_one_phone_give_one_row_with_the_fuller_name(self):
        # Family ids are random UUIDs, so run both creation orders.
        for order in ((('', '0548888888', True),), (('מיכל', '0548888888', True),)), \
                     ((('מיכל', '0548888888', True),), (('', '0548888888', True),)):
            with self.subTest(order=[p[0][0] for p in order]):
                Family.objects.all().delete()
                for parents in order:
                    make_family(self.branch, parents=parents)
                self.assertEqual(contact_index_rows(SCOPE_CURRENT), [('972548888888', 'מיכל', 'x')])

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            contact_index_phones('nobody')


class ContactIndexCsvTests(TestCase):
    def test_each_number_twice_and_the_name_under_the_fields_manychat_maps(self):
        csv_text = contact_index_csv([('972501234567', 'נעמה', 'שלמה'), ('972541111111', 'Dana', '')])
        rows = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual(rows[0], ['WhatsApp ID', INDEX_FIELD_NAME, 'First Name', 'Last Name'])
        self.assertEqual(rows[1:], [
            ['972501234567', '972501234567', 'נעמה', 'שלמה'],
            ['972541111111', '972541111111', 'Dana', ''],
        ])

    def test_a_name_with_a_comma_or_quote_stays_one_cell(self):
        rows = list(csv.reader(io.StringIO(contact_index_csv([('972501234567', 'בן, "דוד"', 'כהן')]))))
        self.assertEqual(rows[1], ['972501234567', '972501234567', 'בן, "דוד"', 'כהן'])

    def test_the_field_is_one_kogo_searches(self):
        self.assertIn(INDEX_FIELD_NAME, PHONE_LOOKUP_FIELD_NAMES)

    def test_the_value_is_a_form_the_lookup_tries(self):
        # The lookup searches the field with each of these; the file writes the
        # form ManyChat's own rule writes (972…), which is among them.
        self.assertIn('972501234567', ManyChatService.phone_lookup_variants('050-1234567'))

    def test_the_failure_row_points_to_the_fix_for_everyone(self):
        self.assertIn('אנשי קשר ש-ManyChat לא מוצא', CONTACT_UNFINDABLE_MESSAGE)
        self.assertIn('קישור לאיש קשר', CONTACT_UNFINDABLE_MESSAGE)


class ContactIndexEndpointTests(NoNetworkMixin, TestCase):
    url = '/api/v1/core/whatsapp/contact-index/'
    export_url = '/api/v1/core/whatsapp/contact-index/export/'

    def setUp(self):
        super().setUp()
        branch = Branch.objects.create(name='Main')
        make_family(branch, parents=[('פעיל', '0501000001', True)], statuses=('active',))
        make_family(branch, parents=[('עזב', '0501000005', True)], statuses=('inactive',))

    def _client(self, role):
        User = get_user_model()
        user = User.objects.create_user(username=f'{role}@x.com', email=f'{role}@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_the_counts_per_scope(self):
        res = self._client(UserProfile.ROLE_MANAGER).get(self.url)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data, {'field_name': 'kogo_whatsapp_phone', 'scopes': {'current': 1, 'all': 2}})

    def test_the_file_downloads_as_csv(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        res = client.get(self.export_url)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res['Content-Type'].startswith('text/csv'))
        self.assertIn('manychat-contacts-current.csv', res['Content-Disposition'])
        self.assertEqual(
            res.content.decode('utf-8'),
            'WhatsApp ID,kogo_whatsapp_phone,First Name,Last Name\n972501000001,972501000001,פעיל,x\n',
        )

        everyone = client.get(self.export_url, {'scope': 'all'})
        self.assertEqual(everyone.content.decode().count('\n'), 3)

    def test_an_unknown_scope_is_a_bad_request(self):
        res = self._client(UserProfile.ROLE_MANAGER).get(self.export_url, {'scope': 'x'})
        self.assertEqual(res.status_code, 400)

    def test_only_a_manager_gets_the_phone_list(self):
        for role in (UserProfile.ROLE_WORKER, UserProfile.ROLE_PARTNER):
            client = self._client(role)
            self.assertEqual(client.get(self.url).status_code, 403, role)
            self.assertEqual(client.get(self.export_url).status_code, 403, role)
        self.assertEqual(APIClient().get(self.export_url).status_code, 401)


class FindExistingTests(NoNetworkMixin, TestCase):
    """The check after the import searches, and never creates or writes to ManyChat."""

    def service(self):
        svc = ManyChatService(api_key='test-key')
        svc.create_whatsapp_subscriber = MagicMock(side_effect=AssertionError('must not create'))
        svc._ensure_phone_indexed = MagicMock(side_effect=AssertionError('must not write to ManyChat'))
        return svc

    def test_a_contact_the_field_now_finds_is_returned_and_remembered(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value={'id': 4455, 'whatsapp_phone': '972545757056'})
        sub = svc.find_existing('054-5757056')
        self.assertEqual(sub['id'], 4455)
        self.assertEqual(ManyChatContact.objects.get(phone='972545757056').subscriber_id, 4455)
        self.assertEqual(ManyChatContact.objects.get(phone='972545757056').source, ManyChatContact.SOURCE_FOUND)

    def test_still_unfindable_is_none_and_nothing_is_created(self):
        svc = self.service()
        svc._resolve_subscriber = MagicMock(return_value=None)
        self.assertIsNone(svc.find_existing('0545757056'))
        self.assertFalse(ManyChatContact.objects.exists())

    def test_a_remembered_contact_answers_without_a_search(self):
        ManyChatContact.objects.create(phone='972545757056', subscriber_id=4455, source=ManyChatContact.SOURCE_MANUAL)
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 4455, 'whatsapp_phone': '972545757056'})
        svc._resolve_subscriber = MagicMock(side_effect=AssertionError('no search needed'))
        self.assertEqual(svc.find_existing('0545757056')['id'], 4455)


class ContactIndexCheckEndpointTests(NoNetworkMixin, TestCase):
    url = '/api/v1/core/whatsapp/contact-index/check/'

    def setUp(self):
        super().setUp()
        overrider = self.settings(MANYCHAT_KEY='test-key')
        overrider.enable()
        self.addCleanup(overrider.disable)

    def _client(self, role):
        User = get_user_model()
        user = User.objects.create_user(username=f'{role}@x.com', email=f'{role}@x.com', password='pass12345!')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_found(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(ManyChatService, 'find_existing', return_value={'id': 4455, 'first_name': 'נעמה', 'last_name': 'שלמה'}):
            res = client.post(self.url, {'phone': '0545757056'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data, {'phone': '972545757056', 'found': True, 'subscriber_id': 4455, 'display_name': 'נעמה שלמה'})

    def test_not_found(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(ManyChatService, 'find_existing', return_value=None):
            res = client.post(self.url, {'phone': '0545757056'}, format='json')
        self.assertEqual(res.data['found'], False)
        self.assertIsNone(res.data['subscriber_id'])

    def test_a_manychat_failure_is_a_502_in_words(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(ManyChatService, 'find_existing', side_effect=ManyChatError('Rate limit')):
            res = client.post(self.url, {'phone': '0545757056'}, format='json')
        self.assertEqual(res.status_code, 502)
        self.assertIn('Rate limit', res.data['error'])

    def test_a_phone_is_required(self):
        self.assertEqual(self._client(UserProfile.ROLE_MANAGER).post(self.url, {}, format='json').status_code, 400)

    def test_no_manychat_key_is_said_not_passed_off_as_not_found(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        with self.settings(MANYCHAT_KEY=''), \
                patch.object(ManyChatService, 'find_existing', side_effect=AssertionError('must not search')):
            res = client.post(self.url, {'phone': '0545757056'}, format='json')
        self.assertEqual(res.status_code, 503)
        self.assertIn('MANYCHAT_KEY', res.data['error'])

    def test_only_a_manager(self):
        with patch.object(ManyChatService, 'find_existing', side_effect=AssertionError('must not search')):
            res = self._client(UserProfile.ROLE_WORKER).post(self.url, {'phone': '0545757056'}, format='json')
        self.assertEqual(res.status_code, 403)
