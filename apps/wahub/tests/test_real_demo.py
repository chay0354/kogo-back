"""
A real conversation with a demo contact (docs/WAHUB-CONTRACT-REAL-DEMO.md, א ב ד):
a recording or a picture kept in the private bucket (storage behind a fake), the
new bot answering NOW inside the conversation, a picture reaching Claude as an
image block, a recording transcribed (or not, saying why) — and nothing ever sent.
"""
import base64
import json
import re
from unittest.mock import MagicMock, patch

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.wahub import knowledge_import, reviewer, shadow, transcribe
from apps.wahub.media import BUCKET, IMAGE_MAX_BYTES, SIGNED_URL_SECONDS
from apps.wahub.models import Contact, KnowledgeProposal, Message, ShadowReply
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user
from apps.wahub.tests.test_shadow import catalogue

# Never the real project or the real key: a test must not be able to reach the owner's live storage.
STORAGE_URL = 'https://storage.test'
SERVICE_KEY = 'test-service-role-key'
STORAGE_REQUEST = 'apps.wahub.media.requests.request'
CLAUDE_POST = 'apps.wahub.shadow.requests.post'
OPENAI_POST = 'apps.wahub.transcribe.requests.post'
SEND = 'apps.wahub.sending._deliver'

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 200 + b'\xff\xd9'
WEBM = b'\x1aE\xdf\xa3' + b'\x00' * 300
VOICE_RULE = 'לא ניתן לשמוע הודעות קוליות, בבקשה להשאיר הודעה כתובה'
PATH = re.compile(r'^demo/(\d+)/[0-9a-f-]{36}\.(jpg|webm)$')


def _json_response(payload, status_code=200, content=b''):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.content = content
    return response


class FakeStorage:
    """
    Supabase Storage, as far as apps/wahub/media.py talks to it: uploads are
    kept by path, a sign call answers a token address, a read serves the bytes.
    `bucket_missing` makes the first upload answer "Bucket not found" (HTTP 400, as Supabase does) until the bucket is made.
    """

    def __init__(self, *, bucket_missing=False, upload_status=200):
        self.objects = {}
        self.calls = []
        self.bucket_missing = bucket_missing
        self.upload_status = upload_status

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert url.startswith(f'{STORAGE_URL}/storage/v1/'), url
        rest = url[len(f'{STORAGE_URL}/storage/v1/'):]
        if method == 'POST' and rest == 'bucket':
            self.bucket_missing = False
            return _json_response({'name': BUCKET})
        if method == 'POST' and rest.startswith(f'object/sign/{BUCKET}/'):
            path = rest[len(f'object/sign/{BUCKET}/'):]
            return _json_response({'signedURL': f'/object/sign/{BUCKET}/{path}?token=abc'})
        if method == 'POST' and rest.startswith(f'object/{BUCKET}/'):
            if self.bucket_missing:
                # Supabase Storage answers a missing bucket with HTTP 400; the 404 is in the body.
                return _json_response({'statusCode': '404', 'error': 'Bucket not found', 'message': 'Bucket not found'}, 400)
            if self.upload_status != 200:
                return _json_response({'message': 'nope'}, self.upload_status)
            path = rest[len(f'object/{BUCKET}/'):]
            self.objects[path] = (kwargs.get('data'), kwargs['headers'].get('Content-Type'))
            return _json_response({'Key': f'{BUCKET}/{path}'})
        if method == 'GET' and rest.startswith(f'object/authenticated/{BUCKET}/'):
            path = rest[len(f'object/authenticated/{BUCKET}/'):]
            if path not in self.objects:
                return _json_response({'message': 'Object not found'}, 404)
            return _json_response({}, content=self.objects[path][0])
        raise AssertionError(f'unexpected storage call {method} {url}')

    def uploads(self):
        return [call for call in self.calls if call[0] == 'POST' and f'/object/{BUCKET}/' in call[1]]


@override_settings(SUPABASE_URL=STORAGE_URL, SUPABASE_SERVICE_ROLE_KEY=SERVICE_KEY, ANTHROPIC_API_KEY='', OPENAI_API_KEY='')
class RealDemoTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.storage = FakeStorage()
        patch(STORAGE_REQUEST, side_effect=self.storage).start()
        # Nothing here may send, ever.
        self.deliver = patch(SEND, side_effect=AssertionError('a demo conversation tried to send')).start()
        self.addCleanup(patch.stopall)
        self.demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו ראשון', 'is_demo': True}).data
        self.contact_id = self.demo['id']

    def inbound_url(self, contact_id=None):
        return f'{BASE}/contacts/{contact_id or self.contact_id}/simulate-inbound/'

    def upload(self, message_type, *, name=None, content=None, content_type=None, text='', contact_id=None):
        if message_type == 'image':
            file = SimpleUploadedFile(name or 'photo.jpg', content if content is not None else JPEG, content_type=content_type or 'image/jpeg')
        else:
            file = SimpleUploadedFile(name or 'voice.webm', content if content is not None else WEBM, content_type=content_type or 'audio/webm;codecs=opus')
        data = {'message_type': message_type, 'file': file}
        if text:
            data['text'] = text
        return self.client.post(self.inbound_url(contact_id), data, format='multipart')

    def say(self, text, contact_id=None):
        return self.client.post(self.inbound_url(contact_id), {'text': text}, format='json')

    def reply(self, body=None, contact_id=None):
        return self.client.post(f'{BASE}/demo/contacts/{contact_id or self.contact_id}/reply/', body or {}, format='json')


class DemoMediaUploadTests(RealDemoTestCase):
    def test_a_picture_and_a_recording_are_kept_in_the_private_bucket_under_the_contact(self):
        response = self.upload('image', text='זה הבן שלי, יש חוג בשבילו?')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['stored'])
        message = Message.objects.get(pk=response.data['stored_message_id'])
        self.assertEqual((message.message_type, message.text, message.direction, message.sender), ('image', 'זה הבן שלי, יש חוג בשבילו?', 'in', 'customer'))
        match = PATH.match(message.media_url)
        self.assertIsNotNone(match, message.media_url)
        self.assertEqual((match.group(1), match.group(2)), (str(self.contact_id), 'jpg'))
        self.assertEqual(response.data['message']['has_media'], True)
        self.assertEqual(response.data['message']['media_url'], message.media_url)
        self.assertEqual(response.data['message']['transcript'], '')
        self.assertEqual(response.data['last_message']['text'], 'זה הבן שלי, יש חוג בשבילו?')
        self.assertTrue(Contact.objects.get(pk=self.contact_id).needs_shadow)

        # The storage got the bytes, typed, at the object's path, with the service key in a header and nowhere else.
        [(method, url, kwargs)] = self.storage.uploads()
        self.assertEqual(url, f'{STORAGE_URL}/storage/v1/object/{BUCKET}/{message.media_url}')
        self.assertEqual(kwargs['data'], JPEG)
        self.assertEqual(kwargs['headers']['Content-Type'], 'image/jpeg')
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {SERVICE_KEY}')
        self.assertNotIn(SERVICE_KEY, url)
        self.assertEqual(kwargs['headers']['x-upsert'], 'false')

        voice = self.upload('voice')
        self.assertEqual(voice.status_code, 200, voice.data)
        recording = Message.objects.get(pk=voice.data['stored_message_id'])
        self.assertEqual((recording.message_type, recording.text), ('voice', ''))
        self.assertTrue(recording.media_url.endswith('.webm'))
        self.assertEqual(self.storage.objects[recording.media_url], (WEBM, 'audio/webm'))
        self.assertEqual(voice.data['last_message']['text'], '🎤 הודעה קולית')
        self.assertEqual(voice.data['messages_count'], 2)
        self.assertEqual(voice.data['chat']['unread_count'], 2)
        self.assertEqual(self.network.call_count, 0)

    def test_safari_names_its_recording_by_type_and_a_phone_picture_by_extension(self):
        response = self.upload('voice', name='blob', content_type='audio/mp4')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Message.objects.get(pk=response.data['stored_message_id']).media_url.endswith('.m4a'))
        response = self.upload('image', name='IMG_0001.HEIC', content_type='application/octet-stream')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Message.objects.get(pk=response.data['stored_message_id']).media_url.endswith('.heic'))

    def test_what_is_refused_never_reaches_the_storage(self):
        plain = self.post('contacts/', {'phone': '050-5559002', 'name': 'רגיל'}).data
        refused = self.upload('image', contact_id=plain['id'])
        self.assertEqual((refused.status_code, refused.data['code']), (400, 'not_demo'))

        no_file = self.client.post(self.inbound_url(), {'message_type': 'image'}, format='multipart')
        self.assertEqual((no_file.status_code, no_file.data['code']), (400, 'bad_file'))
        wrong_kind = self.client.post(self.inbound_url(), {'message_type': 'video', 'text': 'x'}, format='multipart')
        self.assertEqual(wrong_kind.status_code, 400)
        pdf = self.upload('image', name='scan.pdf', content_type='application/pdf')
        self.assertEqual((pdf.status_code, pdf.data['code']), (400, 'bad_file'))
        self.assertIn('jpg, png, webp או heic', pdf.data['detail'])
        exe_as_voice = self.upload('voice', name='song.wav', content_type='audio/wav')
        self.assertEqual(exe_as_voice.status_code, 400)
        self.assertIn('webm, ogg, m4a, mp3 או aac', exe_as_voice.data['detail'])
        huge = self.upload('image', content=b'\xff' * (IMAGE_MAX_BYTES + 1))
        self.assertEqual((huge.status_code, huge.data['code']), (400, 'bad_file'))
        self.assertIn('5MB', huge.data['detail'])
        empty = self.upload('voice', content=b'')
        self.assertEqual(empty.status_code, 400)
        as_bot = self.client.post(
            self.inbound_url(), {'message_type': 'image', 'sender': 'bot', 'file': SimpleUploadedFile('p.jpg', JPEG, content_type='image/jpeg')},
            format='multipart',
        )
        self.assertEqual(as_bot.status_code, 400)

        self.assertEqual(self.storage.calls, [])
        self.assertEqual(Message.objects.count(), 0)
        # Text through the same door still works as before.
        self.assertTrue(self.say('שלום').data['stored'])

    def test_a_storage_that_is_not_configured_or_refuses(self):
        with override_settings(SUPABASE_SERVICE_ROLE_KEY=''):
            response = self.upload('image')
        self.assertEqual((response.status_code, response.data['code']), (503, 'storage_not_configured'))
        self.storage.upload_status = 500
        response = self.upload('image')
        self.assertEqual((response.status_code, response.data['code']), (502, 'storage_failed'))
        self.assertEqual(Message.objects.count(), 0)
        with patch(STORAGE_REQUEST, side_effect=requests.ConnectionError('down')):
            response = self.upload('image')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(Message.objects.count(), 0)

    def test_the_bucket_is_made_on_the_first_upload_when_it_is_missing(self):
        self.storage.bucket_missing = True
        response = self.upload('image')
        self.assertEqual(response.status_code, 200, response.data)
        kinds = [(method, url.split('/storage/v1/', 1)[1].split('/')[0]) for method, url, _ in self.storage.calls]
        self.assertEqual(kinds, [('POST', 'object'), ('POST', 'bucket'), ('POST', 'object')])
        created = self.storage.calls[1][2]['json']
        self.assertEqual(created, {'id': BUCKET, 'name': BUCKET, 'public': False})
        self.assertEqual(len(self.storage.objects), 1)

    def test_two_pictures_a_moment_apart_are_two_messages_but_a_repeated_text_is_still_one(self):
        self.upload('image')
        self.upload('image')
        self.assertEqual(Message.objects.filter(message_type='image').count(), 2)
        self.assertTrue(self.say('שלום').data['stored'])
        self.assertFalse(self.say('שלום').data['stored'])
        self.assertEqual(Message.objects.count(), 3)

    def test_a_messages_media_is_served_as_a_short_signed_address(self):
        picture = Message.objects.get(pk=self.upload('image').data['stored_message_id'])
        response = self.client.get(f'{BASE}/messages/{picture.id}/media/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {
            'url': f'{STORAGE_URL}/storage/v1/object/sign/{BUCKET}/{picture.media_url}?token=abc',
            'expires_in': SIGNED_URL_SECONDS,
        })
        sign = [call for call in self.storage.calls if '/object/sign/' in call[1]]
        self.assertEqual(sign[0][2]['json'], {'expiresIn': SIGNED_URL_SECONDS})

        text = Message.objects.get(pk=self.say('שלום').data['stored_message_id'])
        self.assertEqual(self.client.get(f'{BASE}/messages/{text.id}/media/').status_code, 404)
        self.assertEqual(self.client.get(f'{BASE}/messages/999999/media/').status_code, 404)
        worker = client_for(make_user('worker@wahub.test', role='worker'))
        self.assertEqual(worker.get(f'{BASE}/messages/{picture.id}/media/').status_code, 403)
        with override_settings(SUPABASE_URL=''):
            self.assertEqual(self.client.get(f'{BASE}/messages/{picture.id}/media/').status_code, 503)


class DemoReplyTests(RealDemoTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()

    def test_the_new_bot_answers_a_burst_once_and_writes_it_into_the_conversation_only_with_post(self):
        self.say('היי')
        self.say('יש חוג קפוארה בראש העין לבן 6?')
        ids = list(Message.objects.order_by('id').values_list('id', flat=True))

        response = self.reply({'post': False})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['shadow']['covers_message_ids'], ids)
        self.assertIn('שלישי', response.data['shadow']['text'])
        self.assertEqual(response.data['shadow']['model'], 'stub')
        self.assertIsNone(response.data['message'])
        self.assertEqual(Message.objects.count(), 2)                   # nothing written without post
        self.assertEqual(ShadowReply.objects.count(), 1)
        self.assertFalse(Contact.objects.get(pk=self.contact_id).needs_shadow)

        again = self.reply()
        self.assertEqual((again.status_code, again.data['shadow'], again.data['message']), (200, None, None))
        self.assertEqual(ShadowReply.objects.count(), 1)              # the burst was answered once

        self.say('וכמה זה עולה?')
        posted = self.reply()
        self.assertEqual(posted.status_code, 200, posted.data)
        message = posted.data['message']
        self.assertEqual((message['direction'], message['sender'], message['sender_label']), ('out', 'bot', 'בוט'))
        self.assertTrue(message['new_bot'])
        self.assertEqual((message['status'], message['error']), ('simulated', ''))
        self.assertEqual(message['text'], posted.data['shadow']['text'])
        self.assertIn('₪260', message['text'])
        stored = Message.objects.get(pk=message['id'])
        self.assertEqual((stored.source, stored.new_bot, stored.sent_by), ('kogo', True, None))
        contact = posted.data['contact']
        # The list's preview is the text on one line (state.preview), as for every other message.
        self.assertEqual((contact['last_message']['sender'], contact['last_message']['text']), ('bot', re.sub(r'\s+', ' ', message['text'])))
        self.assertIsNone(contact['chat']['waiting_since'])
        self.assertEqual(contact['chat']['unread_count'], 3)             # the bot's answer reads nothing for the office
        self.assertEqual(contact['messages_count'], 4)
        self.assertEqual(ShadowReply.objects.count(), 2)

        # The new bot's own line is not read as the old bot's answer, and the screen gets it as a message.
        rows = self.get(f'contacts/{self.contact_id}/shadow/').data
        self.assertEqual([row['old_bot_reply'] for row in rows], [None, None])
        listed = self.get(f'contacts/{self.contact_id}/').data['messages']
        self.assertEqual([(row['sender'], row['new_bot']) for row in listed], [('customer', False)] * 3 + [('bot', True)])

        # The next answer sees its own earlier one in the context (not a failed line).
        self.say('תודה')
        self.assertEqual(self.reply().status_code, 200)
        self.deliver.assert_not_called()
        self.assertEqual(self.network.call_count, 0)

    def test_only_a_demo_contact_and_only_a_manager(self):
        plain = self.post('contacts/', {'phone': '050-5559002', 'name': 'רגיל'}).data
        self.say('היי', contact_id=plain['id'])
        self.assertEqual(self.say('היי', contact_id=plain['id']).data['code'], 'not_demo')
        self.incoming('היי', phone='972505559002')
        refused = self.reply(contact_id=plain['id'])
        self.assertEqual((refused.status_code, refused.data['code']), (400, 'not_demo'))
        self.assertEqual(self.reply(contact_id=999999).status_code, 404)
        self.say('היי')
        self.assertEqual(self.reply({'post': 'maybe'}).status_code, 400)
        worker = client_for(make_user('worker@wahub.test', role='worker'))
        self.assertEqual(worker.post(f'{BASE}/demo/contacts/{self.contact_id}/reply/', {}, format='json').status_code, 403)
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)

    def test_a_recording_without_a_transcription_key_gets_the_voice_rule_and_says_why(self):
        recording = Message.objects.get(pk=self.upload('voice').data['stored_message_id'])
        response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        shadow = response.data['shadow']
        self.assertEqual(shadow['text'], VOICE_RULE)
        self.assertIn(transcribe.NO_KEY_REASON, shadow['reasoning'])
        self.assertEqual(response.data['message']['text'], VOICE_RULE)
        recording.refresh_from_db()
        self.assertEqual(recording.transcript, '')
        # The reviewer hunts the OLD bot's "cannot hear" line; the new bot's own answer is not it.
        counts = reviewer.review_contact(Contact.objects.get(pk=self.contact_id))
        self.assertEqual((counts['proposals'], counts['patterns']), (0, {}))
        self.assertEqual(KnowledgeProposal.objects.count(), 0)
        self.assertEqual(self.network.call_count, 0)

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_recording_is_transcribed_and_answered_on_its_transcript(self):
        recording = Message.objects.get(pk=self.upload('voice').data['stored_message_id'])
        with patch(OPENAI_POST, return_value=_json_response({'text': 'יש חוג קפוארה בראש העין לבן 6?'})) as post:
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('שלישי', response.data['shadow']['text'])
        self.assertNotIn(transcribe.NO_KEY_REASON, response.data['shadow']['reasoning'])
        recording.refresh_from_db()
        self.assertEqual(recording.transcript, 'יש חוג קפוארה בראש העין לבן 6?')
        listed = self.get(f'contacts/{self.contact_id}/').data['messages']
        self.assertEqual((listed[0]['message_type'], listed[0]['transcript']), ('voice', 'יש חוג קפוארה בראש העין לבן 6?'))

        self.assertEqual(post.call_count, 1)
        call = post.call_args
        self.assertEqual(call.args[0], transcribe.TRANSCRIPTIONS_URL)
        self.assertEqual(call.kwargs['headers'], {'Authorization': 'Bearer openai-test-key'})
        self.assertEqual(call.kwargs['data'], {'model': 'gpt-4o-mini-transcribe', 'language': 'he', 'response_format': 'json'})
        filename, audio, media_type = call.kwargs['files']['file']
        self.assertEqual((audio, media_type), (WEBM, 'audio/webm'))
        self.assertTrue(filename.endswith('.webm'))
        # Read back from the storage, not from the request that uploaded it.
        self.assertTrue(any(method == 'GET' and f'/object/authenticated/{BUCKET}/{recording.media_url}' in url for method, url, _ in self.storage.calls))

        # Transcribed once: the next answer does not ask again.
        self.say('ומה המחיר?')
        with patch(OPENAI_POST) as post:
            self.assertEqual(self.reply().status_code, 200)
        post.assert_not_called()

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_failed_transcription_is_written_on_the_recording_and_the_bot_answers_by_its_voice_rule(self):
        self.upload('voice')
        with patch(OPENAI_POST, return_value=_json_response({'error': {'message': 'boom'}}, 500)):
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        # The customer is not left without an answer because of a recording nobody could hear.
        self.assertIn(VOICE_RULE, response.data['shadow']['text'])
        self.assertIn('התמלול נכשל (התמלול נכשל (500))', response.data['shadow']['reasoning'])
        recording = Message.objects.get(direction='in')
        self.assertEqual((recording.transcript, recording.error), ('', 'התמלול נכשל (500)'))
        self.assertEqual(ShadowReply.objects.count(), 1)
        self.assertEqual(self.get('status/').data['transcribe'], {
            'configured': True, 'ok': False, 'reason': 'התמלול נכשל (500)', 'at': self.get('status/').data['transcribe']['at'],
        })

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_an_account_with_no_credit_is_said_in_words_and_the_recording_is_heard_once_there_is_credit(self):
        self.upload('voice')
        no_credit = _json_response({'error': {
            'message': 'You have no credits remaining. Add credits to continue using the API at https://platform.openai.com/settings/organization/billing/.',
            'type': 'insufficient_quota', 'code': 'insufficient_quota',
        }}, 429)
        with patch(OPENAI_POST, return_value=no_credit):
            self.assertEqual(self.reply().status_code, 200)
        recording = Message.objects.get(direction='in')
        self.assertEqual(recording.error, transcribe.NO_CREDIT_REASON)
        health = self.get('status/').data['transcribe']
        self.assertEqual((health['configured'], health['ok'], health['reason']), (True, False, transcribe.NO_CREDIT_REASON))
        # A plain rate limit is not "no credit".
        with patch(OPENAI_POST, return_value=_json_response({'error': {'message': 'Rate limit reached', 'type': 'requests', 'code': 'rate_limit_exceeded'}}, 429)):
            with self.assertRaises(transcribe.TranscriptionError) as raised:
                transcribe.transcribe(WEBM, filename='voice.webm', media_type='audio/webm')
        self.assertEqual(str(raised.exception), 'התמלול נכשל (429)')
        Message.objects.filter(pk=recording.pk).update(error=transcribe.NO_CREDIT_REASON)

        # The cron tries it again while there is still no credit: one call, the reason stays.
        with patch(OPENAI_POST, return_value=no_credit) as post:
            self.assertEqual(shadow.hear_again(), 0)
        post.assert_called_once()
        # He added credit: the next look hears it, and the conversation shows what was said.
        with patch(OPENAI_POST, return_value=_json_response({'text': 'יש חוג קפוארה לבן 6?'})):
            self.assertEqual(shadow.hear_again(), 1)
        recording.refresh_from_db()
        self.assertEqual((recording.transcript, recording.error), ('יש חוג קפוארה לבן 6?', ''))
        health = self.get('status/').data['transcribe']
        self.assertEqual((health['ok'], health['reason']), (True, ''))
        # Nothing was answered from there: the bot's one answer is the one it gave by its voice rule.
        self.assertEqual(ShadowReply.objects.count(), 1)
        with patch(OPENAI_POST) as post:
            self.assertEqual(shadow.hear_again(), 0)                     # nothing left to hear
        post.assert_not_called()

    def test_without_a_key_the_recording_says_why_and_the_settings_screen_too(self):
        self.upload('voice')
        self.assertEqual(self.reply().status_code, 200)
        self.assertEqual(Message.objects.get(direction='in').error, transcribe.NO_KEY_REASON)
        self.assertEqual(self.get('status/').data['transcribe'], {'configured': False, 'ok': False, 'reason': transcribe.NO_KEY_REASON, 'at': None})

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_transcription_that_does_not_answer_is_a_reason_on_the_recording_too(self):
        self.upload('voice')
        with patch(OPENAI_POST, side_effect=requests.ReadTimeout('slow')):
            response = self.reply()
        self.assertIn(VOICE_RULE, response.data['shadow']['text'])
        self.assertEqual(Message.objects.get(message_type='voice').error, 'התמלול לא ענה (ReadTimeout)')
        # The cron's second look hears it once the service answers.
        with patch(OPENAI_POST, return_value=_json_response({'text': 'היי'})):
            self.assertEqual(shadow.hear_again(), 1)
        self.assertEqual(Message.objects.get(message_type='voice').transcript, 'היי')

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_recording_from_before_the_reason_was_kept_is_tried_too_and_an_empty_one_is_left_alone(self):
        self.upload('voice')
        recording = Message.objects.get(message_type='voice')
        self.assertEqual((recording.transcript, recording.error), ('', ''))   # never tried, nothing written on it
        no_credit = _json_response({'error': {'message': 'You have no credits remaining.', 'type': 'insufficient_quota'}}, 429)
        with patch(OPENAI_POST, return_value=no_credit):
            self.assertEqual(shadow.hear_again(), 0)
        recording.refresh_from_db()
        self.assertEqual(recording.error, transcribe.NO_CREDIT_REASON)       # now the conversation says why
        self.assertFalse(self.get('status/').data['transcribe']['ok'])
        # A recording the service heard nothing in is not asked about every five minutes.
        with patch(OPENAI_POST, return_value=_json_response({'text': ''})):
            self.assertEqual(shadow.hear_again(), 0)
        recording.refresh_from_db()
        self.assertEqual(recording.error, shadow.EMPTY_TRANSCRIPT)
        with patch(OPENAI_POST) as post:
            self.assertEqual(shadow.hear_again(), 0)
        post.assert_not_called()


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class DemoReplyClaudeTests(RealDemoTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()

    def _answer(self, text, reasoning='ראיתי את התמונה'):
        return _json_response({
            'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps({'text': text, 'reasoning': reasoning, 'knowledge_ids': []}, ensure_ascii=False)}],
        })

    def test_a_picture_reaches_claude_as_an_image_block_with_its_caption(self):
        picture = Message.objects.get(pk=self.upload('image', text='זה הבן שלי, יש חוג בשבילו?').data['stored_message_id'])
        with patch(CLAUDE_POST, return_value=self._answer('איזה חמוד! בן כמה הוא ובאיזו עיר אתם?')) as post:
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['shadow']['model'], 'claude-opus-5-5')
        self.assertEqual(response.data['message']['text'], 'איזה חמוד! בן כמה הוא ובאיזו עיר אתם?')
        self.assertTrue(response.data['message']['new_bot'])

        content = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIsInstance(content, list)
        self.assertEqual([block['type'] for block in content], ['text', 'image'])
        self.assertIn('הלקוח שלח תמונה', content[0]['text'])
        self.assertIn('זה הבן שלי, יש חוג בשבילו?', content[0]['text'])
        self.assertEqual(content[1]['source'], {'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(JPEG).decode('ascii')})
        self.assertTrue(any(method == 'GET' and f'/object/authenticated/{BUCKET}/{picture.media_url}' in url for method, url, _ in self.storage.calls))

    def test_a_text_burst_keeps_the_plain_string_and_a_heic_picture_is_described_not_attached(self):
        self.say('היי')
        with patch(CLAUDE_POST, return_value=self._answer('היי, במה אוכל לעזור?')) as post:
            self.assertEqual(self.reply().status_code, 200)
        self.assertIsInstance(post.call_args.kwargs['json']['messages'][0]['content'], str)

        self.upload('image', name='IMG_1.heic', content_type='image/heic', text='זאת התעודה')
        with patch(CLAUDE_POST, return_value=self._answer('קיבלתי, תודה')) as post:
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        content = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIsInstance(content, str)
        self.assertIn('זאת התעודה', content)
        self.assertIn('image/heic', response.data['shadow']['reasoning'])
        self.assertFalse(any(method == 'GET' for method, _, _ in self.storage.calls))

    def test_claude_failing_is_an_error_with_nothing_kept_so_the_next_call_tries_again(self):
        self.say('היי')
        with patch(CLAUDE_POST, return_value=_json_response({'error': {'message': 'boom'}}, 500)):
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['shadow'], {'error': 'Claude ענה 500'})
        self.assertIsNone(response.data['message'])
        self.assertEqual((ShadowReply.objects.count(), Message.objects.count()), (0, 1))
        self.assertTrue(Contact.objects.get(pk=self.contact_id).needs_shadow)

        with patch(CLAUDE_POST, side_effect=requests.ReadTimeout('slow')):
            response = self.reply()
        self.assertEqual(response.data['shadow']['error'], 'Claude לא ענה (ReadTimeout)')
        with patch(CLAUDE_POST, return_value=_json_response({'model': 'm', 'stop_reason': 'refusal', 'content': []})):
            response = self.reply()
        self.assertEqual(response.data['shadow']['error'], 'Claude סירב לענות')
        self.assertEqual(ShadowReply.objects.count(), 0)

        with patch(CLAUDE_POST, return_value=self._answer('היי, במה אוכל לעזור?')):
            response = self.reply()
        self.assertEqual(response.data['message']['text'], 'היי, במה אוכל לעזור?')
        self.assertEqual(ShadowReply.objects.count(), 1)
        self.deliver.assert_not_called()

    def test_a_picture_the_storage_cannot_serve_is_an_error_not_a_guess(self):
        self.upload('image')
        self.storage.objects.clear()
        with patch(CLAUDE_POST) as post:
            response = self.reply()
        post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn('לא ניתן לקרוא את התמונה מהאחסון', response.data['shadow']['error'])
        self.assertEqual(ShadowReply.objects.count(), 0)
