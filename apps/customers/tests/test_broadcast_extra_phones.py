"""A group message reaches the extra phones the office added on the card, once each."""
from unittest.mock import patch

from django.core.cache import cache

from apps.customers.broadcast import broadcast_to_children
from apps.customers.models import Parent
from apps.customers.tests.test_children_broadcast import BROADCAST_URL, _child, _Studio

CONFIGURED = patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True))


class ExtraPhonesBroadcastTest(_Studio):
    def setUp(self):
        super().setUp()
        cache.clear()
        # Cohen (Noa + Ido) has a grandmother's phone; Levi (Tom) has none.
        self.grandma = Parent.objects.create(
            family=self.cohen, first_name='סבתא', last_name='רחל', phone='052-555-6666',
        )

    def _post(self, **body):
        payload = {
            'child_ids': [str(self.noa.id), str(self.ido.id), str(self.tom.id)],
            'automation_type': 'kind',
            'automation_id': 'subscription',
            'include_extra_phones': True,
        }
        payload.update(body)
        return self.client.post(BROADCAST_URL, payload, format='json')

    def test_the_preview_lists_the_extra_phone_once_for_the_family(self):
        res = self._post()
        self.assertEqual(res.status_code, 200, res.content)
        by_name = {r['child_name']: r for r in res.data['results']}
        noa = by_name['Noa Cohen']
        self.assertEqual(noa['status'], 'preview')
        self.assertEqual([(e['phone'], e['status']) for e in noa['extra_phones']],
                         [('972525556666', 'preview')])
        # Ido shares the family: both of its phones are already covered.
        ido = by_name['Ido Cohen']
        self.assertEqual(ido['reason'], 'duplicate_phone')
        self.assertEqual([e['reason'] for e in ido['extra_phones']], ['duplicate_phone'])
        self.assertEqual(by_name['Tom Levi']['extra_phones'], [])
        self.assertIn('972525556666', res.data['phones'])
        self.assertEqual(res.data['extra_preview_count'], 1)
        # The per-child counts are what they were before extra phones existed.
        self.assertEqual((res.data['preview_count'], res.data['skipped']), (2, 1))

    def test_a_real_send_goes_to_the_parent_and_the_extra_phone(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration',
                               return_value={'sent': True, 'method': 'flow'}) as send:
            res = self._post(child_ids=[str(self.noa.id)], dry_run=False)
        self.assertEqual(res.status_code, 200, res.content)
        phones = [call.kwargs['phone'] for call in send.call_args_list]
        self.assertEqual(phones, ['0501111111', '052-555-6666'])
        extra_call = send.call_args_list[1].kwargs
        self.assertEqual(extra_call['parent_name'], 'סבתא רחל')
        self.assertEqual(extra_call['child_name'], 'Noa Cohen')
        self.assertEqual(extra_call['course_name'], 'Dance')
        row = res.data['results'][0]
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['extra_phones'][0]['status'], 'sent')
        self.assertEqual(res.data['phones'], ['972501111111', '972525556666'])
        self.assertEqual((res.data['sent'], res.data['extra_sent']), (1, 1))

    def test_an_extra_phone_used_in_an_earlier_chunk_is_not_messaged_again(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration',
                               return_value={'sent': True}) as send:
            res = self._post(child_ids=[str(self.ido.id)], dry_run=False,
                             skip_phones=['972501111111', '972525556666'])
        send.assert_not_called()
        self.assertEqual(res.data['results'][0]['extra_phones'][0]['reason'], 'duplicate_phone')

    def test_a_failed_extra_does_not_fail_the_parent_row(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration',
                               side_effect=[{'sent': True}, {'sent': False, 'reason': 'lookup_failed'}]):
            res = self._post(child_ids=[str(self.noa.id)], dry_run=False)
        row = res.data['results'][0]
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['extra_phones'][0]['status'], 'failed')
        self.assertEqual(row['extra_phones'][0]['error'], 'lookup_failed')
        # Only the phone that got the message counts as used.
        self.assertEqual(res.data['phones'], ['972501111111'])
        self.assertEqual(res.data['extra_failed'], 1)

    def test_no_lesson_means_no_message_to_the_extra_phone_either(self):
        idle = _child(self.cohen, 'Idle')
        res = self._post(child_ids=[str(idle.id)])
        row = res.data['results'][0]
        self.assertEqual(row['reason'], 'no_active_lesson')
        self.assertEqual(row['extra_phones'], [])

    def test_a_flow_reaches_the_extra_phone_by_name(self):
        with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.send_automation_to_contact',
                               return_value={'sent': True}) as flow:
            self._post(child_ids=[str(self.noa.id)], automation_type='flow',
                       automation_id='content2026_x', dry_run=False)
        self.assertEqual([c.kwargs['name'] for c in flow.call_args_list], ['Dana Cohen', 'סבתא רחל'])

    def test_an_extra_with_the_parents_own_number_is_not_a_second_recipient(self):
        Parent.objects.filter(pk=self.grandma.pk).update(phone='+972-50-111-1111')
        out = broadcast_to_children([self.noa], automation_type='kind', automation_id='subscription',
                                    include_extra_phones=True)
        self.assertEqual(out['results'][0]['extra_phones'], [])

    def test_a_screen_that_does_not_ask_sends_only_what_it_always_did(self):
        # The screen before this change neither shows nor counts extra phones,
        # so nothing may reach them unless the request asks.
        for flag in (None, False, 'true', 1):
            body = {'child_ids': [str(self.noa.id)], 'dry_run': False, 'include_extra_phones': flag}
            with CONFIGURED, patch('apps.customers.broadcast.ManyChatService.notify_registration',
                                   return_value={'sent': True}) as send:
                res = self._post(**body)
            self.assertEqual(res.status_code, 200, res.content)
            self.assertEqual([c.kwargs['phone'] for c in send.call_args_list], ['0501111111'], flag)
            self.assertEqual(res.data['results'][0]['extra_phones'], [])
            self.assertEqual(res.data['phones'], ['972501111111'])
