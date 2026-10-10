"""Demo mode: invented contacts the owner types for in production. Nothing reaches them, ever."""
from unittest.mock import patch

from django.test import override_settings

from apps.core.models import OfficeAlert
from apps.wahub import knowledge_import
from apps.wahub.models import Contact, KnowledgeProposal, Message, ShadowReply
from apps.wahub.tests.base import WahubTestCase, client_for, make_user

REQUEST = 'apps.core.manychat_service.ManyChatService._request'


class DemoContactTests(WahubTestCase):
    def test_a_demo_contact_is_made_from_the_screen_and_marked_everywhere(self):
        response = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו ראשון', 'is_demo': True})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['is_demo'], response.data['source'], response.data['source_label']), (True, 'demo', 'דמו'))
        plain = self.post('contacts/', {'phone': '050-5559002', 'name': 'רגיל'})
        self.assertEqual((plain.data['is_demo'], plain.data['source']), (False, 'manual'))
        listed = {row['phone']: row['is_demo'] for row in self.get('contacts/').data['results']}
        self.assertEqual(listed, {'972505559001': True, '972505559002': False})

    def test_simulate_inbound_takes_the_manychat_door_for_a_demo_contact_only(self):
        plain = self.post('contacts/', {'phone': '050-5559002', 'name': 'רגיל'}).data
        refused = self.post(f'contacts/{plain["id"]}/simulate-inbound/', {'text': 'שלום'})
        self.assertEqual((refused.status_code, refused.data['code']), (400, 'not_demo'))
        self.assertEqual(Message.objects.count(), 0)

        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        self.assertEqual(self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': ''}).status_code, 400)
        self.assertEqual(self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'x', 'sender': 'office'}).status_code, 400)
        response = self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'אפשר לדבר עם נציג? יש בעיה בחיוב'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['stored'])
        self.assertEqual(response.data['messages_count'], 1)
        self.assertEqual(response.data['chat']['unread_count'], 1)
        self.assertTrue(response.data['chat']['needs_human'])
        contact = Contact.objects.get(pk=demo['id'])
        self.assertTrue(contact.needs_analysis)
        self.assertTrue(contact.needs_shadow)
        self.assertEqual(OfficeAlert.objects.count(), 0)          # no office alert for an invented person
        bot = self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי, במה אוכל לעזור?', 'sender': 'bot'})
        self.assertEqual(bot.data['messages_count'], 2)
        self.assertIsNone(bot.data['chat']['waiting_since'])
        self.assertEqual(Message.objects.filter(contact=contact, sender='bot', direction='out').count(), 1)

    @override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True, WAHUB_SIMULATE_SEND=False)
    def test_nothing_is_sent_to_a_demo_contact_even_with_the_switch_on(self):
        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'שלום'})
        with patch(REQUEST) as request:
            sent = self.post(f'contacts/{demo["id"]}/send/', {'text': 'היי'})
            flow = self.post(f'contacts/{demo["id"]}/send-flow/', {'automation_id': 'content20240101'})
            taken = self.post(f'contacts/{demo["id"]}/takeover/')
            released = self.post(f'contacts/{demo["id"]}/release/')
        request.assert_not_called()
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.data['message']['status'], 'failed')
        self.assertIn('דמו', sent.data['message']['error'])
        self.assertEqual(flow.data['message']['status'], 'failed')
        self.assertEqual((taken.status_code, taken.data['chat']['handled_by']), (200, 'human'))
        self.assertEqual((released.status_code, released.data['chat']['handled_by']), (200, 'bot'))
        self.assertEqual(self.network.call_count, 0)


@override_settings(ANTHROPIC_API_KEY='')
class DemoScenarioTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()

    def test_the_list_and_a_scenario_that_runs_at_once(self):
        scenarios = self.get('demo/scenarios/').data
        keys = {row['key'] for row in scenarios}
        self.assertTrue({'internal_text', 'fake_registration', 'loop', 'burst', 'voice', 'number_unavailable', 'nicole',
                         'broadcast_reply', 'not_answered', 'asks_for_human', 'external_branch', 'office_hours',
                         'human_override'} <= keys)
        self.assertTrue(all(row['title'] and row['description'] for row in scenarios))

        self.assertEqual(self.post('demo/scenario/', {'scenario': 'nope'}).status_code, 400)
        response = self.post('demo/scenario/', {'scenario': 'broadcast_reply'})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['is_demo'])
        self.assertTrue(response.data['phone'].startswith('972505559'))
        self.assertEqual(response.data['source'], 'demo')
        self.assertEqual(response.data['messages_count'], 3)
        self.assertEqual(len(response.data['shadow']), 1)
        self.assertEqual(response.data['shadow'][0]['text'], 'מעולה, רשמנו שאתם מגיעים! נתראה בשיעור 🥳')
        self.assertEqual(response.data['shadow'][0]['old_bot_reply']['text'], 'על מה אתה מאשר? במה אוכל לעזור?')
        self.assertIsNotNone(response.data['known']['analyzed_at'])
        self.assertEqual(response.data['kogo']['outcome'], 'not_found')
        self.assertEqual(response.data['proposals'], [])
        contact = Contact.objects.get(pk=response.data['id'])
        self.assertFalse(contact.needs_shadow)
        self.assertEqual(contact.manychat_subscriber_id, '')
        second = self.post('demo/scenario/', {'scenario': 'broadcast_reply'})
        self.assertNotEqual(second.data['phone'], response.data['phone'])

    def test_a_person_answering_over_the_bot_makes_a_proposal_at_once(self):
        response = self.post('demo/scenario/', {'scenario': 'human_override'})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['chat']['handled_by'], 'human')
        proposal = KnowledgeProposal.objects.get(contact_id=response.data['id'])
        self.assertEqual((proposal.source, proposal.status), ('human_override', 'pending'))
        self.assertEqual([row['who'] for row in proposal.evidence], ['customer', 'bot', 'office'])
        self.assertEqual(response.data['proposals'][0]['id'], proposal.id)
        self.assertEqual([row['id'] for row in self.get('review/proposals/', status='pending').data], [proposal.id])
        self.assertEqual(OfficeAlert.objects.count(), 0)

    def test_delete_takes_every_demo_contact_with_its_shadow_and_proposals(self):
        self.post('demo/scenario/', {'scenario': 'external_branch'})
        self.post('demo/scenario/', {'scenario': 'asks_for_human'})
        real = self.post('contacts/', {'phone': '050-5550777', 'name': 'אמיתי'}).data
        reply = ShadowReply.objects.filter(contact__is_demo=True).first()
        self.post(f'shadow/{reply.id}/verdict/', {'verdict': 'bad', 'note': 'רמת גן חיצוני, לא אומרים מחיר בכלל'})
        self.assertEqual(KnowledgeProposal.objects.count(), 1)
        response = self.client.delete('/api/v1/wahub/demo/contacts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['contacts'], 2)
        self.assertEqual(response.data['proposals'], 1)
        self.assertFalse(Contact.objects.filter(is_demo=True).exists())
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertEqual(KnowledgeProposal.objects.count(), 0)
        self.assertTrue(Contact.objects.filter(pk=real['id']).exists())

    def test_only_a_manager(self):
        worker = client_for(make_user('worker@wahub.test', role='worker'))
        self.assertEqual(worker.get('/api/v1/wahub/demo/scenarios/').status_code, 403)
        self.assertEqual(worker.post('/api/v1/wahub/demo/scenario/', {'scenario': 'loop'}, format='json').status_code, 403)
        self.assertEqual(worker.delete('/api/v1/wahub/demo/contacts/').status_code, 403)
