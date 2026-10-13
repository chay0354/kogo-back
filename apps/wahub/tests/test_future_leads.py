"""
The ✓ and the clock of a conversation, and the notes about a customer
(docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ג): closing a conversation, "the bot
carries on", "a future lead of mine", what the lists of conversations and of
leads make of them on each day — and that nothing of it is ever sent to anybody.

The clock is the test's: every test starts on Monday 12.10.2026 at noon
(Israel), and moves only when the test moves it. The network is cut (base.py);
ManyChat is the stand-in of test_handoff.py.
"""
import json
from datetime import date, datetime
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatError
from apps.core.models import UserProfile
from apps.wahub import future_leads, lead_stage, live_test, queries
from apps.wahub.models import Contact, ContactEvent, LeadFollowUp, Message
from apps.wahub.tests.base import BASE, PHONE, client_for, make_user
from apps.wahub.tests.test_analysis import CARD, NO_LEAD, POST, claude
from apps.wahub.tests.test_handoff import END, REQUEST, START, manychat, writes
from apps.wahub.tests.test_leads_v2 import DAY, TODAY, LeadsCase

SENDING_ON = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
SENDING_OFF = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=False)
IN_DECEMBER = date(2026, 12, 1)
MARKS = ('followup_status', 'followup_due', 'followup_note', 'followup_by_id', 'handled_by', 'needs_human', 'lead_manual')


class ConversationCase(LeadsCase):
    """A real conversation in the database — the customer wrote, the bot answered — to press ✓ and the clock on."""

    def talk(self, *lines, phone=PHONE, **fields) -> Contact:
        for line in lines or ('היי, אשמח לפרטים על קפוארה בראש העין',):
            self.incoming(line, phone=phone, subscriber_id='4242')
        self.bot_reply('בשמחה, בן כמה הילד?', phone=phone)
        contact = self.contact(phone)
        if fields:
            Contact.objects.filter(pk=contact.pk).update(**fields)
        return self.contact(phone)

    def fresh(self, contact) -> Contact:
        return Contact.objects.get(pk=contact.pk)

    def marks(self, contact) -> tuple:
        row = self.fresh(contact)
        return tuple(getattr(row, name) for name in MARKS)

    def close(self, contact):
        return self.post(f'contacts/{contact.id}/close/')

    def future(self, contact, due=IN_DECEMBER, reason='הילד יהיה בן 3 בדצמבר', **extra):
        body = {'due': due.isoformat() if isinstance(due, date) else due, **extra}
        if reason is not None:
            body['reason'] = reason
        return self.post(f'contacts/{contact.id}/future/', body)

    def hand_to_bot(self, contact):
        return self.post(f'contacts/{contact.id}/hand-to-bot/')

    def box(self, name) -> list:
        return [row['id'] for row in self.get('contacts/', box=name).data['results']]

    def journal(self, contact, kind) -> list:
        return list(ContactEvent.objects.filter(contact=contact, kind=kind).order_by('id').values_list('text', flat=True))


# --- ✓ ---------------------------------------------------------------------------------------------------

class CloseTests(ConversationCase):
    def test_closing_lowers_the_flag_marks_it_dealt_with_and_writes_the_journal(self):
        contact = self.talk(needs_human=True, needs_human_reason='ביקש נציג', unread_count=3)
        self.assertIn(contact.id, self.box('open'))
        response = self.close(contact)
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual((data['chat']['open'], data['chat']['open_reason'], data['chat']['needs_human']), (False, '', False))
        self.assertEqual((data['chat']['handled_by'], data['chat']['unread_count'], data['future']), ('bot', 0, None))
        self.assertEqual((data['followup']['status'], data['followup']['by_name']), ('answered', 'דנה מנהלת'))
        self.assertNotIn(contact.id, self.box('open'))
        self.assertEqual(self.journal(contact, 'conversation_closed'), ['השיחה נסגרה'])
        event = ContactEvent.objects.get(contact=contact, kind='conversation_closed')
        self.assertEqual(event.actor, self.manager)

    @override_settings(**SENDING_OFF)
    def test_a_conversation_the_bot_holds_is_closed_without_asking_manychat_anything(self):
        """The switch is off, as in production today: ✓ still works where nobody has to be handed back."""
        contact = self.talk(needs_human=True)
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            self.assertEqual(self.close(contact).status_code, 200)
        self.assertFalse(self.fresh(contact).needs_human)

    @override_settings(**SENDING_ON)
    def test_a_conversation_a_person_holds_goes_back_to_the_bot_in_manychat_too(self):
        contact = self.talk(handled_by='human')
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.close(contact)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes(request), [('/fb/subscriber/removeTagByName', {'subscriber_id': 4242, 'tag_name': START})])
        self.assertEqual(response.data['chat']['handled_by'], 'bot')
        self.assertFalse(response.data['chat']['open'])

    @override_settings(**SENDING_OFF)
    def test_when_manychat_cannot_take_it_back_nothing_changes(self):
        """A muted conversation that says "the bot answers" is a customer nobody answers."""
        contact = self.talk(handled_by='human', needs_human=True)
        before = self.marks(contact)
        response = self.close(contact)
        self.assertEqual(response.status_code, 502)
        self.assertIn('השליחה ללקוחות כבויה', response.data['detail'])
        self.assertEqual(self.marks(contact), before)
        self.assertEqual(self.journal(contact, 'conversation_closed'), [])

    @override_settings(**SENDING_ON)
    def test_a_manychat_failure_changes_nothing_either(self):
        contact = self.talk(handled_by='human')
        before = self.marks(contact)
        with patch(REQUEST, side_effect=manychat(tags=[START], fail_on='removeTagByName', error=ManyChatError('boom', status_code=500))):
            self.assertEqual(self.close(contact).status_code, 502)
        self.assertEqual(self.marks(contact), before)

    def test_a_mark_that_closes_the_lead_is_not_written_over(self):
        for mark in ('registered', 'not_relevant'):
            with self.subTest(mark=mark):
                contact = self.talk(phone=f'97250555{len(mark):04d}', followup_status=mark)
                self.close(contact)
                self.assertEqual(self.fresh(contact).followup_status, mark)

    def test_the_pin_of_the_leads_screen_comes_off(self):
        contact = self.talk()
        self.post(f'leads/{contact.id}/owner/', {'owner': 'office'})
        self.assertEqual(self.fresh(contact).lead_owner, 'office')
        self.close(contact)
        row = self.fresh(contact)
        self.assertNotIn('owner', row.lead_manual)
        self.assertNotEqual(row.lead_owner, 'office')

    def test_the_lead_leaves_the_offices_list(self):
        """A person dealt with what was written until now — on the phone, which WhatsApp never shows."""
        contact = self.talk()
        self.incoming('אז מה המחיר?')                 # nobody answered this
        self.clock.forward(minutes=45)
        lead_stage.refresh(contact.id)
        self.assertEqual((self.fresh(contact).lead_stage, self.fresh(contact).lead_owner), ('needs_reply', 'office'))
        self.close(contact)
        self.assertNotEqual(self.fresh(contact).lead_owner, 'office')
        self.assertNotEqual(self.fresh(contact).lead_stage, 'needs_reply')

    def test_a_new_message_from_the_customer_opens_it_again(self):
        contact = self.talk(needs_human=True)
        self.close(contact)
        self.clock.forward(hours=2)
        self.incoming('אפשר לדבר עם נציג בבקשה?')
        data = self.get(f'contacts/{contact.id}/').data
        self.assertEqual((data['chat']['open'], data['chat']['open_reason'], data['chat']['open_label']), (True, 'needs_human', 'מבקש נציג'))
        self.assertIn(contact.id, self.box('open'))

    def test_a_contact_that_is_not_there(self):
        self.assertEqual(self.post('contacts/999999/close/').status_code, 404)


# --- the clock: a future lead of mine --------------------------------------------------------------------

class FutureLeadTests(ConversationCase):
    def test_only_a_day_and_a_reason_are_asked_for(self):
        contact = self.talk()
        refused = [
            ({}, 'YYYY-MM-DD'),
            ({'due': 'מחר', 'reason': 'x'}, 'YYYY-MM-DD'),
            ({'due': TODAY.isoformat(), 'reason': 'x'}, 'אחרי היום'),
            ({'due': (TODAY - DAY).isoformat(), 'reason': 'x'}, 'אחרי היום'),
            ({'due': IN_DECEMBER.isoformat()}, 'נדרשת סיבה'),
            ({'due': IN_DECEMBER.isoformat(), 'reason': '   '}, 'נדרשת סיבה'),
            ({'due': IN_DECEMBER.isoformat(), 'reason': 'א' * 301}, 'ארוכה מדי'),
        ]
        for body, words in refused:
            with self.subTest(body=body):
                response = self.post(f'contacts/{contact.id}/future/', body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(words, response.data['detail'])
        self.assertEqual(self.fresh(contact).followup_status, '')

    @override_settings(**SENDING_ON)
    def test_it_is_kept_on_the_marks_a_person_fills_in_and_the_bot_is_muted(self):
        contact = self.talk(needs_human=True)
        with patch(REQUEST, side_effect=manychat(tags=[END])) as request:
            response = self.future(contact)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['mute'], {'muted': True, 'detail': ''})
        # The very three things "לקחת שיחה" writes: the old bot skips this customer.
        self.assertEqual([path for path, _body in writes(request)], [
            '/fb/subscriber/removeTagByName', '/fb/subscriber/setCustomFieldByName', '/fb/subscriber/addTagByName',
        ])
        data = response.data['contact']
        self.assertEqual(datetime.fromisoformat(data['future'].pop('at')), self.clock())
        self.assertEqual(data['future'], {
            'due': '2026-12-01', 'reason': 'הילד יהיה בן 3 בדצמבר', 'by_name': 'דנה מנהלת',
            'state': 'parked', 'label': 'ליד עתידי', 'days_left': 50, 'muted': True,
        })
        self.assertEqual((data['chat']['handled_by'], data['chat']['needs_human'], data['chat']['open']), ('human', False, False))
        self.assertEqual(
            (data['followup']['status'], data['followup']['due'], data['followup']['note']),
            ('later', '2026-12-01', 'הילד יהיה בן 3 בדצמבר'),
        )
        self.assertEqual(self.journal(contact, 'followup_changed'), ['ליד עתידי של דנה מנהלת: לחזור ב-1.12.2026 — הילד יהיה בן 3 בדצמבר'])
        self.assertEqual(Message.objects.filter(contact=contact, direction='out', sender='office').count(), 0)

    @override_settings(**SENDING_OFF)
    def test_with_the_switch_off_the_lead_is_kept_and_the_answer_says_the_bot_was_not_muted(self):
        """Production today: "לקחת שיחה" is refused while sending is off, so the old bot would still answer."""
        contact = self.talk()
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.future(contact)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['mute']['muted'])
        self.assertIn('השליחה ללקוחות כבויה', response.data['mute']['detail'])
        data = response.data['contact']
        self.assertEqual((data['future']['state'], data['future']['muted'], data['chat']['handled_by']), ('parked', False, 'bot'))

    @override_settings(**SENDING_ON)
    def test_a_manychat_failure_does_not_undo_the_lead(self):
        contact = self.talk()
        with patch(REQUEST, side_effect=manychat(fail_on='addTagByName', error=ManyChatError('boom', status_code=500))):
            response = self.future(contact)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['mute']['muted'])
        self.assertTrue(response.data['mute']['detail'])
        self.assertEqual((self.fresh(contact).followup_due, self.fresh(contact).handled_by), (IN_DECEMBER, 'bot'))

    @override_settings(**SENDING_OFF)
    def test_a_demo_contact_is_muted_here_alone(self):
        contact = self.talk(is_demo=True)
        with patch(REQUEST, side_effect=AssertionError('ManyChat was asked')):
            response = self.future(contact)
        self.assertEqual(response.data['mute'], {'muted': True, 'detail': ''})
        self.assertEqual(response.data['contact']['chat']['handled_by'], 'human')

    def parked(self, **fields) -> Contact:
        """A future lead a person holds, set now for the first of December."""
        contact = self.talk(handled_by='human', **fields)
        self.assertEqual(self.future(contact).status_code, 200)
        return self.fresh(contact)

    def test_until_the_day_it_is_out_of_the_open_conversations(self):
        contact = self.parked()
        self.assertNotIn(contact.id, self.box('open'))
        self.assertNotIn(contact.id, self.box('human'))            # a person holds it, but there is nothing to do yet
        self.assertEqual(self.box('future'), [contact.id])
        counts = self.get('contacts/counts/').data['boxes']
        self.assertEqual((counts['open'], counts['future'], counts['human']), (0, 1, 0))
        # In the leads it waits for its day, and the day is the office's.
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['owner'], row['next']['who']), ('waiting_date', '', 'office'))
        self.assertEqual((row['future']['state'], row['future']['reason']), ('parked', 'הילד יהיה בן 3 בדצמבר'))
        # And the bot plans nothing for it.
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact).exists())

    def test_on_the_day_it_is_open_again_from_midnight(self):
        contact = self.parked()
        self.clock.to(2026, 11, 30, 23, 59)
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['future']['state'], 'parked')
        self.assertNotIn(contact.id, self.box('open'))

        self.clock.to(2026, 12, 1, 0, 1)
        data = self.get(f'contacts/{contact.id}/').data
        self.assertEqual((data['future']['state'], data['future']['days_left']), ('due', 0))
        self.assertEqual(
            (data['chat']['open'], data['chat']['open_reason'], data['chat']['open_label']),
            (True, 'future_due', 'ליד עתידי — הגיע היום'),
        )
        self.assertEqual(self.box('open'), [contact.id])
        self.assertEqual(self.box('future'), [])
        self.assertEqual(self.get('contacts/updates/').data['boxes']['open'], 1)
        # The cron's look moves the lead to the office's list, and that is what wakes an open screen.
        lead_stage.scan(now=self.clock())
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['owner']), ('callback_due', 'office'))
        self.assertIn(contact.id, [lead['id'] for lead in self.get('leads/', owner='office').data['results']])

    def test_a_customer_who_writes_before_the_day_jumps_up_at_once_and_no_bot_plans_anything(self):
        contact = self.parked()
        self.clock.forward(days=3)
        self.incoming('היי, בסוף אנחנו רוצים להתחיל כבר עכשיו')
        data = self.get(f'contacts/{contact.id}/').data
        self.assertEqual(data['future']['state'], 'wrote')
        self.assertEqual(
            (data['chat']['open'], data['chat']['open_reason'], data['chat']['open_label']),
            (True, 'future_wrote', 'ליד עתידי — הלקוח כתב'),
        )
        self.assertEqual(self.box('open'), [contact.id])
        self.assertEqual(self.box('future'), [])
        # At once on the office's list of leads too — not after thirty minutes with no answer.
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['owner']), ('waiting_date', 'office'))
        self.assertEqual(row['next']['what'], 'הלקוח כתב לפני המועד — לענות לו')
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact).exists())

    @override_settings(ANTHROPIC_API_KEY='test-anthropic-key')
    def test_the_new_bot_keeps_quiet_for_a_future_lead_a_person_holds(self):
        """The owner: "if the customer suddenly writes in the middle, the bot must not start answering him"."""
        contact = self.parked(live_test=True)
        self.clock.forward(days=3)
        self.incoming('היי, יש חדש?')
        with patch(POST, side_effect=AssertionError('Claude was asked')), \
                patch('apps.wahub.live_test.sending.send_new_bot_reply', side_effect=AssertionError('an answer was sent')):
            self.assertEqual(live_test.reply(contact.id)['outcome'], live_test.OUTCOME_HUMAN)
        self.assertFalse(Message.objects.filter(contact=contact, new_bot=True).exists())

    def test_answering_the_customer_does_not_park_it_again_by_itself(self):
        contact = self.parked()
        self.clock.forward(days=3)
        self.incoming('היי, יש חדש?')
        self.clock.forward(minutes=5)
        self.bot_reply('עונה לך מיד')            # somebody answered; the person has still to decide what becomes of the lead
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['future']['state'], 'wrote')
        self.assertIn(contact.id, self.box('open'))

    def test_putting_it_off_keeps_the_reason_and_parks_it_again(self):
        contact = self.parked()
        self.clock.forward(days=3)
        self.incoming('היי, יש חדש?')
        response = self.future(contact, due=date(2027, 1, 10), reason=None)
        self.assertEqual(response.status_code, 200)
        future = response.data['contact']['future']
        self.assertEqual((future['due'], future['reason'], future['state']), ('2027-01-10', 'הילד יהיה בן 3 בדצמבר', 'parked'))
        self.assertNotIn(contact.id, self.box('open'))

    def test_the_day_wins_over_a_message_of_that_day(self):
        contact = self.parked()
        self.clock.to(2026, 12, 1, 9, 0)
        self.incoming('בוקר טוב')
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['chat']['open_reason'], 'future_due')

    def test_done_closes_the_future_lead(self):
        contact = self.parked(is_demo=True)             # a demo: handing back needs no ManyChat
        self.clock.to(2026, 12, 1, 9, 0)
        response = self.close(contact)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['future'], response.data['chat']['open'], response.data['chat']['handled_by']), (None, False, 'bot'))
        self.assertEqual(self.journal(contact, 'conversation_closed'), ['השיחה נסגרה · הליד העתידי נסגר'])
        self.assertEqual(self.box('open'), [])

    def test_the_mark_of_the_older_screen_is_the_same_future_lead(self):
        """One notion: "בזמן אחר" with a day, whichever screen set it. Without a day it is not one."""
        contact = self.talk()
        self.client.patch(f'{BASE}/contacts/{contact.id}/followup/', {'status': 'later', 'due': '2026-12-01', 'note': 'לחזור'}, format='json')
        future = self.get(f'contacts/{contact.id}/').data['future']
        self.assertEqual((future['state'], future['reason'], future['muted']), ('parked', 'לחזור', False))
        self.client.patch(f'{BASE}/contacts/{contact.id}/followup/', {'status': 'later', 'due': None}, format='json')
        self.assertIsNone(self.get(f'contacts/{contact.id}/').data['future'])

    def test_the_per_contact_rule_and_the_query_agree(self):
        """`chat.open` and the box "open" are one rule written twice (queries.py): held against each other."""
        today = TODAY
        rows = [
            dict(needs_human=True),
            dict(handled_by='human'),
            dict(handled_by='human', followup_status='later', followup_due=today + DAY, followup_at=self.clock()),
            dict(followup_status='later', followup_due=today, followup_at=self.clock() - DAY),
            dict(followup_status='later', followup_due=today + DAY, followup_at=self.clock() - DAY, last_inbound_at=self.clock()),
            dict(followup_status='later', followup_due=today + DAY, followup_at=self.clock(), last_inbound_at=self.clock() - DAY),
            dict(followup_status='later'),
            dict(followup_status='answered', followup_due=None),
            dict(),
        ]
        for index, fields in enumerate(rows):
            Contact.objects.create(phone=f'9725055577{index:02d}', **fields)
        for name, twin in (
            ('open', lambda row: bool(queries.open_reason(row, today))),
            ('future', lambda row: queries.future_state(row, today) == 'parked'),
            ('human', lambda row: row.handled_by == 'human' and queries.future_state(row, today) != 'parked'),
        ):
            with self.subTest(box=name):
                by_query = set(Contact.objects.filter(queries.boxes(today)[name]).values_list('id', flat=True))
                self.assertEqual(by_query, {row.id for row in Contact.objects.all() if twin(row)})
                self.assertTrue(by_query)


# --- the clock: the bot carries on -----------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='')
class HandToBotTests(ConversationCase):
    def test_a_child_too_young_is_scheduled_for_the_day_he_is_three(self):
        contact = self.talk('היי, יש חוג קפוארה בראש העין לילד בן שנתיים ו-9 חודשים?', needs_human=True)
        response = self.hand_to_bot(contact)
        self.assertEqual(response.status_code, 200)
        understood = response.data['understood']
        self.assertTrue(understood['ok'])
        self.assertEqual((understood['stage'], understood['stage_label']), ('waiting_date', 'מחכה למועד'))
        self.assertEqual((understood['come_back_on'], understood['why'], understood['source']), ('2027-01-12', 'הילד יהיה בן 3', 'rules'))
        self.assertEqual(understood['text'], 'הילד יהיה בן 3 — הבוט יחזור אליהם ב-12.1.2027')
        following = understood['next']
        self.assertEqual(
            (following['kind'], following['kind_label'], following['template_key'], following['state']),
            ('date_arrived', 'הגיע המועד שדובר עליו', 'date_arrived_age_1', 'planned'),
        )
        self.assertTrue(following['when'].startswith('2027-01-12'))
        self.assertFalse(understood['sending_enabled'])
        # Applied: back with the bot, nobody asked for, the bot's step planned — and nothing sent.
        data = response.data['contact']
        self.assertEqual((data['chat']['handled_by'], data['chat']['needs_human'], data['chat']['open']), ('bot', False, False))
        self.assertEqual((data['followup']['status'], data['lead']['stage'], data['lead']['owner']), ('answered', 'waiting_date', 'bot'))
        planned = LeadFollowUp.objects.get(contact=contact)
        self.assertEqual((planned.kind, planned.status, planned.step), ('date_arrived', 'planned', 1))
        self.assertEqual(self.journal(contact, 'lead_action'), ['הועבר לבוט לטיפול בהמשך: הילד יהיה בן 3 — הבוט יחזור אליהם ב-12.1.2027'])
        self.assertFalse(Message.objects.filter(contact=contact, direction='out', source='kogo').exists())

    def test_what_the_office_wrote_in_a_note_is_what_the_bot_schedules_by(self):
        """She spoke to them on the phone: WhatsApp holds nothing of it, her note does."""
        contact = self.talk()
        self.assertFalse(self.hand_to_bot(contact).data['understood']['ok'])
        self.post(f'contacts/{contact.id}/notes/', {'text': 'דיברנו בטלפון: הילד בן שנתיים ו-9 חודשים, לחזור כשימלאו לו 3'})
        understood = self.hand_to_bot(contact).data['understood']
        self.assertTrue(understood['ok'])
        self.assertEqual((understood['stage'], understood['come_back_on']), ('waiting_date', '2027-01-12'))

    def test_a_note_that_names_a_time_is_a_time_to_come_back_on(self):
        contact = self.talk()
        self.post(f'contacts/{contact.id}/notes/', {'text': 'ביקשה שנחזור בעוד חודשיים'})
        understood = self.hand_to_bot(contact).data['understood']
        self.assertTrue(understood['ok'])
        self.assertEqual(understood['come_back_on'], '2026-12-12')
        self.assertIn('לפי פתק של המשרד', understood['why'])

    def test_when_the_bot_finds_no_step_nothing_changes_and_it_says_so(self):
        contact = self.talk(needs_human=True, handled_by='human')
        before = self.marks(contact)
        with patch('apps.wahub.handoff.release', side_effect=AssertionError('the conversation was handed back')):
            response = self.hand_to_bot(contact)
        self.assertEqual(response.status_code, 200)
        understood = response.data['understood']
        self.assertEqual((understood['ok'], understood['next'], understood['come_back_on']), (False, None, None))
        self.assertIn('ליד עתידי שלי', understood['text'])
        self.assertEqual(self.marks(contact), before)
        self.assertEqual(self.journal(contact, 'lead_action'), [])
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact).exists())
        self.assertTrue(response.data['contact']['chat']['needs_human'])

    def test_a_link_that_went_out_is_a_step_too(self):
        contact = self.talk()
        Contact.objects.filter(pk=contact.pk).update(lead_link_sent_at=self.clock())
        understood = self.hand_to_bot(contact).data['understood']
        self.assertTrue(understood['ok'])
        self.assertEqual((understood['stage'], understood['next']['kind'], understood['come_back_on']), ('link_sent', 'link_reminder', None))
        self.assertEqual(understood['text'], 'קיבל קישור ועוד לא נרשם — הבוט יזכיר את ההרשמה מחר ב-10:30')

    def test_a_registered_customer_has_nothing_to_schedule(self):
        contact = self.talk(kogo_outcome='registered_after', kogo_detail='נרשם ב-10.10')
        understood = self.hand_to_bot(contact).data['understood']
        self.assertEqual((understood['ok'], understood['stage']), (False, 'registered'))
        self.assertEqual(understood['text'], 'נרשם — אין מה לתזמן.')

    @override_settings(**SENDING_OFF)
    def test_a_conversation_a_person_holds_that_manychat_does_not_take_back_stays_as_it_is(self):
        contact = self.talk('היי, יש חוג קפוארה בראש העין לילד בן שנתיים ו-9 חודשים?', handled_by='human')
        before = self.marks(contact)
        response = self.hand_to_bot(contact)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.marks(contact), before)
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact).exists())

    @override_settings(**SENDING_ON)
    def test_a_future_lead_handed_to_the_bot_stops_being_one(self):
        contact = self.talk('היי, יש חוג קפוארה בראש העין לילד בן שנתיים ו-9 חודשים?', handled_by='human')
        self.future(contact)
        with patch(REQUEST, side_effect=manychat(tags=[START])) as request:
            response = self.hand_to_bot(contact)
        self.assertTrue(response.data['understood']['ok'])
        self.assertIn(('/fb/subscriber/removeTagByName', {'subscriber_id': 4242, 'tag_name': START}), writes(request))
        data = response.data['contact']
        self.assertEqual((data['future'], data['chat']['handled_by'], data['lead']['owner']), (None, 'bot', 'bot'))

    def test_taking_it_back_after_the_bot_understood_cancels_the_bots_step(self):
        """ "…so that the representative sees it and can correct or cancel": the second option wins over the first."""
        contact = self.talk('היי, יש חוג קפוארה בראש העין לילד בן שנתיים ו-9 חודשים?', is_demo=True)
        self.assertTrue(self.hand_to_bot(contact).data['understood']['ok'])
        self.assertEqual(LeadFollowUp.objects.get(contact=contact).status, 'planned')
        self.future(contact, due=date(2027, 2, 1), reason='אמא ביקשה פברואר')
        self.assertEqual(LeadFollowUp.objects.get(contact=contact).status, 'skipped')
        self.assertEqual(self.fresh(contact).lead_owner, '')

    @override_settings(ANTHROPIC_API_KEY='test-anthropic-key', WAHUB_AI_MODEL='claude-sonnet-5-5')
    def test_claude_is_given_the_notes_and_told_a_person_passed_it_on(self):
        contact = self.talk()
        self.post(f'contacts/{contact.id}/notes/', {'text': 'אמא אמרה בטלפון שהילד ימלא 3 בינואר'})
        lead = {
            **NO_LEAD, 'intent': 'class', 'interest': 'unsure', 'blocker': 'too_young', 'blocker_text': 'הילד עוד לא בן 3',
            'child': {**NO_LEAD['child'], 'age_years': 2, 'age_months': 9},
            'come_back': {**NO_LEAD['come_back'], 'kind': 'age', 'years': 2, 'months': 9, 'said_on': TODAY.isoformat()},
            'come_back_why': 'הילד יהיה בן 3', 'summary': 'אמא לילד בן שנתיים ו-9 חודשים בראש העין, שאלה על קפוארה',
        }
        card = {**CARD, 'topic': 'info', 'child_age': '2.9', 'callback_on': '', 'flags': ['child_too_young'], 'branch_name': '', 'lead': lead}
        with patch(POST, return_value=claude(card)) as post:
            understood = self.hand_to_bot(contact).data['understood']
        asked = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('פתקים שנציג מהמשרד כתב על הלקוח', asked)
        self.assertIn('פתק: אמא אמרה בטלפון שהילד ימלא 3 בינואר', asked)
        self.assertIn('הנציג העביר עכשיו את השיחה לבוט "לטיפול בהמשך"', asked)
        self.assertEqual((understood['ok'], understood['source'], understood['come_back_on']), (True, 'ai', '2027-01-12'))
        self.assertEqual(understood['text'], 'הילד יהיה בן 3 — הבוט יחזור אליהם ב-12.1.2027')

    @override_settings(ANTHROPIC_API_KEY='test-anthropic-key', WAHUB_AI_MODEL='claude-sonnet-5-5')
    def test_the_ordinary_summary_is_not_told_anything_was_handed_over(self):
        contact = self.talk()
        with patch(POST, return_value=claude(CARD)) as post:
            self.post(f'contacts/{contact.id}/analyze/')
        asked = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertNotIn('לטיפול בהמשך', asked)
        self.assertNotIn('פתקים', asked)          # no note was written: the conversation is all there is


# --- notes ------------------------------------------------------------------------------------------------

class NotesTests(ConversationCase):
    def test_a_note_is_a_line_of_the_journal_shown_in_the_conversation(self):
        contact = self.talk()
        messages = Message.objects.filter(contact=contact).count()
        response = self.post(f'contacts/{contact.id}/notes/', {'text': '  אמא ביקשה שנחזור אחרי החגים  '})
        self.assertEqual(response.status_code, 201)
        note = response.data['note']
        self.assertEqual(set(note), {'id', 'text', 'by_name', 'created_at'})
        self.assertEqual((note['text'], note['by_name']), ('אמא ביקשה שנחזור אחרי החגים', 'דנה מנהלת'))
        self.assertEqual(response.data['contact']['id'], contact.id)
        # No message, to anybody.
        self.assertEqual(Message.objects.filter(contact=contact).count(), messages)

        self.clock.forward(minutes=5)
        self.post(f'contacts/{contact.id}/notes/', {'text': 'הילד בן 4'})
        conversation = self.get(f'contacts/{contact.id}/').data
        self.assertEqual([row['text'] for row in conversation['notes']], ['הילד בן 4', 'אמא ביקשה שנחזור אחרי החגים'])
        self.assertIn('הילד בן 4', [event['text'] for event in conversation['events'] if event['kind'] == 'note'])
        self.assertEqual([row['text'] for row in self.get(f'contacts/{contact.id}/notes/').data['notes']], ['הילד בן 4', 'אמא ביקשה שנחזור אחרי החגים'])
        self.assertEqual([row['text'] for row in self.get(f'leads/{contact.id}/').data['details']['notes']], ['הילד בן 4', 'אמא ביקשה שנחזור אחרי החגים'])

    def test_a_note_is_read_by_the_next_summary(self):
        contact = self.talk()
        Contact.objects.filter(pk=contact.pk).update(needs_analysis=False)
        self.post(f'contacts/{contact.id}/notes/', {'text': 'הילד בן 4'})
        self.assertTrue(self.fresh(contact).needs_analysis)

    def test_an_empty_or_a_too_long_note_is_refused(self):
        contact = self.talk()
        for text in ('', '   ', None, 'א' * 501):
            with self.subTest(text=(text or '')[:5]):
                response = self.post(f'contacts/{contact.id}/notes/', {'text': text})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.data['detail'])
        self.assertEqual(self.get(f'contacts/{contact.id}/notes/').data['notes'], [])

    def test_the_note_written_when_a_contact_is_added_by_hand_is_a_note_too(self):
        response = self.post('contacts/', {'phone': '0505550177', 'name': 'חדשה', 'note': 'התקשרה למשרד'})
        self.assertEqual(response.status_code, 201)
        notes = self.get(f'contacts/{response.data["id"]}/').data['notes']
        self.assertEqual([row['text'] for row in notes], ['התקשרה למשרד'])


# --- who may ---------------------------------------------------------------------------------------------

class PermissionTests(ConversationCase):
    def test_managers_only(self):
        contact = self.talk()
        calls = [
            ('post', f'contacts/{contact.id}/close/', {}),
            ('post', f'contacts/{contact.id}/hand-to-bot/', {}),
            ('post', f'contacts/{contact.id}/future/', {'due': '2026-12-01', 'reason': 'x'}),
            ('get', f'contacts/{contact.id}/notes/', None),
            ('post', f'contacts/{contact.id}/notes/', {'text': 'x'}),
            ('post', f'contacts/{contact.id}/waiting-list/', {'lesson_id': '00000000-0000-0000-0000-000000000000'}),
            ('get', 'waiting-list/', None),
            ('post', 'waiting-list/1/handled/', {}),
            ('post', 'waiting-list/1/remove/', {}),
        ]
        before = self.marks(contact)
        others = {
            'partner': (client_for(make_user('partner@future.test', UserProfile.ROLE_PARTNER)), 403),
            'worker': (client_for(make_user('worker@future.test', UserProfile.ROLE_WORKER)), 403),
            'nobody': (APIClient(), 401),
        }
        for who, (client, refused) in others.items():
            for method, path, body in calls:
                with self.subTest(who=who, call=f'{method} {path}'):
                    response = (
                        getattr(client, method)(f'{BASE}/{path}', body, format='json') if body is not None
                        else getattr(client, method)(f'{BASE}/{path}')
                    )
                    self.assertEqual(response.status_code, refused)
        self.assertEqual(self.marks(contact), before)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='note').count(), 0)

    def test_the_payloads_carry_no_json_that_cannot_be_sent(self):
        contact = self.talk(is_demo=True)
        self.future(contact)
        for path in (f'contacts/{contact.id}/', 'contacts/', f'leads/{contact.id}/', 'contacts/updates/'):
            with self.subTest(path=path):
                json.dumps(self.get(path).data, default=str)
        self.assertEqual(future_leads.REASON_CHARS, 300)
