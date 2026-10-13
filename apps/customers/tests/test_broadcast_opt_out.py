"""Whoever asked "הסר" gets no broadcast from the customers list — as the parent, or as an extra phone of the family."""
from unittest.mock import patch

from django.core.cache import cache
from django.db import DatabaseError

from apps.core import whatsapp_optout
from apps.core.models import WhatsAppOptOut
from apps.customers.broadcast import broadcast_to_children
from apps.customers.models import Parent
from apps.customers.tests.test_children_broadcast import BROADCAST_URL, _child, _Studio

CONFIGURED = patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True))
SENT = {'sent': True, 'method': 'flow'}
LIST_DOWN = patch('apps.core.whatsapp_optout._rows', side_effect=DatabaseError('the list cannot be read'))


class BroadcastOptOutTest(_Studio):
    def setUp(self):
        super().setUp()
        cache.clear()
        # Dana Cohen (Noa + Ido) asked "הסר"; her mother is an extra phone of the family and did not.
        whatsapp_optout.add('050-111-1111', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        self.grandma = Parent.objects.create(family=self.cohen, first_name='סבתא', last_name='רחל', phone='052-555-6666')

    def _post(self, **body):
        payload = {
            'child_ids': [str(self.noa.id), str(self.ido.id), str(self.tom.id)],
            'automation_type': 'kind', 'automation_id': 'subscription',
        }
        payload.update(body)
        return self.client.post(BROADCAST_URL, payload, format='json')

    def test_the_preview_says_who_will_not_get_it_and_counts_them_apart(self):
        res = self._post()
        self.assertEqual(res.status_code, 200, res.content)
        by_name = {row['child_name']: row for row in res.data['results']}
        self.assertEqual((by_name['Noa Cohen']['status'], by_name['Noa Cohen']['reason']), ('skipped', 'opted_out'))
        # The sibling on the same phone says the same thing — not "duplicate": the phone was never used.
        self.assertEqual(by_name['Ido Cohen']['reason'], 'opted_out')
        self.assertEqual(by_name['Tom Levi']['status'], 'preview')
        self.assertEqual((res.data['preview_count'], res.data['skipped'], res.data['opted_out']), (1, 2, 2))
        self.assertEqual(res.data['phones'], ['972502222222'])

    def test_nothing_is_sent_to_the_phone_that_asked(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value=SENT) as send:
            res = self._post(dry_run=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0502222222'])
        self.assertEqual((res.data['sent'], res.data['failed'], res.data['skipped'], res.data['opted_out']), (1, 0, 2, 2))
        # The whole request still adds up for a screen that knows nothing of "הסר".
        self.assertEqual(res.data['total'], res.data['sent'] + res.data['failed'] + res.data['skipped'])

    def test_a_flow_is_stopped_the_same_way(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.send_automation_to_contact', return_value=SENT) as send:
            res = self._post(automation_type='flow', automation_id='content2026_x', dry_run=False)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0502222222'])
        self.assertEqual(res.data['opted_out'], 2)

    def test_the_other_parent_did_not_ask_and_still_gets_it(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value=SENT) as send:
            res = self._post(child_ids=[str(self.noa.id)], dry_run=False, include_extra_phones=True)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['052-555-6666'])
        row = res.data['results'][0]
        self.assertEqual((row['status'], row['reason']), ('skipped', 'opted_out'))
        self.assertEqual([(extra['phone'], extra['status']) for extra in row['extra_phones']], [('972525556666', 'sent')])
        self.assertEqual((res.data['opted_out'], res.data['extra_sent'], res.data['extra_opted_out']), (1, 1, 0))

    def test_an_extra_phone_that_asked_is_skipped_while_the_parent_gets_it(self):
        whatsapp_optout.restore('0501111111')
        whatsapp_optout.add('0525556666', source=WhatsAppOptOut.SOURCE_OFFICE)
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value=SENT) as send:
            res = self._post(child_ids=[str(self.noa.id)], dry_run=False, include_extra_phones=True)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0501111111'])
        extra = res.data['results'][0]['extra_phones'][0]
        self.assertEqual((extra['status'], extra['reason']), ('skipped', 'opted_out'))
        self.assertEqual((res.data['sent'], res.data['opted_out'], res.data['extra_skipped'], res.data['extra_opted_out']), (1, 0, 1, 1))

    def test_a_phone_that_asked_is_one_whatever_it_is_called_for(self):
        """A template with no lesson to fill in is skipped for its own reason; whoever asked is skipped for theirs."""
        idle = _child(self.cohen, 'Idle')
        out = broadcast_to_children([idle], automation_type='kind', automation_id='subscription', include_extra_phones=True)
        row = out['results'][0]
        self.assertEqual((row['reason'], row['extra_phones']), ('opted_out', []))
        self.assertEqual(out['opted_out'], 1)

    def test_a_phone_given_back_gets_broadcasts_again(self):
        whatsapp_optout.restore('0501111111')
        res = self._post()
        self.assertEqual((res.data['preview_count'], res.data['opted_out']), (2, 0))

    def test_when_the_list_cannot_be_read_nothing_leaves_and_the_screen_is_told(self):
        for dry_run in (True, False):
            with CONFIGURED, LIST_DOWN, patch('apps.customers.broadcast.ManyChatService.notify_registration') as kind, \
                    patch('apps.customers.broadcast.ManyChatService.send_automation_to_contact') as flow:
                res = self._post(dry_run=dry_run)
            self.assertEqual(res.status_code, 503, dry_run)
            self.assertEqual(res.data['code'], 'opt_out_list_unavailable')
            self.assertIn('שום הודעה לא נשלחה', res.data['error'])
            kind.assert_not_called()
            flow.assert_not_called()

    def test_the_list_is_read_once_for_the_whole_request(self):
        with patch('apps.core.whatsapp_optout.blocked_phones', wraps=whatsapp_optout.blocked_phones) as read:
            broadcast_to_children([self.noa, self.ido, self.tom], automation_type='kind', automation_id='subscription',
                                  include_extra_phones=True)
        self.assertEqual(read.call_count, 1)


class FromTheMessageToTheBroadcastTest(_Studio):
    """The whole chain, as it runs: ManyChat's copy of a parent's "הסר" comes in, and the next broadcast skips her."""

    def test_a_parent_who_wrote_remove_on_whatsapp_is_skipped_by_the_next_broadcast(self):
        from rest_framework.test import APIClient

        cache.clear()
        key = self.client.post('/api/v1/wahub/settings/inbound-key/').data['key']
        # ManyChat's own body, as its webhook writes it: the parent's WhatsApp number, and what she typed.
        body = {'id': '555001', 'whatsapp_phone': '+972501111111', 'first_name': 'Dana', 'last_input_text': 'הסר'}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('nothing may leave the process')):
            taken = APIClient().post('/api/v1/wahub/inbound/manychat/', body, format='json', HTTP_X_WAHUB_KEY=key)
        self.assertEqual((taken.status_code, taken.data), (200, {'ok': True, 'stored': True}))
        self.assertTrue(whatsapp_optout.is_blocked('050-111-1111'))

        ids = [str(self.noa.id), str(self.tom.id)]
        preview = self.client.post(BROADCAST_URL, {'child_ids': ids, 'automation_type': 'kind', 'automation_id': 'subscription'}, format='json')
        self.assertEqual((preview.data['preview_count'], preview.data['opted_out']), (1, 1))
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value=SENT) as send:
            sent = self.client.post(
                BROADCAST_URL, {'child_ids': ids, 'automation_type': 'kind', 'automation_id': 'subscription', 'dry_run': False}, format='json',
            )
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0502222222'])
        self.assertEqual((sent.data['sent'], sent.data['opted_out']), (1, 1))

        # The office gives her back ("ביקשה לחזור לתפוצה"): the next broadcast reaches her again.
        from apps.wahub.models import Contact

        contact = Contact.objects.get(phone='972501111111')
        self.assertEqual(self.client.post(f'/api/v1/wahub/contacts/{contact.id}/opt-in/').status_code, 200)
        again = self.client.post(BROADCAST_URL, {'child_ids': ids, 'automation_type': 'kind', 'automation_id': 'subscription'}, format='json')
        self.assertEqual((again.data['preview_count'], again.data['opted_out']), (2, 0))
