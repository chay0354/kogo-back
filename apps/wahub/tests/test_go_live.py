"""
The new bot answers everybody (docs/WAHUB-CONTRACT-GO-LIVE.md): the one mode
that says whom it answers and the moment it became live; never an answer to a
message from before that moment; the cron's safety net — a message is not
lost, and nobody is answered twice; what a customer is told when Claude fails;
the emergency brake and the brake on a loop; the follow-ups' own switch, which
never releases what was waiting; and what the status screen is given.

ManyChat and Claude are mocks; `self.network` fails any test that reaches for
the real ones. Every phone here is invented.
"""
import os
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatError
from apps.wahub import bot_mode, cron, knowledge_import, lead_automation, lead_followups, live_test, sending, settings_store, shadow
from apps.wahub.models import (
    Contact, ContactEvent, KnowledgeItem, LeadFollowUp, Message, ShadowReply, WahubSetting, WaitingListEntry,
)
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user
from apps.wahub.tests.test_lead_followups import LINK_FIRST, SENDING_ON, EngineCase, at, follow_ups_on
from apps.wahub.tests.test_leads_v2 import HOUR
from apps.wahub.tests.test_live_test import (
    CLAUDE, KICK_POST, OK, REQUEST, SLEEP, asked_to_do, sends, the_stub_in_claudes_place,
)
from apps.wahub.tests.test_shadow import catalogue

INBOUND_URL = f'{BASE}/inbound/manychat/'
REPLY_URL = f'{BASE}/live-test/reply/'
OWN_REPLY = 'http://testserver/api/v1/wahub/live-test/reply/'
ANSWER_NOW = 'apps.wahub.live_test.shadow.answer_now'
HOURS = 'apps.wahub.live_test.knowledge.office_hours_now'
OPEN = {'configured': True, 'open': True, 'message_if_closed': None}


def phone(index: int) -> str:
    return f'97250555{index:04d}'


@override_settings(
    MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True, WAHUB_SIMULATE_SEND=False, DEBUG=False,
    CRON_TOKEN='cron-secret', ANTHROPIC_API_KEY='test-key', OPENAI_API_KEY='', WAHUB_LIVE_TEST_QUIET_SECONDS=0,
)
class GoLiveCase(WahubTestCase):
    """
    Sending is on, as on the day of the move; the inbound key is set; nobody
    sleeps for real; Claude answers by the stub's rules unless a test is about
    Claude failing; the check that the server reaches itself passes.
    """

    PATCH_KICK = True

    def setUp(self):
        super().setUp()
        patch.dict(os.environ, {'CRON_SECRET': ''}).start()
        self.addCleanup(patch.stopall)
        self.sleep = patch(SLEEP).start()
        if self.PATCH_KICK:
            self.kick = patch('apps.wahub.live_test.kick', return_value=True).start()
        patch(CLAUDE, side_effect=the_stub_in_claudes_place).start()
        self.ping = patch('apps.wahub.live_test.ping', return_value='').start()
        # The status screen also asks ManyChat which holiday boards it has; that is not what these tests look at.
        patch('apps.wahub.holiday_flows.status', return_value={}).start()
        self.anonymous = APIClient()
        self.machine = APIClient()
        self.key = self.post('settings/inbound-key/').data['key']

    # --- what the tests do ---

    def writes(self, text='היי', index=1, event='customer_message'):
        """A customer's message, as ManyChat's copy of it reaches the door."""
        number = phone(index)
        # ManyChat's body: what the customer wrote last, and — for the copy of the old bot's answer — what the bot said.
        said = {'Last_AI_Respone': text} if event == 'bot_reply' else {'Client_Last_Input': text}
        body = {
            'id': 5000 + index, 'first_name': 'לקוח', 'last_name': str(index), 'whatsapp_phone': number,
            'last_input_text': '' if event == 'bot_reply' else text, 'custom_fields': {**said, 'Client_Phone': number},
        }
        return self.anonymous.post(f'{INBOUND_URL}?event={event}', body, format='json', HTTP_X_WAHUB_KEY=self.key)

    def customer(self, index=1) -> Contact:
        return Contact.objects.get(phone=phone(index))

    def mode(self, **body):
        return self.post('settings/bot-mode/', body)

    def go_all(self) -> dict:
        response = self.mode(mode='all', confirm='all')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def bot(self) -> dict:
        return self.get('status/').data['bot']

    def live_for(self, minutes):
        """As if the mode had become live that many minutes ago."""
        moment = (timezone.now() - timedelta(minutes=minutes)).isoformat()
        settings_store.change(settings_store.BOT_MODE, lambda value: {**value, 'since': moment}, default={})

    def aged(self, contact_id, minutes) -> Message:
        """The contact's last message, as if it had reached Kogo that many minutes ago."""
        moment = timezone.now() - timedelta(minutes=minutes)
        last = Message.objects.filter(contact_id=contact_id, direction='in').order_by('-id').first()
        Message.objects.filter(pk=last.pk).update(created_at=moment, sent_at=moment)
        Contact.objects.filter(pk=contact_id).update(last_inbound_at=moment)
        return last

    def reply(self, contact_id, **body):
        return self.machine.post(REPLY_URL, {'contact': contact_id, **body}, format='json', HTTP_X_CRON_TOKEN='cron-secret')

    def journal(self, contact_id):
        return [event.text for event in ContactEvent.objects.filter(contact_id=contact_id, kind='live_test').order_by('id')]

    def waiting(self, count, minutes, start=100) -> list:
        """`count` contacts whose one message reached Kogo `minutes` ago and that nobody answered. Their ids."""
        moment = timezone.now() - timedelta(minutes=minutes)
        ids = []
        for index in range(start, start + count):
            contact = Contact.objects.create(
                phone=phone(index), manychat_subscriber_id=str(5000 + index), last_inbound_at=moment, last_message_at=moment,
                last_message_direction='in', last_message_sender='customer', needs_shadow=True, waiting_since=moment,
            )
            Message.objects.create(contact=contact, direction='in', sender='customer', text='יש חוג קפוארה?', sent_at=moment)
            ids.append(contact.id)
        Message.objects.filter(contact_id__in=ids).update(created_at=moment)
        return ids


# --- the switch itself ------------------------------------------------------------------------------------------

class ModeApiTests(GoLiveCase):
    def test_as_deployed_the_mode_is_test_the_follow_ups_are_off_and_nothing_is_live(self):
        state = self.get('settings/bot-mode/').data
        self.assertEqual((state['mode'], state['live'], state['live_since'], state['stopped']), ('test', False, None, None))
        self.assertEqual((state['chosen_at'], state['chosen_by'], state['warning'], state['log']), (None, '', '', []))
        self.assertEqual(state['old_bot_replies'], 0)
        self.assertEqual(state['text'], 'בדיקה בלבד: הבוט החדש עונה רק למספרי הבדיקה (אין כרגע מספר בדיקה)')
        self.assertTrue(state['sending_enabled'])
        self.assertEqual(state['today'], {'sent': 0, 'failed': 0, 'late': 0, 'needs_human': 0, 'fallback': 0, 'unanswered': 0, 'not_started': 0})
        self.assertEqual(state['followups'], {
            'on': False, 'since': None, 'turned_on_at': None, 'by': '', 'due_now': 0,
            'text': 'כבויות — הבוט מתכנן חזרות ולא שולח אותן',
        })
        self.assertEqual(state['limits'], {
            'late_after_seconds': 60, 'late_within_minutes': 15, 'late_per_run': 8, 'per_hour': 12, 'per_day': 30,
            'brake': {'in_a_row': 5, 'minutes': 10, 'rate_min': 10, 'rate_percent': 50, 'in_a_row_slow': 10, 'slow_minutes': 60},
        })
        # The same on the status screen, without the log; and the follow-ups' own door answers the same body.
        self.assertEqual(self.bot(), {key: value for key, value in state.items() if key != 'log'})
        self.assertEqual(self.get('settings/followups/').data, state)
        # Nothing was written by looking.
        self.assertFalse(WahubSetting.objects.filter(key__startswith='bot_').exists())

    def test_a_customer_is_not_answered_while_the_mode_is_test(self):
        self.assertEqual(self.writes('יש חוג קפוארה?').data, {'ok': True, 'stored': True})
        self.kick.assert_not_called()
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'not_live_test')
            with self.assertRaises(ValueError):
                sending.send_new_bot_reply(self.customer(), 'שלום')
        request.assert_not_called()
        self.assertEqual([contact.id for contact in shadow.pending(timezone.now() + timedelta(minutes=1))], [self.customer().id])

    def test_only_a_manager_reads_or_changes_the_switches(self):
        worker = client_for(make_user('worker@wahub.test', role='worker'))
        for path in ('settings/bot-mode/', 'settings/followups/'):
            self.assertEqual(worker.get(f'{BASE}/{path}').status_code, 403)
            self.assertEqual(self.anonymous.get(f'{BASE}/{path}').status_code, 401)
        self.assertEqual(worker.post(f'{BASE}/settings/bot-mode/', {'mode': 'all', 'confirm': 'all'}, format='json').status_code, 403)
        self.assertEqual(worker.post(f'{BASE}/settings/followups/', {'on': True, 'confirm': 'on'}, format='json').status_code, 403)
        self.assertEqual(self.anonymous.post(f'{BASE}/settings/bot-mode/', {'mode': 'all', 'confirm': 'all'}, format='json').status_code, 401)
        self.assertEqual((bot_mode.mode(), bot_mode.followups_on()), ('test', False))

    def test_all_has_to_be_confirmed_and_a_value_that_is_no_mode_is_refused(self):
        for body, code in (({}, 'bad_mode'), ({'mode': 'some'}, 'bad_mode'), ({'mode': 'all'}, 'confirm_required'),
                           ({'mode': 'all', 'confirm': True}, 'confirm_required'), ({'mode': 'all', 'confirm': 'yes'}, 'confirm_required')):
            with self.subTest(body=body):
                response = self.mode(**body)
                self.assertEqual((response.status_code, response.data['code']), (400, code))
        self.assertEqual(bot_mode.mode(), 'test')
        self.ping.assert_not_called()
        self.assertEqual(self.get('settings/bot-mode/').data['log'], [])

    def test_all_with_sending_on_is_live_from_that_moment_and_says_who_and_when(self):
        before = timezone.now()
        state = self.go_all()
        self.assertEqual((state['mode'], state['live'], state['warning'], state['chosen_by']), ('all', True, '', 'דנה מנהלת'))
        since = bot_mode.live_since()
        self.assertTrue(before <= since <= timezone.now())
        self.assertEqual(state['live_since'], since.isoformat())
        self.assertTrue(state['text'].startswith('הבוט החדש עונה לכולם — להודעות שהגיעו מ-'), state['text'])
        self.ping.assert_called_once_with(OWN_REPLY)                     # the server reached its own reply door first
        self.assertEqual([(row['what'], row['to']) for row in state['log']], [('live', 'all'), ('mode', 'all')])
        self.assertEqual(state['log'][1]['by'], 'דנה מנהלת')
        # The same value again changes nothing: the moment does not move, nothing is checked or written down again.
        again = self.go_all()
        self.assertEqual((again['live_since'], len(again['log'])), (state['live_since'], 2))
        self.ping.assert_called_once()

    @override_settings(WAHUB_SENDING_ENABLED=False)
    def test_all_while_sending_is_off_is_kept_and_says_that_nothing_leaves(self):
        state = self.go_all()
        self.assertEqual((state['mode'], state['live'], state['live_since'], state['sending_enabled']), ('all', False, None, False))
        self.assertEqual(state['warning'], bot_mode.ALL_BUT_NOT_SENDING)
        self.assertTrue(state['warning'].startswith('נבחר "הבוט החדש עונה לכולם", אבל השליחה כבויה — שום דבר לא יוצא.'), state['warning'])
        self.assertEqual(state['text'], 'נבחר "הבוט החדש עונה לכולם", אבל הוא עוד לא עונה לאף לקוח')
        # In the owner's words, never the values of the API.
        for sentence in (state['text'], state['warning']):
            self.assertNotRegex(sentence, r'\ball\b|\btest\b|מצב')
        self.assertEqual([row['what'] for row in state['log']], ['mode'])
        # Nobody is answered, nothing is computed, and the shadow answers everybody as before.
        self.writes('יש חוג קפוארה?')
        self.kick.assert_not_called()
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'not_live_test')
        request.assert_not_called()
        self.assertEqual(ShadowReply.objects.count(), 0)
        self.assertEqual(shadow.pending(timezone.now() + timedelta(minutes=1)).count(), 1)

    def test_sending_turned_on_later_begins_at_the_first_message_after_it_and_never_before(self):
        with override_settings(WAHUB_SENDING_ENABLED=False):
            self.go_all()
            self.writes('שאלה מלפני שהשליחה הודלקה', index=1)
            live_test.flag_unanswered()                                  # the cron looked meanwhile
        self.assertIsNone(bot_mode.live_since())
        self.kick.assert_not_called()
        # The deploy that turned sending on. The next message finds the mode live, and is itself answered.
        self.writes('שאלה חדשה', index=2)
        since = bot_mode.live_since()
        self.assertIsNotNone(since)
        self.kick.assert_called_once_with(self.customer(2).id, OWN_REPLY)
        new = Message.objects.get(contact=self.customer(2))
        self.assertGreaterEqual(new.created_at, since)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply(self.customer(1).id).data['outcome'], 'nothing')     # from before: never
            self.assertEqual(self.reply(self.customer(2).id).data['outcome'], 'sent')
        self.assertEqual([subscriber for subscriber, _text in sends(request)], [5002])

    def test_sending_seen_off_forgets_the_moment_and_coming_back_is_a_new_beginning(self):
        first = self.go_all()['live_since']
        with override_settings(WAHUB_SENDING_ENABLED=False):
            self.assertEqual(self.bot()['live_since'], None)             # a screen that looks moves nothing
            self.assertIsNotNone(bot_mode._dict(settings_store.BOT_MODE)['since'])
            bot_mode.sync()                                              # the cron's look does
            self.assertIsNone(bot_mode._dict(settings_store.BOT_MODE)['since'])
        self.assertIsNone(self.bot()['live_since'])                      # back on, and nobody has seen it yet
        bot_mode.sync()
        second = self.bot()['live_since']
        self.assertGreater(second, first)
        self.assertEqual([row['what'] for row in self.get('settings/bot-mode/').data['log']], ['live', 'live', 'live', 'mode'])

    def test_the_mode_does_not_become_all_while_the_server_cannot_answer(self):
        with override_settings(ANTHROPIC_API_KEY=''):
            response = self.mode(mode='all', confirm='all')
        self.assertEqual((response.status_code, response.data['code']), (409, 'not_ready'))
        self.assertEqual([problem['code'] for problem in response.data['problems']], ['no_claude_key'])
        self.assertEqual((response.data['mode'], response.data['live']), ('test', False))    # the state comes with the refusal
        with override_settings(MANYCHAT_KEY=''):
            response = self.mode(mode='all', confirm='all')
        self.assertEqual([problem['code'] for problem in response.data['problems']], ['no_manychat'])
        self.ping.return_value = 'השרת ענה לעצמו 404'
        response = self.mode(mode='all', confirm='all')
        self.assertEqual(response.status_code, 409)
        [problem] = response.data['problems']
        self.assertEqual((problem['code'], problem['text']), ('self_call_failed', 'השרת לא הצליח לקרוא לעצמו: השרת ענה לעצמו 404'))
        self.assertEqual(bot_mode.mode(), 'test')
        self.assertEqual(self.get('settings/bot-mode/').data['log'], [])

    def test_back_to_test_is_one_press_and_stops_the_answers_at_once(self):
        self.go_all()
        self.writes('יש חוג קפוארה?')
        self.kick.assert_called_once()
        state = self.mode(mode='test').data                              # no confirmation asked
        self.assertEqual((state['mode'], state['live'], state['live_since']), ('test', False, None))
        self.assertEqual(state['log'][0]['text'], 'נבחר "בדיקה בלבד": הבוט החדש עונה רק למספרי הבדיקה')
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'not_live_test')
        request.assert_not_called()
        self.writes('הלו?')
        self.kick.assert_called_once()                                   # no call for the new message
        # And "all" again begins anew: what was written meanwhile is not answered.
        self.go_all()
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'nothing')
        request.assert_not_called()


# --- only what arrives from that moment on ------------------------------------------------------------------------

class OnlyNewMessagesTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()

    def test_two_hundred_contacts_with_old_messages_get_no_answer_when_the_mode_becomes_all(self):
        now = timezone.now()
        for index in range(200):
            self.incoming(f'שאלה ישנה {index}', phone=phone(index), subscriber_id=str(5000 + index))
        ids = list(Contact.objects.order_by('id').values_list('id', flat=True))
        # Old in every way the cron looks at: half of them minutes old — young enough for a late answer — half hours old.
        for index, contact_id in enumerate(ids):
            moment = now - (timedelta(minutes=2 + index % 13) if index % 2 else timedelta(hours=1 + index % 9))
            Message.objects.filter(contact_id=contact_id).update(created_at=moment, sent_at=moment)
            Contact.objects.filter(pk=contact_id).update(last_inbound_at=moment, last_message_at=moment)
        self.assertEqual(shadow.pending().count(), 200)

        self.go_all()
        self.assertEqual(live_test._overdue(timezone.now()), [])
        with patch(REQUEST) as request, override_settings(ANTHROPIC_API_KEY=''):
            ticked = cron.tick(budget_seconds=1, reply_url=OWN_REPLY)
            for contact_id in ids[::9]:                                  # and asked for outright, each is refused
                self.assertEqual(self.reply(contact_id).data['outcome'], 'nothing')
                self.assertEqual(self.reply(contact_id, late=True).data['outcome'], 'nothing')
        self.assertEqual((ticked['live_late_started'], ticked['live_late_not_started'], ticked['live_test_unanswered']), (0, 0, 0))
        self.kick.assert_not_called()
        self.assertEqual(sends(request), [])
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)
        self.assertEqual(ShadowReply.objects.count(), 0)                 # not even a shadow answer that would hide a new message
        self.assertEqual(Contact.objects.filter(needs_human=True).count(), 0)
        self.assertEqual(self.bot()['today']['sent'], 0)

        # One of them writes again: that message, and only it, is answered.
        self.writes('ומה המחיר?', index=7)
        contact = self.customer(7)
        self.kick.assert_called_once_with(contact.id, OWN_REPLY)
        new = Message.objects.filter(contact=contact).order_by('-id').first()
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply(contact.id)
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual([subscriber for subscriber, _text in sends(request)], [5007])
        self.assertEqual(ShadowReply.objects.get().covers_message_ids, [new.id])
        self.assertEqual(self.bot()['today']['sent'], 1)

    def test_a_message_from_seconds_before_the_moment_is_not_part_of_the_answer(self):
        self.writes('היי', index=1)
        self.go_all()
        self.writes('יש חוג קפוארה בראש העין לבן 6?', index=1)           # the same burst, by the clock
        before, after = Message.objects.filter(contact=self.customer()).order_by('id')
        self.assertLess(before.created_at, bot_mode.live_since())
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'sent')
        self.assertEqual(len(sends(request)), 1)
        self.assertEqual(ShadowReply.objects.get().covers_message_ids, [after.id])

    def test_a_message_too_old_for_an_answer_gets_none_whichever_way_the_call_started(self):
        self.go_all()
        self.writes('יש חוג קפוארה?')
        contact = self.customer()
        self.live_for(60)
        self.aged(contact.id, 21)
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(contact.id).data['outcome'], 'too_old')
            self.assertEqual(self.reply(contact.id, late=True).data['outcome'], 'too_old')
        request.assert_not_called()
        self.assertEqual(ShadowReply.objects.count(), 0)

    def test_the_shadow_keeps_only_whom_the_new_bot_does_not_answer(self):
        self.go_all()
        self.writes('שאלה', index=1)
        self.writes('שאלה', index=2)
        Contact.objects.filter(pk=self.customer(2).id).update(handled_by='human')
        demo = self.post('contacts/', {'phone': '050-5559001', 'name': 'דמו', 'is_demo': True}).data
        self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי'})
        later = timezone.now() + timedelta(minutes=1)
        self.assertEqual({contact.id for contact in shadow.pending(later)}, {self.customer(2).id, demo['id']})
        self.mode(mode='test')
        self.assertEqual(shadow.pending(later).count(), 3)

    def test_nobody_but_a_customer_who_wrote_is_called_for(self):
        self.go_all()
        self.kick.reset_mock()
        demo = self.post('contacts/', {'phone': phone(9001), 'name': 'דמו', 'is_demo': True}).data
        self.post(f'contacts/{demo["id"]}/simulate-inbound/', {'text': 'היי'})
        self.assertEqual(self.writes('תשובת הבוט הישן', index=1, event='bot_reply').data['stored'], True)    # a copy of an answer is no question
        self.writes('', index=2)                                         # nothing was written: kept for nobody but a test number
        self.writes('שלום', index=3)
        Contact.objects.filter(pk=self.customer(3).id).update(handled_by='human')
        self.writes('אתם שם?', index=3)                                   # a person holds the conversation
        self.assertEqual(self.writes('שלום', index=4).data['stored'], True)
        self.assertEqual(self.writes('שלום', index=4).data['stored'], False)    # ManyChat repeating itself
        self.assertEqual([call.args[0] for call in self.kick.call_args_list], [self.customer(3).id, self.customer(4).id])
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(demo['id']).data['outcome'], 'not_live_test')
            self.assertEqual(self.reply(self.customer(3).id).data['outcome'], 'human_handles')
        request.assert_not_called()
        self.assertFalse(Contact.objects.filter(phone=phone(2)).exists())


# --- never a second answer on top of somebody else's ------------------------------------------------------------

class SomebodyElseAnsweredTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.go_all()

    def while_claude_thinks(self, happens):
        """Claude's answer comes back only after `happens()` did."""
        real = shadow.answer_now

        def slow(contact, **options):
            proposal = real(contact, **options)
            happens()
            return proposal

        return patch(ANSWER_NOW, side_effect=slow)

    def test_the_old_bot_still_connected_in_manychat_answered_first_and_the_new_one_keeps_quiet(self):
        self.writes('יש חוג קפוארה?')
        contact = self.customer()

        def the_old_bot_answers():
            self.assertEqual(self.writes('כן, יש קפוארה בימי שלישי', event='bot_reply').data['stored'], True)

        with self.while_claude_thinks(the_old_bot_answers), patch(REQUEST) as request, self.assertLogs('apps.wahub.live_test', level='WARNING'):
            response = self.reply(contact.id)
        self.assertEqual(response.data['outcome'], 'answered_by_another')
        request.assert_not_called()                                      # the customer got one answer, not two
        self.assertEqual(Message.objects.filter(contact=contact, new_bot=True).count(), 0)
        self.assertEqual(self.journal(contact.id)[-1], live_test.ANSWERED_MEANWHILE)
        # The status screen says the old bot is still there, and how many of its answers were seen.
        state = self.bot()
        self.assertEqual(state['old_bot_replies'], 1)
        self.assertEqual(state['warning'], bot_mode.OLD_BOT_STILL_ANSWERS.format(n=1))
        self.assertIn('לנתק את צעד ה-POST לספק', state['warning'])
        # Nothing to say on the contact, nothing to start again, and no failure for the brake.
        self.live_for(120)
        self.aged(contact.id, 3)
        self.kick.reset_mock()
        self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)
        self.assertEqual(live_test.flag_unanswered(), 0)
        self.assertFalse(self.customer().needs_human)
        self.assertEqual(self.bot()['today']['failed'], 0)

    def test_the_office_wrote_from_the_screen_while_the_answer_was_computed(self):
        self.writes('יש חוג קפוארה?')
        contact = self.customer()

        def the_office_answers():
            self.assertEqual(self.post(f'contacts/{contact.id}/send/', {'text': 'עונה לך מהמשרד'}).data['message']['status'], 'sent')

        with self.while_claude_thinks(the_office_answers), patch(REQUEST, return_value=OK) as request, self.assertLogs('apps.wahub.live_test', level='WARNING'):
            response = self.reply(contact.id)
        self.assertEqual(response.data['outcome'], 'answered_by_another')
        self.assertEqual(sends(request), [(5001, 'עונה לך מהמשרד')])
        self.assertEqual(self.bot()['old_bot_replies'], 0)                # the office is not the old bot
        # His next message is the new bot's again.
        self.writes('תודה, ומה המחיר?')
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply(contact.id).data['outcome'], 'sent')
        self.assertEqual(len(sends(request)), 1)

    def test_old_bot_answers_from_before_the_mode_was_live_are_not_counted(self):
        self.mode(mode='test')
        self.writes('שאלה', index=2)
        self.writes('תשובת הבוט הישן', index=2, event='bot_reply')
        self.go_all()
        state = self.bot()
        self.assertEqual((state['old_bot_replies'], state['warning']), (0, ''))


# --- a message is not lost, and nobody is answered twice --------------------------------------------------------

class SafetyNetTests(GoLiveCase):
    PATCH_KICK = False

    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.go_all()

    def lost(self, text='יש חוג קפוארה?', index=1, minutes=3) -> Contact:
        """A customer's message whose self-call never left, `minutes` ago — the mode has been live for longer."""
        with patch(KICK_POST, side_effect=requests.ConnectionError('refused')), self.assertLogs('apps.wahub.live_test', level='WARNING'):
            self.assertEqual(self.writes(text, index=index).data, {'ok': True, 'stored': True})
        contact = self.customer(index)
        self.live_for(120)
        self.aged(contact.id, minutes)
        return contact

    def test_a_self_call_that_did_not_leave_is_started_again_by_the_cron_and_answered_once(self):
        contact = self.lost()
        self.assertEqual(self.journal(contact.id), ['התשובה החיה לא הופעלה: השרת לא הגיע לעצמו (ConnectionError)'])
        self.assertFalse(contact.needs_human)
        self.assertEqual(self.bot()['today']['not_started'], 1)
        with patch(KICK_POST, side_effect=requests.ReadTimeout('the answer takes longer')) as post:
            self.assertEqual(live_test.flag_unanswered(), 0)
            self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 1, 'live_late_not_started': 0})
        post.assert_called_once_with(
            OWN_REPLY, json={'contact': contact.id, 'late': True}, headers={'X-Cron-Token': 'cron-secret'}, timeout=(2, 1),
        )
        # That call, as this server gets it.
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply(contact.id, late=True)
        self.assertEqual((response.data['outcome'], response.data['late']), ('sent', True))
        self.assertEqual(len(sends(request)), 1)
        self.assertTrue(self.journal(contact.id)[-1].startswith('תשובה חיה נשלחה: התקבלה '))
        self.assertIn('נענתה באיחור (רשת הביטחון)', self.journal(contact.id)[-1])
        today = self.bot()['today']
        self.assertEqual((today['sent'], today['late'], today['failed'], today['needs_human']), (1, 1, 0, 0))
        # Answered: nothing is started, said or sent a second time — by the cron, or by another call.
        with patch(KICK_POST) as post, patch(REQUEST) as request:
            self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 0, 'live_late_not_started': 0})
            self.assertEqual(live_test.flag_unanswered(), 0)
            self.assertEqual(self.reply(contact.id, late=True).data['outcome'], 'nothing')
        post.assert_not_called()
        request.assert_not_called()
        self.assertFalse(self.customer().needs_human)

    def test_the_whole_cron_call_starts_it_and_two_calls_never_answer_one_message_twice(self):
        contact = self.lost()
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')) as post, override_settings(ANTHROPIC_API_KEY=''):
            ticked = self.machine.get(f'{BASE}/cron/tick/', HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertEqual((ticked['live_late_started'], ticked['live_test_unanswered']), (1, 0))
        self.assertEqual(post.call_args.args[0], OWN_REPLY)
        # The door's own call arrived after all, together with the cron's: the one that holds the contact answers.
        Contact.objects.filter(pk=contact.id).update(live_test_busy_until=timezone.now() + timedelta(seconds=60))
        with patch(REQUEST) as request, patch(KICK_POST) as post:
            self.assertEqual(self.reply(contact.id, late=True).data['outcome'], 'busy')
            self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)       # and the cron leaves it alone
        request.assert_not_called()
        post.assert_not_called()
        Contact.objects.filter(pk=contact.id).update(live_test_busy_until=None)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply(contact.id).data['outcome'], 'sent')
            self.assertEqual(self.reply(contact.id, late=True).data['outcome'], 'nothing')
        self.assertEqual(len(sends(request)), 1)
        self.assertEqual(Message.objects.filter(contact=contact, direction='out').count(), 1)

    def test_a_message_nobody_answered_in_a_quarter_of_an_hour_goes_to_a_person_and_gets_no_late_answer(self):
        contact = self.lost(minutes=16)
        with patch(KICK_POST) as post, patch(REQUEST) as request:
            self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 0, 'live_late_not_started': 0})
            self.assertEqual(live_test.flag_unanswered(), 1)
            self.assertEqual(live_test.flag_unanswered(), 0)             # said once
        post.assert_not_called()
        request.assert_not_called()
        contact = self.customer()
        self.assertTrue(contact.needs_human)
        self.assertEqual(contact.needs_human_reason, 'הבוט החדש לא ענה: הודעה לא נענתה')      # a customer is no "live test"
        self.assertTrue(self.journal(contact.id)[-1].startswith('הודעה לא נענתה: התקבלה '))
        today = self.bot()['today']
        self.assertEqual((today['unanswered'], today['needs_human'], today['sent']), (1, 1, 0))

    def test_an_answer_that_was_computed_with_no_sign_that_it_left_is_said_and_never_sent_again(self):
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')):
            self.writes('יש חוג קפוארה?')
        contact = self.customer()
        last = Message.objects.get(contact=contact)
        # The call was cut between keeping the answer and sending it.
        ShadowReply.objects.create(contact=contact, after_message=last, covers_message_ids=[last.id], text='תשובה', model='stub')
        Contact.objects.filter(pk=contact.id).update(needs_shadow=False)
        self.live_for(120)
        self.aged(contact.id, 1.5)
        with patch(KICK_POST) as post, patch(REQUEST) as request:
            self.assertEqual(live_test.flag_unanswered(), 0)             # the call may still be sending it
            self.aged(contact.id, 3)
            self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 0, 'live_late_not_started': 0})
            self.assertEqual(live_test.flag_unanswered(), 1)
            self.assertEqual(live_test.flag_unanswered(), 0)
            self.assertEqual(self.reply(contact.id, late=True).data['outcome'], 'nothing')
        post.assert_not_called()
        request.assert_not_called()
        contact = self.customer()
        self.assertEqual(contact.needs_human_reason, 'הבוט החדש לא ענה: חושבה תשובה ולא ידוע אם נשלחה')
        self.assertIn('לא נשלחת שוב', self.journal(contact.id)[-1])

    def test_what_somebody_else_answered_a_person_holds_or_a_machine_wrote_is_left_alone(self):
        office = self.lost(index=1)
        with patch(REQUEST, return_value=OK):
            self.post(f'contacts/{office.id}/send/', {'text': 'עונה לך מהמשרד'})
        held = self.lost(index=2)
        Contact.objects.filter(pk=held.id).update(handled_by='human')
        machine = self.lost('תודה שפניתם אלינו, זוהי הודעה אוטומטית ונחזור אליכם בהקדם', index=3)
        old_bot = self.lost(index=4)
        self.assertEqual(self.writes('הבוט הישן עוד מחובר וענה', index=4, event='bot_reply').data['stored'], True)
        for minutes in (3, 16):
            for contact in (office, held, machine, old_bot):
                self.aged(contact.id, minutes)
            with patch(KICK_POST) as post:
                self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 0, 'live_late_not_started': 0})
                self.assertEqual(live_test.flag_unanswered(), 0)
            post.assert_not_called()
        self.assertEqual(Contact.objects.filter(needs_human=True).count(), 0)

    def test_a_business_that_answers_by_machine_gets_no_answer_and_the_journal_says_why_once(self):
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')):
            self.writes('תודה שפניתם אלינו, זוהי הודעה אוטומטית ונחזור אליכם בהקדם')
        contact = self.customer()
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(contact.id).data['outcome'], 'nothing')
        request.assert_not_called()
        self.assertEqual(self.journal(contact.id), [live_test.AUTOMATIC_LINE])
        self.assertFalse(self.customer().needs_human)
        self.live_for(120)
        for minutes in (3, 16):
            self.aged(contact.id, minutes)
            ContactEvent.objects.filter(contact=contact).update(created_at=timezone.now() - timedelta(minutes=minutes - 1))
            with patch(KICK_POST) as post:
                self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)
                self.assertEqual(live_test.flag_unanswered(), 0)
            post.assert_not_called()
        self.assertEqual(self.bot()['today'], {'sent': 0, 'failed': 0, 'late': 0, 'needs_human': 0, 'fallback': 0, 'unanswered': 0, 'not_started': 0})

    def test_a_call_starts_eight_at_the_most_whoever_waited_longest_first(self):
        ids = self.waiting(12, minutes=10) + self.waiting(3, minutes=4, start=200)
        self.live_for(120)
        with patch(KICK_POST, side_effect=requests.ReadTimeout('')) as post:
            self.assertEqual(live_test.answer_late(OWN_REPLY), {'live_late_started': 8, 'live_late_not_started': 0})
        started = [call.kwargs['json']['contact'] for call in post.call_args_list]
        self.assertEqual(len(started), live_test.LATE_PER_RUN)
        self.assertTrue(set(started) <= set(ids[:12]))                   # the ones ten minutes old, before the ones four minutes old
        # A self-call that does not leave is counted as one that did not, and is written on the contact.
        with patch(KICK_POST, return_value=MagicMock(status_code=404)), self.assertLogs('apps.wahub.live_test', level='WARNING'):
            counts = live_test.answer_late(OWN_REPLY)
        self.assertEqual(counts, {'live_late_started': 0, 'live_late_not_started': 8})
        self.assertIn('התשובה החיה לא הופעלה: השרת ענה לעצמו 404', self.journal(ids[0]))

    def test_the_look_costs_the_same_whether_thirty_wait_or_a_hundred_and_fifty(self):
        self.live_for(120)
        self.waiting(30, minutes=3)
        now = timezone.now()
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(live_test.flag_unanswered(now), 0)
        self.assertEqual(len(live_test._overdue(now)), 30)
        self.waiting(120, minutes=3, start=300)
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(live_test.flag_unanswered(now), 0)
        self.assertEqual(len(live_test._overdue(now)), 150)
        self.assertEqual(len(many), len(few))
        self.assertLessEqual(len(many), 9)                               # three for the two switches, six for the contacts
        # More than it reads at once: the latest to write are read, and nothing breaks.
        self.waiting(100, minutes=5, start=500)
        self.assertEqual(len(live_test._overdue(now)), live_test.WATCHED)


# --- the door a machine uses --------------------------------------------------------------------------------------

class SelfCallTests(GoLiveCase):
    PATCH_KICK = False

    def test_the_reply_door_answers_a_ping_and_nothing_else_happens(self):
        with patch(REQUEST) as request:
            response = self.machine.post(REPLY_URL, {'ping': True}, format='json', HTTP_X_CRON_TOKEN='cron-secret')
            self.assertEqual((response.status_code, response.data), (200, {'ok': True, 'pong': True}))
            self.assertEqual(self.machine.post(REPLY_URL, {'ping': True}, format='json').status_code, 401)
            self.assertEqual(self.machine.post(REPLY_URL, {'ping': 'yes'}, format='json', HTTP_X_CRON_TOKEN='cron-secret').status_code, 400)
        request.assert_not_called()

    def test_the_check_says_why_the_server_does_not_reach_itself(self):
        patch.stopall()                                                  # the real check, on a mocked wire
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('network')).start()
        answer = MagicMock(status_code=200)
        answer.json.return_value = {'ok': True, 'pong': True}
        with patch(KICK_POST, return_value=answer) as post:
            self.assertEqual(live_test.ping(OWN_REPLY), '')
        post.assert_called_once_with(OWN_REPLY, json={'ping': True}, headers={'X-Cron-Token': 'cron-secret'}, timeout=live_test.PING_TIMEOUT)
        # The address is wrong (11.10.2026: the public base had no /api/v1 and the door answered 404).
        with patch(KICK_POST, return_value=MagicMock(status_code=404)):
            self.assertEqual(live_test.ping('https://kogo.example/wahub/live-test/reply/'), 'השרת ענה לעצמו 404')
        with patch(KICK_POST, side_effect=requests.ConnectionError('refused')):
            self.assertEqual(live_test.ping(OWN_REPLY), 'השרת לא הגיע לעצמו (ConnectionError)')
        other = MagicMock(status_code=200)
        other.json.side_effect = ValueError('a page, not our answer')
        with patch(KICK_POST, return_value=other):
            self.assertEqual(live_test.ping(OWN_REPLY), 'בכתובת של השרת ענה משהו אחר')
        with override_settings(CRON_TOKEN=''), patch(KICK_POST) as post:
            self.assertIn('CRON_TOKEN', live_test.ping(OWN_REPLY))
        post.assert_not_called()

    def test_the_address_the_server_calls_itself_at(self):
        from apps.wahub.views import _own_url

        request = MagicMock()
        request.build_absolute_uri.side_effect = lambda path: f'http://testserver{path}'
        for base, expected in (
            ('', OWN_REPLY),
            ('http://localhost:8000', OWN_REPLY),
            ('https://kogo-back.example', 'https://kogo-back.example/api/v1/wahub/live-test/reply/'),
            ('https://kogo-back.example/', 'https://kogo-back.example/api/v1/wahub/live-test/reply/'),
            ('https://kogo-back.example/api/v1', 'https://kogo-back.example/api/v1/wahub/live-test/reply/'),
            ('https://kogo-back.example/api/v1/', 'https://kogo-back.example/api/v1/wahub/live-test/reply/'),
        ):
            with self.subTest(base=base), override_settings(CRM_API_BASE_URL=base):
                self.assertEqual(_own_url(request, live_test.REPLY_PATH), expected)


# --- Claude fails while the bot answers everybody -------------------------------------------------------------

class ClaudeFailsTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.go_all()
        self.words = KnowledgeItem.objects.get(kind='phrasing', key='handoff_notice').body
        self.failing = patch(ANSWER_NOW, side_effect=shadow.ShadowModelError('Claude ענה 500')).start()
        self.hours = patch(HOURS, return_value=OPEN).start()

    def test_the_customer_is_told_once_that_a_person_will_come_back_and_the_office_is_asked_for(self):
        self.assertEqual(self.words, 'הפנייה הועברה לנציג מהמשרד, נחזור אליך בהקדם')
        self.writes('יש חוג קפוארה?')
        contact = self.customer()
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply(contact.id)
        self.assertEqual((response.data['outcome'], response.data['error'], response.data['fallback']), ('model_error', 'Claude ענה 500', True))
        self.assertEqual(sends(request), [(5001, self.words)])
        message = Message.objects.get(contact=contact, direction='out')
        self.assertEqual((message.sender, message.new_bot, message.status, message.text), ('bot', True, 'sent', self.words))
        contact = self.customer()
        self.assertTrue(contact.needs_human)
        self.assertEqual(contact.needs_human_reason, 'הבוט החדש לא ענה: Claude ענה 500')
        told, failed = self.journal(contact.id)
        self.assertEqual(told, 'נשלח ללקוח נוסח ההעברה לנציג')
        self.assertTrue(failed.startswith('תשובה חיה נכשלה: Claude ענה 500 (התקבלה '), failed)
        self.assertTrue(failed.endswith('— נשלח ללקוח נוסח ההעברה לנציג'), failed)
        today = self.bot()['today']
        self.assertEqual((today['failed'], today['fallback'], today['needs_human'], today['sent']), (1, 1, 1, 0))
        self.assertEqual(self.bot()['last_failure']['kind'], 'model')

        # He writes again and Claude still fails: not told a second time, and nothing is said about the next messages either.
        self.writes('הלו?')
        self.writes('מישהו?')
        with patch(REQUEST) as request:
            self.assertNotIn('fallback', self.reply(contact.id).data)
        request.assert_not_called()
        self.assertEqual(Message.objects.filter(contact=contact, direction='out').count(), 1)
        self.assertIn('לא נשלח דבר', self.journal(contact.id)[-1])
        # The cron neither starts those again nor says more: the journal already speaks of them.
        self.live_for(120)
        self.aged(contact.id, 3)
        ContactEvent.objects.filter(contact=contact).update(created_at=timezone.now() - timedelta(minutes=2))
        self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)
        self.assertEqual(live_test.flag_unanswered(), 0)

        # Three hours on, a new conversation: told again.
        ContactEvent.objects.filter(contact=contact).update(created_at=timezone.now() - timedelta(hours=3, minutes=1))
        self.writes('אפשר עזרה?')
        with patch(REQUEST, return_value=OK) as request:
            self.assertTrue(self.reply(contact.id).data['fallback'])
        self.assertEqual(sends(request), [(5001, self.words)])

    def test_outside_the_office_hours_the_closed_message_of_the_day_is_what_is_said(self):
        self.hours.return_value = {'configured': True, 'open': False, 'message_if_closed': 'המשרד סגור עכשיו, נחזור אליכם מחר ב-10:30'}
        self.writes('יש חוג קפוארה?')
        with patch(REQUEST, return_value=OK) as request:
            self.assertTrue(self.reply(self.customer().id).data['fallback'])
        self.assertEqual(sends(request), [(5001, 'המשרד סגור עכשיו, נחזור אליכם מחר ב-10:30')])
        # Our own filler for a day with no message filed is not something to send: the phrasing is.
        self.hours.return_value = {'configured': True, 'open': False, 'message_if_closed': 'המשרד סגור כרגע'}
        self.writes('שאלה', index=2)
        with patch(REQUEST, return_value=OK) as request:
            self.reply(self.customer(2).id)
        self.assertEqual(sends(request), [(5002, self.words)])

    def test_with_no_words_in_the_knowledge_nothing_is_sent_and_a_person_is_asked_for(self):
        KnowledgeItem.objects.filter(kind='phrasing', key='handoff_notice').update(is_active=False)
        self.writes('יש חוג קפוארה?')
        with patch(REQUEST) as request:
            response = self.reply(self.customer().id)
        request.assert_not_called()
        self.assertNotIn('fallback', response.data)
        self.assertTrue(self.customer().needs_human)
        self.assertEqual(self.bot()['today']['fallback'], 0)

    def test_a_send_of_the_words_that_did_not_get_through_is_not_tried_again(self):
        self.writes('יש חוג קפוארה?')
        contact = self.customer()
        refusal = ManyChatError('Validation error', status_code=400, payload={'details': {'messages': [{'message': 'not subscribed'}]}})
        with patch(REQUEST, side_effect=refusal):
            self.assertNotIn('fallback', self.reply(contact.id).data)
        failed = Message.objects.get(contact=contact, direction='out')
        self.assertEqual((failed.status, failed.text), ('failed', self.words))
        self.assertTrue(self.journal(contact.id)[0].startswith('נשלח ללקוח נוסח ההעברה לנציג — לא יצא: '))
        self.writes('הלו?')
        with patch(REQUEST) as request:
            self.reply(contact.id)
        request.assert_not_called()

    def test_in_the_mode_test_nothing_is_sent_as_before(self):
        self.mode(mode='test')
        self.writes('היי', index=5)
        Contact.objects.filter(pk=self.customer(5).id).update(live_test=True)
        with patch(REQUEST) as request:
            response = self.reply(self.customer(5).id)
        self.assertEqual(response.data['outcome'], 'model_error')
        request.assert_not_called()
        owner = self.customer(5)
        self.assertEqual(owner.needs_human_reason, 'הבוט החדש לא ענה (בדיקה חיה): Claude ענה 500')
        self.assertIn('לא נשלח דבר', self.journal(owner.id)[-1])

    def test_without_a_claude_key_the_same_words_go_out_and_the_failure_is_counted(self):
        self.failing.stop()
        self.writes('יש חוג קפוארה?')
        with override_settings(ANTHROPIC_API_KEY=''), patch(REQUEST, return_value=OK) as request:
            response = self.reply(self.customer().id)
        self.assertEqual((response.data['outcome'], response.data['fallback']), ('model_error', True))
        self.assertEqual(sends(request), [(5001, self.words)])
        self.sleep.assert_not_called()


# --- the brakes ----------------------------------------------------------------------------------------------------

class BrakeRulesTests(WahubTestCase):
    def outcomes(self, *rows):
        """[(minutes ago, ok)] as the kept list, oldest first."""
        now = timezone.now()
        return [[(now - timedelta(minutes=minutes)).isoformat(), int(ok), 'sent' if ok else 'model'] for minutes, ok in rows], now

    def reason(self, *rows):
        recent, now = self.outcomes(*rows)
        return bot_mode.brake_reason(recent, now)

    def test_five_failures_in_a_row_inside_ten_minutes(self):
        self.assertEqual(self.reason(*[(minutes, False) for minutes in (9, 7, 5, 3, 1)]), '5 תשובות נכשלו ברצף בתוך 10 דקות')
        self.assertEqual(self.reason(*[(minutes, False) for minutes in (7, 5, 3, 1)]), '')
        # One that went out in between starts the count again; so does a failure from longer ago.
        self.assertEqual(self.reason((9, False), (8, False), (7, True), (5, False), (3, False), (1, False)), '')
        self.assertEqual(self.reason((12, False), (7, False), (5, False), (3, False), (1, False)), '')
        self.assertEqual(self.reason((9, True), (8, False), (7, False), (5, False), (3, False), (1, False)), '5 תשובות נכשלו ברצף בתוך 10 דקות')

    def test_half_of_a_busy_ten_minutes(self):
        busy = [(9, False), (9, True), (8, False), (7, True), (6, False), (5, True), (4, False), (3, True), (2, False), (1, True)]
        self.assertEqual(self.reason(*busy), '5 מתוך 10 תשובות נכשלו בתוך 10 דקות')
        self.assertEqual(self.reason(*busy[1:]), '')                     # nine answers are not a busy window
        self.assertEqual(self.reason(*busy[:-2], (2, True), (1, True)), '')              # four of ten

    def test_ten_in_a_row_when_few_people_write(self):
        slow = [(minutes, False) for minutes in (58, 52, 46, 40, 34, 28, 22, 16, 10, 4)]
        self.assertEqual(self.reason(*slow), '10 תשובות נכשלו ברצף בתוך 60 דקות')
        self.assertEqual(self.reason(*slow[1:]), '')
        self.assertEqual(self.reason((70, False), *slow[1:]), '')

    def test_nothing_kept_and_rubbish_kept_stop_nothing(self):
        self.assertEqual(bot_mode.brake_reason([], timezone.now()), '')
        self.assertEqual(bot_mode.brake_reason([['not a time', 0, 'model']] * 20, timezone.now()), '')

    @override_settings(WAHUB_BRAKE_IN_A_ROW=3)
    def test_the_run_is_a_setting(self):
        self.assertEqual(self.reason((5, False), (3, False), (1, False)), '3 תשובות נכשלו ברצף בתוך 10 דקות')


class BrakeTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.go_all()
        self.hours = patch(HOURS, return_value=OPEN).start()

    def fail(self, index):
        self.writes('יש חוג קפוארה?', index=index)
        with patch(ANSWER_NOW, side_effect=shadow.ShadowModelError('Claude ענה 529')), patch(REQUEST, return_value=OK):
            return self.reply(self.customer(index).id).data

    def test_a_run_of_failures_puts_the_mode_back_to_test_by_itself_and_nothing_more_leaves(self):
        with patch('apps.core.office_alerts.raise_office_alert') as alert:
            for index in range(1, 5):
                self.fail(index)
            self.assertEqual((bot_mode.mode(), self.bot()['stopped']), ('all', None))
            alert.assert_not_called()
            with self.assertLogs('apps.wahub.bot_mode', level='ERROR'):
                self.fail(5)
        self.assertEqual(bot_mode.mode(), 'test')
        self.assertIsNone(bot_mode.live_since())
        state = self.get('settings/bot-mode/').data
        self.assertEqual((state['mode'], state['live'], state['live_since']), ('test', False, None))
        self.assertEqual(state['stopped']['reason'], '5 תשובות נכשלו ברצף בתוך 10 דקות')
        self.assertEqual(state['stopped']['text'], 'הבוט החדש עצר את עצמו: 5 תשובות נכשלו ברצף בתוך 10 דקות')
        self.assertIn('עצר את עצמו', state['warning'])
        self.assertEqual((state['log'][0]['what'], state['log'][0]['to'], state['log'][0]['by']), ('brake', 'test', ''))
        self.assertEqual(state['today']['failed'], 5)
        # The office is told, with what to do.
        alert.assert_called_once()
        told = alert.call_args.kwargs
        self.assertEqual((told['kind'], told['what']), ('wahub_bot_stopped', '5 תשובות נכשלו ברצף בתוך 10 דקות'))
        self.assertIn('אף אחד לא עונה ללקוחות אוטומטית', told['action'])

        # From here on: no call for a new message, no answer, no words of handoff — to anybody.
        self.kick.reset_mock()
        self.writes('יש חוג קפוארה?', index=6)
        self.kick.assert_not_called()
        with patch(REQUEST) as request:
            self.assertEqual(self.reply(self.customer(6).id).data['outcome'], 'not_live_test')
            self.assertEqual(self.reply(self.customer(1).id).data['outcome'], 'not_live_test')
            self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)
            self.assertEqual(live_test.flag_unanswered(), 0)
        request.assert_not_called()

        # A person turns it on again: the stop is cleared, and yesterday's failures are not this beginning's.
        state = self.go_all()
        self.assertEqual((state['mode'], state['live'], state['stopped']), ('all', True, None))
        self.fail(7)
        self.assertEqual(bot_mode.mode(), 'all')

    def test_choosing_test_after_a_stop_says_that_a_person_saw_it(self):
        bot_mode.trip('בדיקה')
        self.assertIsNotNone(self.bot()['stopped'])
        state = self.mode(mode='test').data
        self.assertEqual((state['mode'], state['stopped'], state['warning']), ('test', None, ''))
        self.assertEqual([row['what'] for row in state['log']][:2], ['mode', 'brake'])

    def test_an_answer_on_its_way_when_the_bot_stops_is_not_sent(self):
        self.writes('יש חוג קפוארה?')
        self.sleep.side_effect = lambda _seconds: bot_mode.trip('בדיקה')        # stopped while the answer waits for the burst
        with patch(REQUEST) as request, self.assertLogs('apps.wahub.bot_mode', level='ERROR'):
            self.assertEqual(self.reply(self.customer().id).data['outcome'], 'not_live_test')
        request.assert_not_called()
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)

    def test_an_answer_that_was_computed_when_the_bot_stopped_is_kept_and_not_sent(self):
        self.writes('יש חוג קפוארה?')
        real = shadow.answer_now

        def stops_meanwhile(contact, **options):
            proposal = real(contact, **options)
            bot_mode.trip('בדיקה')
            return proposal

        with patch(ANSWER_NOW, side_effect=stops_meanwhile), patch(REQUEST) as request, self.assertLogs('apps.wahub.bot_mode', level='ERROR'):
            response = self.reply(self.customer().id)
        self.assertEqual(response.data['outcome'], 'not_live_test')
        request.assert_not_called()
        self.assertEqual(ShadowReply.objects.count(), 1)                 # what it would have said stays, as a shadow answer

    def test_failures_in_the_mode_test_stop_nothing(self):
        self.mode(mode='test')
        self.writes('היי', index=1)
        Contact.objects.filter(pk=self.customer().id).update(live_test=True)
        for _ in range(12):
            bot_mode.record(bot_mode.KIND_MODEL, text='Claude ענה 500')
        state = self.bot()
        self.assertEqual((state['mode'], state['stopped'], state['today']['failed']), ('test', None, 12))

    def test_a_send_that_failed_and_a_self_call_that_did_not_leave_count_too(self):
        refusal = ManyChatError('Validation error', status_code=400, payload={'details': {'messages': [{'message': 'not subscribed'}]}})
        for index in range(1, 4):
            self.writes('יש חוג קפוארה?', index=index)
            with patch(REQUEST, side_effect=refusal):
                self.assertEqual(self.reply(self.customer(index).id).data['outcome'], 'failed')
        self.assertEqual(self.customer(1).needs_human_reason[:21], 'השליחה ללקוח נכשלה: M')
        bot_mode.record(bot_mode.KIND_NOT_STARTED, text='השרת ענה לעצמו 500')
        self.assertEqual(bot_mode.mode(), 'all')
        with self.assertLogs('apps.wahub.bot_mode', level='ERROR'):
            bot_mode.record(bot_mode.KIND_UNANSWERED)
        state = self.bot()
        self.assertEqual((state['mode'], state['today']['failed'], state['today']['not_started'], state['today']['unanswered']), ('test', 3, 1, 1))
        self.assertEqual(state['last_failure']['kind'], 'unanswered')


class LoopBrakeTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.go_all()

    def answered(self, contact, count, minutes_apart=4):
        now = timezone.now()
        for index in range(count):
            Message.objects.create(
                contact=contact, direction='out', sender='bot', text=str(index), new_bot=True, status='sent',
                sent_at=now - timedelta(minutes=1 + index * minutes_apart),
            )

    def test_twelve_answers_in_an_hour_and_the_bot_stops_answering_him(self):
        self.writes('שלום')
        contact = self.customer()
        self.answered(contact, 11)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(self.reply(contact.id).data['outcome'], 'sent')         # the twelfth
            self.writes('ושוב')
            self.assertEqual(self.reply(contact.id).data['outcome'], 'hourly_limit')
            self.writes('ושוב 2')
            self.assertEqual(self.reply(contact.id).data['outcome'], 'hourly_limit')
        self.assertEqual(len(sends(request)), 1)
        contact = self.customer()
        self.assertTrue(contact.needs_human)
        self.assertEqual(contact.needs_human_reason, live_test.HOURLY_LIMIT_TEXT)
        self.assertEqual(self.journal(contact.id).count(live_test.HOURLY_LIMIT_TEXT), 1)     # said once
        self.sleep.reset_mock()
        # The cron does not start it again, and does not call it "never answered".
        self.live_for(120)
        for minutes in (3, 16):
            self.aged(contact.id, minutes)
            ContactEvent.objects.filter(contact=contact).update(created_at=timezone.now() - timedelta(minutes=30))
            self.kick.reset_mock()
            self.assertEqual(live_test.answer_late(OWN_REPLY)['live_late_started'], 0)
            self.assertEqual(live_test.flag_unanswered(), 0)
        self.kick.assert_not_called()
        # It is no failure of the bot's: the brake does not hear of it.
        self.assertEqual(self.bot()['today']['failed'], 0)
        # An hour later he is answered again.
        Message.objects.filter(contact=contact, new_bot=True).update(sent_at=timezone.now() - timedelta(minutes=61))
        self.writes('אפשר שאלה?')
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply(contact.id).data['outcome'], 'sent')

    def test_the_owners_test_number_has_no_hourly_limit_and_everybody_has_thirty_a_day(self):
        self.writes('שלום', index=1)
        owner = self.customer(1)
        Contact.objects.filter(pk=owner.id).update(live_test=True)
        self.answered(owner, 15, minutes_apart=1)
        with patch(REQUEST, return_value=OK):
            self.assertEqual(self.reply(owner.id).data['outcome'], 'sent')
        self.writes('שלום', index=2)
        customer = self.customer(2)
        self.answered(customer, 30, minutes_apart=0)
        # Thirty since midnight; the hour's own limit is put out of the way so that the day's is the one that speaks.
        with patch.object(live_test, 'HOURLY_LIMIT', 1000), patch(REQUEST) as request:
            self.assertEqual(self.reply(customer.id).data['outcome'], 'daily_limit')
        request.assert_not_called()
        customer = self.customer(2)
        self.assertEqual((customer.needs_human, customer.needs_human_reason), (True, live_test.DAILY_LIMIT_TEXT))


# --- the follow-ups to leads: a switch of their own, and never the pile ------------------------------------------

class FollowUpsSwitchTests(EngineCase):
    def setUp(self):
        super().setUp()
        WahubSetting.objects.filter(key=settings_store.BOT_FOLLOWUPS).delete()       # as deployed: off

    def followups(self, **body):
        return self.post('settings/followups/', body)

    def notes(self, contact):
        return [(row.step, row.status, row.outcome_note) for row in self.rows(contact)]

    @override_settings(**SENDING_ON)
    def test_with_sending_on_and_the_switch_off_nothing_leaves(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST) as request:
            counts = lead_followups.run()
        request.assert_not_called()
        self.assertEqual((counts['followups_due'], counts['followups_sent']), (1, 0))
        self.assertEqual(self.notes(contact), [(1, 'due', 'החזרות האוטומטיות כבויות')])
        self.assertEqual(Message.objects.count(), 0)
        move = self.get(f'leads/{contact.id}/').data['next']
        self.assertEqual((move['state'], move['note']), ('due', 'החזרות האוטומטיות כבויות'))
        # Two days of that and it is let go, as any follow-up that could not leave.
        self.clock.moment = at(2026, 10, 15)
        with patch(REQUEST) as request:
            self.assertEqual(lead_followups.run()['followups_skipped'], 1)
        request.assert_not_called()
        self.assertEqual(self.notes(contact)[0], (1, 'skipped', 'לא נשלח: החזרות האוטומטיות כבויות'))

    @override_settings(**SENDING_ON)
    def test_turning_them_on_lets_go_of_what_was_waiting_and_sends_only_what_falls_due_after(self):
        waiting = [self.link_lead() for _ in range(14)]                  # the pile: due on the 13th, sending was off
        with override_settings(WAHUB_SENDING_ENABLED=False):
            self.clock.moment = at(2026, 10, 13)
            self.assertEqual(lead_followups.run()['followups_due'], 14)
            self.assertEqual(self.get('settings/followups/').data['followups']['due_now'], 14)
        # The next morning sending is on, and he turns the follow-ups on.
        self.clock.moment = at(2026, 10, 14, 11, 0)
        fresh = self.link_lead(wrote=timedelta(minutes=10))              # asked at 10:50 today; due tomorrow at 10:30, inside WhatsApp's day
        self.assertEqual(self.followups(on=True).data['code'], 'confirm_required')
        self.assertEqual(self.followups(on='yes', confirm='on').data['code'], 'bad_value')
        state = self.followups(on=True, confirm='on').data['followups']
        self.assertEqual((state['on'], state['since'], state['by']), (True, self.clock().isoformat(), 'דנה מנהלת'))
        self.assertTrue(state['text'].startswith('דולקות — יוצאות רק חזרות שמועדן אחרי 14.10 11:00'), state['text'])
        self.assertEqual(self.get('settings/bot-mode/').data['log'][1]['text'], 'חזרות הבוט ללידים הודלקו')
        with patch(REQUEST) as request:
            counts = lead_followups.run()                                # the very next cron call: they were not left for half an hour
        request.assert_not_called()
        self.assertEqual((counts['followups_skipped'], counts['followups_sent']), (14, 0))
        for contact in waiting:
            self.assertEqual(self.notes(contact)[0], (1, 'skipped', 'דולג — הגיע זמנו לפני שהחזרות הודלקו'))
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(
            ContactEvent.objects.filter(contact=waiting[0], kind='lead_followup').latest('id').text,
            'קיבל קישור ועוד לא נרשם, פנייה 1: דולג — הגיע זמנו לפני שהחזרות הודלקו',
        )
        # Their rounds go on with the next step, in its own time — nothing is made up for.
        self.assertEqual([(row.step, row.status) for row in self.rows(waiting[0])][1], (2, 'planned'))
        self.assertGreater(self.rows(waiting[0])[1].due_at, self.clock())
        # What falls due after the switch came on leaves, when its moment comes.
        self.clock.moment = at(2026, 10, 15, 10, 31)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        request.assert_called_once()
        self.assertEqual(self.rows(fresh)[0].status, 'sent')
        self.assertEqual(self.rows(fresh)[0].message.text, LINK_FIRST)

    def test_turned_on_while_sending_is_off_it_begins_when_sending_is_seen_on(self):
        contact = self.link_lead()
        state = self.followups(on=True, confirm='on').data['followups']
        self.assertEqual((state['on'], state['since']), (True, None))
        self.assertEqual(state['text'], 'דולקות, אבל השליחה כבויה — שום חזרה לא יוצאת')
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_due'], 1)
        self.assertEqual(self.notes(contact), [(1, 'due', 'השליחה ללקוחות כבויה')])
        # Sending comes on an hour later: the follow-up that was waiting is let go, not sent.
        self.clock.forward(hours=1)
        with override_settings(**SENDING_ON), patch(REQUEST) as request:
            self.assertEqual(lead_followups.run()['followups_skipped'], 1)
            self.assertEqual(self.get('settings/followups/').data['followups']['since'], self.clock().isoformat())
        request.assert_not_called()
        self.assertEqual(self.notes(contact)[0], (1, 'skipped', 'דולג — הגיע זמנו לפני שהחזרות הודלקו'))

    @override_settings(**SENDING_ON)
    def test_sending_seen_off_forgets_the_moment_so_coming_back_releases_nothing(self):
        follow_ups_on(since=at(2026, 10, 12, 9, 0).isoformat())
        contact = self.link_lead()
        with override_settings(WAHUB_SENDING_ENABLED=False):
            bot_mode.sync()                                              # the cron saw sending off
            self.clock.moment = at(2026, 10, 13)
            self.assertEqual(lead_followups.run()['followups_due'], 1)
        self.clock.forward(minutes=40)
        with patch(REQUEST) as request:
            self.assertEqual(lead_followups.run()['followups_skipped'], 1)
        request.assert_not_called()
        self.assertEqual(self.notes(contact)[0][2], 'דולג — הגיע זמנו לפני שהחזרות הודלקו')

    @override_settings(**SENDING_ON)
    def test_off_again_stops_them_and_the_same_value_twice_changes_nothing(self):
        self.followups(on=True, confirm='on')
        since = bot_mode.followups_since()
        self.clock.forward(minutes=5)
        self.assertEqual(self.followups(on=True).data['followups']['since'], since.isoformat())    # no confirmation, no new moment
        contact = self.link_lead()
        self.assertEqual(self.followups(on=False).data['followups']['on'], False)
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST) as request:
            self.assertEqual(lead_followups.run()['followups_due'], 1)
        request.assert_not_called()
        self.assertEqual(self.notes(contact), [(1, 'due', 'החזרות האוטומטיות כבויות')])
        self.assertEqual([row['what'] for row in self.get('settings/bot-mode/').data['log']], ['followups', 'followups', 'followups'])

    def test_the_owners_test_number_is_not_asked_and_gets_its_follow_ups_as_before(self):
        contact = self.link_lead(live_test=True)
        self.clock.moment = at(2026, 10, 13)
        with override_settings(MANYCHAT_KEY='test-key'), patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        request.assert_called_once()
        self.assertEqual(self.rows(contact)[0].status, 'sent')

    @override_settings(**SENDING_ON)
    def test_at_night_nothing_leaves_though_the_cron_now_runs(self):
        follow_ups_on()
        contact = self.link_lead()
        for moment in (at(2026, 10, 13, 2, 0), at(2026, 10, 13, 6, 55), at(2026, 10, 13, 10, 29)):
            self.clock.moment = moment
            with patch(REQUEST) as request:
                counts = lead_followups.run()
            request.assert_not_called()
            self.assertEqual((counts['followups_sent'], counts['followups_due']), (0, 0))
        row = self.open_row(contact)
        self.assertEqual((row.status, row.next_look_at), ('planned', at(2026, 10, 13)))
        # Late on a Thursday evening, then Friday and Saturday: it waits for Sunday's opening.
        self.clock.moment = at(2026, 10, 15, 23, 0)
        Contact.objects.filter(pk=contact.pk).update(last_inbound_at=self.clock() - HOUR, lead_link_sent_at=self.clock() - HOUR)
        LeadFollowUp.objects.filter(pk=row.pk).update(next_look_at=self.clock(), due_at=self.clock())
        with patch(REQUEST) as request:
            lead_followups.run()
        request.assert_not_called()
        self.assertEqual(self.open_row(contact).next_look_at, at(2026, 10, 18))

    @override_settings(**SENDING_ON)
    def test_a_whole_cron_call_in_the_middle_of_the_night_sends_nothing(self):
        follow_ups_on()
        contact = self.link_lead()
        freed = self.link_lead()
        WaitingListEntry.objects.create(
            contact=freed, lesson_id=uuid.uuid4(), course_name='קפוארה', status='freed', freed_at=self.clock(),
        )
        self.clock.moment = at(2026, 10, 13, 2, 0)                       # the follow-up fell due yesterday; it is 02:00
        LeadFollowUp.objects.filter(contact=contact).update(due_at=self.clock() - HOUR, next_look_at=self.clock() - HOUR)
        with patch(REQUEST, return_value={'status': 'success', 'data': []}) as request:
            ticked = cron.tick(budget_seconds=5, reply_url=OWN_REPLY)
        self.assertEqual(asked_to_do(request), [])                       # ManyChat was asked nothing but which automations it has
        self.assertEqual((ticked['followups_sent'], ticked['live_late_started']), (0, 0))
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)
        self.assertEqual(self.open_row(contact).next_look_at, at(2026, 10, 13))      # waits for the office to open
        self.assertIsNone(WaitingListEntry.objects.get().notified_at)                # telling a customer of a seat is a person's press

    @override_settings(**SENDING_ON)
    def test_the_card_says_the_switch_is_what_is_missing_and_what_it_holds_back(self):
        self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()
        flows = patch(REQUEST, return_value={'status': 'success', 'data': []})       # ManyChat lists no automations
        flows.start()
        self.addCleanup(flows.stop)
        card = self.get('leads/automation/').data
        line = next(line for line in card['lines'] if line['key'] == 'followups')
        self.assertEqual((line['ok'], line['title'], line['detail']), (False, 'חזרות אוטומטיות', 'כבויות — הבוט מתכנן חזרות ולא שולח אותן'))
        self.assertTrue(line['how'].startswith('מדליקים במתג שבשורה הזאת.'), line['how'])
        self.assertIs(card['ready'], False)
        [row] = card['upcoming']
        self.assertEqual((row['state'], row['blocked'], row['note']), ('due', 'followups', 'החזרות האוטומטיות כבויות'))
        self.assertEqual((card['banner']['reason'], card['banner']['text']), ('followups', 'הודעה אחת הייתה יוצאת עכשיו — החזרות האוטומטיות כבויות'))
        # On, an hour later: the one that was waiting will be let go, and is no longer "about to leave".
        self.clock.forward(hours=1)
        self.followups(on=True, confirm='on')
        card = self.get('leads/automation/').data
        line = next(line for line in card['lines'] if line['key'] == 'followups')
        self.assertEqual((line['ok'], line['how']), (True, ''))
        [row] = card['upcoming']
        self.assertEqual((row['blocked'], row['note']), ('before', 'תדולג — הגיע זמנה לפני שהחזרות הודלקו'))
        self.assertIsNone(card['banner'])
        self.assertEqual(lead_automation.BLOCKED_WORDS['followups'], bot_mode.FOLLOWUPS_OFF_NOTE)


# --- what the office sees ------------------------------------------------------------------------------------------

class StatusTests(GoLiveCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()

    def test_todays_numbers_follow_what_happened_and_start_again_at_midnight(self):
        self.go_all()
        self.writes('יש חוג קפוארה?', index=1)
        self.writes('אפשר לדבר עם נציג?', index=2)
        with patch(REQUEST, return_value=OK):
            self.reply(self.customer(1).id)
            Contact.objects.filter(pk=self.customer(2).id).update(needs_human=False)     # the door's own mark is not the bot's
            self.reply(self.customer(2).id)                              # the bot asks for a person itself
        today = self.bot()['today']
        self.assertEqual(today, {'sent': 2, 'failed': 0, 'late': 0, 'needs_human': 1, 'fallback': 0, 'unanswered': 0, 'not_started': 0})
        self.assertIsNone(self.bot()['last_failure'])
        # The numbers are kept with their day: tomorrow starts from zero, and the brake still remembers.
        kept = settings_store.get(settings_store.BOT_HEALTH)
        self.assertEqual(len(kept['recent']), 2)
        settings_store.put(settings_store.BOT_HEALTH, {**kept, 'day': '2026-01-01'})
        self.assertEqual(self.bot()['today']['sent'], 0)
        bot_mode.record(bot_mode.KIND_SENT)
        kept = settings_store.get(settings_store.BOT_HEALTH)
        self.assertEqual((kept['counts']['sent'], len(kept['recent'])), (1, 3))

    def test_the_sentence_says_where_the_mode_stands(self):
        self.writes('היי', index=1)
        Contact.objects.filter(pk=self.customer().id).update(live_test=True)
        self.assertEqual(self.bot()['text'], 'בדיקה בלבד: הבוט החדש עונה רק למספרי הבדיקה (מספר בדיקה אחד)')
        self.assertEqual(self.bot()['test_contacts'], 1)
        self.go_all()
        self.assertTrue(self.bot()['text'].startswith('הבוט החדש עונה לכולם — להודעות שהגיעו מ-'))
        with override_settings(WAHUB_SENDING_ENABLED=False):
            state = self.bot()
            self.assertEqual((state['text'], state['warning']), ('נבחר "הבוט החדש עונה לכולם", אבל הוא עוד לא עונה לאף לקוח', bot_mode.ALL_BUT_NOT_SENDING))

    def test_a_fault_in_the_counting_never_reaches_the_answer(self):
        self.go_all()
        self.writes('יש חוג קפוארה?')
        with patch('apps.wahub.bot_mode.settings_store.change', side_effect=RuntimeError('database')), \
                patch(REQUEST, return_value=OK) as request, self.assertLogs('apps.wahub.bot_mode', level='ERROR'):
            response = self.reply(self.customer().id)
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(len(sends(request)), 1)
