"""Answering from the CRM: through ManyChat, mocked here; never out of the process."""
import inspect
from unittest.mock import patch

import requests
from django.test import override_settings

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.wahub import sending
from apps.wahub.models import Contact, Message
from apps.wahub.tests.base import WahubTestCase

REQUEST = 'apps.core.manychat_service.ManyChatService._request'
OK = {'status': 'success'}


@override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
class SendTextTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('מה המחיר?', subscriber_id='4242', name='רותם')
        self.incoming('ויש מקום?')
        self.contact_id = self.contact().id
        self.url = f'contacts/{self.contact_id}/send/'

    def test_a_reply_goes_out_through_manychat_and_settles_the_conversation(self):
        with patch(REQUEST, return_value=OK) as request:
            response = self.post(self.url, {'text': '  260 ₪ לחודש, ויש מקום  '})
        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with('POST', '/fb/sending/sendContent', json_body={
            'subscriber_id': 4242,
            'data': {'version': 'v2', 'content': {
                'type': 'whatsapp', 'messages': [{'type': 'text', 'text': '260 ₪ לחודש, ויש מקום'}],
            }},
        })
        message = response.data['message']
        self.assertEqual(message['status'], 'sent')
        self.assertEqual((message['direction'], message['sender'], message['sender_label']), ('out', 'office', 'משרד'))
        self.assertEqual(message['sender_name'], 'דנה מנהלת')
        self.assertEqual(message['error'], '')
        self.assertEqual(message['message_type'], 'text')

        chat = response.data['contact']['chat']
        self.assertEqual(chat['unread_count'], 0)
        self.assertIsNone(chat['waiting_since'])
        self.assertEqual(response.data['contact']['last_message']['text'], '260 ₪ לחודש, ויש מקום')
        self.assertEqual(response.data['contact']['last_message']['sender'], 'office')
        self.assertEqual(response.data['contact']['messages_count'], 3)

        stored = Message.objects.get(pk=message['id'])
        self.assertEqual((stored.source, stored.sent_by), ('kogo', self.manager))

    def test_a_refusal_is_a_failed_message_with_the_reason_and_changes_nothing_else(self):
        refusal = ManyChatError('Validation error', status_code=400, payload={
            'status': 'error', 'message': 'Validation error',
            'details': {'messages': [{'message': 'Subscriber is not subscribed to WhatsApp'}]},
        })
        with patch(REQUEST, side_effect=refusal):
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.status_code, 200)
        message = response.data['message']
        self.assertEqual(message['status'], 'failed')
        self.assertTrue(message['error'].startswith('ManyChat דחה את הפעולה: Validation error'))
        self.assertIn('not subscribed', message['error'])

        contact = response.data['contact']
        self.assertEqual(contact['chat']['unread_count'], 2)
        self.assertIsNotNone(contact['chat']['waiting_since'])
        self.assertEqual(contact['last_message']['text'], 'ויש מקום?')     # the preview is not replaced
        self.assertEqual(contact['messages_count'], 3)                      # the failed line is in the conversation

    def test_a_request_that_got_no_answer_says_so_and_is_not_repeated(self):
        with patch('apps.core.manychat_service.requests.request', side_effect=requests.ReadTimeout('timed out')) as http:
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(http.call_count, 1)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('לא ידוע אם ההודעה נשלחה', response.data['message']['error'])

    def test_no_connection_at_all(self):
        with patch('apps.core.manychat_service.requests.request', side_effect=requests.ConnectionError('refused')):
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('אין חיבור ל-ManyChat', response.data['message']['error'])

    def test_outside_the_24_hours_nothing_is_sent_or_kept(self):
        self.age(self.contact(), last_inbound_at=24 * 60 + 5)
        with patch(REQUEST) as request:
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'window_closed')
        self.assertIn('24', response.data['detail'])
        request.assert_not_called()
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)

    def test_a_contact_that_never_wrote_cannot_get_free_text(self):
        manual = self.make_contact(phone='972505550188')
        with patch(REQUEST) as request:
            response = self.post(f'contacts/{manual.id}/send/', {'text': 'שלום'})
        self.assertEqual(response.status_code, 409)
        request.assert_not_called()

    def test_text_is_required_and_bounded(self):
        with patch(REQUEST) as request:
            self.assertEqual(self.post(self.url, {'text': '   '}).status_code, 400)
            self.assertEqual(self.post(self.url, {}).status_code, 400)
            self.assertEqual(self.post(self.url, {'text': 'א' * 4097}).status_code, 400)
            request.assert_not_called()
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.post(self.url, {'text': 'א' * 4096}).data['message']['status'], 'sent')

    def test_without_a_remembered_contact_it_is_found_by_phone_and_remembered(self):
        Contact.objects.update(manychat_subscriber_id='')
        with patch.object(ManyChatService, 'lookup_or_create', return_value={'subscriber_id': 777}) as lookup, \
                patch(REQUEST, return_value=OK) as request:
            response = self.post(self.url, {'text': 'שלום'})
        lookup.assert_called_once_with('972505550101', 'רותם')
        self.assertEqual(request.call_args.kwargs['json_body']['subscriber_id'], 777)
        self.assertEqual(response.data['message']['status'], 'sent')
        self.assertEqual(self.contact().manychat_subscriber_id, '777')

    def test_a_contact_manychat_cannot_find_is_a_failed_message(self):
        Contact.objects.update(manychat_subscriber_id='')
        with patch.object(ManyChatService, 'lookup_or_create', side_effect=ManyChatError('מספר הטלפון אינו רשום ב-WhatsApp — לא ניתן לשלוח הודעה.')):
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertEqual(response.data['message']['error'], 'מספר הטלפון אינו רשום ב-WhatsApp — לא ניתן לשלוח הודעה.')

    def test_a_contact_manychat_deleted_is_looked_up_again_once(self):
        gone = ManyChatError('Validation error', status_code=400, payload={
            'message': 'Validation error', 'details': {'messages': [{'message': 'Subscriber does not exist'}]},
        })
        with patch.object(ManyChatService, 'lookup_or_create', return_value={'subscriber_id': 9001}) as lookup, \
                patch(REQUEST, side_effect=[gone, OK]) as request:
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.data['message']['status'], 'sent')
        lookup.assert_called_once()
        self.assertEqual([call.kwargs['json_body']['subscriber_id'] for call in request.call_args_list], [4242, 9001])
        self.assertEqual(self.contact().manychat_subscriber_id, '9001')

    @override_settings(MANYCHAT_KEY='')
    def test_without_a_manychat_key_nothing_is_sent(self):
        response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('MANYCHAT_KEY', response.data['message']['error'])
        self.network.assert_not_called()
        self.assertFalse(self.get('status/').data['send_configured'])

    def test_an_unexpected_fault_is_still_a_failed_message(self):
        with patch(REQUEST, side_effect=RuntimeError('boom')):
            response = self.post(self.url, {'text': 'שלום'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['status'], 'failed')


@override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
class SimulationTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('מה המחיר?', subscriber_id='4242')
        self.url = f'contacts/{self.contact().id}/send/'

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_on_a_developers_machine_the_send_is_only_recorded(self):
        with patch(REQUEST) as request:
            response = self.post(self.url, {'text': 'תשובה לדוגמה'})
        request.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['status'], 'simulated')
        self.assertEqual(response.data['contact']['chat']['unread_count'], 0)
        self.assertIsNone(response.data['contact']['chat']['waiting_since'])
        self.assertTrue(self.get('status/').data['simulate_send'])

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_simulation_still_respects_the_24_hours(self):
        self.age(self.contact(), last_inbound_at=25 * 60)
        self.assertEqual(self.post(self.url, {'text': 'שלום'}).status_code, 409)

    @override_settings(DEBUG=False, WAHUB_SIMULATE_SEND=True)
    def test_outside_debug_the_setting_does_nothing_and_the_send_is_real(self):
        self.assertFalse(sending.simulate_send())
        with patch(REQUEST, return_value=OK) as request:
            response = self.post(self.url, {'text': 'שלום'})
        request.assert_called_once()
        self.assertEqual(response.data['message']['status'], 'sent')
        self.assertFalse(self.get('status/').data['simulate_send'])

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=False)
    def test_debug_alone_does_not_simulate(self):
        self.assertFalse(sending.simulate_send())

    def test_outside_debug_the_setting_is_not_even_looked_at(self):
        class Refusing:
            DEBUG = False

            def __getattr__(self, name):
                raise AssertionError(f'{name} was read with DEBUG off')

        with patch.object(sending, 'settings', Refusing()):
            self.assertFalse(sending.simulate_send())

    def test_the_settings_file_reads_it_only_under_debug(self):
        from config import settings as module

        self.assertIn(
            "WAHUB_SIMULATE_SEND = config('WAHUB_SIMULATE_SEND', default=False, cast=bool) if DEBUG else False",
            inspect.getsource(module),
        )


@override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
class SendFlowTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום', subscriber_id='4242')
        self.age(self.contact(), last_inbound_at=3 * 24 * 60)      # long outside the window
        self.url = f'contacts/{self.contact().id}/send-flow/'

    def test_a_template_goes_out_even_outside_the_window(self):
        with patch(REQUEST, return_value=OK) as request:
            response = self.post(self.url, {'automation_id': 'content20260101_123', 'automation_name': 'חזרה ללידים'})
        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with(
            'POST', '/fb/sending/sendFlow', json_body={'subscriber_id': 4242, 'flow_ns': 'content20260101_123'},
        )
        message = response.data['message']
        self.assertEqual((message['message_type'], message['text'], message['status']), ('template', 'חזרה ללידים', 'sent'))
        self.assertEqual(response.data['contact']['last_message']['text'], 'חזרה ללידים')
        self.assertIsNone(response.data['contact']['chat']['waiting_since'])

    def test_without_a_name_manychats_own_name_for_it_is_shown(self):
        def respond(method, path, **kwargs):
            if path == '/fb/page/getFlows':
                return {'status': 'success', 'data': {'flows': [
                    {'ns': 'content_other', 'name': 'אחר'}, {'ns': 'content20260101_123', 'name': 'חזרה ללידים'},
                ]}}
            return OK

        with patch(REQUEST, side_effect=respond):
            response = self.post(self.url, {'automation_id': 'content20260101_123'})
        self.assertEqual(response.data['message']['text'], 'חזרה ללידים')
        self.assertEqual(response.data['message']['status'], 'sent')

    def test_when_the_name_cannot_be_read_the_id_is_shown_and_the_send_still_goes(self):
        def respond(method, path, **kwargs):
            if path == '/fb/page/getFlows':
                raise ManyChatError('Server error', status_code=500)
            return OK

        with patch(REQUEST, side_effect=respond):
            response = self.post(self.url, {'automation_id': 'content20260101_123'})
        self.assertEqual(response.data['message']['text'], 'content20260101_123')
        self.assertEqual(response.data['message']['status'], 'sent')

    def test_a_refused_template_is_a_failed_message(self):
        with patch(REQUEST, side_effect=ManyChatError('Flow not found', status_code=400)):
            response = self.post(self.url, {'automation_id': 'content_gone', 'automation_name': 'תבנית שנמחקה'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('Flow not found', response.data['message']['error'])

    def test_one_of_kogos_own_kinds_is_sent_through_its_flow(self):
        with override_settings(MANYCHAT_TRIAL_AFTER_TEST_FLOW_NS='content_after_test'), \
                patch(REQUEST, return_value=OK) as request:
            response = self.post(self.url, {'automation_id': 'trial_after_test'})
        self.assertEqual(request.call_args.kwargs['json_body']['flow_ns'], 'content_after_test')
        self.assertEqual(response.data['message']['text'], 'אחרי שיעור ניסיון')

    def test_the_automation_is_required(self):
        with patch(REQUEST) as request:
            self.assertEqual(self.post(self.url, {}).status_code, 400)
            request.assert_not_called()

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_simulated(self):
        with patch(REQUEST) as request:
            response = self.post(self.url, {'automation_id': 'content20260101_123', 'automation_name': 'חזרה ללידים'})
        request.assert_not_called()
        self.assertEqual(response.data['message']['status'], 'simulated')
        self.assertEqual(response.data['message']['message_type'], 'template')


class SendingSwitchTests(WahubTestCase):
    """Until the owner turns sending on, nothing leaves and nothing pretends to."""

    def _contact(self):
        self.incoming('היי', subscriber_id='1001')  # opens the 24-hour window
        return self.contact()

    @override_settings(WAHUB_SENDING_ENABLED=False, WAHUB_SIMULATE_SEND=False, DEBUG=False)
    def test_with_the_switch_off_a_send_is_kept_as_failed_and_manychat_is_not_called(self):
        from unittest import mock
        contact = self._contact()
        with mock.patch('apps.wahub.sending.ManyChatService') as service:
            message = sending.send_text(contact, 'שלום', self.manager)
        service.assert_not_called()
        self.assertEqual(message.status, 'failed')
        self.assertIn('כבויה', message.error)

    @override_settings(WAHUB_SENDING_ENABLED=False, WAHUB_SIMULATE_SEND=False, DEBUG=False)
    def test_with_the_switch_off_the_status_says_so(self):
        self.assertFalse(self.get('status/').data['sending_enabled'])

    @override_settings(WAHUB_SENDING_ENABLED=True, WAHUB_SIMULATE_SEND=False, DEBUG=False)
    def test_with_the_switch_on_the_status_says_so(self):
        self.assertTrue(self.get('status/').data['sending_enabled'])
