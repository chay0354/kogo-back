"""
The conversation from before the switch, copied once from ManyChat (the owner, 14.10.2026: "תתחבר ותעלה את
השיחות האלה שיהיו פה במערכת, שנדע אחורה").

Only what Kogo did not keep itself, only before the switch, only for a contact Kogo has; brought twice, kept once.
Read in its place: on the screen, by the bot that answers now, by the summary. Nothing sent, nothing waits.
"""
from datetime import timedelta

from django.utils import timezone

from apps.wahub import analysis, history, lead_stage, settings_store, shadow
from apps.wahub.models import Contact, ContactEvent, HistoryLine, Message
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

    def test_somebody_kogo_never_saw_becomes_a_quiet_contact_of_their_own(self):
        """The owner, 14.10.2026: "אני צריך את כל השיחות שהיו שיהיה לי אותם"."""
        first = self.asked_at - timedelta(days=20)
        response = self.send({
            'subscriber_id': '999', 'phone': '+972 50-555-0399', 'name': 'הורה  לדוגמה',
            'messages': [
                {'id': 'n1', 'at': self.at(first), 'from': 'customer', 'text': 'היי, יש חוג לגיל 4?'},
                {'id': 'n2', 'at': self.at(first + timedelta(minutes=1)), 'from': 'bot', 'text': 'כן! איפה אתם גרים?'},
                {'id': 'n3', 'at': self.at(first + timedelta(minutes=9)), 'from': 'customer', 'text': 'בכפר סבא'},
                {'id': 'n4', 'at': self.at(first + timedelta(minutes=10)), 'from': 'bot', 'text': 'מעולה, הנה הקישור'},
            ],
        })
        self.assertEqual((response.data['created'], response.data['lines']), (1, 4))
        made = Contact.objects.get(phone='972505550399')
        self.assertEqual((made.name, made.source, made.manychat_subscriber_id), ('הורה לדוגמה', 'import', '999'))
        self.assertEqual((made.first_inbound_at, made.last_inbound_at), (first, first + timedelta(minutes=9)))
        self.assertEqual((made.last_message_at, made.last_message_text, made.last_message_sender), (first + timedelta(minutes=10), 'מעולה, הנה הקישור', 'bot'))
        self.assertEqual((made.waiting_since, made.unread_count, made.needs_human), (None, 0, False))
        self.assertTrue(ContactEvent.objects.filter(contact=made, kind='created', text=history.CREATED_NOTE).exists())
        # Not counted as somebody new today.
        self.assertEqual(self.get('summary/').data['new_contacts_today'], 1)
        # Brought again: the same contact, nothing twice.
        again = self.send({'subscriber_id': '999', 'phone': '0505550399', 'messages': [
            {'id': 'n1', 'at': self.at(first), 'from': 'customer', 'text': 'היי, יש חוג לגיל 4?'},
        ]})
        self.assertEqual((again.data['created'], again.data['lines']), (0, 0))
        self.assertEqual(Contact.objects.filter(phone='972505550399').count(), 1)

    def test_a_contact_kogo_has_by_phone_only_gets_the_lines_and_manychats_number(self):
        other = self.make_contact(phone='972505550398', manychat_subscriber_id='')
        self.send({'subscriber_id': '4242', 'phone': '972505550398', 'messages': [
            {'id': 'p1', 'at': self.at(self.asked_at), 'from': 'office', 'text': 'היי מהמשרד'},
        ]})
        other.refresh_from_db()
        self.assertEqual(other.manychat_subscriber_id, '4242')
        self.assertEqual(HistoryLine.objects.filter(contact=other).count(), 1)

    def test_no_israeli_mobile_an_empty_conversation_and_a_bad_line_are_counted_and_left(self):
        response = self.send(
            self.conversation({'id': 'x', 'at': self.at(self.asked_at), 'from': 'bot', 'text': 'שלום'}, subscriber='998'),
            {'subscriber_id': '997', 'phone': '+1 555 0100', 'messages': [{'id': 'f', 'at': self.at(self.asked_at), 'from': 'customer', 'text': 'hi'}]},
            self.conversation(
                {'id': 'y', 'at': 'not a time', 'from': 'bot', 'text': 'שלום'},
                {'id': 'z', 'at': self.at(self.asked_at), 'from': 'stranger', 'text': 'שלום'},
                {'id': 'w', 'at': self.at(self.asked_at), 'from': 'bot', 'text': '   '},
            ),
        )
        self.assertEqual((response.data['no_phone'], response.data['empty'], response.data['bad'], response.data['lines']), (2, 1, 3, 0))
        self.assertFalse(Contact.objects.filter(manychat_subscriber_id__in=['997', '998']).exists())

    def test_a_demo_is_never_written_to(self):
        demo = self.make_contact(phone='972505550397', manychat_subscriber_id='4343', is_demo=True)
        self.send(self.conversation({'id': 'd', 'at': self.at(self.asked_at), 'from': 'bot', 'text': 'שלום'}, subscriber='4343'))
        self.assertFalse(HistoryLine.objects.filter(contact=demo).exists())

    def test_nothing_waits_nothing_is_unread_nothing_is_sent_and_nothing_jumps_the_queue(self):
        before = Contact.objects.get(pk=self.contact_row.pk)
        self.send(self.manychat_view())
        after = Contact.objects.get(pk=self.contact_row.pk)
        self.assertEqual((after.waiting_since, after.unread_count, after.last_message_at), (before.waiting_since, before.unread_count, before.last_message_at))
        self.assertEqual(Message.objects.filter(contact=after).count(), 1)
        self.assertIsNotNone(after.history_copied_at)
        # Read again later, a few a call, behind every new message — never ahead of them.
        self.assertFalse(after.needs_analysis)
        self.assertIsNone(after.history_read_at)
        self.network.assert_not_called()

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
        self.assertEqual(self.get('history/import/').data['copied'], 1)
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


class ReadAgainTests(HistoryTestCase):
    """The summary reads a conversation again with its past — a few a call, behind every new message (cron 1c)."""

    def setUp(self):
        super().setUp()
        self.send(self.manychat_view())
        # Matched with Kogo and read as a lead before the copy, as every contact Kogo had was.
        Contact.objects.filter(pk=self.contact_row.pk).update(
            kogo_checked_at=self.now, last_inbound_at=self.now - timedelta(days=3), lead_ai={'v': 1},
        )

    def tick(self):
        from unittest.mock import patch

        from apps.wahub import cron

        read = []
        with patch('apps.wahub.analysis.analyze_contact', side_effect=lambda contact, **kw: read.append(contact.pk)), \
                patch('apps.wahub.shadow.pending', return_value=Contact.objects.none()):
            counts = cron.tick(budget_seconds=60)
        return read, counts

    def test_read_once_after_the_copy_and_not_again(self):
        read, counts = self.tick()
        self.assertIn(self.contact_row.pk, read)
        self.assertEqual(counts['history_read'], 1)
        self.assertIsNotNone(Contact.objects.get(pk=self.contact_row.pk).history_read_at)
        read, counts = self.tick()
        self.assertNotIn(self.contact_row.pk, read)
        self.assertEqual(counts['history_read'], 0)

    def test_a_customer_one_not_matched_yet_and_an_old_one_wait_or_are_skipped(self):
        from apps.wahub.models import CUSTOMER_OUTCOMES

        Contact.objects.filter(pk=self.contact_row.pk).update(kogo_outcome=sorted(CUSTOMER_OUTCOMES)[0])
        self.assertNotIn(self.contact_row.pk, self.tick()[0])
        Contact.objects.filter(pk=self.contact_row.pk).update(kogo_outcome='', kogo_checked_at=None)
        from apps.wahub import cron

        self.assertFalse(cron._history_to_read(self.now).filter(pk=self.contact_row.pk).exists())
        # The owner, 15.10.2026: "30 יום" — a conversation quiet longer than that is not read again by Claude.
        Contact.objects.filter(pk=self.contact_row.pk).update(kogo_checked_at=self.now, last_inbound_at=self.now - timedelta(days=31))
        self.assertFalse(cron._history_to_read(self.now).filter(pk=self.contact_row.pk).exists())
        Contact.objects.filter(pk=self.contact_row.pk).update(last_inbound_at=self.now - timedelta(days=29))
        self.assertTrue(cron._history_to_read(self.now).filter(pk=self.contact_row.pk).exists())

    def test_a_new_message_waiting_for_the_summary_comes_first(self):
        from apps.wahub import cron

        source = open(cron.__file__).read()
        self.assertLess(source.index('for contact in _to_analyze(now)'), source.index('for contact in _history_to_read(now)'))


class NoSwitchTests(WahubTestCase):
    def test_before_any_switch_the_count_is_read_and_is_empty(self):
        settings_store.put(settings_store.BOT_MODE, {'mode': 'test'})
        response = self.get('history/import/')
        self.assertEqual((response.status_code, response.data['subscribers'], response.data['copied']), (200, [], 0))
