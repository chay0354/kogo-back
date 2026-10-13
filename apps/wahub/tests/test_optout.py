"""
"הסר" in the WhatsApp section (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.1): which incoming messages are a
request and which are not, what a request does — the system's list, the mark on the contact, the journal —
the two actions of a person, and what stops because of it: the bot's own follow-ups to a lead, and the
telling that a seat came free. What a person writes to one customer is not stopped.

Nothing here reaches ManyChat: `self.network` fails any test that tries.
"""
from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError
from django.test import override_settings

from apps.core import whatsapp_optout
from apps.core.models import UserProfile, WhatsAppOptOut
from apps.wahub import knowledge, knowledge_import, lead_followups, lead_stage, optout, sending, settings_store, shadow, waiting_list
from apps.wahub.models import Contact, ContactEvent, KnowledgeItem, LeadFollowUp, Message
from apps.wahub.tests.base import PHONE, WahubTestCase, client_for, make_user
from apps.wahub.tests.test_lead_followups import OK, REQUEST, SENDING_ON, EngineCase, at
from apps.wahub.tests.test_leads_v2 import DAY, MINUTE, TODAY
from apps.wahub.tests.test_waiting_notify import NotifyCase

LIST_DOWN = patch('apps.core.whatsapp_optout._rows', side_effect=DatabaseError('the list cannot be read'))

REQUESTS = (
    'הסר', 'הסר.', 'הסר!!', 'הסירו', 'הסרה', 'להסיר', 'תסירו', 'הסר בבקשה', 'תודה, הסר', 'הסירו אותי', 'הסר 🙏', 'הֲסֵר',
    'STOP', 'Stop', 'unsubscribe', 'remove me',
    'הסירו אותי מרשימת התפוצה', 'להסיר אותי מהרשימה בבקשה', 'אפשר להסיר אותי מהרשימה?', 'תסירו אותי מהתפוצה',
    'נא להסיר אותי מרשימת הדיוור', 'הסרה מהרשימה', 'מבקשת הסרה', 'מבקש להסיר', 'הסר אותי', 'הסר מתפוצה', 'אנא הסירו',
    'אני מבקש שתסירו אותי מרשימת התפוצה', 'אבקש להסיר אותי מרשימת התפוצה שלכם', 'תורידו אותי', 'תמחקו את המספר שלי',
    'תסירו אותי בבקשה מרשימת התפוצה', 'תפסיקו לשלוח', 'אל תשלחו לי', 'אל־תשלחו לי יותר', 'REMOVE',
    'תפסיקו לשלוח לי הודעות', 'שלום, תפסיקו לשלוח לי הודעות בבקשה, זה מטריד', 'די עם ההודעות',
    'אל תשלחו לי יותר', 'לא לשלוח לי יותר הודעות', 'לא מעוניין לקבל הודעות', 'לא מעוניינת. הסר',
    'stop sending me messages', 'please remove me from the list', "don't message me anymore",
)
NOT_REQUESTS = (
    # A cancellation of a class, or a word about something else: the bot's and the office's, as before.
    'אפשר להסיר את הילד מהחוג?', 'הסר את הבן שלי מהשיעור של יום שלישי', 'אני רוצה להסיר את המנוי', 'תסירו את החיוב הכפול',
    'הסרתי את האפליקציה', 'מה זה הסר?', 'לא להסיר', 'אל תסירו אותי', 'אפשר להוריד אותי מהחוג של יום שלישי?',
    'רוצה להסיר את הבן מהחוג', 'להוריד', 'תפסיקו', 'די', 'לא מעוניין', 'לא רלוונטי',
    # An answer to "shall we send you the link?" is not a request to be taken off anything.
    'לא לשלוח',
    # About one kind of message, not about all of them.
    'אל תשלחו לי את הקבלה למייל הזה', 'תפסיקו לשלוח לי את החשבוניות בדואר', 'לא לשלוח לי תזכורת',
    'אם לא תפסיקו לשלוח לי חיובים אני אפנה לבנק',
    # The word is only somewhere inside.
    'רציתי לשאול לגבי החוג של הבן, להסיר, או להעביר ליום אחר?',
    'ביקשתי כבר פעמיים, הסר, ולא עשיתם כלום עם זה אז אני שואלת שוב מה קורה',
    'היי, אני מחפש חוג קפוארה לילד בן 5 ולא רוצה שתסירו אותי מהרשימה של העדכונים כי זה חשוב לי מאוד מאוד',
    'כתבתם להשיב הסר אז מה זה אומר',
    'שלום', 'כן', '', 'stop by tomorrow?', 'https://example.com/a.ogg',
)


class WordsTests(WahubTestCase):
    def test_what_is_a_request(self):
        for text in REQUESTS:
            with self.subTest(text=text):
                self.assertTrue(optout.is_request(text))

    def test_what_is_not(self):
        for text in NOT_REQUESTS:
            with self.subTest(text=text):
                self.assertFalse(optout.is_request(text))


class IncomingTests(WahubTestCase):
    def test_a_request_puts_the_phone_on_the_list_and_says_so_on_the_contact(self):
        result = self.incoming('הסר', name='רותם')
        self.assertTrue(result.stored)
        contact = self.contact()
        row = WhatsAppOptOut.objects.get()
        self.assertEqual((row.phone, row.source, row.text), (PHONE, 'message', 'הסר'))
        self.assertEqual((row.wahub_contact_id, row.wahub_message_id, row.opted_out_by), (contact.id, result.message_id, None))
        self.assertTrue(whatsapp_optout.is_blocked('050-555-0101'))
        self.assertEqual((contact.opted_out['source'], contact.opted_out['text']), ('message', 'הסר'))
        self.assertEqual(
            list(ContactEvent.objects.filter(contact=contact, kind='opt_out').values_list('text', flat=True)), ['ביקש הסר: "הסר"'],
        )
        # It asks for nobody and changes nothing else about the conversation: the message is a message.
        self.assertEqual((contact.needs_human, contact.handled_by, contact.unread_count), (False, 'bot', 1))
        self.assertEqual(Message.objects.get().text, 'הסר')

        data = self.get(f'contacts/{contact.id}/').data
        self.assertEqual(data['opted_out'], {
            'at': contact.opted_out['at'], 'source': 'message', 'source_label': 'ביקש בהודעה', 'by_name': None, 'text': 'הסר',
            'label': 'ביקש הסר',
        })
        self.assertEqual([event['kind_label'] for event in data['events'] if event['kind'] == 'opt_out'], ['הסר מתפוצה'])
        self.assertEqual(self.get('contacts/').data['results'][0]['opted_out']['label'], 'ביקש הסר')

    def test_nobody_else_carries_the_mark(self):
        self.incoming('אפשר להסיר את הילד מהחוג?')
        contact = self.contact()
        self.assertEqual(contact.opted_out, {})
        self.assertIsNone(self.get(f'contacts/{contact.id}/').data['opted_out'])
        self.assertFalse(WhatsAppOptOut.objects.exists())

    def test_asking_again_changes_nothing(self):
        self.incoming('הסר')
        first = self.contact().opted_out
        self.bot_reply('הוסרתם')
        self.incoming('הסירו אותי מרשימת התפוצה')
        self.assertEqual(self.contact().opted_out, first)
        self.assertEqual(WhatsAppOptOut.objects.count(), 1)
        self.assertEqual(ContactEvent.objects.filter(kind='opt_out').count(), 1)

    def test_the_bots_own_line_is_never_a_request(self):
        self.incoming('שלום')
        self.bot_reply('הסר')
        self.assertEqual(self.contact().opted_out, {})

    def test_a_demo_contact_is_marked_without_touching_the_systems_list(self):
        demo = self.make_contact(phone='972505550999', is_demo=True, source='demo')
        self.incoming('הסר', phone=demo.phone)
        demo.refresh_from_db()
        self.assertEqual(demo.opted_out['source'], 'message')
        self.assertFalse(WhatsAppOptOut.objects.exists())
        self.assertTrue(optout.blocked(demo))

    def test_a_list_that_cannot_be_written_never_loses_the_message(self):
        with LIST_DOWN, self.assertLogs('apps.wahub.optout', level='ERROR'):
            result = self.incoming('הסר')
        self.assertTrue(result.stored)
        contact = self.contact()
        self.assertEqual((contact.opted_out, contact.messages_count), ({}, 1))
        self.assertFalse(ContactEvent.objects.filter(kind='opt_out').exists())

    def test_nothing_is_asked_of_manychat(self):
        with override_settings(**SENDING_ON), patch(REQUEST) as request:
            self.incoming('הסר')
            self.post(f'contacts/{self.contact().id}/opt-in/')
            self.post(f'contacts/{self.contact().id}/opt-out/')
        request.assert_not_called()


class ActionsTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום', name='רותם')
        self.row = self.contact()

    def test_a_person_marks_it_and_takes_it_off(self):
        response = self.post(f'contacts/{self.row.id}/opt-out/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            {key: response.data['opted_out'][key] for key in ('source', 'source_label', 'by_name', 'text', 'label')},
            {'source': 'office', 'source_label': 'סומן על ידי נציג', 'by_name': 'דנה מנהלת', 'text': '', 'label': 'ביקש הסר'},
        )
        listed = WhatsAppOptOut.objects.get()
        self.assertEqual((listed.phone, listed.source, listed.opted_out_by), (PHONE, 'office', self.manager))
        # A second press changes nothing.
        self.assertEqual(self.post(f'contacts/{self.row.id}/opt-out/').data['opted_out']['at'], response.data['opted_out']['at'])
        self.assertEqual(WhatsAppOptOut.objects.count(), 1)

        back = self.post(f'contacts/{self.row.id}/opt-in/')
        self.assertEqual((back.status_code, back.data['opted_out']), (200, None))
        listed.refresh_from_db()
        self.assertEqual(listed.restored_by, self.manager)
        self.assertFalse(whatsapp_optout.is_blocked(PHONE))
        self.assertEqual(self.post(f'contacts/{self.row.id}/opt-in/').data['opted_out'], None)       # already off: nothing to do
        journal = list(ContactEvent.objects.filter(contact=self.row, kind='opt_out').order_by('id').values_list('text', 'actor_id'))
        self.assertEqual(journal, [
            ('סומן "הסר" על ידי נציג — לא יקבל הודעות תפוצה', self.manager.id), ('הוחזר לתפוצה — ביקש לחזור לקבל הודעות', self.manager.id),
        ])

    def test_after_coming_back_a_new_request_puts_them_on_the_list_again(self):
        self.post(f'contacts/{self.row.id}/opt-out/')
        self.post(f'contacts/{self.row.id}/opt-in/')
        self.incoming('הסר')
        self.assertEqual(self.contact().opted_out['source'], 'message')
        self.assertEqual(WhatsAppOptOut.objects.filter(restored_at__isnull=True).count(), 1)
        self.assertEqual(WhatsAppOptOut.objects.count(), 2)

    def test_a_list_that_cannot_be_written_changes_nothing(self):
        for action in ('opt-out', 'opt-in'):
            with LIST_DOWN, self.assertLogs('apps.core.whatsapp_optout', level='ERROR'):
                response = self.post(f'contacts/{self.row.id}/{action}/')
            self.assertEqual((response.status_code, response.data['code']), (503, 'opt_out_list_unavailable'), action)
        self.assertEqual(self.contact().opted_out, {})
        self.assertFalse(ContactEvent.objects.filter(kind='opt_out').exists())

    def test_managers_only_and_a_contact_that_exists(self):
        for role in (UserProfile.ROLE_WORKER, UserProfile.ROLE_PARTNER):
            other = client_for(make_user(f'{role}@optout.test', role=role))
            for action in ('opt-out', 'opt-in'):
                self.assertEqual(other.post(f'/api/v1/wahub/contacts/{self.row.id}/{action}/').status_code, 403)
        self.assertEqual(self.post('contacts/999999/opt-out/').status_code, 404)
        self.assertEqual(self.contact().opted_out, {})

    def test_a_person_still_writes_to_one_customer(self):
        """An answer in the conversation is not a broadcast: the list does not stop it (ט.1.5)."""
        self.post(f'contacts/{self.row.id}/opt-out/')
        with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True):
            response = self.post(f'contacts/{self.row.id}/send/', {'text': 'היי, רק מוודאת שקיבלתם את האישור'})
        self.assertEqual((response.status_code, response.data['message']['status']), (200, 'simulated'))


class LeadTests(EngineCase):
    def test_no_follow_up_is_planned_for_somebody_who_asked(self):
        asked = {'at': self.clock().isoformat(), 'source': 'message', 'text': 'הסר'}
        contact = self.link_lead(opted_out=asked)
        self.assertEqual(self.rows(contact), [])
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('link_sent', ''))               # nobody's move
        self.assertIn('ביקש הסר — הבוט לא חוזר אליו', contact.lead_reason)
        # A hot lead — a trial that took place — is the office's to call, as every hot lead the bot cannot come back to.
        hot = self.trial_lead(opted_out=asked)
        self.assertEqual(self.rows(hot), [])
        self.assertEqual((hot.lead_stage, hot.lead_owner), ('trial_done', 'office'))
        self.assertIn('ביקש הסר — הבוט לא חוזר אליו', hot.lead_reason)

    def test_a_lead_that_is_not_hot_is_nobodys_move_and_says_why(self):
        contact = self.lead(DAY, said={'summary': 'שאלה על קפוארה', 'will_check': True},
                            opted_out={'at': self.clock().isoformat(), 'source': 'office', 'by_name': 'מיכל'})
        contact = self.staged(contact)
        self.assertEqual(contact.lead_owner, '')
        self.assertEqual(self.rows(contact), [])
        row = next(row for row in self.get('leads/', owner='bot').data['results'] if row['contact_id'] == contact.id)
        self.assertEqual(row['next'], {'who': 'none', 'what': 'ביקש הסר — הבוט לא חוזר אליו', 'when': None, 'state': 'none', 'note': ''})
        self.assertEqual((row['opted_out']['source'], row['opted_out']['by_name'], row['opted_out']['label']), ('office', 'מיכל', 'ביקש הסר'))
        self.assertEqual(self.get(f'leads/{contact.id}/').data['opted_out']['label'], 'ביקש הסר')

    def test_a_follow_up_planned_before_they_asked_is_called_off(self):
        contact = self.link_lead()
        self.assertEqual(self.open_row(contact).status, 'planned')
        self.clock.forward(minutes=5)
        self.incoming('הסר', phone=contact.phone)
        (row,) = self.rows(contact)
        self.assertEqual((row.status, row.outcome_note), ('skipped', 'ביקש הסר'))
        details = self.get(f'leads/{contact.id}/').data
        self.assertEqual([(step['status'], step['outcome_note']) for step in details['details']['followups']], [('skipped', 'ביקש הסר')])
        # ... and coming back to the list lets the bot come back to them.
        self.post(f'contacts/{contact.id}/opt-in/')
        self.assertEqual(self.get(f'leads/{contact.id}/').data['opted_out'], None)

    @override_settings(**SENDING_ON)
    def test_the_list_is_read_again_right_before_a_send(self):
        """A phone another part of the system put on the list — the contact does not say so yet — still gets nothing."""
        contact = self.link_lead()
        whatsapp_optout.add(contact.phone, source=WhatsAppOptOut.SOURCE_OFFICE)
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            counts = lead_followups.run()
        request.assert_not_called()
        self.assertEqual(counts['followups_skipped'], 1)
        self.assertEqual([(row.status, row.outcome_note) for row in self.rows(contact)], [('skipped', 'ביקש הסר')])
        # The contact has taken the list's row as its mark, so the rules plan nothing more.
        self.assertEqual(self.fresh(contact).opted_out['source'], 'office')
        self.assertEqual(Message.objects.filter(contact=contact).count(), 0)

    @override_settings(**SENDING_ON)
    def test_when_the_list_cannot_be_read_the_follow_up_waits_and_nothing_leaves(self):
        contact = self.link_lead(30 * MINUTE)                            # wrote at 11:30: WhatsApp's window is open until 11:30 tomorrow
        self.clock.moment = at(2026, 10, 13)
        with LIST_DOWN, patch(REQUEST, return_value=OK) as request, self.assertLogs('apps.core.whatsapp_optout', level='ERROR'):
            lead_followups.run()
        request.assert_not_called()
        row = self.open_row(contact)
        self.assertEqual((row.status, row.next_look_at), ('planned', self.clock() + lead_followups.LOOK_AGAIN))
        # The list is back: the same follow-up leaves.
        self.clock.forward(minutes=30)
        with patch(REQUEST, return_value=OK):
            self.assertEqual(lead_followups.run()['followups_sent'], 1)

    @override_settings(**SENDING_ON)
    def test_the_way_out_itself_refuses_a_follow_up_to_somebody_who_asked(self):
        """Said once more at the door every follow-up of the bot leaves through, whoever calls it."""
        contact = self.link_lead(opted_out={'at': self.clock().isoformat(), 'source': 'message', 'text': 'הסר'})
        with patch(REQUEST, return_value=OK) as request:
            as_text = sending.send_lead_followup(contact, 'היי, רציתי לבדוק אם הצלחתם להירשם')
            as_template = sending.send_lead_followup_flow(contact, 'content_x', 'היי, רציתי לבדוק אם הצלחתם להירשם')
        request.assert_not_called()
        for message in (as_text, as_template):
            self.assertEqual((message.status, message.error), ('failed', 'הלקוח ביקש הסר — הודעה שהבוט יוזם לא נשלחת אליו.'))
        # A failed line is not an answer: the conversation's last message and its clock did not move.
        self.assertEqual(self.fresh(contact).last_message_sender, 'bot')
        self.assertEqual(self.fresh(contact).last_message_at, contact.last_message_at)

    def test_the_one_message_to_many_dormant_leads_skips_whoever_asked(self):
        asked = self.staged(self.lead(40 * DAY, is_demo=True, source='demo', opted_out={'at': self.clock().isoformat(), 'source': 'message'}))
        other = self.staged(self.lead(40 * DAY, is_demo=True, source='demo'))
        self.assertEqual((asked.lead_stage, other.lead_stage), ('cold', 'cold'))
        settings_store.put(settings_store.LEAD_DORMANT_ON, TODAY.isoformat())
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_dormant', 'title': 'נפתחה הרשמה', 'body': 'היי, נפתחה ההרשמה 🙂'})
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 1)
        self.assertEqual(self.rows(asked), [])
        self.assertEqual([row.kind for row in self.rows(other)], ['dormant'])


class SeatFreedTests(NotifyCase):
    @override_settings(**SENDING_ON)
    def test_the_telling_of_a_free_seat_does_not_go_to_somebody_who_asked(self):
        self.post(f'contacts/{self.lead.id}/opt-out/')
        preview = self.get(f'waiting-list/{self.entry_id}/notify/').data['notify']
        self.assertEqual((preview['can'], preview['blocked']), (False, 'opted_out'))
        self.assertIn('הלקוח ביקש הסר', preview['detail'])
        pressed = self.post(f'waiting-list/{self.entry_id}/notify/')
        self.assertEqual(pressed.status_code, 200, pressed.data)
        self.assertEqual((pressed.data['notify']['sent'], pressed.data['notify']['code']), (False, 'opted_out'))
        self.assertEqual(Message.objects.filter(contact=self.lead).count(), self.messages)
        self.assertIsNone(self.entry().notified_at)
        self.assertEqual(self.manychat.sends, 0)

        # Back on the list of who may be written to: the same press goes out.
        self.post(f'contacts/{self.lead.id}/opt-in/')
        self.assertTrue(self.post(f'waiting-list/{self.entry_id}/notify/').data['notify']['sent'])

    @override_settings(**SENDING_ON)
    def test_a_list_that_cannot_be_read_stops_it_too(self):
        with LIST_DOWN, self.assertLogs('apps.core.whatsapp_optout', level='ERROR'):
            pressed = self.post(f'waiting-list/{self.entry_id}/notify/')
        self.assertEqual((pressed.data['notify']['sent'], pressed.data['notify']['code']), (False, 'opt_out_list_unavailable'))
        self.assertEqual(Message.objects.filter(contact=self.lead).count(), self.messages)


class BotAnswerTests(WahubTestCase):
    def test_the_rule_and_the_phrasing_are_filed_with_the_knowledge(self):
        knowledge_import.run()
        rule = KnowledgeItem.objects.get(title='"הסר" — מאשרים בשורה אחת')
        phrasing = KnowledgeItem.objects.get(key='opt_out_done')
        self.assertTrue(rule.is_active and phrasing.is_active)
        self.assertIn('בעל המערכת 13.10.2026 (ערב)', rule.source_note)
        self.assertIn('בעל המערכת 13.10.2026 (ערב)', phrasing.source_note)
        self.assertIn('opt_out_done', rule.body)
        self.assertEqual(phrasing.body, 'הוסרתם מרשימת התפוצה, לא נשלח יותר הודעות 🙏')
        # One short line: no question, no link, no sum.
        self.assertNotRegex(phrasing.body, r'\?|https?://|₪|\n')
        # Filed twice, nothing is made twice.
        knowledge_import.run()
        self.assertEqual(KnowledgeItem.objects.filter(key='opt_out_done').count(), 1)

    def test_without_a_key_the_bot_answers_the_one_line(self):
        knowledge_import.run()
        self.incoming('הסר')
        reply = shadow.propose(self.contact())
        self.assertEqual(reply.text, 'הוסרתם מרשימת התפוצה, לא נשלח יותר הודעות 🙏')
        self.assertFalse(reply.request_human)
        # A cancellation is answered as one, not as a removal from the list.
        self.incoming('אפשר להסיר את הילד מהחוג? רוצה לבטל את החוג', phone='972505550202')
        other = shadow.propose(Contact.objects.get(phone='972505550202'))
        self.assertNotIn('רשימת התפוצה', other.text)
