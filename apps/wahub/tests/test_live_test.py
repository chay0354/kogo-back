"""
The live test from the owner's own WhatsApp (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב and ה):
the switch writes and removes the mute in ManyChat without touching who answers;
sending is allowed per contact; a message from the live-test contact kicks the
reply at once; the reply waits for the burst, sends once through ManyChat (mocked),
keeps the answer as the new bot's with the real status, respects the claim, the
gap and the daily limit; a demo never gets anything; a plain contact sees no change.
ManyChat's files arrive as addresses and are fetched into the bucket (storage faked).
"""
import os
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatError
from apps.wahub import cron, inbound, knowledge_import, live_test, media, sending, shadow
from apps.wahub.models import Contact, ContactEvent, Message, ShadowReply
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user
from apps.wahub.tests.test_handoff import END, FIELD, START, manychat, writes
from apps.wahub.tests.test_real_demo import (
    CLAUDE_POST,
    JPEG,
    OPENAI_POST,
    SERVICE_KEY,
    STORAGE_REQUEST,
    STORAGE_URL,
    VOICE_RULE,
    FakeStorage,
    _json_response,
)
from apps.wahub.tests.test_shadow import catalogue

REQUEST = 'apps.core.manychat_service.ManyChatService._request'
KICK_POST = 'apps.wahub.live_test.requests.post'
SLEEP = 'apps.wahub.live_test.time.sleep'
FETCH = 'apps.wahub.media.requests.get'
OK = {'status': 'success'}
OWNER = '972522659322'
OWNER_SUBSCRIBER = 4242
INBOUND_URL = f'{BASE}/inbound/manychat/'
REPLY_URL = f'{BASE}/live-test/reply/'
LIVE_VALUE = 'בדיקה חיה (Kogo)'
OGG_URL = 'https://manybot-files.s3.eu-central-1.amazonaws.com/5259597/wa/2026/10/11/original_4322756721cec7ae2ff8a964c597410b.ogg'
JPG_URL = 'https://manybot-files.s3.eu-central-1.amazonaws.com/5259597/wa/2026/10/11/original_9f1a.jpg'
BIN_URL = 'https://manybot-files.s3.eu-central-1.amazonaws.com/5259597/wa/2026/10/11/original_77aa.bin'
MP4_URL = 'https://manybot-files.s3.eu-central-1.amazonaws.com/5259597/wa/2026/10/11/original_55bb.mp4'
OGG = b'OggS' + b'\x00' * 300
ARCHIVED = re.compile(r'^manychat/(\d+)/[0-9a-f]{32}\.(ogg|jpg)$')


def another(url, index):
    """A second file of the same kind: ManyChat names each one once, and the same address again is a repeat."""
    return url.replace('original_', f'original_{index}')


def fetched(content, content_type, status=200):
    """What requests.get answers for ManyChat's file address."""
    response = MagicMock()
    response.status_code = status
    response.headers = {'Content-Type': content_type}
    response.iter_content.return_value = [content]
    return response


def sends(request):
    """The sendContent calls ManyChat got, as (subscriber_id, text)."""
    return [
        (call.kwargs['json_body']['subscriber_id'], call.kwargs['json_body']['data']['content']['messages'][0]['text'])
        for call in request.call_args_list if call.args[1] == '/fb/sending/sendContent'
    ]


CLAUDE = 'apps.wahub.shadow._claude'
REAL_CLAUDE = shadow._claude


def the_stub_in_claudes_place(ctx, deadline, **options):
    """Claude, for the tests that are not about Claude: the rule-built answer, so a test can say what it expects."""
    return shadow._stub(ctx)


@override_settings(
    MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=False, WAHUB_SIMULATE_SEND=False, DEBUG=False,
    CRON_TOKEN='cron-secret', ANTHROPIC_API_KEY='test-key', OPENAI_API_KEY='',
)
class LiveTestCase(WahubTestCase):
    """
    The owner wrote once from his phone through ManyChat; the inbound key is
    set; nobody sleeps for real, and the clock the wait is measured by stands
    still at his last message (`since_his_last_message` seconds after it).
    There is a Claude key — without one the live test answers nothing — and
    Claude answers by the stub's rules unless the test is about Claude itself.
    """

    PATCH_KICK = True
    REAL_CLAUDE = False

    def setUp(self):
        super().setUp()
        patch.dict(os.environ, {'CRON_SECRET': ''}).start()
        self.addCleanup(patch.stopall)
        self.sleep = patch(SLEEP).start()
        if self.PATCH_KICK:
            self.kick = patch('apps.wahub.live_test.kick', return_value=True).start()
        if not self.REAL_CLAUDE:
            patch(CLAUDE, side_effect=the_stub_in_claudes_place).start()
        self.since_his_last_message = 0.0
        patch('apps.wahub.live_test._now', side_effect=self.clock).start()
        self.anonymous = APIClient()
        self.machine = APIClient()
        self.key = self.post('settings/inbound-key/').data['key']
        self.manychat_message('היי')
        self.contact_id = Contact.objects.get(phone=OWNER).id

    def his_last_message_at(self):
        return Message.objects.filter(contact_id=self.contact_id, direction='in').order_by('-id').values_list('created_at', flat=True).first()

    def clock(self):
        return self.his_last_message_at() + timedelta(seconds=self.since_his_last_message)

    def last_answer_was(self, seconds_before_his_last_message):
        """Put the new bot's answers that many seconds before his last message, on the clock the wait reads."""
        Message.objects.filter(contact_id=self.contact_id, new_bot=True).update(
            sent_at=self.his_last_message_at() - timedelta(seconds=seconds_before_his_last_message),
        )

    def manychat_message(self, text, *, phone=OWNER, subscriber_id=OWNER_SUBSCRIBER, event='customer_message', **extra):
        # ManyChat's body: what the customer wrote last, and — for the copy of the old bot's answer — what the bot said.
        said = {'Last_AI_Respone': text} if event == 'bot_reply' else {'Client_Last_Input': text}
        body = {
            'id': subscriber_id, 'first_name': 'דור', 'last_name': 'ס', 'whatsapp_phone': phone,
            'last_input_text': '' if event == 'bot_reply' else text, 'custom_fields': {**said, 'Client_Phone': phone, **extra},
        }
        return self.anonymous.post(f'{INBOUND_URL}?event={event}', body, format='json', HTTP_X_WAHUB_KEY=self.key)

    def owner(self) -> Contact:
        return Contact.objects.get(pk=self.contact_id)

    def switch(self, on, contact_id=None):
        return self.post(f'contacts/{contact_id or self.contact_id}/live-test/', {'on': on})

    def turn_on(self, tags=(END,)):
        with patch(REQUEST, side_effect=manychat(tags=list(tags))) as request:
            response = self.switch(True)
        self.assertEqual(response.status_code, 200, response.data)
        return request

    def reply(self, contact_id=None, **headers):
        headers.setdefault('HTTP_X_CRON_TOKEN', 'cron-secret')
        return self.machine.post(REPLY_URL, {'contact': contact_id or self.contact_id}, format='json', **headers)

    def events(self, kind='live_test'):
        return list(ContactEvent.objects.filter(contact_id=self.contact_id, kind=kind).order_by('id'))

    def answers(self):
        """The journal lines the reply wrote — not the ones of the switch."""
        return [event for event in self.events() if not event.text.startswith('בדיקה חיה ')]

    def waits(self):
        """How long each wait of the reply was asked to be."""
        return [call.args[0] for call in self.sleep.call_args_list]


# --- the switch ----------------------------------------------------------------------------------------

class LiveTestSwitchTests(LiveTestCase):
    def test_on_mutes_the_old_bot_in_manychat_with_the_live_value_and_leaves_the_bot_answering(self):
        request = self.turn_on(tags=[END, 'אחר'])
        self.assertEqual(writes(request), [
            ('/fb/subscriber/removeTagByName', {'subscriber_id': OWNER_SUBSCRIBER, 'tag_name': END}),
            ('/fb/subscriber/setCustomFieldByName', {'subscriber_id': OWNER_SUBSCRIBER, 'field_name': FIELD, 'field_value': LIVE_VALUE}),
            ('/fb/subscriber/addTagByName', {'subscriber_id': OWNER_SUBSCRIBER, 'tag_name': START}),
        ])
        contact = self.owner()
        self.assertTrue(contact.live_test)
        self.assertEqual(contact.handled_by, 'bot')                 # the NEW bot answers; no person took over
        self.assertFalse(ContactEvent.objects.filter(kind='handled_by_changed').exists())
        [event] = self.events()
        self.assertIn('הופעלה', event.text)
        self.assertEqual(event.actor, self.manager)
        payload = self.get(f'contacts/{self.contact_id}/').data
        self.assertTrue(payload['live_test'])
        self.assertEqual(payload['chat']['handled_by'], 'bot')
        self.assertEqual(self.get('status/').data['live_test_contacts'], 1)
        # The general switch is off all along; the live test does not need it.
        self.assertFalse(self.get('status/').data['sending_enabled'])

    def test_off_removes_the_start_tag_so_the_old_bot_answers_again(self):
        self.turn_on()
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.switch(False)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(writes(request), [('/fb/subscriber/removeTagByName', {'subscriber_id': OWNER_SUBSCRIBER, 'tag_name': START})])
        self.assertFalse(self.owner().live_test)
        self.assertFalse(response.data['live_test'])
        on, off = [event.text for event in self.events()]
        self.assertTrue(on.startswith('בדיקה חיה הופעלה'), on)
        self.assertTrue(off.startswith('בדיקה חיה כובתה'), off)
        self.assertEqual(self.get('status/').data['live_test_contacts'], 0)

    def test_a_refusal_by_manychat_changes_nothing_here(self):
        for step in ('getInfo', 'setCustomFieldByName', 'addTagByName'):
            with self.subTest(step=step):
                with patch(REQUEST, side_effect=manychat(tags=[END], fail_on=step)):
                    response = self.switch(True)
                self.assertEqual(response.status_code, 502)
                self.assertIn('ManyChat', response.data['detail'])
                self.assertFalse(self.owner().live_test)
                self.assertEqual(self.events(), [])
        self.turn_on()
        with patch(REQUEST, side_effect=manychat(tags=[START], fail_on='removeTagByName')):
            self.assertEqual(self.switch(False).status_code, 502)
        self.assertTrue(self.owner().live_test)

    def test_twice_on_writes_once(self):
        self.turn_on()
        with patch(REQUEST) as request:
            self.assertEqual(self.switch(True).status_code, 200)
        request.assert_not_called()
        self.assertEqual(len(self.events()), 1)

    def test_a_demo_contact_bad_input_and_a_worker_are_refused(self):
        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        with patch(REQUEST) as request:
            refused = self.switch(True, contact_id=demo['id'])
            self.assertEqual((refused.status_code, refused.data['code']), (400, 'demo'))
            self.assertEqual(self.post(f'contacts/{self.contact_id}/live-test/', {'on': 'yes'}).status_code, 400)
            self.assertEqual(self.post(f'contacts/{self.contact_id}/live-test/', {}).status_code, 400)
            worker = client_for(make_user('worker@wahub.test', role='worker'))
            self.assertEqual(worker.post(f'{BASE}/contacts/{self.contact_id}/live-test/', {'on': True}, format='json').status_code, 403)
        request.assert_not_called()
        self.assertFalse(Contact.objects.filter(live_test=True).exists())

    def test_off_while_a_person_holds_the_conversation_gives_it_back_to_the_bot_so_nobody_is_left_muted(self):
        # With the general switch off the office cannot write to him, or hand him back, once the test is over.
        self.turn_on()
        with patch(REQUEST, side_effect=manychat(tags=[START])):
            self.assertEqual(self.post(f'contacts/{self.contact_id}/takeover/').data['chat']['handled_by'], 'human')
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.switch(False)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(writes(request), [('/fb/subscriber/removeTagByName', {'subscriber_id': OWNER_SUBSCRIBER, 'tag_name': START})])
        contact = self.owner()
        self.assertEqual((contact.live_test, contact.handled_by), (False, 'bot'))
        self.assertEqual(response.data['chat']['handled_by'], 'bot')
        self.assertIn('הבוט הישן עונה שוב', self.events()[-1].text)
        self.assertIn('כיבוי הבדיקה החיה', self.events('handled_by_changed')[-1].text)

    @override_settings(WAHUB_SENDING_ENABLED=True)
    def test_off_while_a_person_holds_the_conversation_keeps_the_mute_when_the_office_can_write(self):
        self.turn_on()
        Contact.objects.filter(pk=self.contact_id).update(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.switch(False)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(writes(request), [
            ('/fb/subscriber/setCustomFieldByName', {'subscriber_id': OWNER_SUBSCRIBER, 'field_name': FIELD, 'field_value': 'בטיפול נציג (Kogo)'}),
        ])
        contact = self.owner()
        self.assertEqual((contact.live_test, contact.handled_by), (False, 'human'))
        self.assertIn('נציג עדיין מטפל', self.events()[-1].text)
        # And the person can hand it back as always.
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            self.assertEqual(self.post(f'contacts/{self.contact_id}/release/').data['chat']['handled_by'], 'bot')
        self.assertEqual(writes(request), [('/fb/subscriber/removeTagByName', {'subscriber_id': OWNER_SUBSCRIBER, 'tag_name': START})])

    def test_off_leaves_the_claim_of_an_answer_in_progress_alone(self):
        self.turn_on()
        until = timezone.now() + timedelta(seconds=60)
        Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=until)
        with patch(REQUEST, side_effect=manychat(tags=[START])):
            self.switch(False)
        self.assertEqual(self.owner().live_test_busy_until, until)
        # Turned on again within the same seconds: a new call is still "busy", so no second answer beside the first.
        self.turn_on(tags=[])
        with patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'busy')
        request.assert_not_called()

    def test_takeover_and_release_work_for_the_live_test_contact_with_the_switch_off(self):
        self.turn_on()
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.post(f'contacts/{self.contact_id}/takeover/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['chat']['handled_by'], 'human')
        self.assertEqual([path for path, _ in writes(request)], ['/fb/subscriber/setCustomFieldByName', '/fb/subscriber/addTagByName'])
        # Back to the bot — the NEW one: the mute stays, the status field says "live test" again.
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.post(f'contacts/{self.contact_id}/release/')
        self.assertEqual(response.data['chat']['handled_by'], 'bot')
        self.assertEqual(writes(request), [
            ('/fb/subscriber/setCustomFieldByName', {'subscriber_id': OWNER_SUBSCRIBER, 'field_name': FIELD, 'field_value': LIVE_VALUE}),
        ])
        self.assertTrue(self.owner().live_test)

    @override_settings(WAHUB_LIVE_TEST_STATUS_VALUE='live (test)')
    def test_the_status_value_is_a_setting(self):
        request = self.turn_on()
        self.assertEqual(writes(request)[1][1]['field_value'], 'live (test)')


# --- sending per contact -------------------------------------------------------------------------------

class SendingPerContactTests(LiveTestCase):
    def test_sending_enabled_by_contact(self):
        self.turn_on()
        live = self.owner()
        self.incoming('שלום', phone='972505550102', subscriber_id='9')
        plain = Contact.objects.get(phone='972505550102')
        demo = Contact.objects.create(phone='972505559001', is_demo=True, live_test=True)
        self.assertFalse(sending.sending_enabled())
        self.assertTrue(sending.sending_enabled(live))
        self.assertFalse(sending.sending_enabled(plain))
        self.assertFalse(sending.sending_enabled(demo))
        with override_settings(WAHUB_SENDING_ENABLED=True):
            self.assertTrue(sending.sending_enabled())
            self.assertTrue(sending.sending_enabled(plain))
            self.assertFalse(sending.sending_enabled(demo))             # never, whatever the switch says

    def test_the_new_bot_sends_only_to_a_live_test_contact(self):
        self.incoming('שלום', phone='972505550102', subscriber_id='9')
        plain = Contact.objects.get(phone='972505550102')
        demo = Contact.objects.create(phone='972505559001', is_demo=True, live_test=True, last_inbound_at=timezone.now())
        with patch(REQUEST) as request:
            with self.assertRaises(ValueError):
                sending.send_new_bot_reply(plain, 'שלום')
            with self.assertRaises(ValueError):
                sending.send_new_bot_reply(demo, 'שלום')
            # A plain contact's send from the screen is unchanged: kept as failed, nothing leaves.
            message = sending.send_text(plain, 'שלום', self.manager)
        request.assert_not_called()
        self.assertEqual((message.status, message.error), ('failed', sending.SENDING_OFF))
        self.assertEqual(Message.objects.filter(new_bot=True).count(), 0)

    def test_the_office_may_write_to_the_live_test_contact_while_the_switch_is_off(self):
        self.turn_on()
        with patch(REQUEST, return_value=OK) as request:
            response = self.post(f'contacts/{self.contact_id}/send/', {'text': 'בודקים'})
        self.assertEqual(response.data['message']['status'], 'sent')
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, 'בודקים')])
        self.assertFalse(response.data['message']['new_bot'])


# --- the inbound door kicks the reply ----------------------------------------------------------------------

class InboundKickTests(LiveTestCase):
    PATCH_KICK = False

    def test_a_message_from_the_live_test_contact_kicks_the_reply_at_once_and_keeps_the_raw_body(self):
        self.turn_on()
        with patch(KICK_POST, side_effect=requests.ReadTimeout('the answer takes longer')) as post:
            response = self.manychat_message('יש חוג קפוארה?', Extra='x')
        self.assertEqual((response.status_code, response.data), (200, {'ok': True, 'stored': True}))
        post.assert_called_once_with(
            'http://testserver/api/v1/wahub/live-test/reply/', json={'contact': self.contact_id},
            headers={'X-Cron-Token': 'cron-secret'}, timeout=(2, 1),
        )
        message = Message.objects.filter(contact_id=self.contact_id).order_by('-id').first()
        self.assertEqual(message.text, 'יש חוג קפוארה?')
        self.assertEqual(message.raw['id'], OWNER_SUBSCRIBER)
        self.assertEqual(message.raw['custom_fields']['Extra'], 'x')
        # The first message, before the test was on, kept no body.
        self.assertIsNone(Message.objects.filter(contact_id=self.contact_id).order_by('id').first().raw)

    def test_faults_of_the_self_call_are_swallowed_and_manychat_still_gets_200(self):
        self.turn_on()
        with patch(KICK_POST, side_effect=requests.ConnectionError('refused')), self.assertLogs('apps.wahub.live_test', level='WARNING'):
            self.assertEqual(self.manychat_message('א').data['stored'], True)
        # Nobody will answer that message: the contact says so, where the office looks.
        self.assertEqual(self.answers()[-1].text, 'התשובה החיה לא הופעלה: השרת לא הגיע לעצמו (ConnectionError)')
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIn('התשובה החיה לא הופעלה', contact.needs_human_reason)
        with patch(KICK_POST, return_value=MagicMock(status_code=401)), self.assertLogs('apps.wahub.live_test', level='WARNING'):
            self.assertEqual(self.manychat_message('ב').data['stored'], True)
        self.assertEqual(self.answers()[-1].text, 'התשובה החיה לא הופעלה: השרת ענה לעצמו 401')
        self.assertEqual(len(self.events('needs_human_changed')), 1)      # marked once
        # A call that came back at once (busy, say) is a call that arrived.
        with patch(KICK_POST, return_value=MagicMock(status_code=200)):
            self.assertEqual(self.manychat_message('ב2').data['stored'], True)
        self.assertEqual(len(self.answers()), 2)
        # Nothing mocked: the network blocker of the tests raises, the door still answers.
        with self.assertLogs('apps.wahub.views', level='ERROR'):
            self.assertEqual(self.manychat_message('ג').status_code, 200)

    def test_no_kick_for_a_plain_contact_a_bot_reply_a_duplicate_or_a_demo(self):
        self.turn_on()
        with patch(KICK_POST) as post:
            self.manychat_message('שלום', phone='972505550102', subscriber_id=9)
            self.assertEqual(self.manychat_message('היי').data['stored'], False)    # a repeat of the first message, a moment later
            self.assertEqual(self.manychat_message('תשובת הבוט הישן', event='bot_reply').data['stored'], True)
            demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
            Contact.objects.filter(pk=demo['id']).update(live_test=True)   # could only happen by hand
            self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי'})
            # Even through ManyChat's own door, with a body: a demo contact is never under the test.
            self.assertEqual(self.manychat_message('שלום מ-ManyChat', phone='972505559001', subscriber_id=77).data['stored'], True)
            self.assertEqual(self.manychat_message('', phone='972505559001', subscriber_id=77).data['stored'], False)
        post.assert_not_called()
        self.assertIsNone(Message.objects.get(contact__phone='972505550102').raw)
        self.assertEqual(Message.objects.filter(contact_id=demo['id']).count(), 2)
        self.assertFalse(Message.objects.filter(contact_id=demo['id']).exclude(raw=None).exists())

    def test_the_same_words_again_after_an_answer_are_a_new_message(self):
        # "כן", the bot answers within seconds and asks, "כן" again: the second is not ManyChat repeating the first.
        self.turn_on()
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')) as post:
            self.assertEqual(self.manychat_message('כן').data['stored'], True)
            self.assertEqual(self.manychat_message('כן').data['stored'], False)       # a moment apart: a repeat
            self.assertEqual(post.call_count, 1)
            answer = Message.objects.create(contact_id=self.contact_id, direction='out', sender='bot', text='לאיזה חוג?', new_bot=True, status='sent')
            self.assertEqual(self.manychat_message('כן').data['stored'], True)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(self.manychat_message('כן').data['stored'], False)       # and that one's repeat is a repeat
            # An answer that never reached him is not a turn of the conversation.
            Message.objects.filter(pk=answer.pk).update(status='failed')
            Message.objects.create(contact_id=self.contact_id, direction='out', sender='bot', text='נכשל', new_bot=True, status='failed')
            self.assertEqual(self.manychat_message('כן').data['stored'], False)
        self.assertEqual(Message.objects.filter(contact_id=self.contact_id, direction='in', text='כן').count(), 2)
        # The old bot's copies, the other way round: the same canned line twice is a repeat until the customer wrote.
        self.assertEqual(self.manychat_message('איך אפשר לעזור?', event='bot_reply').data['stored'], True)
        self.assertEqual(self.manychat_message('איך אפשר לעזור?', event='bot_reply').data['stored'], False)
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')):
            self.manychat_message('מה שעות הפעילות?')
        self.assertEqual(self.manychat_message('איך אפשר לעזור?', event='bot_reply').data['stored'], True)

    @override_settings(CRM_API_BASE_URL='https://kogo-back.vercel.app/api/v1')
    def test_the_public_address_is_used_when_it_is_configured(self):
        self.turn_on()
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')) as post:
            self.manychat_message('א')
        self.assertEqual(post.call_args.args[0], 'https://kogo-back.vercel.app/api/v1/wahub/live-test/reply/')

    @override_settings(CRON_TOKEN='')
    def test_without_a_cron_token_nothing_is_called_and_it_is_logged(self):
        self.turn_on()
        with patch(KICK_POST) as post, self.assertLogs('apps.wahub.live_test', level='WARNING'):
            self.assertEqual(self.manychat_message('א').status_code, 200)
        post.assert_not_called()
        self.assertIn('CRON_TOKEN', self.answers()[-1].text)

    def test_an_empty_message_from_the_live_test_contact_is_media_manychat_did_not_pass(self):
        self.turn_on()
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')) as post:
            response = self.manychat_message('')
        self.assertEqual(response.data['stored'], True)
        post.assert_called_once()
        message = Message.objects.filter(contact_id=self.contact_id).order_by('-id').first()
        self.assertEqual((message.message_type, message.text, message.media_url), ('other', inbound.MEDIA_NOT_FORWARDED, ''))
        self.assertIsNotNone(message.raw)
        # A second one right after is a second message, not a repeat.
        self.assertEqual(self.manychat_message('').data['stored'], True)
        # For everybody else an empty message is nothing, as before.
        self.assertEqual(self.manychat_message('', phone='972505550102', subscriber_id=9).data['stored'], False)
        self.assertFalse(Contact.objects.filter(phone='972505550102').exists())


# --- ManyChat passes a file as its address, for every contact --------------------------------------------------

class InboundMediaAddressTests(LiveTestCase):
    def test_a_recording_a_picture_and_an_unknown_file_by_their_addresses(self):
        for text, kind, preview in (
            (OGG_URL, 'voice', '🎤 הודעה קולית'), (JPG_URL, 'image', '🖼️ תמונה'), (BIN_URL, 'other', '📎 קובץ'),
            (MP4_URL, 'other', '📎 קובץ'),          # a video and some audio share the extension: the bytes decide, later
        ):
            with self.subTest(kind=kind):
                response = self.manychat_message(text, phone='972505550102', subscriber_id=9)
                self.assertEqual(response.data['stored'], True)
                message = Message.objects.filter(contact__phone='972505550102').order_by('-id').first()
                self.assertEqual((message.message_type, message.media_url, message.text), (kind, text, ''))
                contact = Contact.objects.get(phone='972505550102')
                self.assertEqual(contact.last_message_text, preview)
                self.assertTrue(contact.needs_shadow)
        # The screen gets the address as it is until the file is fetched.
        message = Message.objects.filter(contact__phone='972505550102', message_type='image').get()
        self.assertEqual(self.client.get(f'{BASE}/messages/{message.id}/media/').data, {'url': JPG_URL, 'expires_in': None})
        payload = self.get(f'contacts/{Contact.objects.get(phone="972505550102").id}/').data['messages'][-1]
        self.assertEqual((payload['message_type'], payload['has_media'], payload['text']), ('other', True, ''))

    def test_two_files_a_moment_apart_are_two_messages_and_a_repeat_of_one_is_not(self):
        other = OGG_URL.replace('original_4322', 'original_9999')
        self.assertEqual(self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9).data['stored'], True)
        self.assertEqual(self.manychat_message(other, phone='972505550102', subscriber_id=9).data['stored'], True)
        # ManyChat names every file once: the same address again is ManyChat saying the same message again.
        self.assertEqual(self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9).data['stored'], False)
        contact = Contact.objects.get(phone='972505550102')
        self.assertEqual((Message.objects.filter(contact=contact).count(), contact.messages_count, contact.unread_count), (2, 2, 2))
        # Also once the first copy's file has been fetched into the bucket.
        Message.objects.filter(contact=contact, media_url=OGG_URL).update(media_url=media.archived_stem(contact.id, OGG_URL) + '.ogg')
        self.assertEqual(self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9).data['stored'], False)
        # Outside the window it is a message again, and another contact's file is its own.
        Message.objects.filter(contact=contact).update(created_at=timezone.now() - timedelta(seconds=31))
        self.assertEqual(self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9).data['stored'], True)
        self.assertEqual(self.manychat_message(OGG_URL, phone='972505550103', subscriber_id=10).data['stored'], True)

    def test_a_link_in_a_sentence_or_a_site_link_stays_text(self):
        for text in (
            'תראו https://kogo.co.il/signup זה הקישור?', 'https://www.kogo.co.il/', 'https://example.com/page.html',
            'http://x.y/a.OGG?x=1 וגם טקסט',
            'https://example.com/photo.jpg',               # a picture on somebody's site is a link he sent, not his file
            OGG_URL.replace('https://', 'http://'),        # ManyChat's bucket is https
            f'{OGG_URL} שמעתם?',
        ):
            with self.subTest(text=text):
                self.manychat_message(text, phone='972505550102', subscriber_id=9)
                message = Message.objects.filter(contact__phone='972505550102').order_by('-id').first()
                self.assertEqual((message.message_type, message.media_url, message.text), ('text', '', text))

    def test_kind_of_url(self):
        bucket = 'https://manybot-files.s3.eu-central-1.amazonaws.com/5259597/wa/2026/10/11'
        self.assertEqual(media.kind_of_url(OGG_URL), 'voice')
        self.assertEqual(media.kind_of_url(OGG_URL.upper().replace('HTTPS', 'https')), 'voice')
        for ext in ('ogg', 'oga', 'opus', 'm4a', 'mp3', 'aac'):
            self.assertEqual(media.kind_of_url(f'{bucket}/a.{ext}?token=1#x'), 'voice', ext)
        for ext in ('jpg', 'JPEG', 'png', 'webp', 'heic'):
            self.assertEqual(media.kind_of_url(f'{bucket}/a.{ext}'), 'image', ext)
        self.assertEqual(media.kind_of_url('  ' + JPG_URL + '  '), 'image')
        self.assertEqual(media.kind_of_url('https://manybot-files.s3.amazonaws.com/1/a.jpg'), 'image')
        # A file of ManyChat's we do not know by its name: still the customer's file.
        for name in ('original_77aa.bin', 'original_77aa', 'clip.mp4', 'clip.webm', 'doc.pdf'):
            self.assertEqual(media.kind_of_url(f'{bucket}/{name}'), 'other', name)
        # Anything that is not on ManyChat's bucket is the text the customer wrote.
        for text in (
            'https://cdn.example.com/a/b.opus', 'https://example.com/photo.jpg', 'https://example.com/file.bin', 'https://example.com/',
            'https://manybot-files.s3.eu-central-1.amazonaws.com.evil.example/a.ogg',
            'https://evil.example/manybot-files.s3.eu-central-1.amazonaws.com/a.ogg',
            'https://user@manybot-files.s3.eu-central-1.amazonaws.com/a.ogg',
            'http://manybot-files.s3.eu-central-1.amazonaws.com/a.ogg', 'ftp://example.com/a.jpg', f'{OGG_URL} {JPG_URL}', '',
            # Not written plainly: what is kept as media_url is later read as an address or as a path in our bucket.
            OGG_URL.replace('https://', 'HTTPS://'), OGG_URL.replace('https://', 'Https://'),
            f'{bucket}/../../../../other-bucket/secret.jpg', f'{bucket}/./a.jpg', f'{bucket}/%2e%2e/a.jpg', f'{bucket}/a\\..\\b.jpg',
        ):
            self.assertEqual(media.kind_of_url(text), '', text)
        with override_settings(WAHUB_MEDIA_HOSTS='files.manychat.example, cdn.example.com'):
            self.assertEqual(media.kind_of_url('https://cdn.example.com/a/b.opus'), 'voice')
            self.assertEqual(media.kind_of_url('https://example.com/photo.jpg'), '')


# --- the reply ------------------------------------------------------------------------------------------------

class LiveReplyTests(LiveTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.turn_on()

    def test_the_reply_waits_answers_the_burst_once_sends_through_manychat_and_keeps_the_new_bots_line(self):
        self.manychat_message('יש חוג קפוארה בראש העין לבן 6?')
        ids = list(Message.objects.filter(contact_id=self.contact_id).order_by('id').values_list('id', flat=True))
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(self.waits(), [4.0])                            # four seconds from his last message, and nothing more arrived
        [(subscriber_id, text)] = sends(request)
        self.assertEqual(subscriber_id, OWNER_SUBSCRIBER)
        self.assertIn('שלישי', text)

        message = Message.objects.get(pk=response.data['message_id'])
        self.assertEqual((message.direction, message.sender, message.new_bot, message.status, message.error, message.source), ('out', 'bot', True, 'sent', '', 'kogo'))
        self.assertEqual(message.text, text)
        self.assertIsNone(message.sent_by)
        proposal = ShadowReply.objects.get(pk=response.data['shadow_id'])
        self.assertEqual((proposal.covers_message_ids, proposal.model, proposal.text), (ids, 'stub', text))
        contact = self.owner()
        self.assertFalse(contact.needs_shadow)
        self.assertIsNone(contact.waiting_since)
        self.assertIsNone(contact.live_test_busy_until)
        self.assertEqual(contact.last_message_sender, 'bot')
        self.assertEqual(contact.unread_count, 2)                        # the bot reads nothing for the office
        [event] = self.answers()
        self.assertTrue(event.text.startswith('תשובה חיה נשלחה: התקבלה '), event.text)
        self.assertIn('→ חישוב', event.text)
        self.assertIn('→ נשלחה', event.text)
        self.assertIn('סה"כ', event.text)
        self.assertIsInstance(response.data['seconds'], float)

        # Answered: a second call sends nothing.
        with patch(REQUEST) as request:
            again = self.reply()
        self.assertEqual(again.data['outcome'], 'nothing')
        request.assert_not_called()
        listed = self.get(f'contacts/{self.contact_id}/').data['messages']
        self.assertEqual([(row['sender'], row['new_bot'], row['status']) for row in listed], [('customer', False, 'received')] * 2 + [('bot', True, 'sent')])
        self.assertEqual(self.get(f'contacts/{self.contact_id}/shadow/').data[0]['old_bot_reply'], None)

    def test_a_message_during_the_wait_extends_it_and_the_cap_still_answers(self):
        def another_message(_seconds):
            self.manychat_message(f'ועוד שאלה {self.sleep.call_count}')

        self.sleep.side_effect = another_message
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(self.waits(), [4.0, 4.0, 4.0])                  # 4 + 4 + 4 seconds at most
        self.assertEqual(len(sends(request)), 1)
        proposal = ShadowReply.objects.get()
        self.assertEqual(len(proposal.covers_message_ids), 4)            # the first one and the three that came while waiting
        self.assertEqual(Message.objects.filter(contact_id=self.contact_id, direction='in').count(), 4)

        # One late message: the wait is extended once and then the answer goes.
        self.sleep.reset_mock()
        self.sleep.side_effect = lambda _seconds: self.sleep.call_count == 1 and self.manychat_message('אחרונה')
        self.manychat_message('עוד אחת')
        self.last_answer_was(60)
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.waits(), [4.0, 4.0])
        self.assertEqual(len(ShadowReply.objects.order_by('-id').first().covers_message_ids), 2)

    def test_the_wait_counts_from_his_last_message_not_from_the_call(self):
        # The call reached us three seconds after the message (a cold start): one second is left to wait.
        self.since_his_last_message = 3
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.waits(), [1.0])
        # Ten seconds after it: no wait at all.
        self.sleep.reset_mock()
        self.manychat_message('ועוד שאלה')
        self.last_answer_was(60)
        self.since_his_last_message = 10
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.sleep.assert_not_called()

    @override_settings(WAHUB_LIVE_TEST_SETTLE_SECONDS=2.5)
    def test_the_wait_is_a_setting(self):
        with patch(REQUEST, return_value=OK):
            self.reply()
        self.assertEqual(self.waits(), [2.5])

    def test_a_message_that_arrives_while_the_answer_is_computed_is_answered_by_the_same_call(self):
        # His second message lands while the first answer is on its way out; its own call finds the contact busy.
        late = []

        def manychat_takes_a_moment(method, path, **kwargs):
            if path == '/fb/sending/sendContent' and not late:
                late.append(self.manychat_message('ומה המחיר?').data['stored'])
                late.append(self.reply().data['outcome'])
            return OK

        with patch(REQUEST, side_effect=manychat_takes_a_moment) as request:
            response = self.reply()
        self.assertEqual(late, [True, 'busy'])
        self.assertEqual((response.data['outcome'], response.data['followed_by']), ('sent', ['sent']))
        self.assertEqual(len(sends(request)), 2)                         # one answer each, never two for one
        first, second = ShadowReply.objects.order_by('id')
        self.assertEqual(len(first.covers_message_ids), 1)
        self.assertEqual(Message.objects.get(pk=second.covers_message_ids[0]).text, 'ומה המחיר?')
        self.assertEqual(len(self.answers()), 2)
        contact = self.owner()
        self.assertFalse(contact.needs_shadow)
        self.assertIsNone(contact.live_test_busy_until)
        # The second answer kept its distance from the first: five seconds, not the burst's four.
        self.assertEqual(self.waits(), [4.0, 5.0])

    def test_one_call_keeps_answering_a_while_and_what_is_left_is_said_by_the_cron(self):
        def keeps_writing(method, path, **kwargs):
            if path == '/fb/sending/sendContent':
                self.manychat_message(f'ועוד {Message.objects.count()}')
            return OK

        with patch(REQUEST, side_effect=keeps_writing) as request:
            response = self.reply()
        self.assertEqual(response.data['followed_by'], ['sent'] * (live_test.MAX_ANSWERS - 1))
        self.assertEqual(len(sends(request)), live_test.MAX_ANSWERS)
        self.assertTrue(self.owner().needs_shadow)                       # the next message's own call carries on
        self.assertIsNone(self.owner().live_test_busy_until)
        # No next message came. Two minutes on, the cron says the last one was never answered —
        # the answers that went out since were for the ones before it.
        self.assertEqual(live_test.flag_unanswered(), 0)
        left = Message.objects.filter(contact_id=self.contact_id, direction='in').order_by('-id').first()
        Message.objects.filter(pk=left.pk).update(created_at=timezone.now() - timedelta(minutes=3))
        ContactEvent.objects.filter(contact_id=self.contact_id, text__startswith='בדיקה חיה הופעלה').update(created_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(live_test.flag_unanswered(), 1)
        self.assertTrue(self.owner().needs_human)
        self.assertEqual(live_test.flag_unanswered(), 0)

    def test_a_call_that_ran_past_its_time_stops_following_up(self):
        def keeps_writing(method, path, **kwargs):
            if path == '/fb/sending/sendContent':
                self.manychat_message(f'ועוד {Message.objects.count()}')
            return OK

        with patch.object(live_test, 'FOLLOW_UP_SECONDS', -1), patch(REQUEST, side_effect=keeps_writing) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertNotIn('followed_by', response.data)
        self.assertEqual(len(sends(request)), 1)

    def test_the_door_takes_the_cron_token_only(self):
        with patch(REQUEST) as request:
            for headers in ({'HTTP_X_CRON_TOKEN': ''}, {'HTTP_X_CRON_TOKEN': 'wrong'}, {'HTTP_AUTHORIZATION': 'Bearer wrong'}):
                self.assertEqual(self.machine.post(REPLY_URL, {'contact': self.contact_id}, format='json', **headers).status_code, 401)
            self.assertEqual(self.client.post(REPLY_URL, {'contact': self.contact_id}, format='json').status_code, 401)
            self.assertEqual(self.reply(HTTP_X_CRON_TOKEN='', HTTP_AUTHORIZATION='Bearer cron-secret').status_code, 200)
            self.assertEqual(self.machine.post(REPLY_URL, {}, format='json', HTTP_X_CRON_TOKEN='cron-secret').status_code, 400)
            self.assertEqual(self.machine.post(REPLY_URL, {'contact': 'x'}, format='json', HTTP_X_CRON_TOKEN='cron-secret').status_code, 400)
            self.assertEqual(self.machine.post(REPLY_URL, [self.contact_id], format='json', HTTP_X_CRON_TOKEN='cron-secret').status_code, 400)
            self.assertEqual(self.reply(contact_id=999999).status_code, 404)
        self.assertEqual(len(sends(request)), 1)

    def test_a_claim_in_progress_is_busy_and_nothing_is_sent(self):
        Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=timezone.now() + timedelta(seconds=60))
        with patch(REQUEST) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'busy')
        request.assert_not_called()
        self.sleep.assert_not_called()
        self.assertTrue(self.owner().needs_shadow)
        # An expired claim (a cut call) does not block.
        Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=timezone.now() - timedelta(seconds=1))
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')

    def test_a_row_held_for_a_moment_is_asked_for_again_and_one_held_all_along_is_busy(self):
        # The door storing his next message holds the contact's row for a moment; that is not "somebody is answering".
        real, asked = Contact.objects.select_for_update, []

        def held(times):
            def select_for_update(**kwargs):
                asked.append(kwargs)
                return Contact.objects.none() if len(asked) <= times else real(**kwargs)
            return select_for_update

        with patch.object(Contact.objects, 'select_for_update', side_effect=held(2)), patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(asked[0], {'skip_locked': True})
        self.assertEqual(self.waits()[:2], [live_test.CLAIM_RETRY_SECONDS] * 2)

        asked.clear()
        self.sleep.reset_mock()
        self.manychat_message('ועוד שאלה')
        with patch.object(Contact.objects, 'select_for_update', side_effect=held(99)), patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'busy')
        request.assert_not_called()
        self.assertEqual(len(asked), live_test.CLAIM_TRIES)
        self.assertEqual(self.waits(), [live_test.CLAIM_RETRY_SECONDS] * (live_test.CLAIM_TRIES - 1))

    def test_a_claim_that_expired_and_was_taken_by_another_call_is_not_released_by_the_first(self):
        other_calls_claim = timezone.now() + timedelta(seconds=77)

        def slow(*args, **kwargs):
            # This call's claim ran out while ManyChat kept it waiting, and another call took the contact.
            if args[1] == '/fb/sending/sendContent':
                Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=other_calls_claim)
            return OK

        with patch(REQUEST, side_effect=slow):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.owner().live_test_busy_until, other_calls_claim)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_without_a_claude_key_nothing_is_sent_to_a_real_phone(self):
        # The stub's rule-built line is for a developer's screen; it is not the new bot.
        with patch(REQUEST) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'model_error')
        self.assertIn('Anthropic', response.data['error'])
        request.assert_not_called()
        self.sleep.assert_not_called()
        self.assertEqual((ShadowReply.objects.count(), Message.objects.filter(direction='out').count()), (0, 0))
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIn('Anthropic', contact.needs_human_reason)
        self.assertIn('לא נשלח דבר', self.answers()[-1].text)
        # On a developer's machine, where sends are only recorded, the stub may stand in: nothing leaves.
        with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True), patch(REQUEST) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        request.assert_not_called()
        message = Message.objects.get(pk=response.data['message_id'])
        self.assertEqual((message.status, message.new_bot, message.sender), ('simulated', True, 'bot'))
        self.assertEqual(ShadowReply.objects.get().model, 'stub')

    def test_one_answer_per_five_seconds(self):
        Message.objects.create(contact_id=self.contact_id, direction='out', sender='bot', text='קודמת', new_bot=True, status='sent')
        self.last_answer_was(2)
        # The wait for the burst covers the three seconds that are left: one wait, not two.
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.waits(), [4.0])
        # With no wait for the burst, the distance alone holds the answer back.
        self.sleep.reset_mock()
        self.manychat_message('ועוד שאלה')
        self.last_answer_was(2)
        with override_settings(WAHUB_LIVE_TEST_SETTLE_SECONDS=0), patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.waits(), [3.0])

    def test_thirty_a_day_and_not_one_more(self):
        now = timezone.now()
        for index in range(30):
            Message.objects.create(contact_id=self.contact_id, direction='out', sender='bot', text=str(index), new_bot=True, status='sent', sent_at=now - timedelta(minutes=index))
        with patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'daily_limit')
            self.assertEqual(self.reply().data['outcome'], 'daily_limit')
        request.assert_not_called()
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertEqual([event.text for event in self.answers()], [live_test.DAILY_LIMIT_TEXT])   # said once
        self.sleep.assert_not_called()                                   # refused before any waiting
        # Yesterday's answers do not count.
        Message.objects.filter(new_bot=True).update(sent_at=now - timedelta(days=1, hours=1))
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')

    def test_a_refused_send_is_a_failed_line_and_asks_for_a_person(self):
        refusal = ManyChatError('Validation error', status_code=400, payload={'details': {'messages': [{'message': 'not subscribed'}]}})
        with patch(REQUEST, side_effect=refusal):
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'failed')
        message = Message.objects.get(pk=response.data['message_id'])
        self.assertEqual((message.status, message.new_bot, message.sender), ('failed', True, 'bot'))
        self.assertIn('not subscribed', message.error)
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIn('השליחה ללקוח נכשלה', contact.needs_human_reason)
        self.assertIsNotNone(contact.waiting_since)                      # nobody answered him
        self.assertIn('תשובה חיה נכשלה', self.events()[-1].text)
        self.assertIn('not subscribed', self.events()[-1].text)

    def test_nothing_for_a_plain_contact_a_demo_a_person_handling_or_a_test_turned_off_meanwhile(self):
        self.incoming('היי', phone='972505550102', subscriber_id='9')
        plain = Contact.objects.get(phone='972505550102')
        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי'})
        Contact.objects.filter(pk=demo['id']).update(live_test=True)
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(contact_id=plain.id).data['outcome'], 'not_live_test')
            self.assertEqual(self.reply(contact_id=demo['id']).data['outcome'], 'not_live_test')
            Contact.objects.filter(pk=self.contact_id).update(handled_by='human')
            self.assertEqual(self.reply().data['outcome'], 'human_handles')
            Contact.objects.filter(pk=self.contact_id).update(handled_by='bot')
            self.sleep.side_effect = lambda _s: Contact.objects.filter(pk=self.contact_id).update(live_test=False)
            self.assertEqual(self.reply().data['outcome'], 'not_live_test')
        request.assert_not_called()
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertTrue(Contact.objects.get(pk=plain.id).needs_shadow)   # the cron's shadow answers him as before

    def test_the_bot_asking_for_a_person_marks_the_contact(self):
        self.manychat_message('אפשר לדבר עם נציג?')
        Contact.objects.filter(pk=self.contact_id).update(needs_human=False, needs_human_reason='')   # the door marked it already; test the reply's own mark
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(len(sends(request)), 1)
        self.assertTrue(ShadowReply.objects.get().request_human)
        self.assertTrue(self.owner().needs_human)

    def test_the_cron_says_on_the_contact_when_a_message_was_never_answered(self):
        self.manychat_message('יש חוג קפוארה?')
        last = Message.objects.filter(contact_id=self.contact_id).order_by('-id').first()
        with patch(REQUEST) as request:
            # A moment ago: its call may still be on its way.
            self.assertEqual(live_test.flag_unanswered(), 0)
            # Three minutes, and no call ever took it (the self-call did not arrive, or was cut).
            Message.objects.filter(pk=last.pk).update(created_at=timezone.now() - timedelta(minutes=3))
            ContactEvent.objects.filter(contact_id=self.contact_id).update(created_at=timezone.now() - timedelta(minutes=10))
            Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=timezone.now() + timedelta(seconds=30))
            self.assertEqual(live_test.flag_unanswered(), 0)              # somebody is answering right now
            Contact.objects.filter(pk=self.contact_id).update(live_test_busy_until=None)
            self.assertEqual(cron.tick()['live_test_unanswered'], 1)
            self.assertEqual(live_test.flag_unanswered(), 0)              # said once
        request.assert_not_called()                                      # nothing is sent late
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIn('לא נענתה', contact.needs_human_reason)
        self.assertTrue(self.answers()[-1].text.startswith('הודעה בבדיקה החיה לא נענתה: התקבלה '))
        # The next message is answered as always.
        self.manychat_message('הלו?')
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(live_test.flag_unanswered(), 0)

    def test_the_cron_says_nothing_about_what_the_journal_or_a_person_already_covered(self):
        old = timezone.now() - timedelta(minutes=3)
        # A message from before the test was turned on (setUp's greeting): the test was not on when it came.
        Message.objects.filter(contact_id=self.contact_id).update(created_at=old - timedelta(minutes=5))
        self.assertEqual(live_test.flag_unanswered(), 0)
        # A failure the journal already explains.
        self.manychat_message('שאלה')
        with patch('apps.wahub.live_test.shadow.answer_now', side_effect=shadow.ShadowModelError('Claude ענה 500')):
            self.assertEqual(self.reply().data['outcome'], 'model_error')
        Message.objects.filter(contact_id=self.contact_id, direction='in').update(created_at=old)
        self.assertEqual(live_test.flag_unanswered(), 0)
        self.assertEqual(len(self.answers()), 1)
        # A person holds the conversation, or the office wrote to him since.
        ContactEvent.objects.filter(contact_id=self.contact_id).update(created_at=old - timedelta(minutes=10))
        Contact.objects.filter(pk=self.contact_id).update(handled_by='human')
        self.assertEqual(live_test.flag_unanswered(), 0)
        Contact.objects.filter(pk=self.contact_id).update(handled_by='bot')
        with patch(REQUEST, return_value=OK):
            self.post(f'contacts/{self.contact_id}/send/', {'text': 'עונה לך מהמשרד'})
        self.assertEqual(live_test.flag_unanswered(), 0)
        # A plain contact and a demo are none of this look's business.
        self.incoming('היי', phone='972505550102', subscriber_id='9')
        Message.objects.filter(contact__phone='972505550102').update(created_at=old)
        self.assertEqual(live_test.flag_unanswered(), 0)
        self.assertEqual(len(self.answers()), 1)

    def test_the_cron_shadow_leaves_live_test_contacts_alone(self):
        self.age(self.owner(), last_inbound_at=5)
        self.assertEqual(list(shadow.pending()), [])
        with patch(REQUEST, side_effect=manychat(tags=[START])):
            self.switch(False)
        self.assertEqual([contact.id for contact in shadow.pending()], [self.contact_id])

    def test_outside_the_24_hours_nothing_leaves(self):
        self.age(self.owner(), last_inbound_at=25 * 60)
        with patch(REQUEST) as request:
            response = self.reply()
        request.assert_not_called()
        self.assertEqual(response.data['outcome'], 'failed')
        self.assertIn('24', Message.objects.get(pk=response.data['message_id']).error)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class LiveReplyClaudeTests(LiveTestCase):
    REAL_CLAUDE = True

    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.turn_on()

    def _answer(self, text):
        import json
        return _json_response({
            'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps({'text': text, 'reasoning': 'כי', 'knowledge_ids': []}, ensure_ascii=False)}],
        })

    def test_claude_answers_within_a_lower_ceiling_and_the_answer_is_sent(self):
        with patch(CLAUDE_POST, return_value=self._answer('היי דור, במה אוכל לעזור?')) as post, patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 4000)
        self.assertEqual(post.call_args.kwargs['json']['output_config']['effort'], 'medium')   # the shadow's own, unless told
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, 'היי דור, במה אוכל לעזור?')])
        self.assertEqual(ShadowReply.objects.get().model, 'claude-opus-5-5')
        with override_settings(WAHUB_LIVE_TEST_MAX_TOKENS=3000, WAHUB_LIVE_TEST_EFFORT='low'):
            self.manychat_message('עוד')
            with patch(CLAUDE_POST, return_value=self._answer('כן')) as post, patch(REQUEST, return_value=OK):
                self.reply()
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 3000)
        self.assertEqual(post.call_args.kwargs['json']['output_config']['effort'], 'low')
        # Its own first answer is its side of the conversation, not something the old bot said.
        conversation = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('את (הבוט החדש): היי דור, במה אוכל לעזור?', conversation)
        self.assertNotIn('הבוט הישן: היי דור', conversation)
        with override_settings(WAHUB_LIVE_TEST_EFFORT='fastest'):          # not an effort Claude knows: the shadow's own
            self.manychat_message('ועוד')
            with patch(CLAUDE_POST, return_value=self._answer('כן')) as post, patch(REQUEST, return_value=OK):
                self.reply()
        self.assertEqual(post.call_args.kwargs['json']['output_config']['effort'], 'medium')
        # The demo conversation keeps the shadow's own ceiling.
        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי'})
        with patch(CLAUDE_POST, return_value=self._answer('היי')) as post:
            self.post(f'demo/contacts/{demo["id"]}/reply/', {'post': False})
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 16000)

    def test_claude_failing_sends_nothing_and_asks_for_a_person(self):
        with patch(CLAUDE_POST, return_value=_json_response({'error': {'message': 'boom'}}, 500)), patch(REQUEST) as request:
            response = self.reply()
        self.assertEqual(response.data, {'ok': True, 'outcome': 'model_error', 'error': 'Claude ענה 500'})
        request.assert_not_called()
        self.assertEqual((ShadowReply.objects.count(), Message.objects.filter(direction='out').count()), (0, 0))
        contact = self.owner()
        self.assertTrue(contact.needs_shadow)                            # still unanswered
        self.assertTrue(contact.needs_human)
        self.assertIn('הבוט החדש לא ענה', contact.needs_human_reason)
        self.assertIn('Claude ענה 500', contact.needs_human_reason)
        self.assertIsNone(contact.live_test_busy_until)
        self.assertEqual(len(self.events('needs_human_changed')), 1)
        [event] = self.answers()
        self.assertIn('לא נשלח דבר', event.text)
        self.assertIn('Claude ענה 500', event.text)
        # The next message tries again, and the stub never answers in Claude's place.
        self.manychat_message('הלו?')
        with patch(CLAUDE_POST, side_effect=requests.ReadTimeout('slow')), patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'model_error')
        request.assert_not_called()
        self.assertEqual(len(self.events('needs_human_changed')), 1)      # marked once

    def test_a_fault_of_ours_sends_nothing_says_so_and_frees_the_contact(self):
        with patch('apps.wahub.live_test.shadow.answer_now', side_effect=RuntimeError('boom')), patch(REQUEST) as request, \
                self.assertLogs('apps.wahub.live_test', level='ERROR'):
            response = self.reply()
        self.assertEqual(response.data, {'ok': True, 'outcome': 'model_error', 'error': 'תקלה לא צפויה'})
        request.assert_not_called()
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIsNone(contact.live_test_busy_until)
        self.assertIn('תקלה לא צפויה', self.answers()[-1].text)

    def test_nothing_leaves_when_the_test_is_turned_off_or_a_person_takes_over_while_claude_thinks(self):
        for change, outcome in (({'handled_by': 'human'}, 'human_handles'), ({'live_test': False}, 'not_live_test')):
            with self.subTest(outcome=outcome):
                Contact.objects.filter(pk=self.contact_id).update(handled_by='bot', live_test=True)

                def meanwhile(*args, **kwargs):
                    Contact.objects.filter(pk=self.contact_id).update(**change)
                    return self._answer('היי דור')

                with patch(CLAUDE_POST, side_effect=meanwhile) as post, patch(REQUEST) as request:
                    response = self.reply()
                post.assert_called_once()
                self.assertEqual(response.data['outcome'], outcome)
                request.assert_not_called()
                self.assertEqual(Message.objects.filter(direction='out').count(), 0)
                self.manychat_message(f'הלו {outcome}')


# --- ManyChat's files: fetched into the bucket when answered ----------------------------------------------------

@override_settings(SUPABASE_URL=STORAGE_URL, SUPABASE_SERVICE_ROLE_KEY=SERVICE_KEY)
class LiveMediaTests(LiveTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.storage = FakeStorage()
        patch(STORAGE_REQUEST, side_effect=self.storage).start()
        self.turn_on()
        # The greeting that opened the conversation is not part of these bursts.
        Message.objects.filter(contact_id=self.contact_id).delete()

    def latest(self) -> Message:
        return Message.objects.filter(contact_id=self.contact_id, direction='in').order_by('-id').first()

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_recording_is_fetched_into_the_bucket_transcribed_and_answered(self):
        self.manychat_message(OGG_URL)
        recording = self.latest()
        self.assertEqual((recording.message_type, recording.media_url), ('voice', OGG_URL))
        order = []
        self.sleep.side_effect = lambda seconds: order.append('wait')

        def fetch_it(*args, **kwargs):
            order.append('fetch')
            return fetched(OGG, 'audio/ogg')

        def transcribe_it(*args, **kwargs):
            order.append('transcribe')
            return _json_response({'text': 'יש חוג קפוארה בראש העין לבן 6?'})

        with patch(FETCH, side_effect=fetch_it) as fetch, patch(OPENAI_POST, side_effect=transcribe_it) as transcribe, \
                patch(REQUEST, side_effect=lambda *args, **kwargs: order.append('send') or OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        # Fetched once and transcribed once — before the wait for the burst is slept, not after it.
        self.assertEqual(order, ['fetch', 'transcribe', 'wait', 'send'])
        self.assertEqual(fetch.call_args.args[0], OGG_URL)
        self.assertEqual((fetch.call_args.kwargs['stream'], fetch.call_args.kwargs['allow_redirects']), (True, False))
        self.assertEqual(fetch.call_args.kwargs['timeout'], (5, 10))
        recording.refresh_from_db()
        match = ARCHIVED.match(recording.media_url)
        self.assertIsNotNone(match, recording.media_url)
        self.assertEqual((match.group(1), match.group(2)), (str(self.contact_id), 'ogg'))
        self.assertEqual(self.storage.objects[recording.media_url], (OGG, 'audio/ogg'))
        self.assertEqual(recording.media_url, media.archived_stem(self.contact_id, OGG_URL) + '.ogg')
        [(_, _, upload)] = self.storage.uploads()
        self.assertEqual(upload['headers']['x-upsert'], 'true')            # the same file fetched again lands on itself
        self.assertEqual(recording.transcript, 'יש חוג קפוארה בראש העין לבן 6?')
        filename, audio, media_type = transcribe.call_args.kwargs['files']['file']
        self.assertEqual((audio, media_type), (OGG, 'audio/ogg'))
        [(_, text)] = sends(request)
        self.assertIn('שלישי', text)
        self.assertNotIn('לא הורד', ShadowReply.objects.get().reasoning)
        # The screen now gets a signed address of the copy.
        self.assertIn('/object/sign/', self.client.get(f'{BASE}/messages/{recording.id}/media/').data['url'])

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_the_bucket_is_made_when_the_storage_says_not_found_the_way_supabase_does(self):
        """Production, 11.10.2026: HTTP 400 with statusCode "404" in the body — and the bucket was never made."""
        self.storage.bucket_missing = True
        self.manychat_message(OGG_URL)
        with patch(FETCH, return_value=fetched(OGG, 'audio/ogg')), \
                patch(OPENAI_POST, return_value=_json_response({'text': 'יש חוג קפוארה בראש העין לבן 6?'})), \
                patch(REQUEST, return_value=OK):
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        recording = self.latest()
        self.assertTrue(recording.media_url.startswith('manychat/'), recording.media_url)
        self.assertEqual(recording.transcript, 'יש חוג קפוארה בראש העין לבן 6?')
        self.assertFalse(self.storage.bucket_missing)

    @override_settings(OPENAI_API_KEY='openai-test-key')
    def test_a_recording_is_heard_even_when_the_storage_refuses_the_copy(self):
        """The customer's words do not wait for the storage: transcribed straight from ManyChat's file."""
        self.storage.upload_status = 400
        self.manychat_message(OGG_URL)
        with patch(FETCH, return_value=fetched(OGG, 'audio/ogg')), \
                patch(OPENAI_POST, return_value=_json_response({'text': 'יש חוג קפוארה בראש העין לבן 6?'})) as transcribe, \
                patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        recording = self.latest()
        self.assertEqual(recording.media_url, OGG_URL)                      # the copy failed: ManyChat's address stays
        self.assertEqual(recording.transcript, 'יש חוג קפוארה בראש העין לבן 6?')
        _filename, audio, media_type = transcribe.call_args.kwargs['files']['file']
        self.assertEqual((audio, media_type), (OGG, 'audio/ogg'))
        [(_, text)] = sends(request)
        self.assertIn('שלישי', text)
        self.assertNotIn('לא ניתן לשמוע', text)

    def test_a_picture_reaches_claude_as_an_image_block(self):
        import json
        answer = _json_response({
            'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps({'text': 'איזה חמוד', 'reasoning': 'ראיתי', 'knowledge_ids': []}, ensure_ascii=False)}],
        })
        self.manychat_message(JPG_URL)
        with patch(CLAUDE, REAL_CLAUDE), patch(FETCH, return_value=fetched(JPEG, 'image/jpeg')), \
                patch(CLAUDE_POST, return_value=answer) as post, patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        content = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertEqual([block['type'] for block in content], ['text', 'image'])
        self.assertEqual(content[1]['source']['media_type'], 'image/jpeg')
        self.assertTrue(self.latest().media_url.startswith('manychat/'))
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, 'איזה חמוד')])

        # A picture larger than Claude reads is kept and shown, and answered without being looked at.
        huge = JPEG + b'\x00' * media.IMAGE_MAX_BYTES
        self.manychat_message(another(JPG_URL, 1))
        with patch(CLAUDE, REAL_CLAUDE), patch(FETCH, return_value=fetched(huge, 'image/jpeg')), \
                patch(CLAUDE_POST, return_value=answer) as post, patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertIsInstance(post.call_args.kwargs['json']['messages'][0]['content'], str)
        self.assertTrue(self.latest().media_url.startswith('manychat/'))
        self.assertIn('גדולה מ-5MB', ShadowReply.objects.order_by('-id').first().reasoning)

    def test_a_fetch_that_fails_leaves_the_address_and_the_bot_answers_by_the_voice_rule(self):
        self.manychat_message(OGG_URL)
        with patch(FETCH, side_effect=requests.ConnectionError('down')), patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        self.assertEqual((self.latest().media_url, self.latest().message_type, self.latest().transcript), (OGG_URL, 'voice', ''))
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, VOICE_RULE)])
        self.assertIn('לא הורד', ShadowReply.objects.get().reasoning)
        self.assertEqual(self.storage.uploads(), [])

        # Too big, refused by ManyChat, sent elsewhere, cut in the middle: the same.
        cut = fetched(OGG, 'audio/ogg')
        cut.iter_content.side_effect = requests.exceptions.ChunkedEncodingError('cut')
        for index, response_ in enumerate((
            fetched(b'\x00' * (media.FETCH_MAX_BYTES + 1), 'audio/ogg'), fetched(OGG, 'audio/ogg', status=403),
            fetched(b'', 'text/html', status=302), cut,
        )):
            url = another(OGG_URL, index)
            self.manychat_message(url)
            with patch(FETCH, return_value=response_), patch(REQUEST, return_value=OK) as request:
                self.assertEqual(self.reply().data['outcome'], 'sent')
            self.assertEqual(self.latest().media_url, url)
            self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, VOICE_RULE)])
        self.assertEqual(self.storage.uploads(), [])

    def test_only_an_address_on_manychats_bucket_is_ever_fetched(self):
        with patch(FETCH) as fetch:
            for url in ('https://example.com/a.ogg', 'http://169.254.169.254/latest/a.jpg', OGG_URL.replace('https://', 'http://')):
                with self.assertRaises(media.MediaError):
                    media.archive(self.contact_id, url)
        fetch.assert_not_called()

    def test_the_storage_is_only_ever_asked_for_a_path_this_code_wrote(self):
        # The service-role key reads any bucket; a text a customer typed must never become the path it reads.
        typed = 'HTTPS://manybot-files.s3.amazonaws.com/../../../../other-bucket/secret.jpg'
        self.manychat_message(typed, phone='972505550102', subscriber_id=9)
        message = Message.objects.get(contact__phone='972505550102')
        self.assertEqual((message.message_type, message.media_url, message.text), ('text', '', typed))
        self.assertTrue(media.is_external(typed))
        self.assertTrue(media.is_external('  Https://x.example/a.jpg'))
        # And were such a value ever in media_url, neither the bot nor the screen would hand it to the storage.
        before = len(self.storage.calls)
        for path in (typed, '../other-bucket/secret.jpg', 'demo/1/../../other-bucket/secret.jpg', '/etc/passwd', 'demo/1/a.jpg?x=1', '', 'manychat/x/abc.ogg'):
            with self.assertRaises(media.MediaError):
                media.download(path)
            with self.assertRaises(media.MediaError):
                media.signed_url(path)
        self.assertEqual(len(self.storage.calls), before)
        Message.objects.filter(pk=message.pk).update(message_type='image', media_url='demo/1/../../other-bucket/secret.jpg')
        self.assertEqual(self.client.get(f'{BASE}/messages/{message.id}/media/').status_code, 502)
        self.assertEqual(len(self.storage.calls), before)

    def test_media_manychat_did_not_pass_on_is_answered_like_a_recording_nobody_can_hear(self):
        self.manychat_message('')
        message = self.latest()
        self.assertEqual((message.message_type, message.text), ('other', inbound.MEDIA_NOT_FORWARDED))
        with patch(FETCH) as fetch, patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply().data['outcome'], 'sent')
        fetch.assert_not_called()
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, VOICE_RULE)])
        # With words beside it, the words are answered.
        self.manychat_message('')
        self.manychat_message('יש חוג קפוארה בראש העין לבן 6?')
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertIn('שלישי', sends(request)[0][1])

    def test_an_unknown_file_takes_the_kind_of_its_bytes(self):
        self.manychat_message(BIN_URL)
        self.assertEqual(self.latest().message_type, 'other')
        with patch(FETCH, return_value=fetched(JPEG, 'image/jpeg')), patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        message = self.latest()
        self.assertEqual(message.message_type, 'image')
        self.assertTrue(message.media_url.endswith('.jpg'))
        # Bytes of a kind nobody reads stay where they are, and the bot answers as for a recording it cannot hear.
        self.manychat_message(another(BIN_URL, 1))
        with patch(FETCH, return_value=fetched(b'%PDF', 'application/pdf')), patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual((self.latest().message_type, self.latest().media_url), ('other', another(BIN_URL, 1)))
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, VOICE_RULE)])
        # A video shares its extension with some audio: the type it is served with decides.
        self.manychat_message(MP4_URL)
        with patch(FETCH, return_value=fetched(b'\x00\x00\x00\x18ftypmp42', 'video/mp4')), patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual((self.latest().message_type, self.latest().media_url), ('other', MP4_URL))
        self.manychat_message(another(MP4_URL, 1))
        with patch(FETCH, return_value=fetched(b'\x00\x00\x00\x18ftypM4A ', 'audio/mp4')), patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply().data['outcome'], 'sent')
        self.assertEqual(self.latest().message_type, 'voice')
        self.assertTrue(self.latest().media_url.endswith('.m4a'))

    def test_the_cron_shadow_archives_a_plain_contacts_recording_too(self):
        self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9)
        contact = Contact.objects.get(phone='972505550102')
        self.age(contact, last_inbound_at=1)
        with patch(FETCH, return_value=fetched(OGG, 'audio/ogg')), patch(REQUEST) as request:
            counts = cron.tick()
        self.assertEqual(counts['shadow_proposed'], 1)
        request.assert_not_called()                                      # the shadow never sends
        self.assertEqual(ShadowReply.objects.get(contact=contact).text, VOICE_RULE)   # no transcription key here
        message = Message.objects.get(contact=contact)
        self.assertTrue(ARCHIVED.match(message.media_url))
        self.assertEqual(len(self.storage.uploads()), 1)
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)

    def test_the_cron_shadow_keeps_the_file_of_an_older_message_its_answer_passes_over(self):
        # A recording, and a question five minutes later, before the cron came round: the answer is for the question.
        self.manychat_message(OGG_URL, phone='972505550102', subscriber_id=9)
        contact = Contact.objects.get(phone='972505550102')
        Message.objects.filter(contact=contact).update(sent_at=timezone.now() - timedelta(minutes=5))
        self.manychat_message('יש חוג קפוארה בראש העין לבן 6?', phone='972505550102', subscriber_id=9)
        with patch(FETCH, return_value=fetched(OGG, 'audio/ogg')) as fetch:
            proposal = shadow.propose(contact)
        self.assertEqual(len(proposal.covers_message_ids), 1)
        fetch.assert_called_once()
        recording = Message.objects.get(contact=contact, message_type='voice')
        self.assertTrue(ARCHIVED.match(recording.media_url))


# --- the list of who is under the test ------------------------------------------------------------------------

@override_settings(SUPABASE_URL=STORAGE_URL, SUPABASE_SERVICE_ROLE_KEY=SERVICE_KEY, OPENAI_API_KEY='openai-test-key')
class MediaAtOnceTests(LiveTestCase):
    """A plain contact's recording is fetched and transcribed when it arrives, not at the next cron."""

    PLAIN = '972501112233'

    def setUp(self):
        super().setUp()
        self.storage = FakeStorage()
        patch(STORAGE_REQUEST, side_effect=self.storage).start()

    def test_the_inbound_door_starts_the_preparation_for_a_file_and_not_for_text(self):
        with patch('apps.wahub.views.requests.post', side_effect=requests.ReadTimeout('not waited for')) as post:
            self.assertEqual(self.manychat_message('שלום', phone=self.PLAIN, subscriber_id='777').status_code, 200)
            self.assertEqual(post.call_count, 0)
            self.assertEqual(self.manychat_message(OGG_URL, phone=self.PLAIN, subscriber_id='777').status_code, 200)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith('/wahub/media/prepare/'))
        self.assertEqual(post.call_args.kwargs['timeout'], (2, 1))
        self.assertIn('X-Cron-Token', post.call_args.kwargs['headers'])

    def test_prepare_fetches_keeps_and_transcribes_and_answers_nobody(self):
        with patch('apps.wahub.views.requests.post', side_effect=requests.ReadTimeout('not waited for')):
            self.manychat_message(OGG_URL, phone=self.PLAIN, subscriber_id='777')
        contact = Contact.objects.get(phone=self.PLAIN)
        client = APIClient()
        self.assertEqual(client.post(f'{BASE}/media/prepare/', {'contact': contact.id}, format='json').status_code, 401)
        with patch(FETCH, return_value=fetched(OGG, 'audio/ogg')), \
                patch(OPENAI_POST, return_value=_json_response({'text': 'יש חוג קפוארה לבן 6?'})), \
                patch(REQUEST, side_effect=AssertionError('nothing is sent')) :
            response = client.post(
                f'{BASE}/media/prepare/', {'contact': contact.id}, format='json', HTTP_X_CRON_TOKEN='cron-secret',
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['prepared'], 1)
        recording = Message.objects.get(contact=contact, direction='in')
        self.assertEqual(recording.transcript, 'יש חוג קפוארה לבן 6?')
        self.assertTrue(recording.media_url.startswith('manychat/'))
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertEqual(Message.objects.filter(contact=contact, direction='out').count(), 0)


class LiveTestListTests(LiveTestCase):
    def test_the_contacts_list_filters_by_live_test(self):
        self.incoming('שלום', phone='972505550102', subscriber_id='9')
        self.turn_on()
        everybody = self.get('contacts/', view='chats').data['results']
        self.assertEqual(len(everybody), 2)
        self.assertEqual({row['phone']: row['live_test'] for row in everybody}, {OWNER: True, '972505550102': False})
        for value in ('1', 'true'):
            only = self.get('contacts/', view='chats', live_test=value, page_size=100).data['results']
            self.assertEqual([row['id'] for row in only], [self.contact_id])
        # Anything else is no filter at all, as for a server that never knew it.
        self.assertEqual(len(self.get('contacts/', view='chats', live_test='0').data['results']), 2)
        self.assertEqual(self.get('contacts/counts/', live_test='1').data['boxes']['all'], 1)
        with patch(REQUEST, side_effect=manychat(tags=[START])):
            self.switch(False)
        self.assertEqual(self.get('contacts/', view='chats', live_test='1').data['results'], [])


# --- the schema, for the code that is already running -----------------------------------------------------

class LiveTestSchemaTests(WahubTestCase):
    def test_the_database_itself_fills_live_test_for_code_that_does_not_know_it(self):
        # A branch's build migrates production while the previous code still creates a contact for every
        # new number (migration 0004): the default must be the database's own, not only Django's.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name IN ('wahub_contacts', 'wahub_messages') AND column_name IN ('live_test', 'live_test_busy_until', 'raw') "
                "ORDER BY column_name"
            )
            self.assertEqual(cursor.fetchall(), [('live_test', 'NO', 'false'), ('live_test_busy_until', 'YES', None), ('raw', 'YES', None)])
