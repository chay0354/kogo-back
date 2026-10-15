"""
The writing box, 15.10.2026: the two openers of a closed conversation, a template kept as what it says, a file
the office attaches (three steps, the browser uploading it itself), and ✓ saying goodbye before it closes.
ManyChat and the storage are fakes here; nothing leaves the process.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings
from django.utils import timezone

from apps.core.manychat_service import ManyChatError
from apps.wahub import office_templates, settings_store
from apps.wahub.media import BUCKET, OFFICE_READ_SECONDS
from apps.wahub.models import Contact, ContactEvent, Message, QuickReply
from apps.wahub.tests.base import WahubTestCase

REQUEST = 'apps.core.manychat_service.ManyChatService._request'
STORAGE_REQUEST = 'apps.wahub.media.requests.request'
OK = {'status': 'success'}
# Never the real project or the real key: a test must not be able to reach the owner's live storage.
STORAGE_URL = 'https://storage.test'
SERVICE_KEY = 'test-service-role-key'
SENDING_ON = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True, WAHUB_SIMULATE_SEND=False, DEBUG=False)

OPEN_WORDS = 'היי רותם 🙂 כאן סטודיו קוגומלו. חזרנו אליכם בהמשך לפנייה שלכם, אפשר לענות כאן ונמשיך מכאן'
CONTINUE_WORDS = 'היי רותם 🙂 ממשיכים את השיחה שלנו מקוגומלו. אפשר לענות להודעה הזאת ונמשיך מכאן'
GOODBYE = 'השיחה נסגרה. הבוט שלנו חזר לזמינות ואשמח לעזור בכל שאלה 🤖'


class FakeManyChat:
    """ManyChat as far as these sends talk to it: its automations, and every send recorded."""

    def __init__(self, flows=(), *, flows_error=None, send_error=None, refuse_picture=False):
        self.flows = list(flows)
        self.flows_error = flows_error
        self.send_error = send_error
        self.refuse_picture = refuse_picture
        self.calls = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((path, kwargs.get('json_body')))
        if path == '/fb/page/getFlows':
            if self.flows_error is not None:
                raise self.flows_error
            return {'status': 'success', 'data': {'flows': self.flows}}
        if path in ('/fb/sending/sendFlow', '/fb/sending/sendContent'):
            body = kwargs.get('json_body') or {}
            messages = ((body.get('data') or {}).get('content') or {}).get('messages') or []
            if self.refuse_picture and any(message.get('type') == 'image' for message in messages):
                raise ManyChatError('Validation error', status_code=400, payload={'status': 'error', 'message': 'Validation error'})
            if self.send_error is not None:
                raise self.send_error
            return OK
        raise AssertionError(f'unexpected ManyChat call {path}')

    def paths(self):
        return [path for path, _body in self.calls]

    def sends(self):
        return [body for path, body in self.calls if path.startswith('/fb/sending/')]


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class FakeStorage:
    """Supabase Storage as media.py talks to it here: an upload address, and a signed address of an object it holds."""

    def __init__(self, *, holds=True, bucket_missing=False, absolute=False):
        self.calls = []
        self.holds = holds
        self.bucket_missing = bucket_missing
        self.absolute = absolute

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert url.startswith(f'{STORAGE_URL}/storage/v1/'), url
        rest = url[len(f'{STORAGE_URL}/storage/v1/'):]
        if method == 'POST' and rest == 'bucket':
            self.bucket_missing = False
            return _response({'name': BUCKET})
        if method == 'POST' and rest.startswith(f'object/upload/sign/{BUCKET}/'):
            if self.bucket_missing:
                return _response({'statusCode': '404', 'error': 'Bucket not found', 'message': 'Bucket not found'}, 400)
            path = rest[len(f'object/upload/sign/{BUCKET}/'):]
            if self.absolute:
                return _response({'url': f'{STORAGE_URL}/storage/v1/object/upload/sign/{BUCKET}/{path}', 'token': 'up'})
            return _response({'url': f'/object/upload/sign/{BUCKET}/{path}?token=up'})
        if method == 'POST' and rest.startswith(f'object/sign/{BUCKET}/'):
            if not self.holds:
                return _response({'statusCode': '404', 'error': 'not_found', 'message': 'Object not found'}, 404)
            path = rest[len(f'object/sign/{BUCKET}/'):]
            return _response({'signedURL': f'/object/sign/{BUCKET}/{path}?token=read'})
        raise AssertionError(f'unexpected storage call {method} {url}')


class ComposerCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום, אפשר פרטים?', subscriber_id='4242', name='רותם כהן')
        self.contact_obj = self.contact()
        self.url = f'contacts/{self.contact_obj.id}/'

    def close_window(self):
        return self.age(self.contact(), last_inbound_at=24 * 60 + 5)

    def outbound(self):
        return list(Message.objects.filter(contact=self.contact_obj, direction='out').order_by('id'))


# --- 1. the two openers --------------------------------------------------------------------------------

@override_settings(**SENDING_ON)
class OpenersTests(ComposerCase):
    FLOWS = [
        {'ns': 'content_open', 'name': 'Open Chat'},
        {'ns': 'content_continue', 'name': 'chat_continue'},
        {'ns': 'content_other', 'name': 'after-test'},
    ]

    def opening(self, kind):
        return self.post(f'{self.url}send-opening/', {'kind': kind})

    def test_open_chat_leaves_at_once_outside_the_24_hours_and_is_kept_as_its_words(self):
        self.close_window()
        fake = FakeManyChat(self.FLOWS)
        with patch(REQUEST, side_effect=fake):
            response = self.opening('open')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(fake.sends(), [{'subscriber_id': 4242, 'flow_ns': 'content_open'}])
        message = response.data['message']
        self.assertEqual((message['status'], message['message_type'], message['sender']), ('sent', 'template', 'office'))
        self.assertEqual(message['text'], OPEN_WORDS)
        self.assertEqual(response.data['contact']['last_message']['text'], OPEN_WORDS)
        # The office wrote: the conversation is a person's from here on, as after any send.
        self.assertEqual(response.data['contact']['chat']['handled_by'], 'human')

    def test_chat_continue_goes_through_its_own_automation(self):
        self.close_window()
        fake = FakeManyChat(self.FLOWS)
        with patch(REQUEST, side_effect=fake):
            response = self.opening('continue')
        self.assertEqual(fake.sends(), [{'subscriber_id': 4242, 'flow_ns': 'content_continue'}])
        self.assertEqual(response.data['message']['text'], CONTINUE_WORDS)

    def test_the_name_is_found_whatever_the_capitals_and_the_spaces(self):
        for spelled in ('OPEN-CHAT', 'openchat', '  open   chat ', 'Open_Chat'):
            with self.subTest(spelled=spelled):
                settings_store.put(office_templates.FOUND_KEY, {})
                fake = FakeManyChat([{'ns': 'content_x', 'name': spelled}])
                with patch(REQUEST, side_effect=fake):
                    self.assertEqual(self.opening('open').status_code, 200)
                self.assertEqual(fake.sends()[-1]['flow_ns'], 'content_x')

    def test_no_name_no_first_name_and_no_space_before_it(self):
        Contact.objects.filter(pk=self.contact_obj.pk).update(name='')
        with patch(REQUEST, side_effect=FakeManyChat(self.FLOWS)):
            response = self.opening('open')
        self.assertEqual(response.data['message']['text'], 'היי 🙂 כאן סטודיו קוגומלו. חזרנו אליכם בהמשך לפנייה שלכם, אפשר לענות כאן ונמשיך מכאן')

    def test_a_template_manychat_does_not_have_is_said_and_nothing_is_sent_or_kept(self):
        fake = FakeManyChat([{'ns': 'content_other', 'name': 'after-test'}])
        with patch(REQUEST, side_effect=fake):
            response = self.opening('open')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'template_missing')
        self.assertEqual(response.data['detail'], 'חסרה ב-ManyChat תבנית בשם open chat')
        self.assertEqual(fake.sends(), [])
        self.assertEqual(self.outbound(), [])

    def test_two_automations_under_the_name_are_nobodys(self):
        fake = FakeManyChat([{'ns': 'a', 'name': 'chat continue'}, {'ns': 'b', 'name': 'Chat-Continue'}])
        with patch(REQUEST, side_effect=fake):
            response = self.opening('continue')
        self.assertEqual((response.status_code, response.data['code']), (409, 'template_missing'))
        self.assertIn('יותר מאוטומציה אחת', response.data['detail'])
        self.assertEqual(fake.sends(), [])

    def test_a_fresh_answer_is_not_asked_for_again_and_a_name_it_lacked_is(self):
        fake = FakeManyChat(self.FLOWS)
        with patch(REQUEST, side_effect=fake):
            self.opening('open')
            self.opening('open')
        self.assertEqual(fake.paths().count('/fb/page/getFlows'), 1)
        # The owner makes the automation a minute after a press that found none: the next press finds it.
        settings_store.put(office_templates.FOUND_KEY, {'at': timezone.now().isoformat(), 'flows': {}, 'twice': []})
        fake = FakeManyChat(self.FLOWS)
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.opening('open').status_code, 200)
        self.assertEqual(fake.paths(), ['/fb/page/getFlows', '/fb/sending/sendFlow'])

    def test_a_manychat_that_does_not_answer_with_nothing_kept_is_a_502_and_nothing_is_sent(self):
        fake = FakeManyChat(flows_error=ManyChatError('timeout'))
        with patch(REQUEST, side_effect=fake):
            response = self.opening('open')
        self.assertEqual((response.status_code, response.data['code']), (502, 'manychat_unreachable'))
        self.assertEqual(self.outbound(), [])

    def test_a_manychat_that_does_not_answer_leaves_the_last_answer_standing(self):
        stale = (timezone.now() - timedelta(hours=1)).isoformat()
        settings_store.put(office_templates.FOUND_KEY, {
            'at': stale, 'flows': {'open_chat': {'flow': 'content_open', 'name': 'open chat'}}, 'twice': [],
        })
        fake = FakeManyChat(flows_error=ManyChatError('timeout'))
        with patch(REQUEST, side_effect=fake):
            response = self.opening('open')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake.sends(), [{'subscriber_id': 4242, 'flow_ns': 'content_open'}])

    def test_a_refused_send_is_a_failed_line_with_the_reason(self):
        fake = FakeManyChat(self.FLOWS, send_error=ManyChatError('Validation error', status_code=400, payload={'message': 'Template not approved'}))
        with patch(REQUEST, side_effect=fake):
            response = self.opening('open')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('ManyChat דחה', response.data['message']['error'])
        self.assertEqual(response.data['contact']['chat']['handled_by'], 'bot')

    def test_an_unknown_kind_is_refused(self):
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            self.assertEqual(self.opening('hello').status_code, 400)
            self.assertEqual(self.post(f'{self.url}send-opening/', {}).status_code, 400)

    @override_settings(WAHUB_SENDING_ENABLED=False)
    def test_with_the_switch_off_manychat_is_not_asked_and_the_line_says_why(self):
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.opening('open')
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('השליחה ללקוחות כבויה', response.data['message']['error'])
        self.assertEqual(response.data['message']['text'], OPEN_WORDS)

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_on_a_developers_machine_it_is_only_recorded(self):
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.opening('continue')
        self.assertEqual((response.data['message']['status'], response.data['message']['text']), ('simulated', CONTINUE_WORDS))

    def test_a_demo_contact_gets_nothing_and_manychat_is_not_asked(self):
        Contact.objects.filter(pk=self.contact_obj.pk).update(is_demo=True)
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.opening('open')
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('דמו', response.data['message']['error'])

    def test_managers_only(self):
        from apps.core.models import UserProfile
        from apps.wahub.tests.base import client_for, make_user

        worker = client_for(make_user('worker@wahub.test', role=UserProfile.ROLE_WORKER))
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            for path in ('send-opening/', 'attachments/', 'send-media/'):
                response = worker.post(f'/api/v1/wahub/{self.url}{path}', {}, format='json')
                self.assertEqual(response.status_code, 403, path)


class FirstNameTests(WahubTestCase):
    def test_filled_with_the_first_word_or_left_out_with_its_space(self):
        fill = office_templates.fill_first_name
        self.assertEqual(fill('היי {{first_name}} 🙂 שלום', 'דנה כהן'), 'היי דנה 🙂 שלום')
        self.assertEqual(fill('היי {{ first_name }}, שלום', 'דנה'), 'היי דנה, שלום')
        self.assertEqual(fill('היי {{first_name}} 🙂 שלום', ''), 'היי 🙂 שלום')
        self.assertEqual(fill('היי {{first_name}}, שלום', '  '), 'היי, שלום')
        self.assertEqual(fill('{{first_name}}, שלום', None), 'שלום')


# --- 18. a template is kept as what it says ------------------------------------------------------------

@override_settings(**SENDING_ON)
class TemplateWordsTests(ComposerCase):
    def test_a_follow_ups_template_sent_by_hand_is_kept_as_its_sentence_not_its_name(self):
        settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {
            'at': timezone.now().isoformat(), 'ok': True,
            'flows': {'link_reminder_1': {'flow': 'content_l1', 'name': 'kogo_lead_link_1'}},
        })
        fake = FakeManyChat()
        with patch(REQUEST, side_effect=fake):
            response = self.post(f'{self.url}send-flow/', {'automation_id': 'content_l1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['message']['text'], 'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם לחוג או שמשהו נתקע בדרך')
        self.assertEqual(response.data['message']['message_type'], 'template')
        # The words are known: ManyChat is not asked for the automation's name.
        self.assertEqual(fake.paths(), ['/fb/sending/sendFlow'])

    def test_an_opener_sent_through_the_template_list_is_kept_as_its_words(self):
        settings_store.put(office_templates.FOUND_KEY, {
            'at': timezone.now().isoformat(), 'flows': {'chat_continue': {'flow': 'content_c', 'name': 'chat continue'}}, 'twice': [],
        })
        with patch(REQUEST, side_effect=FakeManyChat()):
            response = self.post(f'{self.url}send-flow/', {'automation_id': 'content_c'})
        self.assertEqual(response.data['message']['text'], CONTINUE_WORDS)

    def test_any_other_automation_is_kept_as_its_name_as_before(self):
        fake = FakeManyChat([{'ns': 'content_9', 'name': 'after-test'}])
        with patch(REQUEST, side_effect=fake):
            response = self.post(f'{self.url}send-flow/', {'automation_id': 'content_9'})
        self.assertEqual(response.data['message']['text'], 'after-test')

    def test_the_words_of_every_follow_up_template_are_its_sentence_without_variables(self):
        for key in ('link_reminder_1', 'will_check_1', 'trial_feedback_2', 'date_arrived_age_1', 'link_reminder_external_1'):
            with self.subTest(key=key):
                said = office_templates.lead_template_words(key)
                self.assertTrue(said)
                self.assertNotIn('{', said)
                self.assertNotIn('[', said)
        self.assertEqual(office_templates.lead_template_words('no_such_template_1'), '')


# --- 3 and 4. a file the office attaches ---------------------------------------------------------------

@override_settings(SUPABASE_URL=STORAGE_URL, SUPABASE_SERVICE_ROLE_KEY=SERVICE_KEY, **SENDING_ON)
class AttachmentTests(ComposerCase):
    def prepare(self, name='תמונה.jpg', content_type='image/jpeg', size=200_000, storage=None):
        storage = storage or FakeStorage()
        with patch(STORAGE_REQUEST, side_effect=storage):
            response = self.post(f'{self.url}attachments/', {'name': name, 'content_type': content_type, 'size': size})
        return response, storage

    def send(self, path, kind='image', caption='', storage=None, manychat=None, name=''):
        storage = storage or FakeStorage()
        manychat = manychat or FakeManyChat()
        with patch(STORAGE_REQUEST, side_effect=storage), patch(REQUEST, side_effect=manychat):
            response = self.post(f'{self.url}send-media/', {'path': path, 'kind': kind, 'caption': caption, 'name': name})
        return response, storage, manychat

    def test_step_one_hands_out_an_upload_address_for_this_contact_only(self):
        response, storage = self.prepare()
        self.assertEqual(response.status_code, 200, response.data)
        path = response.data['path']
        self.assertRegex(path, rf'^office/{self.contact_obj.id}/[0-9a-f-]{{36}}\.jpg$')
        self.assertEqual((response.data['kind'], response.data['content_type']), ('image', 'image/jpeg'))
        self.assertEqual(response.data['upload_url'], f'{STORAGE_URL}/storage/v1/object/upload/sign/{BUCKET}/{path}?token=up')
        # The key stays on the server: the storage is asked with it, the browser never gets it.
        method, url, kwargs = storage.calls[0]
        self.assertEqual((method, url), ('POST', f'{STORAGE_URL}/storage/v1/object/upload/sign/{BUCKET}/{path}'))
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {SERVICE_KEY}')
        self.assertNotIn(SERVICE_KEY, str(response.data))

    def test_an_address_with_its_token_apart_and_a_missing_bucket(self):
        response, storage = self.prepare(storage=FakeStorage(absolute=True, bucket_missing=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['upload_url'].endswith('?token=up'))
        self.assertIn(('POST', f'{STORAGE_URL}/storage/v1/bucket'), [(m, u) for m, u, _k in storage.calls])

    def test_the_kinds_and_their_ceilings(self):
        cases = [
            ('a.png', 'image/png', 5 * 1024 * 1024, 200, 'image'),
            ('a.webp', '', 1000, 200, 'image'),
            ('a.jpg', 'image/jpeg', 5 * 1024 * 1024 + 1, 400, None),
            ('clip.mp4', 'video/mp4', 16 * 1024 * 1024, 200, 'video'),
            ('clip.3gp', 'video/3gpp', 1000, 200, 'video'),
            ('clip.mp4', 'video/mp4', 16 * 1024 * 1024 + 1, 400, None),
            ('טופס.pdf', 'application/pdf', 25 * 1024 * 1024, 200, 'file'),
            ('a.docx', '', 1000, 200, 'file'),
            ('a.xlsx', '', 1000, 200, 'file'),
            ('a.pdf', 'application/pdf', 25 * 1024 * 1024 + 1, 400, None),
            ('a.heic', 'image/heic', 1000, 400, None),
            ('a.exe', 'application/octet-stream', 1000, 400, None),
            ('blob', 'image/png', 1000, 200, 'image'),
            ('a.png', 'image/png', 0, 400, None),
        ]
        for name, content_type, size, status_code, kind in cases:
            with self.subTest(name=name, size=size):
                response, _storage = self.prepare(name, content_type, size)
                self.assertEqual(response.status_code, status_code, response.data)
                if kind:
                    self.assertEqual(response.data['kind'], kind)
                else:
                    self.assertEqual(response.data['code'], 'bad_file')
        too_big, _ = self.prepare('clip.mp4', 'video/mp4', 17 * 1024 * 1024)
        self.assertIn('סרטון עד 16MB', too_big.data['detail'])

    def test_no_upload_address_outside_the_24_hours(self):
        self.close_window()
        response, storage = self.prepare()
        self.assertEqual((response.status_code, response.data['code']), (409, 'window_closed'))
        self.assertEqual(storage.calls, [])

    @override_settings(SUPABASE_SERVICE_ROLE_KEY='')
    def test_without_storage_it_says_so(self):
        response, _ = self.prepare()
        self.assertEqual((response.status_code, response.data['code']), (503, 'storage_not_configured'))

    def test_a_picture_leaves_as_a_picture_and_its_words_as_a_message_after_it(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, storage, manychat = self.send(path, caption='  זה מערך השיעור  ')
        self.assertEqual(response.status_code, 200, response.data)
        sign = storage.calls[0]
        self.assertEqual(sign[1], f'{STORAGE_URL}/storage/v1/object/sign/{BUCKET}/{path}')
        self.assertEqual(sign[2]['json'], {'expiresIn': OFFICE_READ_SECONDS})
        read_url = f'{STORAGE_URL}/storage/v1/object/sign/{BUCKET}/{path}?token=read'
        self.assertEqual(manychat.sends(), [{
            'subscriber_id': 4242,
            'data': {'version': 'v2', 'content': {'type': 'whatsapp', 'messages': [
                {'type': 'image', 'url': read_url},
                {'type': 'text', 'text': 'זה מערך השיעור'},
            ]}},
        }])
        message = response.data['message']
        self.assertEqual((message['status'], message['message_type'], message['text']), ('sent', 'image', 'זה מערך השיעור'))
        self.assertEqual((message['media_url'], message['has_media']), (path, True))
        self.assertEqual(response.data['contact']['chat']['handled_by'], 'human')
        # The bubble reads the file back through the same door as every kept file.
        with patch(STORAGE_REQUEST, side_effect=FakeStorage()):
            media = self.client.get(f'/api/v1/wahub/messages/{message["id"]}/media/')
        self.assertEqual(media.status_code, 200)
        self.assertIn(f'object/sign/{BUCKET}/{path}', media.data['url'])

    def test_a_picture_without_words_is_one_message_and_the_list_says_picture(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.png'
        response, _storage, manychat = self.send(path)
        self.assertEqual(len(manychat.sends()[0]['data']['content']['messages']), 1)
        self.assertEqual(response.data['message']['text'], '')
        self.assertEqual(response.data['contact']['last_message']['text'], '🖼️ תמונה')

    def test_a_video_and_a_document_leave_as_their_link_since_whatsapp_takes_no_other(self):
        for ext, kind, head in (('mp4', 'video', '🎬 סרטון'), ('pdf', 'file', '📎 קובץ')):
            with self.subTest(kind=kind):
                path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.{ext}'
                response, _storage, manychat = self.send(path, kind=kind, caption='לצפייה', name='הסבר.pdf' if kind == 'file' else '')
                messages = manychat.sends()[0]['data']['content']['messages']
                url = f'{STORAGE_URL}/storage/v1/object/sign/{BUCKET}/{path}?token=read'
                expected_head = f'{head}: הסבר.pdf' if kind == 'file' else head
                self.assertEqual(messages, [{'type': 'text', 'text': f'{expected_head}\n{url}'}, {'type': 'text', 'text': 'לצפייה'}])
                self.assertEqual((response.data['message']['message_type'], response.data['message']['media_url']), ('other', path))

    def test_a_picture_manychat_refuses_leaves_once_more_as_its_link(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, _storage, manychat = self.send(path, manychat=FakeManyChat(refuse_picture=True))
        self.assertEqual(response.data['message']['status'], 'sent')
        sends = manychat.sends()
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[1]['data']['content']['messages'][0]['text'].split('\n')[0], '🖼️ תמונה')

    def test_a_send_that_got_no_answer_is_not_tried_again(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        timeout = ManyChatError('timed out')
        timeout.__cause__ = requests.ReadTimeout('timed out')
        response, _storage, manychat = self.send(path, manychat=FakeManyChat(send_error=timeout))
        self.assertEqual(len(manychat.sends()), 1)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertIn('לא ידוע אם ההודעה נשלחה', response.data['message']['error'])

    def test_a_path_that_is_not_this_contacts_upload_is_refused_before_anything(self):
        other = self.make_contact(phone='972505550199')
        for path in (
            f'office/{other.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg',
            f'demo/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg',
            f'office/{self.contact_obj.id}/../x.jpg',
            f'office/{self.contact_obj.id}/0b5a8f3e.exe',
            '',
        ):
            with self.subTest(path=path):
                storage = FakeStorage()
                manychat = FakeManyChat()
                response, _s, _m = self.send(path, storage=storage, manychat=manychat)
                self.assertEqual((response.status_code, response.data['code']), (400, 'bad_path'))
                self.assertEqual((storage.calls, manychat.calls), ([], []))
        mismatch, _s, _m = self.send(f'office/{self.contact_obj.id}/0b5a8f3e-1c2d.pdf', kind='image')
        self.assertEqual(mismatch.status_code, 400)

    def test_a_file_that_was_never_uploaded_is_not_sent(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, _storage, manychat = self.send(path, storage=FakeStorage(holds=False))
        self.assertEqual((response.status_code, response.data['code']), (502, 'storage_failed'))
        self.assertEqual(manychat.calls, [])
        self.assertEqual(self.outbound(), [])

    def test_the_24_hours_hold_for_a_file_as_for_words(self):
        self.close_window()
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, storage, manychat = self.send(path)
        self.assertEqual((response.status_code, response.data['code']), (409, 'window_closed'))
        self.assertEqual((storage.calls, manychat.calls, self.outbound()), ([], [], []))

    def test_a_caption_is_bounded(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, _storage, manychat = self.send(path, caption='א' * 4097)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(manychat.calls, [])

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_on_a_developers_machine_the_file_is_only_recorded(self):
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, _storage, manychat = self.send(path, manychat=FakeManyChat())
        self.assertEqual(response.data['message']['status'], 'simulated')
        self.assertEqual(manychat.calls, [])

    def test_a_demo_contact_gets_nothing(self):
        Contact.objects.filter(pk=self.contact_obj.pk).update(is_demo=True)
        path = f'office/{self.contact_obj.id}/0b5a8f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b.jpg'
        response, _storage, manychat = self.send(path)
        self.assertEqual(response.data['message']['status'], 'failed')
        self.assertEqual(manychat.calls, [])


# --- 6. ✓ says goodbye first ---------------------------------------------------------------------------

@override_settings(**SENDING_ON)
class GoodbyeTests(ComposerCase):
    def setUp(self):
        super().setUp()
        QuickReply.objects.create(title='  סגירת שיחה ', text=GOODBYE)

    def close(self):
        return self.post(f'{self.url}close/')

    def test_the_goodbye_leaves_first_and_then_the_conversation_is_closed(self):
        fake = FakeManyChat()
        with patch(REQUEST, side_effect=fake):
            response = self.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['goodbye'], response.data['goodbye_reason']), ('sent', ''))
        self.assertEqual(fake.sends()[0]['data']['content']['messages'], [{'type': 'text', 'text': GOODBYE}])
        [line] = self.outbound()
        self.assertEqual((line.text, line.sender, line.sent_by, line.status), (GOODBYE, 'office', self.manager, 'sent'))
        # Closed: back to the bot, nobody waits, and the journal says so — after the goodbye, not instead of it.
        self.assertEqual((response.data['chat']['handled_by'], response.data['chat']['open']), ('bot', False))
        self.assertEqual(ContactEvent.objects.filter(contact=self.contact_obj, kind='conversation_closed').count(), 1)

    def test_its_first_name_is_filled(self):
        QuickReply.objects.all().delete()
        QuickReply.objects.create(title='סגירת שיחה', text='תודה {{first_name}}, השיחה נסגרה')
        fake = FakeManyChat()
        with patch(REQUEST, side_effect=fake):
            self.close()
        self.assertEqual(self.outbound()[0].text, 'תודה רותם, השיחה נסגרה')

    def test_a_goodbye_that_failed_still_closes_and_says_why(self):
        fake = FakeManyChat(send_error=ManyChatError('Validation error', status_code=400, payload={'message': 'Subscriber is not subscribed'}))
        with patch(REQUEST, side_effect=fake):
            response = self.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['goodbye'], 'failed')
        self.assertIn('ManyChat דחה', response.data['goodbye_reason'])
        self.assertFalse(response.data['chat']['open'])

    def test_outside_the_24_hours_nothing_is_sent_and_it_closes(self):
        self.close_window()
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.close()
        self.assertEqual((response.status_code, response.data['goodbye']), (200, 'skipped'))
        self.assertIn('24 שעות', response.data['goodbye_reason'])
        self.assertEqual(self.outbound(), [])

    @override_settings(WAHUB_SENDING_ENABLED=False)
    def test_with_the_switch_off_nothing_is_sent_and_it_closes(self):
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.close()
        self.assertEqual((response.status_code, response.data['goodbye']), (200, 'skipped'))
        self.assertIn('כבויה', response.data['goodbye_reason'])

    def test_without_the_ready_made_reply_it_closes_as_before(self):
        QuickReply.objects.all().delete()
        QuickReply.objects.create(title='סגירת שיחה מהירה', text='משהו אחר')
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.close()
        self.assertEqual((response.status_code, response.data['goodbye'], response.data['goodbye_reason']), (200, 'skipped', ''))
        self.assertEqual(self.outbound(), [])

    def test_a_second_press_does_not_say_it_twice(self):
        fake = FakeManyChat()
        with patch(REQUEST, side_effect=fake):
            self.close()
            again = self.close()
        self.assertEqual(again.data['goodbye'], 'skipped')
        self.assertEqual(len(fake.sends()), 1)

    def test_a_demo_contact_is_closed_and_gets_nothing(self):
        Contact.objects.filter(pk=self.contact_obj.pk).update(is_demo=True)
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.close()
        self.assertEqual((response.status_code, response.data['goodbye']), (200, 'skipped'))

    def test_when_the_conversation_cannot_be_handed_back_the_goodbye_is_still_said_in_the_answer(self):
        Contact.objects.filter(pk=self.contact_obj.pk).update(handled_by='human')
        fake = FakeManyChat()

        def manychat(method, path, **kwargs):
            if 'Tag' in path or 'subscriber/getInfo' in path:
                raise ManyChatError('boom', status_code=500)
            return fake(method, path, **kwargs)

        with patch(REQUEST, side_effect=manychat):
            response = self.close()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data['goodbye'], 'sent')


# --- 7. "השב": the quote is the message's, the preview is the answer's ----------------------------------

@override_settings(**SENDING_ON)
class ReplyQuoteTests(ComposerCase):
    def test_a_reply_goes_out_with_its_quote_and_the_list_previews_the_answer(self):
        text = '> שלום, אפשר פרטים?\n\n260 ₪ לחודש, ויש מקום'
        fake = FakeManyChat()
        with patch(REQUEST, side_effect=fake):
            response = self.post(f'{self.url}send/', {'text': text})
        self.assertEqual(fake.sends()[0]['data']['content']['messages'], [{'type': 'text', 'text': text}])
        self.assertEqual(response.data['message']['text'], text)
        self.assertEqual(response.data['contact']['last_message']['text'], '260 ₪ לחודש, ויש מקום')

    def test_words_that_only_look_like_a_quote_are_previewed_as_they_are(self):
        from apps.wahub.sending import _without_quote

        self.assertEqual(_without_quote('>לא ציטוט'), '>לא ציטוט')
        self.assertEqual(_without_quote('> רק ציטוט'), '> רק ציטוט')
        self.assertEqual(_without_quote('שלום\n> באמצע'), 'שלום\n> באמצע')
        self.assertEqual(_without_quote('> א\n> ב\nג'), 'ג')
