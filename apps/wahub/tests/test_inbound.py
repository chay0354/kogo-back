"""ManyChat's copy of the messages: the key, the two bodies, and what is and is not kept."""
from datetime import timedelta

from django.utils import timezone

from apps.core.models import IntegrationCredential, OfficeAlert
from apps.wahub.models import Contact, ContactEvent, Message
from apps.wahub.tests.base import BASE, PHONE, WahubTestCase
from rest_framework.test import APIClient

URL = f'{BASE}/inbound/manychat/'


class InboundKeyTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.anonymous = APIClient()
        self.body = {'event': 'customer_message', 'phone': '050-5550101', 'name': 'רותם', 'text': 'שלום'}

    def _key(self) -> str:
        return self.post('settings/inbound-key/').data['key']

    def test_no_key_configured_is_503_and_nothing_is_kept(self):
        response = self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY='anything')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Contact.objects.exists())

    def test_missing_key_is_403(self):
        self._key()
        response = self.anonymous.post(URL, self.body, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Contact.objects.exists())

    def test_wrong_key_is_403(self):
        self._key()
        response = self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY='not-the-key')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Contact.objects.exists())

    def test_right_key_stores_the_message(self):
        key = self._key()
        response = self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY=key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'ok': True, 'stored': True})
        contact = Contact.objects.get()
        self.assertEqual(contact.phone, PHONE)
        self.assertEqual(contact.name, 'רותם')

    def test_the_key_is_long_shown_once_and_kept_only_as_a_hash(self):
        key = self._key()
        self.assertGreaterEqual(len(key), 40)
        stored = IntegrationCredential.objects.get(key='WAHUB_INBOUND_KEY')
        self.assertNotIn(key, stored.value)
        self.assertTrue(stored.value.startswith('sha256$'))
        self.assertEqual(stored.updated_by, self.manager)

    def test_a_new_key_replaces_the_old_one(self):
        old = self._key()
        new = self._key()
        self.assertNotEqual(old, new)
        self.assertEqual(self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY=old).status_code, 403)
        self.assertEqual(self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY=new).status_code, 200)

    def test_a_key_stored_by_hand_in_plain_text_also_works(self):
        IntegrationCredential.objects.create(key='WAHUB_INBOUND_KEY', value='typed-by-hand-key')
        ok = self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY='typed-by-hand-key')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.anonymous.post(URL, self.body, format='json', HTTP_X_WAHUB_KEY='other').status_code, 403)

    def test_a_logged_in_manager_without_the_key_is_still_refused(self):
        self._key()
        self.assertEqual(self.client.post(URL, self.body, format='json').status_code, 403)

    def test_a_body_that_is_not_json_is_answered_and_not_kept(self):
        key = self._key()
        response = self.anonymous.post(URL, '{not json', content_type='application/json', HTTP_X_WAHUB_KEY=key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'ok': True, 'stored': False})

    def test_the_event_can_come_from_the_address(self):
        key = self._key()
        body = {'id': 77, 'whatsapp_phone': '972505550101', 'custom_fields': {'Last_AI_Respone': 'שלום, כאן דנה'}}
        response = self.anonymous.post(f'{URL}?event=bot_reply', body, format='json', HTTP_X_WAHUB_KEY=key)
        self.assertEqual(response.data, {'ok': True, 'stored': True})
        message = Message.objects.get()
        self.assertEqual((message.direction, message.sender, message.text), ('out', 'bot', 'שלום, כאן דנה'))

    def test_status_says_whether_the_door_is_open(self):
        self.assertFalse(self.get('status/').data['inbound_configured'])
        self._key()
        data = self.get('status/').data
        self.assertTrue(data['inbound_configured'])
        self.assertIsNotNone(data['inbound_key_set_at'])
        self.assertTrue(data['inbound_url'].endswith('/api/v1/wahub/inbound/manychat/'))


class InboundBodyTests(WahubTestCase):
    def test_compact_body(self):
        from apps.wahub import inbound

        result = inbound.handle_payload({
            'event': 'customer_message', 'subscriber_id': '555', 'phone': '+972 50-555-0101',
            'name': 'רותם לוי', 'text': 'אשמח לפרטים על קפוארה', 'ts': timezone.now().isoformat(),
        })
        self.assertTrue(result.stored)
        contact = self.contact()
        self.assertEqual(contact.manychat_subscriber_id, '555')
        self.assertEqual(contact.source, 'whatsapp')
        message = Message.objects.get()
        self.assertEqual((message.direction, message.sender, message.status), ('in', 'customer', 'received'))
        self.assertEqual(message.source, 'manychat')

    def test_full_contact_data_body_with_custom_fields_as_a_dictionary(self):
        from apps.wahub import inbound

        result = inbound.handle_payload({
            'id': 9001, 'first_name': 'רותם', 'last_name': 'לוי', 'whatsapp_phone': '972505550101',
            'phone': '', 'last_input_text': 'יש מקום בחוג?',
            'custom_fields': {'Client_Phone': '0509999999', 'Client_Last_Input': 'ישן'},
        })
        self.assertTrue(result.stored)
        contact = self.contact()
        self.assertEqual(contact.name, 'רותם לוי')
        self.assertEqual(contact.manychat_subscriber_id, '9001')
        self.assertEqual(Message.objects.get().text, 'יש מקום בחוג?')

    def test_full_contact_data_body_with_custom_fields_as_a_list(self):
        from apps.wahub import inbound

        result = inbound.handle_payload({
            'id': 9002, 'name': 'נועם', 'whatsapp_phone': None, 'phone': None, 'last_input_text': None,
            'custom_fields': [
                {'id': 1, 'name': 'kogo_whatsapp_phone', 'type': 'text', 'value': '050-5550101'},
                {'id': 2, 'name': 'Client_Phone', 'type': 'text', 'value': '0508888888'},
                {'id': 3, 'name': 'Client_Last_Input', 'type': 'text', 'value': 'מה המחיר?'},
            ],
        })
        self.assertTrue(result.stored)
        self.assertEqual(self.contact().phone, PHONE)
        self.assertEqual(Message.objects.get().text, 'מה המחיר?')

    def test_phone_order_whatsapp_then_kogo_field_then_bot_field_then_phone(self):
        from apps.wahub import inbound

        fields = {'kogo_whatsapp_phone': '0505550102', 'Client_Phone': '0505550103'}
        parsed = inbound.parse_payload({'whatsapp_phone': '0505550101', 'phone': '0505550104', 'custom_fields': fields})
        self.assertEqual(parsed['phone'], '0505550101')
        parsed = inbound.parse_payload({'phone': '0505550104', 'custom_fields': fields})
        self.assertEqual(parsed['phone'], '0505550102')
        parsed = inbound.parse_payload({'phone': '0505550104', 'custom_fields': {'Client_Phone': '0505550103'}})
        self.assertEqual(parsed['phone'], '0505550103')
        self.assertEqual(inbound.parse_payload({'phone': '0505550104'})['phone'], '0505550104')

    def test_a_timestamped_copy_of_a_field_answers_for_the_clean_name(self):
        from apps.wahub import inbound

        parsed = inbound.parse_payload({
            'custom_fields': {'Client_Phone (2026-07-26 07:28:15)': '0505550101', 'Client_Phone': ''},
            'last_input_text': 'היי',
        })
        self.assertEqual(parsed['phone'], '0505550101')

    def test_bot_reply_text_comes_from_text_or_the_bot_field(self):
        from apps.wahub import inbound

        direct = inbound.parse_payload({'event': 'bot_reply', 'text': 'תשובה', 'last_input_text': 'שאלה'})
        self.assertEqual(direct['text'], 'תשובה')
        field = inbound.parse_payload(
            {'last_input_text': 'שאלה', 'custom_fields': {'Last_AI_Respone': 'תשובת הבוט'}}, 'bot_reply',
        )
        self.assertEqual(field['text'], 'תשובת הבוט')

    def test_invalid_or_foreign_phone_is_not_kept(self):
        for phone in ('', '12345', '+1 415 555 1234', '03-9001234', 'abc'):
            with self.subTest(phone=phone):
                result = self.incoming(phone=phone)
                self.assertFalse(result.stored)
                self.assertEqual(result.reason, 'invalid_phone')
        self.assertFalse(Contact.objects.exists())

    def test_empty_text_is_not_kept(self):
        result = self.incoming(text='   ')
        self.assertFalse(result.stored)
        self.assertFalse(Contact.objects.exists())

    def test_an_unknown_event_is_not_kept(self):
        from apps.wahub import inbound

        self.assertFalse(inbound.handle_payload({'event': 'something_else', 'phone': PHONE, 'text': 'x'}).stored)

    def test_the_same_text_within_30_seconds_is_kept_once(self):
        self.assertTrue(self.incoming('כן').stored)
        again = self.incoming('כן')
        self.assertFalse(again.stored)
        self.assertEqual(again.reason, 'duplicate')
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.contact().unread_count, 1)

    def test_the_same_text_later_or_in_the_other_direction_is_a_new_message(self):
        self.incoming('כן')
        Message.objects.update(created_at=timezone.now() - timedelta(seconds=31))
        self.assertTrue(self.incoming('כן').stored)
        self.assertTrue(self.bot_reply('כן').stored)
        self.assertEqual(Message.objects.count(), 3)

    def test_a_time_from_manychat_is_used_when_believable(self):
        written = timezone.now() - timedelta(minutes=3)
        self.incoming('ראשונה', ts=written.isoformat())
        self.assertEqual(Message.objects.get().sent_at, written)
        self.incoming('שנייה', ts=int((timezone.now() - timedelta(minutes=1)).timestamp()))
        self.assertLess(timezone.now() - Message.objects.order_by('-id').first().sent_at, timedelta(minutes=2))

    def test_a_time_in_the_future_or_long_ago_is_replaced_by_now(self):
        for ts in ((timezone.now() + timedelta(hours=3)).isoformat(), '2020-01-01T10:00:00+02:00', 'not a time'):
            Message.objects.all().delete()
            Contact.objects.all().delete()
            self.incoming('היי', ts=ts)
            self.assertLess(abs(timezone.now() - Message.objects.get().sent_at), timedelta(seconds=30))

    def test_the_first_message_opens_a_contact_and_a_journal_line(self):
        self.incoming('שלום', name='רותם')
        contact = self.contact()
        self.assertEqual(list(ContactEvent.objects.filter(contact=contact).values_list('kind', flat=True)), ['created'])
        self.incoming('ועוד שאלה')
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='created').count(), 1)

    def test_a_name_the_office_typed_is_not_overwritten(self):
        self.incoming('שלום', name='רותם')
        Contact.objects.update(name='רותם לוי (אמא של נועה)')
        self.incoming('עוד הודעה', name='Rotem')
        self.assertEqual(self.contact().name, 'רותם לוי (אמא של נועה)')


class AsksForHumanTests(WahubTestCase):
    def test_the_phrases(self):
        from apps.wahub.inbound import asks_for_human

        for text in (
            'אפשר נציג?', 'אני רוצה לדבר עם בן אדם', 'יש מענה אנושי?', 'אשמח שיחזרו אליי',
            'תחזרו אליי בבקשה', 'אפשר לדבר עם מישהו?', 'בנאדם אמיתי בבקשה', 'תחזרו אלי',
        ):
            self.assertTrue(asks_for_human(text), text)
        for text in ('שלום', 'מה המחיר של החוג?', 'הילד בן 5', 'תודה רבה'):
            self.assertFalse(asks_for_human(text), text)

    def test_asking_for_a_person_lights_the_contact_and_alerts_the_office_once_a_day(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.incoming('אפשר לדבר עם נציג בבקשה?', name='רותם')
        contact = self.contact()
        self.assertTrue(contact.needs_human)
        self.assertEqual(contact.needs_human_reason, 'ביקש נציג')
        self.assertIsNotNone(contact.needs_human_at)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='needs_human_changed').count(), 1)
        alert = OfficeAlert.objects.get()
        self.assertEqual(alert.kind, 'wahub_needs_human')
        self.assertIn(str(contact.id), alert.dedup_key)
        self.assertIn('050-5550101', alert.customer)
        # No office template is set up in tests: the alert is kept, nothing is sent.
        self.assertEqual(alert.status, OfficeAlert.STATUS_NOT_CONFIGURED)

        with self.captureOnCommitCallbacks(execute=True):
            self.incoming('נציג!!')
        self.assertEqual(OfficeAlert.objects.count(), 1)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='needs_human_changed').count(), 1)

    def test_a_bot_reply_never_lights_it(self):
        self.incoming('שלום')
        self.bot_reply('נציג יחזור אליך בהקדם')
        self.assertFalse(self.contact().needs_human)

    def test_marking_handled_and_marking_by_hand(self):
        self.incoming('נציג בבקשה')
        contact = self.contact()
        response = self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['chat']['needs_human'])
        self.assertEqual(response.data['chat']['needs_human_reason'], '')
        self.assertIsNone(response.data['chat']['needs_human_at'])

        response = self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': True, 'reason': 'ביקש הנחה'})
        self.assertTrue(response.data['chat']['needs_human'])
        self.assertEqual(response.data['chat']['needs_human_reason'], 'ביקש הנחה')
        self.assertEqual(self.post(f'contacts/{contact.id}/needs-human/', {}).status_code, 400)
