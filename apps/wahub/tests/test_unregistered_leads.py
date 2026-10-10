"""
"שאלו ולא נרשמו" (docs/WAHUB-CONTRACT-STAGE3.md, א): who is on the list, who is
hot, the order, the counts, and the same numbers on the "today" tab.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import Contact, Message
from apps.wahub.tests.base import WahubTestCase


class UnregisteredLeadsTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.today = state.now_israel_date()
        self.made = 0

    def noon(self, days_ago: int):
        """Noon, Israel time, that many calendar days back — so "days since" never depends on the hour the test runs."""
        return timezone.make_aware(datetime.combine(self.today - timedelta(days=days_ago), time(12)))

    def lead(self, first_days_ago=5, last_days_ago=None, outcome='not_found', text='אשמח לפרטים על קפוארה', **fields):
        self.made += 1
        first = self.noon(first_days_ago)
        last = self.noon(first_days_ago if last_days_ago is None else last_days_ago)
        defaults = dict(
            name=f'ליד {self.made}', kogo_outcome=outcome,
            first_inbound_at=first, last_inbound_at=last, last_message_at=last,
            last_message_direction='in', last_message_sender='customer', last_message_text=text,
        )
        defaults.update(fields)
        contact = Contact.objects.create(phone=f'9725055502{self.made:02d}', **defaults)
        Message.objects.create(contact=contact, direction='in', sender='customer', text=text, sent_at=last)
        return contact

    def listed(self, **params):
        response = self.get('leads/unregistered/', **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def ids(self, **params):
        return [row['id'] for row in self.listed(**params)['leads']]


class WhoIsOnTheListTests(UnregisteredLeadsTestCase):
    def test_every_outcome_that_is_not_a_paying_child_is_in(self):
        wanted = [
            self.lead(outcome=outcome).id
            for outcome in ('not_found', 'pending', 'signup_declined', 'trial_only', 'trial_upcoming', 'in_system', '')
        ]
        self.assertEqual(set(self.ids()), set(wanted))

    def test_only_a_paying_child_before_or_after_takes_a_person_off(self):
        for outcome in ('registered_after', 'customer_before'):
            with self.subTest(outcome=outcome):
                Contact.objects.all().delete()
                self.lead(outcome=outcome)
                self.assertEqual(self.ids(), [])

    def test_a_mark_that_closes_the_lead_takes_it_off_and_the_others_do_not(self):
        for status in ('registered', 'not_relevant'):
            with self.subTest(status=status):
                Contact.objects.all().delete()
                self.lead(followup_status=status)
                self.assertEqual(self.ids(), [])
        for status in ('', 'waiting_us', 'no_answer', 'answered', 'later'):
            with self.subTest(status=status):
                Contact.objects.all().delete()
                lead = self.lead(followup_status=status)
                self.assertEqual(self.ids(), [lead.id])

    def test_the_window_is_thirty_days_unless_asked_otherwise(self):
        recent = self.lead(first_days_ago=3)
        older = self.lead(first_days_ago=20)
        old = self.lead(first_days_ago=45)
        data = self.listed()
        self.assertEqual(data['days'], 30)
        self.assertEqual({row['id'] for row in data['leads']}, {recent.id, older.id})
        self.assertEqual(self.ids(days=7), [recent.id])
        self.assertEqual(set(self.ids(days=90)), {recent.id, older.id, old.id})

    def test_days_is_clamped_to_a_year_and_a_bad_value_means_thirty(self):
        self.assertEqual(self.listed(days=400)['days'], 365)
        self.assertEqual(self.listed(days=0)['days'], 1)
        self.assertEqual(self.listed(days='abc')['days'], 30)
        self.assertEqual(self.listed(days='-5')['days'], 30)

    def test_the_window_counts_from_the_first_message_not_the_last(self):
        self.lead(first_days_ago=45, last_days_ago=2)      # wrote again yesterday, but first asked long ago
        self.assertEqual(self.ids(), [])

    def test_a_demo_contact_is_listed_and_marked(self):
        demo = self.lead(is_demo=True)
        real = self.lead()
        rows = {row['id']: row['is_demo'] for row in self.listed()['leads']}
        self.assertEqual(rows, {demo.id: True, real.id: False})


class HotAndOrderTests(UnregisteredLeadsTestCase):
    def test_hot_is_wants_to_register_or_a_step_the_registrations_show(self):
        hot = {
            self.lead(known_interest='hot').id,
            self.lead(outcome='signup_declined').id,
            self.lead(outcome='trial_upcoming').id,
            self.lead(outcome='trial_only').id,
        }
        cold = {
            self.lead(known_interest='warm').id,
            self.lead(known_interest='cold').id,
            self.lead(known_interest='none').id,
            self.lead().id,
            self.lead(outcome='pending').id,
        }
        rows = {row['id']: row['hot'] for row in self.listed()['leads']}
        self.assertEqual({lead for lead, is_hot in rows.items() if is_hot}, hot)
        self.assertEqual({lead for lead, is_hot in rows.items() if not is_hot}, cold)

    def test_hot_first_then_whoever_has_waited_longest(self):
        waited_3 = self.lead(first_days_ago=3)
        waited_10 = self.lead(first_days_ago=12, last_days_ago=10)
        hot_waited_1 = self.lead(first_days_ago=1, known_interest='hot')
        hot_waited_6 = self.lead(first_days_ago=6, outcome='trial_only')
        waited_0 = self.lead(first_days_ago=20, last_days_ago=0)
        self.assertEqual(
            self.ids(), [hot_waited_6.id, hot_waited_1.id, waited_10.id, waited_3.id, waited_0.id],
        )
        waits = [row['days_since_last'] for row in self.listed()['leads']]
        self.assertEqual(waits, [6, 1, 10, 3, 0])

    def test_only_the_hot_ones_when_asked_and_the_counts_do_not_move(self):
        self.lead(first_days_ago=3)
        hot = self.lead(first_days_ago=6, known_interest='hot')
        for value in ('1', 'true', 'yes'):
            with self.subTest(hot=value):
                data = self.listed(hot=value)
                self.assertEqual([row['id'] for row in data['leads']], [hot.id])
                self.assertEqual(data['counts'], {'total': 2, 'hot': 1, 'oldest_days': 6})
        self.assertEqual(len(self.listed(hot='0')['leads']), 2)


class CountsAndRowsTests(UnregisteredLeadsTestCase):
    def test_counts(self):
        self.lead(first_days_ago=3)
        self.lead(first_days_ago=25, last_days_ago=14)
        self.lead(first_days_ago=8, outcome='signup_declined')
        self.lead(first_days_ago=40)                      # outside the window
        self.lead(first_days_ago=2, followup_status='registered')
        self.assertEqual(self.listed()['counts'], {'total': 3, 'hot': 1, 'oldest_days': 14})

    def test_an_empty_list_has_no_oldest(self):
        data = self.listed()
        self.assertEqual(data, {'days': 30, 'counts': {'total': 0, 'hot': 0, 'oldest_days': None}, 'leads': []})

    def test_a_row(self):
        lead = self.lead(
            first_days_ago=9, last_days_ago=4, outcome='pending', kogo_detail='התחיל רישום ב-1.10 · נועה לוי',
            known_interest='warm', known_city='ראש העין', known_branch_name='ראש העין', known_course_type='קפוארה',
            followup_status='later', followup_due=self.today + timedelta(days=2), handled_by='human', needs_human=True,
        )
        lead.refresh_from_db()   # the database hands times back in UTC; so does the API
        row = self.listed()['leads'][0]
        self.assertEqual(set(row), {
            'id', 'name', 'phone', 'phone_display', 'is_demo', 'first_inbound_at', 'last_inbound_at', 'days_since_first',
            'days_since_last', 'asked', 'known_interest', 'known_interest_label', 'known_city', 'known_branch_name',
            'known_course_type', 'kogo_outcome', 'kogo_outcome_label', 'kogo_detail', 'followup_status',
            'followup_status_label', 'followup_due', 'hot', 'handled_by', 'needs_human',
        })
        self.assertEqual(row['id'], lead.id)
        self.assertEqual((row['name'], row['phone'], row['phone_display']), ('ליד 1', '972505550201', '050-5550201'))
        self.assertEqual((row['days_since_first'], row['days_since_last']), (9, 4))
        self.assertEqual(row['first_inbound_at'], lead.first_inbound_at.isoformat())
        self.assertEqual(row['last_inbound_at'], lead.last_inbound_at.isoformat())
        self.assertEqual(row['asked'], 'אשמח לפרטים על קפוארה')
        self.assertEqual((row['known_interest'], row['known_interest_label']), ('warm', 'מתעניין'))
        self.assertEqual((row['known_city'], row['known_branch_name'], row['known_course_type']), ('ראש העין', 'ראש העין', 'קפוארה'))
        self.assertEqual((row['kogo_outcome'], row['kogo_outcome_label']), ('pending', 'התחיל רישום ולא סיים'))
        self.assertEqual(row['kogo_detail'], 'התחיל רישום ב-1.10 · נועה לוי')
        self.assertEqual((row['followup_status'], row['followup_status_label']), ('later', 'בזמן אחר'))
        self.assertEqual(row['followup_due'], (self.today + timedelta(days=2)).isoformat())
        self.assertEqual((row['hot'], row['handled_by'], row['needs_human']), (False, 'human', True))

    def test_asked_is_the_summary_or_else_the_last_customer_message_not_the_bots(self):
        summarised = self.lead(known_summary='שאלה על קפוארה לילד בן 7 בראש העין')
        plain = self.lead(text='מה המחיר?')
        # The bot answered last: the list still quotes the customer.
        Message.objects.create(contact=plain, direction='out', sender='bot', text='המחיר הוא 260 ₪', status='sent')
        Contact.objects.filter(pk=plain.pk).update(
            last_message_text='המחיר הוא 260 ₪', last_message_direction='out', last_message_sender='bot',
        )
        long = self.lead(text='א' * 50 + '  \n ' + 'ב' * 200)
        asked = {row['id']: row['asked'] for row in self.listed()['leads']}
        self.assertEqual(asked[summarised.id], 'שאלה על קפוארה לילד בן 7 בראש העין')
        self.assertEqual(asked[plain.id], 'מה המחיר?')
        self.assertEqual(asked[long.id], 'א' * 50 + ' ' + 'ב' * 109)
        self.assertEqual(len(asked[long.id]), 160)

    def test_the_list_is_one_query_whatever_its_length(self):
        for _ in range(6):
            self.lead()
        with self.assertNumQueries(3):   # the user, the profile, the list
            self.assertEqual(self.listed()['counts']['total'], 6)


class SummaryTests(UnregisteredLeadsTestCase):
    def test_the_today_tab_carries_the_same_three_numbers(self):
        self.lead(first_days_ago=3)
        self.lead(first_days_ago=25, last_days_ago=14)
        self.lead(first_days_ago=8, outcome='trial_upcoming')
        self.lead(first_days_ago=40)
        self.lead(first_days_ago=2, followup_status='not_relevant')
        self.lead(first_days_ago=2, outcome='customer_before')
        with self.assertNumQueries(5):   # the user, the profile, and still three for the tab
            data = self.get('summary/').data
        self.assertEqual(data['unregistered_leads'], {'total': 3, 'hot': 1, 'oldest_days': 14})
        self.assertEqual(data['unregistered_leads'], self.listed()['counts'])

    def test_with_nobody_the_tab_says_so(self):
        self.assertEqual(self.get('summary/').data['unregistered_leads'], {'total': 0, 'hot': 0, 'oldest_days': None})
