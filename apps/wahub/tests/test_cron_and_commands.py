"""The five-minute slice, and the two local-only commands."""
import random
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import cron, demo
from apps.wahub.models import HIDDEN_OUTCOMES, OUTCOME_CHOICES, Contact, Message, QuickReply, Tag
from apps.wahub.tests.base import BASE, WahubTestCase

URL = f'{BASE}/cron/tick/'


@override_settings(CRON_TOKEN='cron-secret', ANTHROPIC_API_KEY='')
class CronTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.machine = APIClient()

    def tick(self, **headers):
        return self.machine.get(URL, **headers)

    def test_without_the_token_it_is_401_and_nothing_runs(self):
        self.incoming('אפשר שיעור ניסיון בקפוארה?')
        self.age(self.contact(), last_message_at=10)
        for headers in ({}, {'HTTP_X_CRON_TOKEN': 'wrong'}, {'HTTP_AUTHORIZATION': 'Bearer wrong'}):
            self.assertEqual(self.tick(**headers).status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 401)      # a manager's session is not the token
        self.assertTrue(self.contact().needs_analysis)

    @override_settings(CRON_TOKEN='')
    def test_with_no_token_configured_nobody_gets_in(self):
        self.assertEqual(self.tick(HTTP_X_CRON_TOKEN='').status_code, 401)

    def test_the_token_is_accepted_the_ways_the_other_crons_accept_it(self):
        self.assertEqual(self.tick(HTTP_X_CRON_TOKEN='cron-secret').status_code, 200)
        self.assertEqual(self.tick(HTTP_AUTHORIZATION='Bearer cron-secret').status_code, 200)
        self.assertEqual(self.machine.post(f'{URL}?token=cron-secret').status_code, 200)

    def test_a_quiet_conversation_is_summarised_and_matched(self):
        family = TestDataFactory.create_family(phone='050-5550101')
        TestDataFactory.create_child(family=family, status='pending')
        self.incoming('אפשר שיעור ניסיון בקפוארה? הוא בן 5')
        self.age(self.contact(), last_message_at=6)

        response = self.tick(HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertTrue(data['ok'])
        self.assertEqual((data['analyzed'], data['analyzed_rules'], data['analyzed_ai']), (1, 1, 0))
        self.assertEqual((data['matched'], data['outcomes_changed']), (1, 1))
        self.assertEqual((data['pending_analysis'], data['pending_matching']), (0, 0))
        self.assertIn('seconds', data)

        contact = self.contact()
        self.assertFalse(contact.needs_analysis)
        self.assertEqual((contact.known_topic, contact.known_child_age, contact.analysis_source), ('trial', '5', 'rules'))
        self.assertEqual(contact.kogo_outcome, 'pending')
        self.assertEqual(contact.kogo_family_id, family.id)

    def test_a_conversation_still_going_waits_for_five_quiet_minutes(self):
        self.incoming('אפשר שיעור ניסיון?')
        self.age(self.contact(), last_message_at=4)
        data = self.tick(HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertEqual(data['analyzed'], 0)
        self.assertTrue(self.contact().needs_analysis)
        self.assertEqual(data['matched'], 1)     # the matching does not wait for that

    def test_matching_comes_back_after_twelve_hours_and_not_before(self):
        self.incoming('שלום')
        self.tick(HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(self.tick(HTTP_X_CRON_TOKEN='cron-secret').data['matched'], 0)

        Contact.objects.update(kogo_checked_at=timezone.now() - timedelta(hours=11, minutes=50))
        self.assertEqual(self.tick(HTTP_X_CRON_TOKEN='cron-secret').data['matched'], 0)
        Contact.objects.update(kogo_checked_at=timezone.now() - timedelta(hours=12, minutes=1))
        data = self.tick(HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertEqual((data['matched'], data['outcomes_changed']), (1, 0))

    def test_a_call_out_of_time_stops_and_the_next_one_carries_on(self):
        for index in range(6):
            self.incoming('אפשר פרטים על חוג קפוארה?', phone=f'9725055501{index:02d}')
        Contact.objects.update(last_message_at=timezone.now() - timedelta(minutes=10))

        spent = cron.tick(budget_seconds=0)
        self.assertEqual((spent['analyzed'], spent['matched']), (0, 0))
        self.assertEqual((spent['pending_analysis'], spent['pending_matching']), (6, 6))

        done = cron.tick()
        self.assertEqual((done['analyzed'], done['matched']), (6, 6))
        self.assertEqual((done['pending_analysis'], done['pending_matching']), (0, 0))

    def test_one_contact_that_fails_does_not_stop_the_rest(self):
        for index in range(3):
            self.incoming('אפשר פרטים על חוג קפוארה?', phone=f'9725055501{index:02d}')
        Contact.objects.update(last_message_at=timezone.now() - timedelta(minutes=10))
        from apps.wahub import analysis, matching

        real_analyze, real_match = analysis.analyze_contact, matching.recheck_contact
        broken = Contact.objects.order_by('id').first().pk

        def analyze(contact, **kwargs):
            if contact.pk == broken:
                raise RuntimeError('boom')
            return real_analyze(contact, **kwargs)

        def match(contact):
            if contact.pk == broken:
                raise RuntimeError('boom')
            return real_match(contact)

        with patch('apps.wahub.cron.analysis.analyze_contact', side_effect=analyze), \
                patch('apps.wahub.cron.matching.recheck_contact', side_effect=match), \
                self.assertLogs('apps.wahub.cron', level='ERROR'):
            counts = cron.tick()
        self.assertEqual((counts['analyzed'], counts['matched']), (2, 2))
        # The one that failed is not first in line again five minutes later.
        self.assertEqual((counts['pending_analysis'], counts['pending_matching']), (0, 0))

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_claude_is_never_given_longer_than_the_call_has_left(self):
        self.incoming('אפשר שיעור ניסיון בקפוארה?')
        self.age(self.contact(), last_message_at=10)
        import requests

        with patch('apps.wahub.analysis.requests.post', side_effect=requests.Timeout('slow')) as post:
            data = self.tick(HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertLessEqual(max(post.call_args.kwargs['timeout']), 12)
        self.assertEqual((data['analyzed'], data['analyzed_rules']), (1, 1))

    def test_the_schedule_is_in_vercel_json_and_the_others_were_left_alone(self):
        import json
        from pathlib import Path

        from django.conf import settings

        crons = json.loads((Path(settings.BASE_DIR) / 'vercel.json').read_text())['crons']
        self.assertIn({'path': '/api/v1/wahub/cron/tick/', 'schedule': '*/5 4-20 * * *'}, crons)
        self.assertIn({'path': '/api/v1/customers/cron/recurring-billing/', 'schedule': '*/5 5-17 * * *'}, crons)
        self.assertEqual(len(crons), 10)


class LocalGuardTests(WahubTestCase):
    def test_the_commands_refuse_a_database_that_is_not_local(self):
        """The test database itself (test_…) is not a kogo_local one — nor is production's."""
        for command in ('wahub_seed_demo', 'wahub_simulate_inbound'):
            with self.subTest(command=command), self.assertRaises(CommandError) as raised:
                call_command(command, stdout=StringIO())
            self.assertIn('kogo_local', str(raised.exception))
        self.assertFalse(Contact.objects.exists())

    def test_the_guard_reads_the_database_name(self):
        for name, allowed in (('kogo_local', True), ('kogo_local_wahub', True), ('postgres', False),
                              ('test_kogo_local_wahub', False), ('', False)):
            fake = SimpleNamespace(DATABASES={'default': {'NAME': name}})
            with self.subTest(name=name), patch.object(demo, 'settings', fake):
                if allowed:
                    self.assertEqual(demo.ensure_local_database(), name)
                else:
                    with self.assertRaises(CommandError):
                        demo.ensure_local_database()


class DemoDataTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.made = demo.seed_demo(self.manager)

    def test_about_forty_five_invented_contacts_with_invented_phones(self):
        self.assertEqual(self.made['contacts'], len(demo.SCENARIOS))
        self.assertTrue(40 <= Contact.objects.count() <= 50)
        self.assertFalse(Contact.objects.exclude(phone__startswith='97250555').exists())
        self.assertEqual(len({scenario['phone'] for scenario in demo.SCENARIOS}), len(demo.SCENARIOS))

    def test_conversations_of_two_to_twelve_messages_from_all_three_sides(self):
        sizes = [len(scenario['talk']) for scenario in demo.SCENARIOS if len(scenario['talk']) > 1]
        self.assertGreaterEqual(len(sizes), 35)
        self.assertEqual((min(sizes), max(sizes)), (2, 12))
        self.assertEqual(set(Message.objects.values_list('sender', flat=True)), {'customer', 'bot', 'office'})
        self.assertEqual(
            set(Message.objects.values_list('status', flat=True)), {'received', 'sent', 'failed'},
        )
        self.assertTrue(Message.objects.filter(message_type='template').exists())

    def test_every_box_and_every_queue_has_somebody(self):
        counts = self.get('contacts/counts/', show_hidden=1).data
        for name, count in {**counts['boxes'], **counts['queues']}.items():
            with self.subTest(name=name):
                self.assertGreater(count, 0)
        default = self.get('contacts/counts/').data['queues']
        for name in ('all', 'due', 'none', 'no_answer', 'answered', 'later', 'not_relevant', 'hidden'):
            with self.subTest(queue=name):
                self.assertGreater(default[name], 0)

    def test_every_answer_of_the_matching_appears(self):
        found = set(Contact.objects.values_list('kogo_outcome', flat=True))
        self.assertEqual(found, {value for value, _ in OUTCOME_CHOICES} | {''})
        hidden = Contact.objects.filter(kogo_outcome__in=HIDDEN_OUTCOMES).count()
        self.assertEqual(self.get('contacts/', view='leads').data['count'], Contact.objects.count() - hidden)

    def test_the_summaries_are_filled_by_the_rules_and_one_contact_is_left_for_the_cron(self):
        self.assertEqual(Contact.objects.filter(needs_analysis=True).count(), 1)
        self.assertEqual(Contact.objects.filter(kogo_checked_at__isnull=True).count(), 1)
        self.assertFalse(Contact.objects.filter(analysis_source='ai').exists())
        topics = set(Contact.objects.values_list('known_topic', flat=True))
        self.assertTrue({'trial', 'registration', 'info', 'other'} <= topics)
        self.assertTrue(Contact.objects.exclude(known_branch_id=None).exists())
        self.assertTrue(Contact.objects.exclude(known_callback_on=None).exists())
        self.network.assert_not_called()

    def test_tags_and_ready_made_replies(self):
        self.assertEqual(Tag.objects.count(), len(demo.TAGS))
        self.assertEqual(QuickReply.objects.count(), len(demo.QUICK_REPLIES))
        self.assertTrue(Contact.objects.filter(tags__isnull=False).exists())

    def test_running_it_again_replaces_and_does_not_double(self):
        from apps.customers.models import Family

        contacts, families = Contact.objects.count(), Family.objects.filter(notes=demo.DEMO_MARK).count()
        demo.seed_demo(self.manager)
        self.assertEqual(Contact.objects.count(), contacts)
        self.assertEqual(Family.objects.filter(notes=demo.DEMO_MARK).count(), families)
        self.assertEqual(Tag.objects.count(), len(demo.TAGS))
        self.assertEqual(QuickReply.objects.count(), len(demo.QUICK_REPLIES))

    def test_the_live_update_starts_quiet(self):
        cursor = self.get('contacts/updates/').data['cursor']
        self.assertEqual(self.get('contacts/updates/', since=cursor).data['contacts'], [])

    def test_the_local_manager_is_made_once_and_is_a_manager(self):
        user, created = demo.ensure_local_admin('a-local-password')
        self.assertTrue(created)
        self.assertEqual(user.username, 'local-admin@kogo.test')
        self.assertEqual(user.profile.role, UserProfile.ROLE_MANAGER)
        self.assertTrue(user.check_password('a-local-password'))
        again, created = demo.ensure_local_admin('another')
        self.assertFalse(created)
        self.assertTrue(again.check_password('a-local-password'))    # an existing password is never replaced

    def test_without_a_password_the_manager_cannot_log_in_until_one_is_set(self):
        user, _ = demo.ensure_local_admin('')
        self.assertFalse(user.has_usable_password())


class SimulatorTests(WahubTestCase):
    def test_messages_arrive_through_the_same_door_as_manychats(self):
        demo.seed_demo(self.manager)
        Contact.objects.update(touched_at=timezone.now() - timedelta(minutes=5))
        cursor = self.get('contacts/updates/').data['cursor']
        before = Message.objects.count()
        rng = random.Random(7)

        with patch('apps.wahub.demo.inbound.store_event', wraps=demo.inbound.store_event) as door:
            phone, text, result = demo.simulate_one(rng)
            reply = demo.simulate_bot_reply(phone, rng)
        self.assertEqual(door.call_count, 2)
        self.assertTrue(result.stored)
        self.assertTrue(reply.stored)
        self.assertTrue(phone.startswith('97250555'))
        self.assertEqual(Message.objects.count(), before + 2)

        changed = self.get('contacts/updates/', since=cursor).data['contacts']
        self.assertEqual([row['phone'] for row in changed], [phone])
        self.assertIsNone(changed[0]['chat']['waiting_since'])      # the bot answered

    def test_a_new_phone_becomes_a_new_contact(self):
        rng = random.Random(3)
        phone, _text, result = demo.simulate_one(rng)      # nobody exists yet
        self.assertTrue(result.stored)
        self.assertEqual(Contact.objects.get().phone, phone)
