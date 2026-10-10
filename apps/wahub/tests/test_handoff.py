"""Taking a conversation over from the bot and handing it back: what is written in ManyChat, and when nothing is."""
from unittest.mock import patch

from django.test import override_settings

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.wahub.models import Contact, ContactEvent
from apps.wahub.tests.base import WahubTestCase

REQUEST = 'apps.core.manychat_service.ManyChatService._request'
START = 'התחיל טיפול: נציג אנושי'
END = 'סיים טיפול: נציג אנושי'
FIELD = 'סטטוס ט.אנושי'


def manychat(tags=(), fail_on=None, error=None, tags_listed=True):
    """A stand-in for ManyChat: answers getInfo with the tags, and refuses one path when asked to."""
    def respond(method, path, *, params=None, json_body=None, timeout=30):
        if fail_on and path.endswith(fail_on):
            raise error or ManyChatError('Validation error', status_code=400, payload={'details': 'Tag not found'})
        if path == '/fb/subscriber/getInfo':
            data = {'id': 4242, 'whatsapp_phone': '972505550101'}
            if tags_listed:
                data['tags'] = [{'id': index, 'name': name} for index, name in enumerate(tags)]
            return {'status': 'success', 'data': data}
        return {'status': 'success'}
    return respond


def writes(request):
    """The calls that changed something in ManyChat, as (path, body)."""
    return [(call.args[1], call.kwargs.get('json_body')) for call in request.call_args_list if call.args[0] == 'POST']


@override_settings(MANYCHAT_KEY='test-key')
class HandoffTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום', subscriber_id='4242')
        self.contact_id = self.contact().id

    def takeover(self):
        return self.post(f'contacts/{self.contact_id}/takeover/')

    def release(self):
        return self.post(f'contacts/{self.contact_id}/release/')

    def test_takeover_writes_the_three_things_the_bot_checks_in_order(self):
        with patch(REQUEST, side_effect=manychat(tags=[END, 'אחר'])) as request:
            response = self.takeover()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes(request), [
            ('/fb/subscriber/removeTagByName', {'subscriber_id': 4242, 'tag_name': END}),
            ('/fb/subscriber/setCustomFieldByName',
             {'subscriber_id': 4242, 'field_name': FIELD, 'field_value': 'בטיפול נציג (Kogo)'}),
            ('/fb/subscriber/addTagByName', {'subscriber_id': 4242, 'tag_name': START}),
        ])
        self.assertEqual(response.data['chat']['handled_by'], 'human')
        self.assertEqual(response.data['chat']['handled_by_label'], 'נציג עונה')
        event = ContactEvent.objects.get(kind='handled_by_changed')
        self.assertEqual(event.actor, self.manager)

    def test_the_end_tag_is_not_removed_when_it_is_not_there(self):
        with patch(REQUEST, side_effect=manychat(tags=['אחר'])) as request:
            self.takeover()
        self.assertEqual([path for path, _ in writes(request)], [
            '/fb/subscriber/setCustomFieldByName', '/fb/subscriber/addTagByName',
        ])

    def test_a_refusal_at_any_step_is_502_and_leaves_the_bot_answering_here(self):
        for step in ('getInfo', 'removeTagByName', 'setCustomFieldByName', 'addTagByName'):
            with self.subTest(step=step):
                with patch(REQUEST, side_effect=manychat(tags=[END], fail_on=step)):
                    response = self.takeover()
                self.assertEqual(response.status_code, 502)
                self.assertIn('ManyChat', response.data['detail'])
                self.assertEqual(Contact.objects.get(pk=self.contact_id).handled_by, 'bot')
                self.assertFalse(ContactEvent.objects.filter(kind='handled_by_changed').exists())

    def test_release_removes_the_start_tag(self):
        Contact.objects.update(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.release()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes(request), [('/fb/subscriber/removeTagByName', {'subscriber_id': 4242, 'tag_name': START})])
        self.assertEqual(response.data['chat']['handled_by'], 'bot')
        self.assertTrue(ContactEvent.objects.filter(kind='handled_by_changed').exists())

    def test_release_with_no_tag_to_remove_still_hands_back(self):
        Contact.objects.update(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags=[])) as request:
            response = self.release()
        self.assertEqual(writes(request), [])
        self.assertEqual(response.data['chat']['handled_by'], 'bot')

    def test_a_refused_release_is_502_and_the_person_keeps_the_conversation(self):
        Contact.objects.update(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags=[START], fail_on='removeTagByName')):
            response = self.release()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(Contact.objects.get(pk=self.contact_id).handled_by, 'human')

    def test_when_manychat_does_not_list_the_tags_a_nothing_to_remove_answer_is_not_a_failure(self):
        Contact.objects.update(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags_listed=False, fail_on='removeTagByName')) as request:
            response = self.release()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(writes(request)), 1)

        Contact.objects.update(handled_by='human')
        outage = ManyChatError('Server error', status_code=500)
        with patch(REQUEST, side_effect=manychat(tags_listed=False, fail_on='removeTagByName', error=outage)):
            self.assertEqual(self.release().status_code, 502)
        self.assertEqual(Contact.objects.get(pk=self.contact_id).handled_by, 'human')

    def test_the_contact_is_found_by_phone_when_it_is_not_remembered(self):
        Contact.objects.update(manychat_subscriber_id='')
        with patch.object(ManyChatService, 'lookup_or_create', return_value={'subscriber_id': 555}), \
                patch(REQUEST, side_effect=manychat()) as request:
            self.assertEqual(self.takeover().status_code, 200)
        self.assertEqual({body['subscriber_id'] for _, body in writes(request)}, {555})
        self.assertEqual(self.contact().manychat_subscriber_id, '555')

    @override_settings(
        WAHUB_HUMAN_START_TAG='נציג התחיל', WAHUB_HUMAN_END_TAG='נציג סיים',
        WAHUB_HUMAN_STATUS_FIELD='human_status', WAHUB_HUMAN_STATUS_VALUE='yes',
    )
    def test_the_names_are_settings(self):
        with patch(REQUEST, side_effect=manychat(tags=['נציג סיים'])) as request:
            self.takeover()
        self.assertEqual(writes(request), [
            ('/fb/subscriber/removeTagByName', {'subscriber_id': 4242, 'tag_name': 'נציג סיים'}),
            ('/fb/subscriber/setCustomFieldByName', {'subscriber_id': 4242, 'field_name': 'human_status', 'field_value': 'yes'}),
            ('/fb/subscriber/addTagByName', {'subscriber_id': 4242, 'tag_name': 'נציג התחיל'}),
        ])

    @override_settings(MANYCHAT_KEY='')
    def test_without_a_manychat_key_it_is_502(self):
        response = self.takeover()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(Contact.objects.get(pk=self.contact_id).handled_by, 'bot')
        self.network.assert_not_called()

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_in_simulation_only_the_local_state_changes(self):
        with patch(REQUEST) as request:
            self.assertEqual(self.takeover().data['chat']['handled_by'], 'human')
            self.assertEqual(self.release().data['chat']['handled_by'], 'bot')
        request.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(ContactEvent.objects.filter(kind='handled_by_changed').count(), 2)

    def test_taking_over_twice_writes_one_journal_line(self):
        with patch(REQUEST, side_effect=manychat()):
            self.takeover()
            self.takeover()
        self.assertEqual(ContactEvent.objects.filter(kind='handled_by_changed').count(), 1)
