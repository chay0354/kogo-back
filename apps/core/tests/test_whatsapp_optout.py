"""
"הסר" — the system's list of phones that get no broadcast (apps/core/whatsapp_optout.py), and the two
sends to a list of contacts on the WhatsApp page of the settings, which read it.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core import whatsapp_optout
from apps.core.models import UserProfile, WhatsAppOptOut

BULK_FLOW = '/api/v1/core/whatsapp/bulk-flow/'
BULK_SEND = '/api/v1/core/whatsapp/bulk-send/'
CONFIGURED = patch('apps.core.manychat_views.ManyChatService.is_configured', new=property(lambda self: True))
LIST_DOWN = patch('apps.core.whatsapp_optout._rows', side_effect=DatabaseError('the list cannot be read'))


def _manager(email='optout@core.test'):
    user = get_user_model().objects.create_user(username=email, email=email, password='x', first_name='מיכל', last_name='כהן')
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    return user


class TheListTests(TestCase):
    def test_a_phone_is_kept_in_one_spelling_and_found_in_any(self):
        row, made = whatsapp_optout.add('050-123 4567', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        self.assertTrue(made)
        self.assertEqual((row.phone, row.source, row.text), ('972501234567', 'message', 'הסר'))
        for spelling in ('0501234567', '+972501234567', '972-50-1234567', '501234567'):
            self.assertTrue(whatsapp_optout.is_blocked(spelling), spelling)
        self.assertFalse(whatsapp_optout.is_blocked('0507654321'))
        self.assertFalse(whatsapp_optout.is_blocked(''))
        self.assertEqual(whatsapp_optout.blocked_phones(), frozenset({'972501234567'}))

    def test_asking_twice_keeps_the_first_row(self):
        first, _made = whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        again, made = whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_OFFICE)
        self.assertFalse(made)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(WhatsAppOptOut.objects.count(), 1)

    def test_giving_a_phone_back_keeps_the_history_and_a_new_request_is_a_new_row(self):
        user = _manager()
        whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        self.assertTrue(whatsapp_optout.restore('0501234567', by=user))
        self.assertFalse(whatsapp_optout.restore('0501234567', by=user))       # nothing is standing any more
        self.assertFalse(whatsapp_optout.is_blocked('0501234567'))
        kept = WhatsAppOptOut.objects.get()
        self.assertEqual(kept.restored_by, user)
        self.assertIsNotNone(kept.restored_at)

        _row, made = whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_OFFICE, by=user)
        self.assertTrue(made)
        self.assertEqual(WhatsAppOptOut.objects.count(), 2)
        self.assertEqual(whatsapp_optout.standing('0501234567').opted_out_by, user)

    def test_the_database_itself_allows_one_standing_row_a_phone(self):
        from django.db import IntegrityError, transaction
        from django.utils import timezone

        WhatsAppOptOut.objects.create(phone='972501234567', source='message', opted_out_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            WhatsAppOptOut.objects.create(phone='972501234567', source='office', opted_out_at=timezone.now())

    def test_something_that_is_no_phone_is_refused(self):
        with self.assertRaises(ValueError):
            whatsapp_optout.add('', source=WhatsAppOptOut.SOURCE_OFFICE)

    def test_a_list_that_cannot_be_read_says_so_and_never_answers_nobody(self):
        with LIST_DOWN:
            with self.assertRaises(whatsapp_optout.OptOutListUnavailable) as raised:
                whatsapp_optout.blocked_phones()
            with self.assertRaises(whatsapp_optout.OptOutListUnavailable):
                whatsapp_optout.is_blocked('0501234567')
        self.assertEqual(raised.exception.code, 'opt_out_list_unavailable')
        self.assertIn('שום הודעה לא נשלחה', raised.exception.detail)

    def test_the_table_is_closed_to_everybody_but_the_application(self):
        """It holds phones: row level security is on, not forced on the owner, and no policy lets anybody in."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'whatsapp_opt_outs'")
            self.assertEqual(cursor.fetchone(), (True, False))
            cursor.execute("SELECT count(*) FROM pg_policies WHERE tablename = 'whatsapp_opt_outs'")
            self.assertEqual(cursor.fetchone()[0], 0)


class SettingsPageSendsTests(TestCase):
    """bulk-flow and bulk-send go to a list of contacts: a broadcast, and so they skip whoever asked "הסר"."""

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=_manager()).key}')
        whatsapp_optout.add('0501111111', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        self.contacts = [{'phone': '050-111-1111', 'name': 'דנה'}, {'phone': '0502222222', 'name': 'רינה'}]

    def test_an_automation_skips_whoever_asked_and_says_how_many(self):
        with CONFIGURED, patch(
            'apps.core.manychat_views.ManyChatService.send_automation_to_contact', return_value={'sent': True, 'method': 'flow'},
        ) as send:
            res = self.client.post(BULK_FLOW, {'automation_type': 'flow', 'automation_id': 'content_x', 'contacts': self.contacts}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0502222222'])
        self.assertEqual((res.data['sent'], res.data['skipped'], res.data['opted_out']), (1, 1, 1))
        self.assertEqual(res.data['results'][0], {'phone': '050-111-1111', 'name': 'דנה', 'status': 'skipped', 'reason': 'opted_out'})

    def test_the_preview_says_it_too(self):
        with CONFIGURED:
            res = self.client.post(
                BULK_FLOW, {'automation_type': 'flow', 'automation_id': 'content_x', 'contacts': self.contacts, 'dry_run': True}, format='json',
            )
        self.assertEqual((res.data['preview_count'], res.data['opted_out']), (1, 1))

    def test_a_text_to_many_skips_whoever_asked(self):
        with CONFIGURED, patch('apps.core.manychat_views.ManyChatService.lookup_or_create', return_value={'subscriber_id': 7}), \
                patch('apps.core.manychat_views.ManyChatService.send_whatsapp_text', return_value={}) as send:
            res = self.client.post(BULK_SEND, {'text': 'שלום', 'contacts': self.contacts}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(send.call_count, 1)
        self.assertEqual((res.data['sent'], res.data['skipped'], res.data['opted_out']), (1, 1, 1))

    def test_when_the_list_cannot_be_read_nothing_is_sent_to_anybody(self):
        for url, body in (
            (BULK_FLOW, {'automation_type': 'flow', 'automation_id': 'content_x', 'contacts': self.contacts}),
            (BULK_SEND, {'text': 'שלום', 'contacts': self.contacts}),
        ):
            with CONFIGURED, LIST_DOWN, patch('apps.core.manychat_views.ManyChatService.send_automation_to_contact') as flow, \
                    patch('apps.core.manychat_views.ManyChatService.lookup_or_create') as lookup, \
                    patch('apps.core.manychat_views.ManyChatService.send_whatsapp_text') as text:
                res = self.client.post(url, body, format='json')
            self.assertEqual(res.status_code, 503, url)
            self.assertEqual(res.data['code'], 'opt_out_list_unavailable')
            for sender in (flow, lookup, text):
                sender.assert_not_called()

    def test_one_message_to_one_number_is_not_a_broadcast(self):
        """A person writing to one customer is not stopped by the list (the table of the contract, ט.1.5)."""
        with CONFIGURED, patch('apps.core.manychat_views.ManyChatService.send_whatsapp_text', return_value={}) as send:
            res = self.client.post('/api/v1/core/whatsapp/send/', {'text': 'שלום', 'subscriber_id': 7, 'phone': '0501111111'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        send.assert_called_once()
