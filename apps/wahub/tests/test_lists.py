"""The two lists, their boxes and queues, the counts, the search and the "today" tab."""
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import Contact, Message, Tag
from apps.wahub.tests.base import WahubTestCase


def phones(response):
    return [row['phone'] for row in response.data['results']]


class ListTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.today = state.now_israel_date()

        def contact(suffix, minutes_ago, **fields):
            moment = now - timedelta(minutes=minutes_ago)
            defaults = dict(
                last_message_at=moment, last_inbound_at=moment, first_inbound_at=moment,
                last_message_direction='in', last_message_sender='customer', last_message_text='שלום',
            )
            defaults.update(fields)
            return Contact.objects.create(phone=f'9725055501{suffix}', **defaults)

        self.waiting = contact('01', 5, name='ממתינה', waiting_since=now - timedelta(minutes=5), unread_count=2)
        self.human = contact('02', 10, name='נציג עונה', handled_by='human', needs_human=True, needs_human_reason='ביקש נציג')
        self.answered = contact(
            '03', 20, name='ענו לו', followup_status='answered',
            # The bot wrote last: newer in the chats list, older in the leads list.
            last_message_at=now - timedelta(minutes=1), last_message_direction='out', last_message_sender='bot',
        )
        self.later_due = contact('04', 30, followup_status='later', followup_due=self.today)
        self.later_future = contact('05', 40, followup_status='later', followup_due=self.today + timedelta(days=3))
        self.customer = contact('06', 50, kogo_outcome='customer_before', followup_status='waiting_us')
        self.trial_ahead = contact('07', 60, kogo_outcome='trial_upcoming')
        self.registered = contact('08', 70, followup_status='registered', kogo_outcome='registered_after')
        self.never_wrote = Contact.objects.create(phone='972505550109', name='ידני', source='manual')

    # --- chats ---

    def test_chats_is_the_default_shows_everyone_newest_message_first(self):
        response = self.get('contacts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 9)
        self.assertEqual(phones(response)[:3], ['972505550103', '972505550101', '972505550102'])
        self.assertEqual(phones(response)[-1], '972505550109')   # no message at all: last

    def test_boxes(self):
        expected = {
            'all': 9, 'waiting': 1, 'needs_human': 1, 'unread': 1, 'human': 1, 'bot': 8,
        }
        for box, count in expected.items():
            with self.subTest(box=box):
                self.assertEqual(self.get('contacts/', box=box).data['count'], count)
        self.assertEqual(phones(self.get('contacts/', box='waiting')), ['972505550101'])
        self.assertEqual(phones(self.get('contacts/', box='human')), ['972505550102'])

    # --- leads ---

    def test_leads_hide_customers_and_trials_ahead_and_keep_a_fixed_order(self):
        response = self.get('contacts/', view='leads')
        self.assertEqual(
            phones(response),
            ['972505550101', '972505550102', '972505550103', '972505550104', '972505550105', '972505550109'],
        )
        self.assertTrue(all(not row['kogo']['hidden_by_default'] for row in response.data['results']))

    def test_leads_order_does_not_move_when_a_mark_changes(self):
        before = phones(self.get('contacts/', view='leads'))
        self.client.patch(
            f'/api/v1/wahub/contacts/{self.later_future.id}/followup/', {'status': 'waiting_us'}, format='json',
        )
        self.assertEqual(phones(self.get('contacts/', view='leads')), before)

    def test_show_hidden_brings_them_back(self):
        response = self.get('contacts/', view='leads', show_hidden=1)
        self.assertEqual(response.data['count'], 9)
        hidden = {row['phone'] for row in response.data['results'] if row['kogo']['hidden_by_default']}
        self.assertEqual(hidden, {'972505550106', '972505550107', '972505550108'})
        customers = {row['phone'] for row in response.data['results'] if row['kogo']['is_customer']}
        self.assertEqual(customers, {'972505550106', '972505550108'})

    def test_queues(self):
        expected = {
            'all': 6, 'due': 1, 'none': 3, 'no_answer': 0, 'answered': 1, 'later': 2, 'registered': 0, 'not_relevant': 0,
        }
        for queue, count in expected.items():
            with self.subTest(queue=queue):
                self.assertEqual(self.get('contacts/', view='leads', queue=queue).data['count'], count)
        self.assertEqual(phones(self.get('contacts/', view='leads', queue='due')), ['972505550104'])
        # With the hidden ones: the customer marked "waiting for us" is due too.
        self.assertEqual(
            phones(self.get('contacts/', view='leads', queue='due', show_hidden=1)), ['972505550104', '972505550106'],
        )

    def test_counts_match_the_lists(self):
        data = self.get('contacts/counts/').data
        self.assertEqual(data['boxes'], {'all': 9, 'waiting': 1, 'needs_human': 1, 'unread': 1, 'human': 1, 'bot': 8})
        self.assertEqual(data['queues'], {
            'all': 6, 'due': 1, 'none': 3, 'no_answer': 0, 'answered': 1, 'later': 2, 'registered': 0,
            'not_relevant': 0, 'hidden': 3,
        })
        with_hidden = self.get('contacts/counts/', show_hidden=1).data['queues']
        self.assertEqual(with_hidden['all'], 9)
        self.assertEqual(with_hidden['due'], 2)
        self.assertEqual(with_hidden['registered'], 1)
        self.assertEqual(with_hidden['hidden'], 3)

    def test_counts_are_one_query_and_follow_the_filters(self):
        tag = Tag.objects.create(name='חם')
        self.waiting.tags.add(tag)
        self.customer.tags.add(tag)
        with self.assertNumQueries(3):   # the user, the profile, one aggregate
            data = self.get('contacts/counts/', tag=tag.id).data
        self.assertEqual(data['boxes']['all'], 2)
        self.assertEqual(data['queues']['all'], 1)
        self.assertEqual(data['queues']['hidden'], 1)

    # --- paging ---

    def test_paging(self):
        response = self.get('contacts/', page_size=4)
        self.assertEqual(len(response.data['results']), 4)
        self.assertEqual(response.data['count'], 9)
        self.assertIsNotNone(response.data['next'])
        self.assertIsNone(response.data['previous'])
        self.assertEqual(len(self.get('contacts/', page_size=4, page=3).data['results']), 1)
        self.assertEqual(len(self.get('contacts/', page_size=500).data['results']), 9)   # capped at 100

    def test_the_list_does_not_grow_queries_with_rows(self):
        with self.assertNumQueries(5):   # the user, the profile, the count, the rows, their tags
            self.get('contacts/')

    # --- filters ---

    def test_search_by_name_message_and_summary(self):
        Contact.objects.filter(pk=self.answered.pk).update(last_message_text='אשמח לפרטים על קפוארה')
        Contact.objects.filter(pk=self.later_due.pk).update(known_summary='שאל על קפוארה בראש העין')
        self.assertEqual(phones(self.get('contacts/', search='ממתינה')), ['972505550101'])
        self.assertEqual(set(phones(self.get('contacts/', search='קפוארה'))), {'972505550103', '972505550104'})
        self.assertEqual(phones(self.get('contacts/', search='אין כזה')), [])

    def test_search_by_phone_however_it_is_typed(self):
        for typed in ('0505550104', '050-5550104', '050 555 0104', '+972505550104', '972-50-5550104', '5550104'):
            with self.subTest(typed=typed):
                self.assertEqual(phones(self.get('contacts/', search=typed)), ['972505550104'])
        self.assertEqual(self.get('contacts/', search='050555').data['count'], 9)

    def test_filters_by_tag_outcome_topic_interest_flag_and_branch(self):
        import uuid

        tag = Tag.objects.create(name='חם')
        self.waiting.tags.add(tag)
        branch = uuid.uuid4()
        Contact.objects.filter(pk=self.human.pk).update(
            known_topic='trial', known_interest='hot', known_flags=['price', 'lives_far'], known_branch_id=branch,
        )
        self.assertEqual(phones(self.get('contacts/', tag=tag.id)), ['972505550101'])
        self.assertEqual(phones(self.get('contacts/', outcome='customer_before')), ['972505550106'])
        self.assertEqual(
            set(phones(self.get('contacts/', outcome='customer_before,trial_upcoming'))), {'972505550106', '972505550107'},
        )
        self.assertEqual(self.get('contacts/', outcome='unchecked').data['count'], 6)
        self.assertEqual(phones(self.get('contacts/', topic='trial')), ['972505550102'])
        self.assertEqual(phones(self.get('contacts/', interest='hot')), ['972505550102'])
        self.assertEqual(phones(self.get('contacts/', flag='price')), ['972505550102'])
        self.assertEqual(phones(self.get('contacts/', flag='class_full')), [])
        self.assertEqual(phones(self.get('contacts/', branch=str(branch))), ['972505550102'])
        self.assertEqual(phones(self.get('contacts/', branch='not-a-uuid')), [])
        self.assertEqual(phones(self.get('contacts/', tag='abc')), [])

    # --- the "today" tab ---

    def test_summary(self):
        now = timezone.now()
        # Noon of the day before, by the calendar: "24 hours ago" is the same
        # day on the night the clock goes back.
        yesterday = timezone.make_aware(datetime.combine(self.today - timedelta(days=1), time(12)))
        Message.objects.bulk_create([
            Message(contact=self.waiting, direction='in', sender='customer', text='א', sent_at=now),
            Message(contact=self.waiting, direction='in', sender='customer', text='ב', sent_at=now),
            Message(contact=self.waiting, direction='out', sender='bot', text='ג', status='sent', sent_at=now),
            Message(contact=self.waiting, direction='out', sender='office', text='ד', status='failed', sent_at=now),
            Message(contact=self.human, direction='in', sender='customer', text='ה', sent_at=yesterday),
            Message(contact=self.human, direction='in', sender='customer', text='ו', sent_at=now - timedelta(days=9)),
        ])
        Contact.objects.filter(pk=self.human.pk).update(created_at=yesterday)
        Contact.objects.filter(pk=self.registered.pk).update(created_at=now - timedelta(days=20))

        with self.assertNumQueries(5):   # the user, the profile, and three for the tab
            data = self.get('summary/').data
        self.assertEqual(data['waiting'], 1)
        self.assertEqual(data['needs_human'], 1)
        self.assertEqual(data['unread'], 1)
        self.assertEqual(data['due_followups'], 1)
        self.assertEqual(data['inbound_today'], 2)
        self.assertEqual(data['outbound_today'], 1)     # the failed send is not counted
        self.assertEqual(data['new_contacts_today'], 7)
        self.assertEqual(data['new_contacts_7d'], 8)
        self.assertEqual(data['oldest_waiting_since'], self.waiting.waiting_since)
        days = data['by_day']
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1]['date'], self.today.isoformat())
        self.assertEqual(days[0]['date'], (self.today - timedelta(days=6)).isoformat())
        self.assertEqual(days[-1], {'date': self.today.isoformat(), 'inbound': 2, 'outbound': 1, 'new_contacts': 7})
        self.assertEqual(days[-2]['inbound'], 1)
        self.assertEqual(days[-2]['new_contacts'], 1)

    def test_status(self):
        Message.objects.create(contact=self.waiting, direction='in', sender='customer', text='א')
        data = self.get('status/').data
        self.assertEqual(
            set(data),
            {'inbound_configured', 'inbound_key_set_at', 'bot_replies_seen', 'ai_configured', 'send_configured',
             'sending_enabled', 'simulate_send', 'last_inbound_at', 'contacts_total', 'messages_last_24h', 'inbound_url'},
        )
        self.assertEqual(data['contacts_total'], 9)
        self.assertEqual(data['messages_last_24h'], 1)
        self.assertFalse(data['bot_replies_seen'])
        self.assertFalse(data['simulate_send'])
        self.assertEqual(data['last_inbound_at'], self.waiting.last_inbound_at)

        self.bot_reply('תשובה', phone=self.waiting.phone)
        self.assertTrue(self.get('status/').data['bot_replies_seen'])


class InboundUrlTests(WahubTestCase):
    def test_on_a_developers_machine_it_is_the_address_the_request_came_to(self):
        from django.test import override_settings

        with override_settings(CRM_API_BASE_URL='http://127.0.0.1:8000/api/v1'):
            self.assertEqual(self.get('status/').data['inbound_url'], 'http://testserver/api/v1/wahub/inbound/manychat/')
        with override_settings(CRM_API_BASE_URL=''):
            self.assertEqual(self.get('status/').data['inbound_url'], 'http://testserver/api/v1/wahub/inbound/manychat/')

    def test_in_production_it_is_the_servers_public_address(self):
        from django.test import override_settings

        with override_settings(CRM_API_BASE_URL='https://api.example.test/api/v1'):
            self.assertEqual(
                self.get('status/').data['inbound_url'], 'https://api.example.test/api/v1/wahub/inbound/manychat/',
            )
