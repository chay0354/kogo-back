"""
What came before the switch was answered by the old bot (the owner, 14.10.2026, an hour
after it: "המערכת לא מסונכרנת עם מה שהיה — זה מראה לי לידים שאולי ענו להם והם כבר לא רלוונטיים").

The old bot answered every customer's message, and its answers were never copied to Kogo.
From the first moment the new bot answered everybody (bot_mode.first_live_at) every answer is
Kogo's own; before it, nothing is read as unanswered: no wait, no "unanswered question" the
summary read in a conversation it saw only half of, and no request for a person made a day and
more before the switch that the office dealt with in ManyChat's own inbox.
"""
from datetime import timedelta

from django.utils import timezone

from apps.wahub import bot_mode, lead_stage, settings_store
from apps.wahub.models import Contact
from apps.wahub.tests.base import WahubTestCase


class SwitchTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        for cache in (lead_stage._seen, lead_stage._switch_seen):
            cache.update(at=None, since=None)
        self.addCleanup(lambda: [cache.update(at=None, since=None) for cache in (lead_stage._seen, lead_stage._switch_seen)])
        self.now = timezone.now()
        self.switch = self.now - timedelta(hours=1)
        settings_store.put(settings_store.BOT_MODE, {'mode': 'all', 'since': self.switch.isoformat(), 'first_since': self.switch.isoformat()})

    def contact_that_wrote(self, ago, phone='972505550301', **fields):
        self.incoming('יש חוג קפוארה לבן 6?', phone=phone)
        contact = Contact.objects.get(phone=phone)
        moment = self.now - ago
        # The summary read the conversation a minute after the message: what it concluded is about this conversation.
        Contact.objects.filter(pk=contact.pk).update(
            waiting_since=moment, last_inbound_at=moment, last_message_at=moment, first_inbound_at=moment,
            analyzed_at=moment + timedelta(minutes=1), **fields,
        )
        return Contact.objects.get(pk=contact.pk)

    def stage(self, contact):
        return lead_stage.compute(contact, self.now)


class BeforeTheSwitchTests(SwitchTestCase):
    def test_a_message_from_before_the_switch_was_answered_and_one_since_is_measured(self):
        before = self.contact_that_wrote(timedelta(hours=3))
        result = self.stage(before)
        self.assertNotEqual(result['stage'], 'needs_reply')
        self.assertNotIn('עוד לא קיבל תשובה', result['reason'])
        # Forty minutes ago — after the switch, and nobody answered: that one waits.
        after = self.contact_that_wrote(timedelta(minutes=40), phone='972505550302')
        self.assertEqual(self.stage(after)['stage'], 'needs_reply')

    def test_an_unanswered_question_read_in_a_conversation_the_old_bot_answered_is_not_one(self):
        said = {'blocker': 'open_question', 'blocker_text': 'כמה ילדים יש בקבוצה?'}
        old = self.contact_that_wrote(timedelta(days=2), lead_ai=said)
        self.assertNotEqual(self.stage(old)['stage'], 'needs_reply')
        # Asked after the switch: the new bot's answers are all on record, and the summary is believed.
        new = self.contact_that_wrote(timedelta(minutes=10), phone='972505550303', lead_ai=said)
        self.assertEqual(self.stage(new)['stage'], 'needs_reply')
        # What only a person can settle stands, whenever it was said.
        call = self.contact_that_wrote(timedelta(days=2), phone='972505550304', lead_ai={'blocker': 'call_someone', 'blocker_text': 'ביקש שיתקשרו'})
        self.assertEqual(self.stage(call)['stage'], 'needs_reply')

    def test_a_request_for_a_person_a_day_before_the_switch_was_the_offices_in_manychat(self):
        old = self.contact_that_wrote(timedelta(days=3), needs_human=True, needs_human_at=self.now - timedelta(days=3))
        self.assertNotEqual(self.stage(old)['stage'], 'needs_reply')
        # Hours before the switch: it may still wait for somebody — it stays the office's.
        recent = self.contact_that_wrote(
            timedelta(hours=3), phone='972505550305', needs_human=True, needs_human_at=self.now - timedelta(hours=3),
        )
        self.assertEqual(self.stage(recent)['stage'], 'needs_reply')
        # An old request from somebody who wrote again since the switch: the rules see them again.
        again = self.contact_that_wrote(
            timedelta(minutes=20), phone='972505550306', needs_human=True, needs_human_at=self.now - timedelta(days=3),
        )
        self.assertEqual(self.stage(again)['stage'], 'needs_reply')

    def test_a_contact_kogo_answered_itself_is_measured_as_before(self):
        own = self.contact_that_wrote(timedelta(hours=3), live_test=True)
        self.assertEqual(self.stage(own)['stage'], 'needs_reply')

    def test_before_any_switch_nothing_changes(self):
        settings_store.put(settings_store.BOT_MODE, {'mode': 'test'})
        lead_stage._switch_seen.update(at=None, since=None)
        old = self.contact_that_wrote(timedelta(days=3), needs_human=True, needs_human_at=self.now - timedelta(days=3))
        self.assertEqual(self.stage(old)['stage'], 'needs_reply')


class FirstLiveTests(WahubTestCase):
    def test_the_first_live_moment_is_kept_when_the_mode_goes_back_to_test(self):
        moment = timezone.now() - timedelta(hours=2)
        bot_mode_value = {'mode': 'all', 'since': moment.isoformat()}
        settings_store.put(settings_store.BOT_MODE, bot_mode_value)
        # Live before the first moment was kept: it is the first one, and is kept from now on.
        self.assertEqual(bot_mode.first_live_at(), moment)
        self.assertEqual(settings_store.get(settings_store.BOT_MODE)['first_since'], moment.isoformat())
        kept = dict(settings_store.get(settings_store.BOT_MODE), mode='test', since=None)
        settings_store.put(settings_store.BOT_MODE, kept)
        self.assertEqual(bot_mode.first_live_at(), moment)

    def test_never_live_is_no_moment(self):
        settings_store.put(settings_store.BOT_MODE, {'mode': 'test'})
        self.assertIsNone(bot_mode.first_live_at())
