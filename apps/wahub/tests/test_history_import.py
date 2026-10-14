"""
The conversation from before the switch, copied once from ManyChat (the owner, 14.10.2026: "תתחבר ותעלה את
השיחות האלה שיהיו פה במערכת, שנדע אחורה").

Only what Kogo did not keep itself, only before the switch, only for a contact Kogo has; brought twice, kept once.
Read in its place: on the screen, by the bot that answers now, by the summary. Nothing sent, nothing waits.
"""
from datetime import timedelta

from django.utils import timezone

from apps.wahub import analysis, history, lead_stage, settings_store, shadow
from apps.wahub.models import Contact, HistoryLine, Message
from apps.wahub.tests.base import WahubTestCase, make_user, client_for
from apps.core.models import UserProfile

SUBSCRIBER = '555000111'


class HistoryTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        for cache in (lead_stage._seen, lead_stage._switch_seen):
            cache.update(at=None, since=None)
        self.addCleanup(lambda: [cache.update(at=None, since=None) for cache in (lead_stage._seen, lead_stage._switch_seen)])
        self.now = timezone.now()
        self.switch = self.now - timedelta(hours=2)
        settings_store.put(settings_store.BOT_MODE, {'mode': 'all', 'since': self.switch.isoformat(), 'first_since': self.switch.isoformat()})
        # Three days ago the customer asked; Kogo kept the question (ManyChat's copy of every customer message).
        self.asked_at = self.now - timedelta(days=3)
        self.incoming('כמה עולה חוג קפוארה?')
        self.contact_row = self.contact()
        Message.objects.filter(contact=self.contact_row).update(sent_at=self.asked_at)
        Contact.objects.filter(pk=self.contact_row.pk).update(
            manychat_subscriber_id=SUBSCRIBER, last_inbound_at=self.asked_at, last_message_at=self.asked_at,
            first_inbound_at=self.asked_at, needs_analysis=False,
        )

    def at(self, moment):
        return moment.isoformat()

    def conversation(self, *lines, subscriber=SUBSCRIBER):
        return {'subscriber_id': subscriber, 'messages': list(lines)}

    def manychat_view(self):
        """The same three days as ManyChat's inbox shows them: the question, the old bot's answer, the office's line."""
        return self.conversation(
            {'id': 'm1', 'at': self.at(self.asked_at + timedelta(seconds=20)), 'from': 'customer', 'text': 'כמה  עולה חוג קפוארה?'},
            {'id': 'm2', 'at': self.at(self.asked_at + timedelta(minutes=1)), 'from': 'bot', 'text': 'החוג עולה 230 ₪ לחודש'},
            {'id': 'm3', 'at': self.at(self.asked_at + timedelta(hours=1)), 'from': 'office', 'text': 'נשמח לראות אתכם בשיעור ניסיון'},
            # After the switch: the new bot's own, already here.
            {'id': 'm4', 'at': self.at(self.now - timedelta(minutes=30)), 'from': 'bot', 'text': 'היי'},
        )

    def send(self, *conversations):
        return self.post('history/import/', {'conversations': list(conversations)})


class ImportTests(HistoryTestCase):
    def test_only_what_kogo_did_not_keep_before_the_switch_is_kept_once(self):
        response = self.send(self.manychat_view())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['lines'], 2)
        self.assertEqual(response.data['already_kept'], 1)
        self.assertEqual(response.data['after_switch'], 1)
        self.assertEqual(
            list(HistoryLine.objects.filter(contact=self.contact_row).values_list('sender', 'text')),
            [('bot', 'החוג עולה 230 ₪ לחודש'), ('office', 'נשמח לראות אתכם בשיעור ניסיון')],
        )
        # Brought again: nothing new.
        again = self.send(self.manychat_view())
        self.assertEqual(again.data['lines'], 0)
        self.assertEqual(HistoryLine.objects.filter(contact=self.contact_row).count(), 2)

    def test_two_equal_messages_stay_two(self):
        self.send(self.conversation(
            {'id': 'a', 'at': self.at(self.asked_at + timedelta(seconds=5)), 'from': 'customer', 'text': 'כמה עולה חוג קפוארה?'},
            {'id': 'b', 'at': self.at(self.asked_at + timedelta(seconds=40)), 'from': 'customer', 'text': 'כמה עולה חוג קפוארה?'},
        ))
        # Kogo kept one of them; the other is the copy's.
        self.assertEqual(HistoryLine.objects.filter(contact=self.contact_row).count(), 1)

    def test_a_contact_kogo_does_not_have_and_a_bad_line_are_counted_and_left(self):
        response = self.send(
            self.conversation({'id': 'x', 'at': self.at(self.asked_at), 'from': 'bot', 'text': 'שלום'}, subscriber='999'),
            self.conversation(
                {'id': 'y', 'at': 'not a time', 'from': 'bot', 'text': 'שלום'},
                {'id': 'z', 'at': self.at(self.asked_at), 'from': 'stranger', 'text': 'שלום'},
                {'id': 'w', 'at': self.at(self.asked_at), 'from': 'bot', 'text': '   '},
            ),
        )
        self.assertEqual((response.data['unknown'], response.data['bad'], response.data['lines']), (1, 3, 0))
        self.assertFalse(Contact.objects.filter(manychat_subscriber_id='999').exists())

    def test_nothing_waits_nothing_is_unread_nothing_is_sent_and_the_lead_is_read_again(self):
        before = Contact.objects.get(pk=self.contact_row.pk)
        self.send(self.manychat_view())
        after = Contact.objects.get(pk=self.contact_row.pk)
        self.assertEqual((after.waiting_since, after.unread_count, after.last_message_at), (before.waiting_since, before.unread_count, before.last_message_at))
        self.assertEqual(Message.objects.filter(contact=after).count(), 1)
        self.assertIsNotNone(after.history_copied_at)
        self.assertTrue(after.needs_analysis)
        self.network.assert_not_called()

    def test_an_old_conversation_is_copied_and_not_read_again_as_a_lead(self):
        long_ago = self.now - timedelta(days=200)
        Contact.objects.filter(pk=self.contact_row.pk).update(last_inbound_at=long_ago)
        self.send(self.conversation({'id': 'o', 'at': self.at(long_ago), 'from': 'bot', 'text': 'שלום'}))
        self.assertFalse(Contact.objects.get(pk=self.contact_row.pk).needs_analysis)

    def test_before_any_switch_there_is_nothing_to_copy(self):
        settings_store.put(settings_store.BOT_MODE, {'mode': 'test'})
        response = self.send(self.manychat_view())
        self.assertEqual((response.status_code, response.data['code']), (400, 'no_switch'))

    def test_managers_only_and_small_batches(self):
        worker = client_for(make_user('worker@wahub.test', role=UserProfile.ROLE_WORKER))
        self.assertEqual(worker.post('/api/v1/wahub/history/import/', {'conversations': [self.manychat_view()]}, format='json').status_code, 403)
        too_many = [self.manychat_view()] * (history.MAX_CONVERSATIONS + 1)
        self.assertEqual(self.send(*too_many).data['code'], 'too_many')
        self.assertEqual(self.send().data['code'], 'empty')

    def test_times_as_manychat_pages_carry_them(self):
        seconds = int((self.asked_at + timedelta(minutes=2)).timestamp())
        self.send(self.conversation(
            {'id': 's', 'at': seconds, 'from': 'bot', 'text': 'בשניות'},
            {'id': 'ms', 'at': seconds * 1000 + 1000, 'from': 'bot', 'text': 'במילישניות'},
        ))
        moments = list(HistoryLine.objects.filter(contact=self.contact_row).values_list('sent_at', flat=True))
        self.assertEqual([int(moment.timestamp()) for moment in moments], [seconds, seconds + 1])


class StillToCopyTests(HistoryTestCase):
    def test_every_real_conversation_from_before_the_switch_and_not_copied_yet_latest_first(self):
        newer = self.make_contact(phone='972505550201', manychat_subscriber_id='777', first_inbound_at=self.now - timedelta(days=1), last_inbound_at=self.now - timedelta(days=1))
        self.make_contact(phone='972505550202', manychat_subscriber_id='778', first_inbound_at=self.now - timedelta(minutes=5))
        self.make_contact(phone='972505550203', manychat_subscriber_id='779', first_inbound_at=self.asked_at, is_demo=True)
        self.make_contact(phone='972505550204', manychat_subscriber_id='', first_inbound_at=self.asked_at)
        self.assertEqual(self.get('history/import/').data['subscribers'], [newer.manychat_subscriber_id, SUBSCRIBER])
        self.send(self.manychat_view())
        self.assertEqual(self.get('history/import/').data['subscribers'], ['777'])
        self.assertEqual(self.get('history/import/', again='1').data['subscribers'], ['777', SUBSCRIBER])


class ReadInPlaceTests(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.send(self.manychat_view())
        # After the switch the customer wrote again, and Kogo has it.
        self.incoming('ומה עם שיעור ניסיון?')
        self.contact_row = Contact.objects.get(pk=self.contact_row.pk)

    def test_the_screen_gets_the_copied_lines_with_the_conversation(self):
        response = self.get(f'contacts/{self.contact_row.id}/')
        self.assertEqual([line['sender'] for line in response.data['history_lines']], ['bot', 'office'])
        self.assertTrue(response.data['history']['copied_at'])

    def test_a_conversation_never_copied_asks_for_nothing(self):
        other = self.make_contact(phone='972505550199')
        with self.assertNumQueries(0):
            self.assertEqual(history.lines_for(other), [])

    def test_the_bot_reads_the_old_bots_answer_in_its_place(self):
        rows = shadow._conversation(self.contact_row)
        self.assertEqual([row['who'] for row in rows], ['customer', 'bot', 'office', 'customer'])
        self.assertEqual(rows[1]['text'], 'החוג עולה 230 ₪ לחודש')
        self.assertIsNone(rows[1]['id'])
        self.assertFalse(rows[1]['new_bot'])

    def test_an_earlier_message_answered_again_sees_nothing_after_it(self):
        first = Message.objects.filter(contact=self.contact_row).order_by('id').first()
        rows = shadow._conversation(self.contact_row, before_id=first.id)
        self.assertEqual([row['who'] for row in rows], ['customer'])

    def test_the_summary_reads_them_too(self):
        lines = analysis.conversation_lines(self.contact_row)
        self.assertEqual([line.who for line in lines], ['customer', 'bot', 'office', 'customer'])

    def test_a_long_conversation_keeps_the_last_lines(self):
        start = self.now - timedelta(minutes=50)
        for index in range(shadow.MAX_CONTEXT_MESSAGES):
            self.incoming(f'הודעה {index}')
        Message.objects.filter(contact=self.contact_row, text__startswith='הודעה').update(sent_at=start)
        rows = shadow._conversation(self.contact_row)
        self.assertEqual(len(rows), shadow.MAX_CONTEXT_MESSAGES)
        # The copied lines are older than every row kept: they are not squeezed in at the head.
        self.assertTrue(all(row['id'] is not None for row in rows))


class InterleaveTests(WahubTestCase):
    def test_each_copied_line_goes_just_before_the_first_later_row(self):
        own = [{'n': 'a', 't': 1}, {'n': 'b', 't': 5}, {'n': 'c', 't': None}, {'n': 'd', 't': 9}]
        copied = [{'n': 'x', 't': 0}, {'n': 'y', 't': 6}, {'n': 'z', 't': 20}]
        merged = history.interleave(own, copied, lambda row: row['t'])
        self.assertEqual([row['n'] for row in merged], ['x', 'a', 'b', 'c', 'y', 'd', 'z'])
