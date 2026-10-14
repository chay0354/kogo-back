"""
No lead is lost in silence, and the screen tells the truth about the follow-ups
(docs/WAHUB-CONTRACT-LEADS-NO-ONE-LOST.md, ב–ה): whether the bot's planned step
will really leave (`next.hold`), the two new rounds — an external branch and a
lead that went quiet — the daily look for a class that opened for a lead with
nothing to offer, and the price or the days in a lead that is not hot.

Nothing is sent from here: ManyChat is the fake of test_lead_automation.py, and
`self.network` fails any test that reaches for the real one. The clock is the
test's (tests/test_leads_v2.py): Monday 12.10.2026, noon.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import bot_mode, lead_automation, lead_templates, sending, settings_store
from apps.wahub.models import Contact, LeadFollowUp
from apps.wahub.tests.test_lead_automation import AutomationCase
from apps.wahub.tests.test_lead_followups import SENDING_ON, at
from apps.wahub.tests.test_leads_v2 import DAY, HOUR, TODAY

EXTERNAL_TEXT = 'היי מה נשמע 🙂 רציתי לבדוק אם הסתדרתם עם ההרשמה לקפוארה בשוהם מול העירייה. אם משהו לא ברור או נתקע תכתבו לי ואעזור'
SILENT_TEXT = 'היי מה נשמע 🙂 רציתי לבדוק אם נשארו שאלות לגבי קפוארה. אפשר להתחיל משיעור ניסיון אחד ולראות אם זה מתאים, לשלוח לכם קישור?'


class NoOneLostCase(AutomationCase):
    def next_of(self, contact) -> dict:
        response = self.get(f'leads/{contact.id}/')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['next']

    def listed_next(self, contact, owner='bot') -> dict:
        rows = self.get('leads/', owner=owner).data['results']
        return next(row['next'] for row in rows if row['contact_id'] == contact.id)


# --- ב. will the planned step really leave? ------------------------------------------------------------------

class HoldTests(NoOneLostCase):
    """`next.hold`: '' when the bot's step will leave when its moment comes, else what stops it — on the list and on the lead."""

    def setUp(self):
        super().setUp()
        # Got a link three hours ago: the first reminder is tomorrow at 10:30 — after WhatsApp's 24 hours,
        # so only a template may leave then.
        self.contact = self.link_lead(3 * HOUR)
        self.assertEqual(self.open_row(self.contact).due_at, at(2026, 10, 13))

    def both(self) -> tuple:
        on_the_list, in_full = self.listed_next(self.contact), self.next_of(self.contact)
        self.assertEqual(on_the_list, in_full)
        self.assertEqual((in_full['who'], in_full['state']), ('bot', 'planned'))
        return in_full['hold'], in_full['what']

    def test_sending_off_is_said(self):
        self.assertEqual(self.both(), ('השליחה כבויה', 'הבוט יזכיר את ההרשמה'))

    @override_settings(**SENDING_ON)
    def test_the_follow_ups_switched_off_are_said_and_not_a_missing_template(self):
        settings_store.WahubSetting.objects.filter(key=settings_store.BOT_FOLLOWUPS).delete()      # as before he turns them on
        self.assertEqual(self.both()[0], 'החזרות האוטומטיות כבויות')

    @override_settings(**SENDING_ON)
    def test_a_missing_template_is_said_and_a_connected_one_lets_it_leave(self):
        self.assertEqual(self.both()[0], 'חסרה תבנית')
        self.connect('link_reminder_1')
        self.assertEqual(self.both()[0], '')
        # Inside the 24 hours no template is needed: a lead who wrote this morning, reminded this afternoon.
        LeadFollowUp.objects.filter(pk=self.open_row(self.contact).pk).update(due_at=self.clock() + HOUR)
        settings_store.set_lead_template('link_reminder_1', '', user=self.manager)
        self.assertEqual(self.both()[0], '')

    @override_settings(**SENDING_ON)
    def test_a_step_whose_moment_came_says_why_it_did_not_leave(self):
        self.clock.moment = at(2026, 10, 13, 11, 0)
        LeadFollowUp.objects.filter(contact=self.contact).update(status='due', outcome_note='חסרה תבנית: …')
        nxt = self.next_of(self.contact)
        self.assertEqual((nxt['state'], nxt['hold']), ('due', 'חסרה תבנית'))

    def test_every_other_step_has_an_empty_hold(self):
        office = self.lead(2 * HOUR, answer=None)
        self.staged(office)
        self.assertEqual((self.next_of(office)['who'], self.next_of(office)['hold']), ('office', ''))
        booked = self.staged(self.lead(HOUR, kogo_outcome='trial_upcoming'))
        self.assertEqual((self.next_of(booked)['who'], self.next_of(booked)['hold']), ('none', ''))
        closed = self.staged(self.lead(HOUR, kogo_outcome='registered_after'))
        self.assertEqual(self.next_of(closed)['hold'], '')

    def test_a_demo_lead_is_written_into_its_conversation(self):
        demo = self.link_lead(3 * HOUR, is_demo=True)
        self.assertEqual(self.next_of(demo)['hold'], lead_automation.DEMO_WORDS)

    @override_settings(**SENDING_ON)
    def test_the_switches_and_the_templates_are_read_once_for_a_whole_list(self):
        def reads(rows: int) -> tuple:
            while LeadFollowUp.objects.filter(status='planned').count() < rows:
                self.link_lead(3 * HOUR)
            with patch.object(lead_templates, 'connections', wraps=lead_templates.connections) as templates, \
                    patch.object(sending, 'manychat_reachable', wraps=sending.manychat_reachable) as reachable, \
                    patch.object(bot_mode, 'followups_state', wraps=bot_mode.followups_state) as switch:
                results = self.get('leads/', owner='bot').data['results']
            self.assertEqual(len(results), rows)
            self.assertTrue(all(row['next']['hold'] == 'חסרה תבנית' for row in results))
            return templates.call_count, reachable.call_count, switch.call_count

        self.assertEqual(reads(1), (1, 1, 1))
        self.assertEqual(reads(6), (1, 1, 1))
        # A page with no step of the bot's on it reads none of them.
        with patch.object(lead_templates, 'connections') as templates:
            self.get('leads/', owner='office')
        templates.assert_not_called()
