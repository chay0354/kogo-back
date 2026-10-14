"""
"הכול אוטומטי" (docs/WAHUB-CONTRACT-LEADS-V2.md, י): connecting the WhatsApp
templates, the card that says what is still missing — and the table of the
contract (ג, ח.10, ט) played from end to end: every kind of lead gets the
right message at the right moment, and nobody else gets anything.

ManyChat is a fake behind the client's one door: it lists its automations and
notes every send with the moment it was asked for; `self.network` fails any
test that reaches for the real one. The clock is the test's (Monday 12.10.2026,
noon), and `play` lets it run the way the five-minute cron would see it.
"""
from datetime import date, datetime, timedelta
from unittest.mock import patch

import requests
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatError
from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import (
    analysis, inbound, knowledge, lead_ai, lead_automation, lead_followup_seed, lead_followups, lead_stage, leads,
    lead_templates, matching, sending, settings_store,
)
from apps.wahub.models import Contact, ContactEvent, LeadFollowUp, Message, WahubSetting
from apps.wahub.tests.base import BASE, client_for, make_user
from apps.wahub.tests.test_lead_followups import LINK_FIRST, OK, REQUEST, SENDING_ON, EngineCase, at
from apps.wahub.tests.test_leads_v2 import DAY, HOUR, MINUTE, START, TODAY, Card, LeadsCase, model_lead, said, special_day

STAGE_ONE = ['link_reminder_1', 'will_check_1', 'trial_feedback_1', 'trial_no_show_1', 'date_arrived_age_1']
OPENING_TEXT = 'היי 🙂 ראיתי שהתעניינתם בחוגים שלנו. לאיזה גיל ובאיזו עיר?'


def flow_of(template_key: str) -> str:
    return f'content_{template_key}'


def name_of(template_key: str) -> str:
    """What the fake ManyChat calls the automation of a follow-up — never the follow-up's own fixed name."""
    return f'ידני {template_key}'


class ManyChat:
    """ManyChat as the client's `_request` sees it: the automations it has, and every send with its moment."""

    def __init__(self, clock):
        self.clock = clock
        # One automation for every follow-up, under names of its own: nothing is found by its fixed name
        # unless a test names the automations so (`named`).
        self.flows = [{'ns': flow_of(key), 'name': name_of(key)} for key in (*lead_templates.FLOW_NAMES, 'dormant_1')]
        self.sent = []
        self.asked = []
        self.down = False            # getFlows does not answer
        self.refuse = None           # every send raises this

    def __call__(self, method, path, **kwargs):
        self.asked.append(path)
        if path == '/fb/page/getFlows':
            if self.down:
                raise ManyChatError('Server error', status_code=500)
            return {'status': 'success', 'data': {'flows': list(self.flows)}}
        body = kwargs.get('json_body') or {}
        if path == '/fb/sending/sendFlow':
            what = ('template', body['flow_ns'])
        elif path == '/fb/sending/sendContent':
            what = ('text', body['data']['content']['messages'][0]['text'])
        else:
            raise AssertionError(f'a call to ManyChat nobody expected: {method} {path}')
        if self.refuse is not None:
            raise self.refuse
        self.sent.append((self.clock(), *what))
        return OK

    @property
    def sends(self) -> int:
        return sum(1 for path in self.asked if path.startswith('/fb/sending/'))

    @property
    def lists(self) -> int:
        return self.asked.count('/fb/page/getFlows')

    def named(self, *template_keys, spell=str):
        """ManyChat now has one automation under the fixed name of each of these follow-ups (all of them, by default)."""
        names = dict.fromkeys(lead_templates.FLOW_NAMES[key] for key in (template_keys or lead_templates.FLOW_NAMES))
        self.flows = [{'ns': f'auto_{name}', 'name': spell(name)} for name in names]


class AutomationCase(EngineCase):
    def setUp(self):
        self.read_registrations = matching.recheck_contact          # the real one, before the engine's mock
        super().setUp()
        self.manychat = ManyChat(self.clock)
        door = patch(REQUEST, side_effect=self.manychat)
        door.start()
        self.addCleanup(door.stop)

    def put(self, template_key, body):
        return self.client.put(f'{BASE}/leads/templates/{template_key}/', body, format='json')

    def connect(self, *template_keys):
        for key in template_keys:
            settings_store.set_lead_template(key, flow_of(key), name=key, user=self.manager)

    def connect_all(self):
        self.connect(*[row['template_key'] for row in lead_automation.templates() if row['active']])

    def card(self) -> dict:
        response = self.get('leads/automation/')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def line(self, key) -> dict:
        return next(line for line in self.card()['lines'] if line['key'] == key)

    def family_with_a_trial(self, contact, outcome='attended', held=TODAY - timedelta(days=1), **sent):
        """
        The registrations themselves: a family by the contact's phone, one child,
        a trial lesson that took place — and, in `sent`, what the system's own
        automations have already written to the family about it.
        """
        from apps.enrollments.models import LessonEnrollment

        branch = TestDataFactory.create_branch(name='ראש העין', city=TestDataFactory.create_city('ראש העין'))
        local = f'0{contact.phone[3:]}'
        family = TestDataFactory.create_family(name='לוי', branch=branch, phone=local)
        TestDataFactory.create_parent(family=family, phone=local, first_name='רותם', last_name='לוי')
        child = TestDataFactory.create_child(family=family, first_name='נועה', last_name='לוי', status='trial_completed')
        course = TestDataFactory.create_course(name='קפוארה גילאי 5–7', branch=branch, course_type=TestDataFactory.create_course_type('קפוארה'))
        lesson = TestDataFactory.create_lesson(course=course, branch=branch)
        self.trial = LessonEnrollment.objects.create(
            lesson=lesson, child=child, status='active', trial_lesson_date=held, trial_outcome=outcome, **sent,
        )
        self.recheck.side_effect = self.read_registrations           # from here the registrations are read for real
        matching.recheck_contact(contact)
        lead_stage.refresh(contact)
        return child, lesson

    def play(self, until):
        """
        Let the time pass until `until` the way the five-minute cron sees it:
        at every moment it would have something to do — a follow-up to look
        at, a stage whose time has come — its two slices run, the stages and
        then the follow-ups. Nothing is skipped over and nothing is run early.
        """
        while True:
            lead_stage.scan()
            lead_followups.run()
            now = self.clock()
            ahead = [
                *LeadFollowUp.objects.filter(status__in=('planned', 'due'), next_look_at__gt=now).values_list('next_look_at', flat=True),
                *Contact.objects.filter(lead_recheck_at__gt=now).values_list('lead_recheck_at', flat=True),
            ]
            if not ahead or min(ahead) > until:
                break
            self.clock.moment = min(ahead)
        self.clock.moment = until
        lead_stage.scan()
        lead_followups.run()

    def assert_sent(self, *expected):
        """Exactly these left, at exactly these moments — and every one of them inside the office hours."""
        self.assertEqual(self.manychat.sent, list(expected))
        for moment, _how, _what in self.manychat.sent:
            local = timezone.localtime(moment)
            self.assertNotIn(local.weekday(), (4, 5), f'{local} is a Friday or a Saturday')
            self.assertTrue(lead_followups.is_open(moment), f'{local} is outside the office hours')


# --- connecting the templates --------------------------------------------------------------------------------

@override_settings(MANYCHAT_KEY='test-key')
class TemplatesApiTests(AutomationCase):
    def test_every_text_says_when_it_is_sent_in_which_stage_and_what_is_connected(self):
        response = self.get('leads/templates/')
        self.assertEqual(response.status_code, 200)
        rows = response.data
        contract = {'template_key', 'kind_label', 'when_label', 'text', 'stage', 'mapped', 'target'}
        self.assertTrue(all(contract <= set(row) for row in rows))
        self.assertTrue(all(row['kind_label'] and row['when_label'] and row['text'] for row in rows))
        # The five of stage 1 first, in the order of the owner's document; everything else is stage 2.
        self.assertEqual([row['template_key'] for row in rows[:5]], STAGE_ONE)
        self.assertEqual([row['stage'] for row in rows], [1] * 5 + [2] * (len(rows) - 5))
        self.assertTrue(all(row['target'] is None and row['mapped'] is False and row['target_name'] == '' for row in rows))
        by_key = {row['template_key']: row for row in rows}
        self.assertEqual(len(by_key), len(rows))
        for key, when in (
            ('link_reminder_1', 'יום אחרי שקיבל קישור להרשמה ולא נרשם'),
            ('link_reminder_2', '3 ימים אחרי שקיבל קישור להרשמה ולא נרשם'),
            ('link_reminder_3', 'שבוע אחרי שקיבל קישור להרשמה ולא נרשם. הפנייה האחרונה'),
            ('link_reminder_signup_1', 'יום אחרי שהתחיל הרשמה באתר ולא שילם'),
            ('link_reminder_commitment_1', 'יום אחרי ששאל מה קורה אם החוג לא יתאים'),
            ('will_check_1', 'יום אחרי שאמר שיבדוק ויחזור. פעם אחת בלבד. בלי תשובה — עובר אליך'),
            ('trial_feedback_1', 'יום אחרי שיעור ניסיון שלא הסתיים בהרשמה. לא יוצאת למי שכבר קיבל את ההודעה האוטומטית שאחרי השיעור'),
            ('trial_feedback_2', '3 ימים אחרי שיעור ניסיון שלא הסתיים בהרשמה'),
            ('trial_no_show_1', 'יום אחרי שיעור ניסיון שלא הגיעו אליו. לא יוצאת למי שכבר קיבל הודעה אוטומטית על היעדרויות'),
            ('trial_feedback_3', 'שבוע אחרי שיעור ניסיון שלא הסתיים בהרשמה. בלי תשובה — עובר אליך'),
            ('trial_no_show_2', '3 ימים אחרי שיעור ניסיון שלא הגיעו אליו. בלי תשובה — עובר אליך'),
            ('date_arrived_1', 'ביום שהלקוח עצמו אמר ("אחרי החגים", "בעוד חצי שנה")'),
            ('date_arrived_3', 'שבוע אחרי היום שהלקוח אמר. הפנייה האחרונה'),
            ('date_arrived_age_1', 'ביום שבו מלאו לילד 3'),
            ('date_arrived_age_2', 'שבוע אחרי שמלאו לילד 3. הפנייה האחרונה'),
            ('date_arrived_not_ready_1', 'ב-1 בספטמבר, למי שאמר שהילד לא רצה. פעם אחת בלבד'),
        ):
            self.assertEqual(by_key[key]['when_label'], when, key)
        self.assertIn('שעתיים אחרי', by_key['opening_1']['when_label'])
        # The days are the engine's own: a child old enough is written to on the day and a week later.
        self.assertEqual((by_key['date_arrived_age_1']['days'], by_key['date_arrived_age_2']['days']), (0, 7))
        # The fixed name of each one's automation in ManyChat; the last reminder is one automation for two rows.
        self.assertEqual(by_key['link_reminder_1']['flow_name'], 'kogo_lead_link_1')
        self.assertEqual(by_key['link_reminder_3']['flow_name'], by_key['date_arrived_3']['flow_name'])
        self.assertEqual({row['template_key']: row['flow_name'] for row in rows if row['active']}, lead_templates.FLOW_NAMES)
        self.assertTrue(all(row['by'] is None for row in rows))
        # What needs a template: every live text but the opening, which leaves inside WhatsApp's 24 hours.
        self.assertEqual({key for key, row in by_key.items() if not row['required']}, {'opening_1', 'dormant_1'})

    def test_what_the_screen_says_is_what_the_engine_plans(self):
        """`when_label` and `days` are read from the very delays a round is planned by."""
        rows = self.get('leads/templates/').data
        # The message a person sends by a press is planned by nobody (test_waiting_notify has it).
        by_hand = [row for row in rows if row['kind'] in lead_followup_seed.BY_HAND]
        self.assertEqual([(row['template_key'], row['days']) for row in by_hand], [('seat_freed_1', None)])
        for row in rows:
            if row in by_hand:
                continue
            delays = lead_stage.delays_for(row['kind'], row['variant'])
            with self.subTest(key=row['template_key']):
                self.assertEqual(row['days'], delays[row['step'] - 1].days)
                self.assertEqual(row['when_label'], lead_automation.when_label(row['kind'], row['variant'], row['step']))
                self.assertEqual(lead_followups.phrasing(row['kind'], row['step'], row['variant'])[1] or row['template_key'], row['template_key'])

    def test_the_automations_manychat_has_are_offered_by_name(self):
        self.manychat.flows = [
            {'ns': 'content_b', 'name': 'kogo_lead_trial_1'}, {'ns': 'content_a', 'name': 'kogo_lead_link_1'},
            {'ns': 'content_a', 'name': 'the same flow twice'}, {'ns': '', 'name': 'no id'}, {'ns': 'content_c'},
        ]
        response = self.get('leads/templates/options/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'reason': '', 'options': [
            {'id': 'content_c', 'name': 'content_c'}, {'id': 'content_a', 'name': 'kogo_lead_link_1'},
            {'id': 'content_b', 'name': 'kogo_lead_trial_1'},
        ]})
        self.assertEqual(self.manychat.asked, ['/fb/page/getFlows'])

    def test_no_options_when_manychat_cannot_be_asked(self):
        self.manychat.down = True
        self.assertEqual(self.get('leads/templates/options/').data, {'options': None, 'reason': 'ManyChat לא ענה'})
        with override_settings(MANYCHAT_KEY=''):
            self.assertEqual(self.get('leads/templates/options/').data, {'options': None, 'reason': 'השרת לא מחובר ל-ManyChat'})
        asked = len(self.manychat.asked)
        with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True):
            self.assertIsNone(self.get('leads/templates/options/').data['options'])
        self.assertEqual(len(self.manychat.asked), asked)            # a machine that only records asks nobody

    def test_connecting_by_id_and_by_name_and_taking_it_off(self):
        response = self.put('link_reminder_1', {'target': 'content_link_reminder_1'})
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data
        self.assertEqual((row['template_key'], row['mapped'], row['target'], row['target_name']),
                         ('link_reminder_1', True, 'content_link_reminder_1', 'ידני link_reminder_1'))
        self.assertEqual((row['by'], row['connected_by'], row['connected_at']), ('manual', 'דנה מנהלת', self.clock().isoformat()))
        self.assertEqual(settings_store.lead_templates(), {'link_reminder_1': 'content_link_reminder_1'})
        # By the name ManyChat calls it — however it was typed — and what is kept is the flow.
        named = self.put('will_check_1', {'target': '  ידני  WILL-check_1 '}).data
        self.assertEqual((named['target'], named['target_name']), ('content_will_check_1', 'ידני will_check_1'))
        listed = {row['template_key']: row['target'] for row in self.get('leads/templates/').data if row['mapped']}
        self.assertEqual(listed, {'link_reminder_1': 'content_link_reminder_1', 'will_check_1': 'content_will_check_1'})

        # Taking one off leaves the other; who took it off and when is kept.
        self.clock.forward(hours=1)
        off = self.put('link_reminder_1', {'target': None})
        self.assertEqual((off.status_code, off.data['mapped'], off.data['target'], off.data['target_name'], off.data['by']), (200, False, None, '', None))
        self.assertEqual(settings_store.lead_templates(), {'will_check_1': 'content_will_check_1'})
        kept = settings_store.lead_template_entries()['link_reminder_1']
        self.assertEqual((kept['flow'], kept['by'], kept['by_name'], kept['at']), ('', self.manager.pk, 'דנה מנהלת', self.clock().isoformat()))
        self.assertEqual(WahubSetting.objects.get(key='lead_followup_templates').updated_by, self.manager)
        # An empty name takes it off as well, and taking off what is not connected changes nothing.
        self.assertFalse(self.put('will_check_1', {'target': ' '}).data['mapped'])
        before = settings_store.lead_template_entries()
        self.assertEqual(self.put('trial_feedback_1', {'target': None}).status_code, 200)
        self.assertEqual(settings_store.lead_template_entries(), before)

    def test_a_connection_is_kept_only_when_manychat_itself_lists_the_automation(self):
        def refused(body, code, words, key='link_reminder_1'):
            response = self.put(key, body)
            self.assertEqual(response.status_code, code, response.data)
            self.assertIn(words, response.data['detail'])

        refused({'target': 'content_gone'}, 400, 'לא נמצאה ב-ManyChat')
        refused({'target': 'a name nobody has'}, 400, 'לא נמצאה ב-ManyChat')
        self.manychat.flows += [{'ns': 'content_twin_a', 'name': 'תאומים'}, {'ns': 'content_twin_b', 'name': 'תאומים'}]
        refused({'target': 'תאומים'}, 400, 'כמה אוטומציות בשם הזה')                  # two automations share that name
        refused({'target': 12}, 400, 'target')
        refused({'target': ['content_link_reminder_1']}, 400, 'target')
        refused({}, 400, 'target')
        refused({'target': 'content_link_reminder_1'}, 404, 'אין חזרה כזאת', key='no_such_follow_up')
        self.manychat.down = True
        refused({'target': 'content_link_reminder_1'}, 502, 'אי אפשר לבדוק עכשיו מול ManyChat')
        with override_settings(MANYCHAT_KEY=''):
            refused({'target': 'content_link_reminder_1'}, 502, 'השרת לא מחובר ל-ManyChat')
        self.assertEqual(settings_store.lead_templates(), {})
        self.assertFalse(WahubSetting.objects.filter(key='lead_followup_templates').exists())

    def test_connecting_one_never_loses_another_and_the_first_shape_of_the_mapping_is_still_read(self):
        settings_store.put(settings_store.LEAD_FOLLOWUP_TEMPLATES, {'trial_feedback_1': 'content_old_shape', 'will_check_1': ' '})
        self.put('link_reminder_1', {'target': 'content_link_reminder_1'})
        self.put('trial_no_show_1', {'target': 'content_trial_no_show_1'})
        self.assertEqual(settings_store.lead_templates(), {
            'trial_feedback_1': 'content_old_shape', 'link_reminder_1': 'content_link_reminder_1',
            'trial_no_show_1': 'content_trial_no_show_1',
        })
        old = next(row for row in self.get('leads/templates/').data if row['template_key'] == 'trial_feedback_1')
        self.assertEqual((old['mapped'], old['target'], old['target_name'], old['connected_by']), (True, 'content_old_shape', 'content_old_shape', ''))

    @override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True)
    def test_on_a_machine_that_only_records_the_name_is_kept_as_typed(self):
        row = self.put('link_reminder_1', {'target': 'whatever-i-typed', 'target_name': 'תבנית לבדיקה'}).data
        self.assertEqual((row['mapped'], row['target'], row['target_name']), (True, 'whatever-i-typed', 'תבנית לבדיקה'))
        self.assertEqual(self.manychat.asked, [])

    def test_only_a_manager_connects_and_reads(self):
        reads = ('leads/automation/', 'leads/templates/', 'leads/templates/options/')
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            client = client_for(make_user(f'{role}@automation.test', role))
            for path in reads:
                self.assertEqual(client.get(f'{BASE}/{path}').status_code, 403, path)
            self.assertEqual(client.put(f'{BASE}/leads/templates/link_reminder_1/', {'target': 'content_link_reminder_1'}, format='json').status_code, 403)
        anonymous = APIClient()
        for path in reads:
            self.assertIn(anonymous.get(f'{BASE}/{path}').status_code, (401, 403), path)
        self.assertIn(anonymous.put(f'{BASE}/leads/templates/link_reminder_1/', {'target': 'x'}, format='json').status_code, (401, 403))
        self.assertEqual(settings_store.lead_templates(), {})
        self.assertEqual(self.manychat.asked, [])
        # Reading is reading: nothing is changed by a POST, and the key is not a place to POST to.
        self.assertEqual(self.client.post(f'{BASE}/leads/automation/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.post(f'{BASE}/leads/templates/link_reminder_1/', {'target': 'x'}, format='json').status_code, 405)
        self.assertEqual(self.client.get(f'{BASE}/leads/templates/link_reminder_1/').status_code, 405)

    @override_settings(**SENDING_ON)
    def test_a_follow_up_that_waited_for_the_template_leaves_on_the_next_call_once_it_is_connected(self):
        contact = self.link_lead(3 * HOUR)                           # tomorrow 10:30 is outside the 24 hours
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_due'], 1)
        row = self.open_row(contact)
        self.assertEqual((row.status, row.next_look_at), ('due', at(2026, 10, 13, 11, 0)))
        self.assertTrue(row.outcome_note.startswith('חסרה תבנית'))
        self.clock.forward(minutes=5)
        self.assertEqual(self.put('link_reminder_1', {'target': name_of('link_reminder_1')}).status_code, 200)
        self.assertEqual(self.open_row(contact).next_look_at, self.clock())
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assert_sent((at(2026, 10, 13, 10, 35), 'template', 'content_link_reminder_1'))


# --- the card: does it all run by itself, and if not, what is missing ---------------------------------------

class AutomationCardTests(AutomationCase):
    def test_before_anything_is_set_up_the_card_says_what_is_missing(self):
        settings_store.WahubSetting.objects.filter(key=settings_store.BOT_FOLLOWUPS).delete()     # as it is before he turns them on
        card = self.card()
        self.assertEqual(set(card), {'ready', 'lines', 'today', 'upcoming', 'banner'})
        self.assertEqual([line['key'] for line in card['lines']], ['classify', 'schedule', 'sending', 'followups', 'templates'])
        self.assertTrue(all(set(line) == {'key', 'ok', 'title', 'detail', 'how'} for line in card['lines']))
        self.assertEqual([line['title'] for line in card['lines']],
                         ['זיהוי וסיווג הלידים', 'תזמון החזרות', 'שליחה ללקוחות', 'חזרות אוטומטיות', 'תבניות וואטסאפ'])
        classify, schedule, sent, follow_ups, templates = card['lines']
        self.assertEqual((follow_ups['ok'], follow_ups['detail']), (False, 'כבויות — הבוט מתכנן חזרות ולא שולח אותן'))
        self.assertEqual(follow_ups['how'], 'מדליקים במתג שבשורה הזאת. רק חזרות שמועדן אחרי ההדלקה יוצאות — מה שחיכה עד אז מדולג')
        self.assertEqual((classify['ok'], classify['detail'], classify['how']), (True, 'אין לידים פתוחים', ''))
        self.assertEqual((schedule['ok'], schedule['detail'], schedule['how']), (True, 'אין כרגע חזרות מתוכננות', ''))
        self.assertEqual((sent['ok'], sent['detail'], sent['how']),
                         (False, 'כבויה — שום הודעה לא יוצאת ללקוחות', 'מדליקים בהגדרות המערכת (WAHUB_SENDING_ENABLED)'))
        self.assertEqual((templates['ok'], templates['detail'], templates['how']),
                         (False, '0 מתוך 5 של שלב 1 מחוברות', 'יוצרים ב-ManyChat ובוחרים כאן למטה'))
        self.assertIs(card['ready'], False)
        self.assertEqual(card['today'], {'planned': 0, 'due': 0, 'sent': 0, 'skipped': 0, 'failed': 0})
        self.assertEqual((card['upcoming'], card['banner']), ([], None))
        self.assertEqual(self.manychat.asked, [])                    # with no ManyChat on the server, nobody is asked

    @override_settings(**SENDING_ON)
    def test_ready_is_the_five_templates_of_stage_one_and_the_switch(self):
        self.connect(*STAGE_ONE[:4])
        self.assertIs(self.card()['ready'], False)
        self.assertEqual(self.line('templates')['detail'], '4 מתוך 5 של שלב 1 מחוברות')
        self.connect(STAGE_ONE[4], 'link_reminder_2')
        card = self.card()
        self.assertIs(card['ready'], True)
        self.assertTrue(all(line['ok'] and line['how'] == '' for line in card['lines']))
        self.assertEqual(self.line('templates')['detail'], '5 מתוך 5 של שלב 1 מחוברות · שלב 2: 1 מתוך 15')
        self.assertEqual(self.line('sending')['detail'], 'דולקת — הודעות יוצאות ללקוחות, בשעות המשרד בלבד')
        # One of the five taken off: not ready any more.
        settings_store.set_lead_template('trial_feedback_1', '', user=self.manager)
        self.assertIs(self.card()['ready'], False)

    def test_a_text_the_owner_turned_off_is_not_asked_for(self):
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_will_check', 'title': 'כבוי', 'body': 'x', 'is_active': False})
        self.assertEqual(self.line('templates')['detail'], '0 מתוך 4 של שלב 1 מחוברות')
        self.connect('link_reminder_1', 'trial_feedback_1', 'trial_no_show_1', 'date_arrived_age_1')
        self.assertTrue(self.line('templates')['ok'])

    def test_the_sending_line_in_every_state(self):
        off = self.line('sending')
        self.assertEqual((off['ok'], off['detail']), (False, 'כבויה — שום הודעה לא יוצאת ללקוחות'))
        self.lead(live_test=True)
        self.assertEqual(self.line('sending')['detail'], 'כבויה — שום הודעה לא יוצאת ללקוחות (חוץ ממספר הבדיקה שלך)')
        with override_settings(WAHUB_SENDING_ENABLED=True, MANYCHAT_KEY=''):
            no_key = self.line('sending')
            self.assertEqual((no_key['ok'], no_key['detail']), (False, 'דולקת, אבל השרת לא מחובר ל-ManyChat — שום הודעה לא יוצאת'))
            self.assertIn('MANYCHAT_KEY', no_key['how'])
        with override_settings(**SENDING_ON):
            self.assertTrue(self.line('sending')['ok'])
        with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True):
            simulated = self.line('sending')
            self.assertEqual((simulated['ok'], simulated['detail']), (True, 'הדמיה במחשב פיתוח — הודעות נרשמות ולא יוצאות לאף אחד'))

    def test_the_classify_line_counts_the_open_leads_and_says_how_fresh_the_look_is(self):
        self.staged(self.lead(HOUR))
        self.staged(self.lead(2 * HOUR, answer=None))                # waits for an answer: the office's
        self.staged(self.lead(HOUR, kogo_outcome='registered_after'))            # closed: not counted
        line = self.line('classify')
        self.assertEqual((line['ok'], line['detail']), (True, '2 לידים פתוחים, עודכן עכשיו'))
        self.clock.forward(minutes=7)
        self.assertEqual(self.line('classify')['detail'], '2 לידים פתוחים, עודכן לפני 7 דק׳')
        self.clock.forward(hours=2)
        self.assertEqual(self.line('classify')['detail'], '2 לידים פתוחים, עודכן לפני שעתיים')
        Contact.objects.filter(lead_stage='needs_reply').delete()
        self.assertIn('ליד פתוח אחד', self.line('classify')['detail'])

    def test_the_classify_line_says_when_the_look_has_stopped(self):
        self.staged(self.lead(HOUR))
        # The night's rest of the cron is not a fault: every open lead waits for its look from midnight.
        self.clock.to(2026, 10, 13, 8, 59)
        self.assertTrue(self.line('classify')['ok'])
        self.clock.to(2026, 10, 13, 9, 0)
        stopped = self.line('classify')
        self.assertEqual((stopped['ok'], stopped['detail']), (False, 'איש קשר אחד מחכה לסיווג יותר מ-9 שעות'))
        self.assertTrue(stopped['how'])
        lead_stage.scan()
        self.assertTrue(self.line('classify')['ok'])
        # A contact nobody has looked at yet, or one touched since its look, waits the same way.
        fresh = self.lead(HOUR)
        self.assertTrue(self.line('classify')['ok'])
        Contact.objects.filter(pk=fresh.pk).update(created_at=self.clock() - 9 * HOUR)
        self.assertEqual(self.line('classify')['detail'], 'איש קשר אחד מחכה לסיווג יותר מ-9 שעות')

    def test_the_classify_line_says_that_the_bots_answers_do_not_reach_kogo(self):
        self.staged(self.lead(HOUR))
        with patch.object(lead_stage, 'replies_copied_since', return_value=None):
            line = self.line('classify')
        self.assertEqual(line['ok'], False)
        self.assertEqual(line['detail'], 'ליד פתוח אחד. תשובות הבוט לא מגיעות ל-Kogo — לכן לא ידוע מי כבר קיבל קישור להרשמה')
        self.assertIn('ManyChat', line['how'])

    def test_the_schedule_line_and_todays_numbers(self):
        waiting = self.link_lead()                                   # planned for tomorrow 10:30
        asked = self.link_lead(3 * HOUR)
        self.link_lead(is_demo=True, source='demo')                  # a demo: listed, never counted
        self.assertEqual(self.line('schedule')['detail'], '2 חזרות מתוכננות (ועוד אחת של איש קשר דמו)')
        self.assertEqual(self.card()['today'], {'planned': 2, 'due': 0, 'sent': 0, 'skipped': 0, 'failed': 0})
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()                                         # the switch is off: both are due, the demo is written
        card = self.card()
        self.assertEqual(card['today'], {'planned': 0, 'due': 2, 'sent': 0, 'skipped': 0, 'failed': 0})
        self.assertEqual(self.line('schedule')['detail'], '2 חזרות הגיע זמנן (ועוד אחת של איש קשר דמו)')
        leads.close(asked.id, 'not_interested', '', self.manager)    # called off today
        self.assertEqual(self.card()['today'], {'planned': 0, 'due': 1, 'sent': 0, 'skipped': 1, 'failed': 0})
        self.assertEqual(self.line('schedule')['detail'], 'חזרה אחת הגיע זמנה (ועוד אחת של איש קשר דמו)')
        # Tomorrow, what was called off yesterday is no longer "today".
        self.clock.to(2026, 10, 14, 0, 5)
        self.assertEqual(self.card()['today']['skipped'], 0)
        self.assertEqual(self.open_row(waiting).status, 'due')

    @override_settings(**SENDING_ON)
    def test_what_left_and_what_failed_today_are_counted(self):
        self.connect('link_reminder_1')
        self.link_lead(3 * HOUR)
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()
        self.assertEqual(self.card()['today'], {'planned': 1, 'due': 0, 'sent': 1, 'skipped': 0, 'failed': 0})
        self.link_lead(26 * HOUR)
        self.manychat.refuse = ManyChatError('Validation error', status_code=400)
        lead_followups.run()
        self.assertEqual(self.card()['today'], {'planned': 2, 'due': 0, 'sent': 1, 'skipped': 0, 'failed': 1})

    def test_the_schedule_line_says_when_follow_ups_are_not_being_acted_on(self):
        self.link_lead()
        self.clock.moment = at(2026, 10, 13, 11, 29)
        self.assertTrue(self.line('schedule')['ok'])                 # under an hour late: the next call takes it
        self.clock.moment = at(2026, 10, 13, 11, 30)
        late = self.line('schedule')
        self.assertEqual((late['ok'], late['detail']), (False, 'חזרה אחת עברה את זמנה ועוד לא טופלה'))
        self.assertTrue(late['how'])
        lead_followups.run()
        self.assertTrue(self.line('schedule')['ok'])

    def test_the_next_follow_ups_say_for_whom_what_when_and_what_stops_them(self):
        inside = self.link_lead(name='רותם לוי')                     # wrote 11:00: tomorrow 10:30 is inside the 24 hours
        outside = self.link_lead(3 * HOUR, name='דנה כהן')           # wrote 9:00: outside
        demo = self.link_lead(is_demo=True, source='demo', name='לקוח דמו')
        upcoming = self.card()['upcoming']
        self.assertEqual([row['contact_id'] for row in upcoming], [inside.id, outside.id, demo.id])     # by time, then as planned
        first = upcoming[0]
        self.assertTrue({'contact_id', 'name', 'kind_label', 'text', 'when', 'state', 'needs_template', 'template_mapped'} <= set(first))
        self.assertEqual(
            (first['name'], first['kind_label'], first['step'], first['text'], first['state']),
            ('רותם לוי', 'קיבל קישור ועוד לא נרשם', 1, LINK_FIRST, 'planned'),
        )
        self.assertEqual(datetime.fromisoformat(first['when']), at(2026, 10, 13))
        self.assertEqual((first['needs_template'], first['template_mapped'], first['blocked'], first['note']),
                         (False, False, 'sending', 'השליחה כבויה'))
        second = upcoming[1]
        self.assertEqual((second['needs_template'], second['template_key'], second['template_mapped'], second['blocked']),
                         (True, 'link_reminder_1', False, 'sending'))
        third = upcoming[2]
        self.assertEqual((third['is_demo'], third['needs_template'], third['blocked'], third['note']),
                         (True, False, '', 'ייכתב לשיחת הדמו — לא נשלח לאף אחד'))
        self.assertIsNone(self.card()['banner'])                     # nothing is due yet

        with override_settings(**SENDING_ON):
            now_on = {row['contact_id']: row for row in self.card()['upcoming']}
            self.assertEqual((now_on[inside.id]['blocked'], now_on[inside.id]['note']), ('', ''))
            self.assertEqual((now_on[outside.id]['blocked'], now_on[outside.id]['note']), ('template', 'חסרה תבנית'))
            self.connect('link_reminder_1')
            connected = {row['contact_id']: row for row in self.card()['upcoming']}
            self.assertEqual((connected[outside.id]['template_mapped'], connected[outside.id]['blocked']), (True, ''))

    def test_a_signup_begun_is_called_by_its_own_name_among_the_next_ones(self):
        self.staged(self.lead(4 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום ב-11.10 · נועה לוי'))
        (row,) = self.card()['upcoming']
        self.assertEqual((row['kind'], row['kind_label'], row['template_key']), ('link_reminder', 'התחיל הרשמה ולא סיים', 'link_reminder_signup_1'))

    def test_only_the_fifteen_nearest_are_listed(self):
        for _ in range(17):
            self.link_lead()
        card = self.card()
        self.assertEqual((len(card['upcoming']), card['today']['planned']), (15, 17))

    def test_the_line_at_the_head_of_the_bots_list_says_how_many_would_be_leaving_now_and_why_not(self):
        self.link_lead()
        self.link_lead(3 * HOUR)
        self.link_lead(is_demo=True, source='demo')
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()
        banner = self.card()['banner']
        self.assertEqual((banner['count'], banner['reason'], banner['text']), (2, 'sending', '2 הודעות היו יוצאות עכשיו — השליחה כבויה'))
        with override_settings(**SENDING_ON):
            # The switch is on: the one inside the 24 hours will leave on the next call; the other lacks a template.
            banner = self.card()['banner']
            self.assertEqual((banner['count'], banner['reason'], banner['text']), (1, 'template', 'הודעה אחת הייתה יוצאת עכשיו — חסרה תבנית'))
            self.connect('link_reminder_1')
            self.assertIsNone(self.card()['banner'])
        with override_settings(WAHUB_SENDING_ENABLED=True, MANYCHAT_KEY=''):
            self.assertEqual(self.card()['banner']['text'], '2 הודעות היו יוצאות עכשיו — השרת לא מחובר ל-ManyChat')

    def test_the_card_asks_the_same_number_of_questions_however_many_leads_there_are(self):
        for _ in range(3):
            self.link_lead()
        with CaptureQueriesContext(connection) as few:
            lead_automation.status()
        for _ in range(9):
            self.link_lead()
        with CaptureQueriesContext(connection) as many:
            lead_automation.status()
        self.assertEqual(len(many), len(few))


# --- a follow-up after the 24 hours leaves through the connected template -----------------------------------

class TemplateSendingTests(AutomationCase):
    def late_lead(self) -> Contact:
        """Wrote at 9:00 and got a link: tomorrow at 10:30 WhatsApp's 24 hours are over."""
        return self.link_lead(3 * HOUR)

    def test_after_24_hours_it_leaves_only_with_a_template_connected_and_the_switch_on(self):
        contact = self.late_lead()
        self.clock.moment = at(2026, 10, 13)
        # The switch off, a template connected: nothing.
        self.connect('link_reminder_1')
        with override_settings(MANYCHAT_KEY='test-key'):
            lead_followups.run()
        row = self.open_row(contact)
        self.assertEqual((row.status, row.outcome_note, self.manychat.asked), ('due', 'השליחה ללקוחות כבויה', []))
        # The switch on, no template: nothing — and the reason is the template.
        settings_store.set_lead_template('link_reminder_1', '', user=self.manager)
        self.clock.forward(minutes=30)
        with override_settings(**SENDING_ON):
            lead_followups.run()
            row = self.open_row(contact)
            self.assertEqual((row.status, self.manychat.asked), ('due', []))
            self.assertTrue(row.outcome_note.startswith('חסרה תבנית'))
            move = self.get(f'leads/{contact.id}/').data['next']
            self.assertEqual((move['state'], move['note']), ('due', row.outcome_note))
            self.assertEqual(Message.objects.filter(contact=contact).count(), 0)
            # Both: it leaves — as that template, once.
            self.connect('link_reminder_1')
            self.clock.forward(minutes=30)
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assert_sent((at(2026, 10, 13, 11, 30), 'template', 'content_link_reminder_1'))
        sent = self.rows(contact)[0]
        self.assertEqual((sent.status, sent.outcome_note, sent.acted_at), ('sent', '', self.clock()))
        message = sent.message
        self.assertEqual((message.message_type, message.sender, message.new_bot, message.status, message.error, message.text),
                         ('template', 'bot', True, 'sent', '', LINK_FIRST))
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 1)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_followup').latest('id').text,
                         f'קיבל קישור ועוד לא נרשם, פנייה 1: {LINK_FIRST}')

    @override_settings(**SENDING_ON)
    def test_it_leaves_the_very_way_the_screens_send_template_does(self):
        contact = self.late_lead()
        self.connect('link_reminder_1')
        self.clock.moment = at(2026, 10, 13)
        with patch.object(sending, 'send_flow', wraps=sending.send_flow) as the_screens_own:
            lead_followups.run()
        the_screens_own.assert_called_once()
        self.assertEqual(the_screens_own.call_args.args[:2], (Contact.objects.get(pk=contact.pk), 'content_link_reminder_1'))
        by_the_bot = self.manychat.sent[-1]
        # "שלח תבנית" on the screen, for the same contact and the same automation: the same call to ManyChat.
        response = self.post(f'contacts/{contact.id}/send-flow/', {'automation_id': 'content_link_reminder_1', 'automation_name': 'תזכורת'})
        self.assertEqual(response.data['message']['status'], 'sent')
        self.assertEqual(self.manychat.sent[-1][1:], by_the_bot[1:])
        self.assertEqual(self.manychat.asked, ['/fb/sending/sendFlow', '/fb/sending/sendFlow'])
        # The office's is the office's line, the bot's is the bot's.
        mine, theirs = Message.objects.filter(contact=contact).order_by('id')
        self.assertEqual((mine.sender, mine.new_bot, theirs.sender, theirs.new_bot), ('bot', True, 'office', False))

    @override_settings(**SENDING_ON)
    def test_what_manychat_refused_is_kept_with_the_reason_and_never_sent_again(self):
        contact = self.late_lead()
        self.connect_all()
        self.clock.moment = at(2026, 10, 13)
        self.manychat.refuse = ManyChatError('Flow not found', status_code=400, payload={'status': 'error', 'message': 'Flow not found'})
        self.assertEqual(lead_followups.run()['followups_failed'], 1)
        self.manychat.refuse = None
        for _ in range(4):                                           # the calls of the next two hours
            self.clock.forward(minutes=30)
            lead_followups.run()
        self.assertEqual((self.manychat.sends, self.manychat.sent), (1, []))
        first, second = self.rows(contact)
        self.assertEqual((first.status, first.acted_at), ('failed', at(2026, 10, 13)))
        self.assertIn('Flow not found', first.outcome_note)
        self.assertEqual((first.message.status, first.message.message_type), ('failed', 'template'))
        self.assertIn('Flow not found', first.message.error)
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 0)                # nothing reached the customer
        # The round goes on to its next step, on its own day — it is not the same message again.
        self.assertEqual((second.step, second.status, second.template_key, second.due_at), (2, 'planned', 'link_reminder_2', at(2026, 10, 15)))
        self.play(at(2026, 10, 15))
        self.assert_sent((at(2026, 10, 15), 'template', 'content_link_reminder_2'))

    @override_settings(**SENDING_ON)
    def test_when_manychat_does_not_answer_nobody_knows_and_it_is_not_sent_again(self):
        contact = self.late_lead()
        self.connect_all()
        self.clock.moment = at(2026, 10, 13)
        silence = ManyChatError('timeout')
        silence.__cause__ = requests.ReadTimeout()
        self.manychat.refuse = silence
        lead_followups.run()
        self.manychat.refuse = None
        self.clock.forward(minutes=40)
        lead_followups.run()
        self.assertEqual(self.manychat.sends, 1)
        first = self.rows(contact)[0]
        self.assertEqual(first.status, 'failed')
        self.assertIn('לא ידוע אם ההודעה נשלחה', first.outcome_note)

    @override_settings(WAHUB_SENDING_ENABLED=True, MANYCHAT_KEY='')
    def test_with_no_manychat_on_the_server_the_follow_up_waits_and_costs_the_round_nothing(self):
        contact = self.late_lead()
        self.connect('link_reminder_1')
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_due'], 1)
        row = self.open_row(contact)
        self.assertEqual((row.status, row.outcome_note, row.step), ('due', 'השרת לא מחובר ל-ManyChat', 1))
        self.assertEqual((Message.objects.filter(contact=contact).count(), self.manychat.asked), (0, []))
        with override_settings(MANYCHAT_KEY='test-key'):
            self.clock.forward(minutes=30)
            self.assertEqual(lead_followups.run()['followups_sent'], 1)

    @override_settings(**SENDING_ON)
    def test_the_words_that_leave_are_the_phrasings_words_on_that_day(self):
        """The owner rewrote the phrasing after the follow-up was planned: what leaves is what he wrote."""
        contact = self.link_lead()                                   # wrote 11:00: tomorrow 10:30 is inside the 24 hours
        self.assertEqual(self.open_row(contact).text, LINK_FIRST)
        item = knowledge.create_item({
            'kind': 'phrasing', 'key': 'lead_link_reminder', 'title': 'שלי', 'body': 'היי מה קורה 🙂 נרשמתם בסוף ל{חוג}?',
            'data': {'variants': ['היי רק מזכירה 🙂']},
        }, self.manager)
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()
        self.assert_sent((at(2026, 10, 13), 'text', 'היי מה קורה 🙂 נרשמתם בסוף לקפוארה?'))
        sent = self.rows(contact)[0]
        self.assertEqual((sent.text, sent.message.text), ('היי מה קורה 🙂 נרשמתם בסוף לקפוארה?',) * 2)
        # A phrasing turned off after the next one was planned: what was planned stands, as before.
        second = self.open_row(contact)
        knowledge.soft_delete(item, self.manager)
        self.assertEqual(lead_followups.words_now(second, self.fresh(contact)), second.text)

    @override_settings(**SENDING_ON)
    def test_with_the_connection_taken_off_the_next_one_stays_due(self):
        contact = self.late_lead()
        self.connect('link_reminder_1', 'link_reminder_2')
        self.play(at(2026, 10, 13))
        self.put('link_reminder_2', {'target': None})
        self.play(at(2026, 10, 15, 12, 0))
        self.assert_sent((at(2026, 10, 13), 'template', 'content_link_reminder_1'))
        row = self.open_row(contact)
        self.assertEqual((row.step, row.status), (2, 'due'))
        self.assertTrue(row.outcome_note.startswith('חסרה תבנית'))


# --- the table of the contract, from end to end --------------------------------------------------------------

@override_settings(**SENDING_ON)
class CadenceTests(AutomationCase):
    """
    Every kind of lead, a real contact, the switch on and every template
    connected: what leaves, through which door, at which moment — and what
    becomes of the lead when nobody answers.
    """

    def setUp(self):
        super().setUp()
        self.connect_all()

    def lead(self, *args, **fields):
        return super().lead(*args, **{'manychat_subscriber_id': '4242', **fields})

    def template(self, moment, key):
        return (moment, 'template', flow_of(key))

    def after(self, contact):
        fresh = self.fresh(contact)
        return fresh.lead_stage, fresh.lead_owner

    # --- got a link: a day, three days, a week -----------------------------------------------------------

    def test_got_a_link_a_day_three_days_and_a_week_later_and_then_dormant(self):
        contact = self.link_lead(3 * HOUR)                           # wrote Monday 9:00, answered with a link at 9:01
        self.play(at(2026, 10, 22, 10, 29))
        self.assert_sent(
            self.template(at(2026, 10, 13), 'link_reminder_1'),      # Tuesday, when the office opens
            self.template(at(2026, 10, 15), 'link_reminder_2'),      # Thursday
            self.template(at(2026, 10, 19), 'link_reminder_3'),      # the Monday after
        )
        self.assertEqual([row.status for row in self.rows(contact)], ['sent', 'sent', 'sent'])
        self.assertEqual(self.after(contact), ('link_sent', 'bot'))   # three days to answer the last one
        self.play(at(2026, 10, 30))
        self.assertEqual(self.after(contact), ('cold', ''))
        self.assertEqual(len(self.manychat.sent), 3)                 # and never a fourth

    def test_inside_whatsapps_24_hours_the_first_one_is_the_words_themselves(self):
        self.link_lead(HOUR)                                         # wrote Monday 11:00: Tuesday 10:30 is still inside
        self.play(at(2026, 10, 20))
        self.assert_sent(
            (at(2026, 10, 13), 'text', LINK_FIRST),
            self.template(at(2026, 10, 15), 'link_reminder_2'),
            self.template(at(2026, 10, 19), 'link_reminder_3'),
        )

    def test_began_to_register_and_did_not_finish(self):
        contact = self.staged(self.lead(4 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום ב-11.10 · נועה לוי'))
        self.assertEqual(self.after(contact), ('signup_started', 'bot'))
        self.play(at(2026, 10, 21, 10, 29))
        self.assert_sent(
            self.template(at(2026, 10, 12, 12, 0), 'link_reminder_signup_1'),   # the day after: today, now
            self.template(at(2026, 10, 14, 12, 0), 'link_reminder_2'),          # never sooner than two days after the one before
            self.template(at(2026, 10, 18), 'link_reminder_3'),                 # a week after: Sunday
        )
        self.assertIn('שהתחלתם להירשם', self.rows(contact)[0].text)
        self.assertEqual(self.after(contact), ('signup_started', 'bot'))
        self.play(at(2026, 10, 21, 10, 30))
        self.assertEqual(self.after(contact), ('signup_started', 'office'))     # a hot lead: a person calls

    def test_afraid_to_commit_with_a_link_gets_an_answer_to_that_first(self):
        self.link_lead(3 * HOUR, said={'blocker': 'commitment', 'blocker_text': 'שאלה מה אם לא יתאים'})
        self.play(at(2026, 10, 20))
        self.assert_sent(
            self.template(at(2026, 10, 13), 'link_reminder_commitment_1'),
            self.template(at(2026, 10, 15), 'link_reminder_2'),
            self.template(at(2026, 10, 19), 'link_reminder_3'),
        )

    def test_afraid_to_commit_with_no_link_is_answered_once_a_day_later(self):
        contact = self.staged(self.lead(3 * HOUR, said={'blocker': 'commitment', 'blocker_text': 'שאלה מה אם לא יתאים'}))
        self.assertEqual(self.after(contact), ('in_conversation', 'bot'))
        (row,) = self.rows(contact)
        self.assertEqual(row.text, 'היי לגבי מה ששאלתם 🙂 אפשר להתחיל משיעור ניסיון אחד ולראות אם זה מתאים. רוצים קישור?')
        self.play(at(2026, 10, 30))
        self.assert_sent(self.template(at(2026, 10, 13), 'link_reminder_commitment_1'))
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])       # once: no second, no third

    def test_afraid_to_commit_on_a_thursday_is_answered_on_sunday(self):
        self.clock.to(2026, 10, 15, 12, 0)                           # Thursday
        contact = self.staged(self.lead(3 * HOUR, said={'blocker': 'commitment', 'blocker_text': 'שאלה מה אם לא יתאים'}))
        self.play(at(2026, 10, 25))
        # By Sunday the conversation is "silent", no longer "talking": the follow-up stands all the same.
        self.assert_sent(self.template(at(2026, 10, 18), 'link_reminder_commitment_1'))
        self.assertEqual(self.rows(contact)[0].status, 'sent')

    # --- said they would check: once, and then the office ------------------------------------------------

    def test_said_they_would_check_once_and_then_it_is_a_phone_call(self):
        contact = self.staged(self.lead(3 * HOUR, said={'blocker': 'will_check', 'blocker_text': 'אמרה שתחשוב'}))
        self.assertEqual(self.after(contact), ('will_check', 'bot'))
        self.play(at(2026, 10, 16, 10, 29))
        self.assert_sent(self.template(at(2026, 10, 13), 'will_check_1'))
        self.assertEqual(self.after(contact), ('will_check', 'bot'))            # three days to answer
        self.play(at(2026, 10, 30))
        fresh = self.fresh(contact)
        self.assertEqual((fresh.lead_stage, fresh.lead_owner, fresh.lead_next_step), ('will_check', 'office', 'להתקשר למי שמחליט'))
        self.assertEqual(len(self.manychat.sent), 1)                 # once, by the owner's word

    # --- after a trial: first "how was it" ---------------------------------------------------------------

    def test_after_a_trial_first_how_was_it_then_the_link_then_a_call(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact)
        self.assertEqual(self.after(contact), ('trial_done', 'bot'))
        self.play(at(2026, 10, 21, 10, 29))
        self.assert_sent(
            self.template(at(2026, 10, 12, 12, 0), 'trial_feedback_1'),         # the day after the trial
            self.template(at(2026, 10, 14, 12, 0), 'trial_feedback_2'),         # three days after it
            self.template(at(2026, 10, 18), 'trial_feedback_3'),                # a week after it: Sunday
        )
        first, second, third = self.rows(contact)
        self.assertEqual(first.text, 'היי איך היה לנועה בשיעור הניסיון? 🙂')      # the child's name is Kogo's own
        self.assertNotIn('http', first.text)                         # never a link to pay in the first one
        self.assertIn('אפשר להירשם כאן\n🔗 https://', second.text)
        self.assertEqual(self.after(contact), ('trial_done', 'bot'))
        self.play(at(2026, 10, 21, 10, 30))
        fresh = self.fresh(contact)
        self.assertEqual((fresh.lead_stage, fresh.lead_owner, fresh.lead_next_step), ('trial_done', 'office', 'להתקשר: איך היה, ולהציע הרשמה'))

    def test_a_family_that_registered_after_the_trial_is_not_asked_how_it_was(self):
        from apps.enrollments.models import LessonEnrollment

        contact = self.lead(4 * DAY)
        child, lesson = self.family_with_a_trial(contact)
        self.play(at(2026, 10, 12, 12, 0))
        self.assertEqual(len(self.manychat.sent), 1)
        # They registered that evening. The registrations are read again before the next one — and it is called off.
        type(child).objects.filter(pk=child.pk).update(status='active')
        another_day = TestDataFactory.create_lesson(course=lesson.course, day_of_week=2)
        LessonEnrollment.objects.create(lesson=another_day, child=child, status='active')
        self.play(at(2026, 10, 30))
        self.assertEqual(len(self.manychat.sent), 1)
        self.assertEqual(self.after(contact), ('registered', ''))
        self.assertEqual((self.rows(contact)[1].status, self.rows(contact)[1].outcome_note), ('skipped', 'נרשם'))

    def test_did_not_come_to_the_trial_twice_and_then_a_call(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact, outcome='no_show')
        self.assertEqual(self.after(contact), ('trial_no_show', 'bot'))
        self.play(at(2026, 10, 30))
        self.assert_sent(
            self.template(at(2026, 10, 12, 12, 0), 'trial_no_show_1'),
            self.template(at(2026, 10, 14, 12, 0), 'trial_no_show_2'),
        )
        self.assertEqual(self.rows(contact)[0].text, 'היי ראיתי שלא יצא לכם להגיע לשיעור הניסיון, קורה 🙂 רוצים שנקבע מועד אחר?')
        self.assertEqual(self.after(contact), ('trial_no_show', 'office'))

    # --- a day to come back on ---------------------------------------------------------------------------

    def test_too_young_on_the_day_the_child_is_three_and_once_more_a_week_later(self):
        too_young = {
            'blocker': 'too_young', 'come_back': {'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'},
            'come_back_on': '2027-03-12', 'come_back_why': 'הילד יהיה בן 3',
        }
        contact = self.staged(self.lead(said=too_young, known_callback_on=date(2027, 3, 12)))
        self.assertEqual(self.after(contact), ('waiting_date', 'bot'))
        self.play(at(2027, 3, 13, 23, 0))
        self.assert_sent()                                           # five months, and not a word
        self.play(at(2027, 4, 15))
        self.assert_sent(
            self.template(at(2027, 3, 14), 'date_arrived_age_1'),    # 12.3.2027 is a Friday: Sunday morning
            self.template(at(2027, 3, 21), 'date_arrived_age_2'),    # a week later — and that is all
        )
        self.assertEqual(self.after(contact), ('cold', ''))

    def test_another_time_on_the_day_the_customer_named(self):
        contact = self.staged(self.lead(3 * HOUR, known_callback_on=date(2026, 10, 21)))
        self.assertEqual(self.after(contact), ('waiting_date', 'bot'))
        self.play(at(2026, 11, 10))
        self.assert_sent(
            self.template(at(2026, 10, 21), 'date_arrived_1'),       # the day itself, a Wednesday
            self.template(at(2026, 10, 25), 'date_arrived_2'),       # three days later is a Saturday: Sunday
            self.template(at(2026, 10, 28), 'date_arrived_3'),       # a week after the day
        )
        self.assertEqual(self.after(contact), ('cold', ''))

    def test_a_day_the_office_set_is_the_offices_and_the_bot_writes_nothing(self):
        contact = self.link_lead(3 * HOUR)
        leads.snooze(contact.id, '2026-10-21', 'אחרי החגים', self.manager)
        self.assertEqual(self.after(contact), ('waiting_date', ''))
        self.assertEqual((self.rows(contact)[0].status, self.rows(contact)[0].outcome_note), ('skipped', 'המצב השתנה: מחכה למועד'))
        self.play(at(2026, 10, 30))
        self.assert_sent()
        self.assertEqual(self.after(contact), ('callback_due', 'office'))

    def test_the_child_did_not_want_to_once_when_the_next_year_opens(self):
        contact = self.staged(self.lead(
            said={'blocker': 'child_not_ready', 'blocker_text': 'הוא לא רצה', 'come_back_on': '2027-09-01'},
            known_callback_on=date(2027, 9, 1),
        ))
        self.assertEqual(self.after(contact), ('waiting_date', 'bot'))
        self.play(at(2027, 9, 30))
        self.assert_sent(self.template(at(2027, 9, 1), 'date_arrived_not_ready_1'))
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])

    # --- an opening with nothing in it -------------------------------------------------------------------

    def test_an_empty_opening_is_asked_once_two_hours_later_in_words(self):
        contact = self.staged(self.lead(10 * MINUTE, said={'empty_opening': True}))
        self.play(at(2026, 10, 30))
        self.assert_sent((at(2026, 10, 12, 13, 50), 'text', OPENING_TEXT))       # inside the 24 hours: no template is needed
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])

    def test_an_empty_opening_before_the_weekend_is_asked_on_sunday(self):
        self.clock.to(2026, 10, 15, 17, 40)                          # Thursday evening
        contact = self.staged(self.lead(10 * MINUTE, said={'empty_opening': True}))
        self.assertEqual(self.open_row(contact).due_at, at(2026, 10, 18))        # two hours later the office is closed
        self.play(at(2026, 10, 30))
        # More than 24 hours by then: it leaves as its template.
        self.assert_sent(self.template(at(2026, 10, 18), 'opening_1'))

    def test_an_old_empty_opening_is_not_asked_days_later(self):
        contact = self.staged(self.lead(3 * DAY, said={'empty_opening': True}))
        self.assertEqual(self.rows(contact), [])
        self.play(at(2026, 10, 30))
        self.assert_sent()

    # --- only in the office hours ------------------------------------------------------------------------

    def test_never_on_a_friday_a_saturday_or_a_special_day(self):
        special_day('חג', date(2026, 10, 18))                        # the Sunday is a holiday
        self.clock.to(2026, 10, 15, 12, 0)                           # Thursday
        self.link_lead(3 * HOUR)
        self.play(at(2026, 10, 30))
        self.assert_sent(
            self.template(at(2026, 10, 19), 'link_reminder_1'),      # a day later is Friday; Sunday is the holiday: Monday
            self.template(at(2026, 10, 21), 'link_reminder_2'),      # two days after the one before
            self.template(at(2026, 10, 25), 'link_reminder_3'),      # a week after is Thursday 22.10 — but two days after Wednesday
        )

    def test_a_special_day_filed_after_the_follow_up_was_planned_still_holds_it(self):
        contact = self.link_lead(3 * HOUR)
        self.assertEqual(self.open_row(contact).due_at, at(2026, 10, 13))
        special_day('יום סגור', date(2026, 10, 13))
        self.play(at(2026, 10, 14))
        self.assert_sent(self.template(at(2026, 10, 14), 'link_reminder_1'))

    def test_the_offices_own_hours_are_the_hours(self):
        week = {day: {'open': True, 'from': '09:00', 'to': '13:00'} for day in ('sun', 'mon', 'tue', 'wed', 'thu')}
        week.update(fri={'open': True, 'from': '09:00', 'to': '12:00'}, sat={'open': False})
        knowledge.create_item({'kind': 'office_hours', 'title': 'שעות', 'weekly': week})
        self.clock.to(2026, 10, 15, 12, 0)                           # Thursday
        self.link_lead(5 * HOUR)
        self.play(at(2026, 10, 19))
        self.assert_sent(self.template(at(2026, 10, 18, 9, 0), 'link_reminder_1'))      # not Friday, whatever the office says

    # --- what calls a follow-up off ----------------------------------------------------------------------

    def test_the_customer_answering_calls_it_off(self):
        contact = self.link_lead(3 * HOUR)
        self.play(at(2026, 10, 13))
        self.clock.forward(hours=3)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='תודה, נרשמתי לבד בסוף? לא בטוחה')
        second = self.rows(contact)[1]
        self.assertEqual((second.step, second.status, second.outcome_note), (2, 'skipped', 'הלקוח כתב בינתיים'))
        self.play(at(2026, 10, 13, 17, 0))
        self.assertEqual(len(self.manychat.sent), 1)

    def test_no_to_the_last_reminder_is_the_last_word(self):
        """The third reminder says "תכתבו לי לא ואני מפסיקה" — and that is kept."""
        contact = self.link_lead(3 * HOUR)
        self.play(at(2026, 10, 19))
        self.assertEqual(len(self.manychat.sent), 3)
        self.assertIn('תכתבו לי לא ואני מפסיקה', self.rows(contact)[2].text)
        self.clock.forward(hours=2)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='לא')
        self.clock.forward(minutes=6)
        analysis.analyze_contact(self.fresh(contact), use_ai=False)              # the summary, five quiet minutes later
        self.play(at(2026, 12, 15))
        fresh = self.fresh(contact)
        self.assertEqual((fresh.lead_stage, fresh.lead_owner), ('not_interested', ''))
        self.assertIn('כתב: "לא"', fresh.lead_reason)
        self.assertEqual(len(self.manychat.sent), 3)                 # not one more, ever
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact).count(), 3)

    def test_no_to_the_last_reminder_starts_no_new_round_while_kogo_does_not_see_the_old_bots_answers(self):
        """
        As production stands today: ManyChat copies no reply of the old bot, so
        a customer's message never reads as "waiting for an answer" — and two
        quiet days after it a new round of three reminders used to begin.
        """
        with patch.object(lead_stage, 'replies_copied_since', return_value=None):
            contact = self.link_lead(3 * HOUR)
            self.play(at(2026, 10, 19))
            self.clock.forward(hours=2)
            inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='לא')
            self.clock.forward(minutes=6)
            analysis.analyze_contact(self.fresh(contact), use_ai=False)
            self.play(at(2026, 11, 20))
        self.assertEqual(self.after(contact), ('not_interested', ''))
        self.assertEqual(len(self.manychat.sent), 3)

    def test_no_to_any_other_reminder_is_an_answer_for_a_person_to_read(self):
        contact = self.link_lead(3 * HOUR)
        self.play(at(2026, 10, 13))                                  # "רציתי לבדוק אם הצלחתם להירשם…"
        self.clock.forward(hours=2)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='לא')
        self.clock.forward(minutes=6)
        analysis.analyze_contact(self.fresh(contact), use_ai=False)
        self.play(at(2026, 10, 14, 9, 0))
        self.assertEqual(self.after(contact), ('needs_reply', 'office'))        # nobody answered them yet: the office's
        self.assertEqual(len(self.manychat.sent), 1)

    def test_registering_calls_it_off_at_the_last_moment(self):
        contact = self.link_lead(3 * HOUR)

        def registered(row):
            Contact.objects.filter(pk=row.pk).update(kogo_outcome='registered_after', kogo_detail='נרשם ב-12.10 · נועה לוי')
            return True

        self.recheck.side_effect = registered
        self.play(at(2026, 10, 30))
        self.assert_sent()
        self.assertEqual(self.after(contact), ('registered', ''))
        self.assertEqual([(row.status, row.outcome_note) for row in self.rows(contact)], [('skipped', 'נרשם')])

    def test_a_person_closing_taking_or_answering_calls_it_off(self):
        closed, taken, answered, said_no = (self.link_lead(3 * HOUR) for _ in range(4))
        leads.close(closed.id, 'deleted', '', self.manager)
        leads.set_owner(taken.id, 'office', self.manager)
        Contact.objects.filter(pk=answered.pk).update(handled_by='human')
        lead_stage.refresh(answered)
        Contact.objects.filter(pk=said_no.pk).update(
            lead_ai={'intent': 'class', 'interest': 'no', 'quote': 'תודה, לא רלוונטי', 'quote_at': self.clock().isoformat()},
            analyzed_at=self.clock(),
        )
        lead_stage.refresh(said_no)
        self.assertEqual(
            [self.rows(contact)[0].outcome_note for contact in (closed, taken, answered, said_no)],
            ['הליד נסגר: נמחק', 'עבר לטיפול המשרד', 'עבר לטיפול המשרד', 'הליד נסגר: לא מעוניין'],
        )
        self.play(at(2026, 10, 30))
        self.assert_sent()
        self.assertEqual(LeadFollowUp.objects.filter(status__in=('planned', 'due')).count(), 0)

    def test_leads_the_bot_does_not_come_back_to(self):
        link = dict(lead_link_sent_at=self.clock() - 3 * HOUR)
        for fields in (
            dict(said={'blocker': 'price', 'blocker_text': 'יקר לה'}, **link),             # nothing new to tell them
            dict(said={'blocker': 'schedule', 'blocker_text': 'הימים לא מתאימים'}, **link),
            dict(said={'blocker': 'no_branch', 'blocker_text': 'אין סניף קרוב'}, **link),  # kept for the day something opens
            dict(said={'blocker': 'full', 'blocker_text': 'החוג מלא'}),
            dict(known_flags=['complaint'], **link),                                       # an open complaint
            dict(kogo_outcome='customer_before'),                                          # a customer, not a lead
            dict(kogo_outcome='trial_upcoming'),                                           # the trial is still ahead
        ):
            # (An external branch and a lead that went quiet are each asked once: test_leads_no_one_lost.py.)
            self.staged(self.lead(3 * HOUR, **fields))
        self.play(at(2026, 11, 30))
        self.assert_sent()
        self.assertEqual(LeadFollowUp.objects.count(), 0)


# --- the one bare "לא" that is a goodbye -------------------------------------------------------------------

class StopWordTests(LeadsCase):
    LAST = lead_followup_seed.LAST_TIME

    def lines(self, *after, before='bot'):
        return (
            said('אפשר פרטים על קפוארה?', when=START - 9 * DAY)
            + said(self.LAST, who=before, when=START - HOUR)
            + [line for text in after for line in said(text, when=START)]
        )

    def by_rules(self, lines):
        return lead_ai.from_rules(lines, Card(), today=TODAY)

    def by_model(self, lines, **fields):
        return lead_ai.from_model(model_lead(**fields), lines=lines, known=Card(), today=TODAY)

    def test_no_to_our_own_write_no_and_i_will_stop_closes_the_lead(self):
        for answer in ('לא', 'לא.', ' לא!! ', "'לא'", 'לא 🙏'):
            with self.subTest(answer=answer):
                for lead in (self.by_rules(self.lines(answer)), self.by_model(self.lines(answer))):
                    self.assertEqual((lead['interest'], lead['quote'], lead['quote_at']), ('no', answer.strip(), START.isoformat()))
        # Whoever of us wrote the reminder — and a word of thanks before the "לא" changes nothing.
        self.assertEqual(self.by_rules(self.lines('לא', before='office'))['interest'], 'no')
        self.assertEqual(self.by_rules(self.lines('תודה', 'לא'))['interest'], 'no')

    def test_a_bare_no_anywhere_else_closes_nothing(self):
        asked = said('האם אתם מגיעים מחר?', who='bot', when=START - HOUR) + said('לא', when=START)
        wrong = said('השיעור ביום שני', who='bot', when=START - HOUR) + said('לא!!', when=START)
        moved_on = self.lines('לא', 'בעצם כן, מה המחיר?')
        answered_since = self.lines('לא') + said('בסדר גמור, לא נכתוב יותר', who='bot', when=START) + said('רגע, יש מחול?', when=START)
        for lines in (asked, wrong, moved_on, answered_since, said('לא')):
            with self.subTest(last=lines[-1].text):
                self.assertNotEqual(self.by_rules(lines)['interest'], 'no')
                self.assertNotEqual(self.by_model(lines)['interest'], 'no')
        # What the reader itself understood stands: "we already signed up" is not turned into a "no".
        registered = self.by_model(self.lines('לא') + said('בסוף נרשמנו אצלכם', when=START), interest='registered', quote='בסוף נרשמנו אצלכם')
        self.assertEqual(registered['interest'], 'registered')

    def test_asking_that_the_messages_stop_closes_the_lead_by_the_rules_too(self):
        for text in ('תפסיקו לשלוח לי הודעות', 'בבקשה להפסיק לשלוח', 'נא לא לשלוח לי יותר', 'אל תכתבו לי', 'די לשלוח!'):
            with self.subTest(text=text):
                lead = self.by_rules(said('אפשר פרטים על קפוארה?', when=START - 9 * DAY) + said(text))
                self.assertEqual((lead['interest'], lead['quote']), ('no', text))
        # A question about something stopping is not a request to stop.
        self.assertNotEqual(self.by_rules(said('מתי תפסיקו לשלוח את הילדים הביתה לבד?'))['interest'], 'no')

    def test_every_last_reminder_the_bot_sends_asks_for_that_very_word(self):
        """The promise and the rule that keeps it must not drift apart: the filed texts are checked against the rule."""
        last = {key: draft['texts'][-1] for key, draft in lead_followup_seed.DEFAULTS.items() if 'פעם אחרונה' in draft['texts'][-1]}
        self.assertEqual(set(last), {'lead_link_reminder', 'lead_date_arrived'})
        for text in last.values():
            self.assertEqual(text, lead_followup_seed.LAST_TIME)
            self.assertTrue(lead_ai.P_INVITED_NO.search(text), text)
        # No other text of the bot's reads as that request.
        others = [text for draft in lead_followup_seed.DEFAULTS.values() for text in draft['texts'] if text != lead_followup_seed.LAST_TIME]
        self.assertEqual([text for text in others if lead_ai.P_INVITED_NO.search(text)], [])

    def test_the_last_reminder_as_it_was_first_filed_is_kept_the_same_way(self):
        """A row in production may still carry the first wording — or the owner's own edit of it."""
        first_filed = "היי, פעם אחרונה שאני מזכירה 🙂 אם זה כבר לא רלוונטי — כתבו 'לא' ולא אשלח יותר. ואם כן, אני כאן."
        for ours in (first_filed, 'אם זה לא מתאים השיבו לא ונפסיק לכתוב'):
            lines = said('אפשר פרטים?', when=START - 9 * DAY) + said(ours, who='bot', when=START - HOUR) + said('לא')
            self.assertEqual(self.by_rules(lines)['interest'], 'no', ours)


# --- an automation under the follow-up's own name is connected by itself --------------------------------

@override_settings(MANYCHAT_KEY='test-key')
class ByNameTests(AutomationCase):
    def listed(self) -> dict:
        """The rows of the templates list, by key."""
        return {row['template_key']: row for row in self.get('leads/templates/').data}

    def test_an_automation_named_as_the_follow_up_is_named_needs_nobody_to_connect_it(self):
        self.manychat.named()
        rows = self.listed()
        for key, name in lead_templates.FLOW_NAMES.items():
            with self.subTest(key=key):
                row = rows[key]
                self.assertEqual((row['mapped'], row['target'], row['target_name'], row['by']), (True, f'auto_{name}', name, 'name'))
                self.assertEqual((row['connected_by'], row['connected_at'], row['flow_name']), ('', None, name))
        self.assertEqual((rows['dormant_1']['mapped'], rows['dormant_1']['by']), (False, None))     # it has no name to be found by
        # One automation serves the two last reminders.
        self.assertEqual(rows['link_reminder_3']['target'], rows['date_arrived_3']['target'])
        templates = self.line('templates')
        self.assertEqual((templates['ok'], templates['detail']), (True, '5 מתוך 5 של שלב 1 מחוברות · שלב 2: 15 מתוך 15'))
        self.assertEqual(settings_store.lead_templates(), {})        # nothing was written as a person's decision

    def test_only_some_are_there_yet(self):
        self.manychat.named('link_reminder_1', 'trial_feedback_1')
        mapped = {key for key, row in self.listed().items() if row['mapped']}
        self.assertEqual(mapped, {'link_reminder_1', 'trial_feedback_1'})
        self.assertEqual(self.line('templates')['detail'], '2 מתוך 5 של שלב 1 מחוברות')

    def test_capitals_hyphens_and_spaces_in_the_name_do_not_matter(self):
        for spell in (str.upper, lambda name: name.replace('_', '-'), lambda name: f'  {name.replace("_", " ").title()} '):
            with self.subTest(spelled=spell('kogo_lead_link_1')):
                settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {})
                self.manychat.named('link_reminder_1', spell=spell)
                row = self.listed()['link_reminder_1']
                self.assertEqual((row['mapped'], row['target'], row['by']), (True, 'auto_kogo_lead_link_1', 'name'))
        # A name that only begins the same way is another name.
        settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {})
        self.manychat.flows = [{'ns': 'content_other', 'name': 'kogo_lead_link_10'}]
        self.assertFalse(self.listed()['link_reminder_1']['mapped'])

    def test_two_automations_under_one_name_are_nobodys(self):
        self.manychat.named('link_reminder_1', 'will_check_1')
        self.manychat.flows.append({'ns': 'content_copy', 'name': 'Kogo-Lead-Link-1'})
        with self.assertLogs('apps.wahub.lead_templates', level='WARNING'):
            rows = self.listed()
        self.assertEqual((rows['link_reminder_1']['mapped'], rows['will_check_1']['mapped']), (False, True))
        # A person choosing one of the two settles it.
        chosen = self.put('link_reminder_1', {'target': 'content_copy'}).data
        self.assertEqual((chosen['mapped'], chosen['target'], chosen['by']), (True, 'content_copy', 'manual'))

    def test_what_a_person_decided_on_the_screen_stands_above_the_name(self):
        self.manychat.named()
        self.manychat.flows.append({'ns': 'content_mine', 'name': 'הנוסח שלי'})
        mine = self.put('link_reminder_1', {'target': 'הנוסח שלי'}).data
        self.assertEqual((mine['target'], mine['target_name'], mine['by'], mine['connected_by']), ('content_mine', 'הנוסח שלי', 'manual', 'דנה מנהלת'))
        self.assertEqual(lead_templates.flow_for('link_reminder_1'), 'content_mine')
        # Taking the connection off is a decision too: the automation that carries the name does not come back by itself.
        off = self.put('will_check_1', {'target': None}).data
        self.assertEqual((off['mapped'], off['target'], off['by']), (False, None, None))
        self.assertEqual(lead_templates.flow_for('will_check_1'), '')
        self.clock.forward(hours=3)
        self.assertFalse(self.listed()['will_check_1']['mapped'])
        self.assertEqual(self.line('templates')['detail'], '4 מתוך 5 של שלב 1 מחוברות')
        # ... until a person connects one again.
        back = self.put('will_check_1', {'target': 'kogo_lead_check_1'}).data
        self.assertEqual((back['mapped'], back['target'], back['by']), (True, 'auto_kogo_lead_check_1', 'manual'))
        # The rest are still by their names.
        self.assertEqual(self.listed()['trial_feedback_1']['by'], 'name')

    def test_manychat_is_asked_once_in_ten_minutes_however_often_the_screen_looks(self):
        self.manychat.named('link_reminder_1')
        for _ in range(3):
            self.listed()
            self.card()
        self.assertEqual(self.manychat.lists, 1)
        self.clock.forward(minutes=9)
        self.card()
        self.assertEqual(self.manychat.lists, 1)
        # An automation made meanwhile is seen on the first look after the ten minutes.
        self.manychat.named('link_reminder_1', 'will_check_1')
        self.assertFalse(self.listed()['will_check_1']['mapped'])
        self.clock.forward(minutes=1)
        self.assertTrue(self.listed()['will_check_1']['mapped'])
        self.assertEqual(self.manychat.lists, 2)

    def test_the_list_of_options_brings_the_names_up_to_date_at_once(self):
        self.listed()                                                  # ManyChat has nothing under the names yet
        self.manychat.named('link_reminder_1')
        self.assertFalse(self.listed()['link_reminder_1']['mapped'])   # the kept answer is still fresh
        self.get('leads/templates/options/')
        self.assertTrue(self.listed()['link_reminder_1']['mapped'])
        self.assertEqual(self.manychat.lists, 2)

    def test_when_manychat_does_not_answer_the_last_answer_stands(self):
        self.manychat.down = True
        self.assertFalse(self.listed()['link_reminder_1']['mapped'])   # never found, and nobody to ask
        self.assertEqual(self.manychat.lists, 1)
        self.listed()
        self.assertEqual(self.manychat.lists, 1)                     # not asked again on every look
        self.manychat.down = False
        self.manychat.named('link_reminder_1')
        self.clock.forward(minutes=10)
        self.assertTrue(self.listed()['link_reminder_1']['mapped'])
        self.manychat.down = True
        self.clock.forward(minutes=30)
        self.assertTrue(self.listed()['link_reminder_1']['mapped'])    # ManyChat is down: what it last said stands
        self.assertEqual(lead_templates.flow_for('link_reminder_1'), 'auto_kogo_lead_link_1')

    def test_an_automation_that_was_removed_or_renamed_is_no_longer_connected(self):
        self.manychat.named('link_reminder_1')
        self.assertTrue(self.listed()['link_reminder_1']['mapped'])
        self.manychat.flows = [{'ns': 'auto_kogo_lead_link_1', 'name': 'ישן - לא בשימוש'}]
        self.clock.forward(minutes=10)
        self.assertFalse(self.listed()['link_reminder_1']['mapped'])
        self.assertEqual(lead_templates.flow_for('link_reminder_1'), '')

    def test_with_no_manychat_to_ask_nobody_is_asked(self):
        self.manychat.named()
        with override_settings(MANYCHAT_KEY=''):
            self.assertFalse(self.listed()['link_reminder_1']['mapped'])
        with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True):
            self.assertFalse(self.listed()['link_reminder_1']['mapped'])
        self.assertEqual(self.manychat.asked, [])
        # With every follow-up decided by a person there is nothing left to look for.
        self.connect_all()
        asked = self.manychat.lists
        self.clock.forward(hours=1)
        self.listed()
        self.card()
        self.assertEqual(self.manychat.lists, asked)

    @override_settings(**SENDING_ON)
    def test_the_bot_sends_through_the_automation_found_by_name_and_asks_manychat_once_for_a_whole_run(self):
        self.manychat.named()
        leads_ = [self.link_lead(3 * HOUR) for _ in range(3)]        # tomorrow 10:30 is outside the 24 hours
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_sent'], 3)
        self.assertEqual(self.manychat.sent, [(at(2026, 10, 13), 'template', 'auto_kogo_lead_link_1')] * 3)
        self.assertEqual(self.manychat.lists, 1)
        self.assertEqual({self.rows(contact)[0].status for contact in leads_}, {'sent'})

    @override_settings(**SENDING_ON)
    def test_a_whole_round_leaves_by_the_names_alone(self):
        self.manychat.named()
        self.link_lead(3 * HOUR)
        self.play(at(2026, 10, 20))
        self.assert_sent(
            (at(2026, 10, 13), 'template', 'auto_kogo_lead_link_1'),
            (at(2026, 10, 15), 'template', 'auto_kogo_lead_link_2'),
            (at(2026, 10, 19), 'template', 'auto_kogo_lead_last'),
        )
        self.assertEqual(settings_store.lead_templates(), {})

    @override_settings(**SENDING_ON)
    def test_a_follow_up_whose_automation_is_not_there_yet_waits_and_leaves_when_it_is_made(self):
        contact = self.link_lead(3 * HOUR)
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_due'], 1)
        self.assertTrue(self.rows(contact)[0].outcome_note.startswith('חסרה תבנית'))
        banner = self.card()['banner']
        self.assertEqual((banner['count'], banner['reason']), (1, 'template'))
        self.manychat.named('link_reminder_1')                       # the automation is made in ManyChat, under its name
        self.clock.forward(minutes=30)                               # the next look at a follow-up that is due
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assert_sent((at(2026, 10, 13, 11, 0), 'template', 'auto_kogo_lead_link_1'))
        self.assertIsNone(self.card()['banner'])


# --- nobody is asked the same thing twice: the system's own automations ----------------------------------

@override_settings(**SENDING_ON)
class AlreadySaidTests(AutomationCase):
    """
    apps/enrollments already writes to a trial family by itself: `after-test`,
    two hours after the lesson, and `didnt_arrive`, when a child is marked
    absent time after time. Where one of them went out, the bot's first
    message of the same round does not.
    """

    def setUp(self):
        super().setUp()
        self.connect_all()

    def lead(self, *args, **fields):
        return super().lead(*args, **{'manychat_subscriber_id': '4242', **fields})

    def template(self, moment, key):
        return (moment, 'template', flow_of(key))

    def test_the_registrations_say_what_the_family_was_already_told(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact)
        self.assertEqual(self.fresh(contact).kogo_trial, {'on': '2026-10-11', 'outcome': 'attended', 'course': 'קפוארה גילאי 5–7'})
        type(self.trial).objects.filter(pk=self.trial.pk).update(trial_followup_reminder_sent_at=self.clock())
        self.assertTrue(matching.recheck_contact(self.fresh(contact)))                     # a change the stage hears about
        self.assertEqual(self.fresh(contact).kogo_trial, {
            'on': '2026-10-11', 'outcome': 'attended', 'course': 'קפוארה גילאי 5–7', 'after_test_sent': True,
        })
        type(self.trial).objects.filter(pk=self.trial.pk).update(didnt_arrive_whatsapp_sent_at=self.clock())
        matching.recheck_contact(self.fresh(contact))
        self.assertIs(self.fresh(contact).kogo_trial['didnt_arrive_sent'], True)

    def test_the_rules_have_the_round_begin_from_its_second_step(self):
        yesterday = (TODAY - timedelta(days=1)).isoformat()

        def follow(**trial):
            contact = self.lead(4 * DAY, kogo_outcome='trial_only', kogo_trial={'on': yesterday, 'course': '', **trial})
            return self.stage(contact)['follow']

        self.assertEqual(follow(outcome='attended')['from_step'], 1)
        self.assertEqual(follow(outcome='attended', after_test_sent=True)['from_step'], 2)
        self.assertEqual(follow(outcome='', after_test_sent=True)['from_step'], 2)                # not marked: asked all the same
        # Not coming is another matter: the message after the lesson does not stand for it, the one about absences does.
        self.assertEqual(follow(outcome='no_show', after_test_sent=True)['from_step'], 1)
        self.assertEqual(follow(outcome='no_show', didnt_arrive_sent=True)['from_step'], 2)
        self.assertEqual(follow(outcome='attended', didnt_arrive_sent=True)['from_step'], 1)
        # Every other round begins from its first step.
        self.assertEqual(self.stage(self.link_lead())['follow']['from_step'], 1)

    def test_after_the_systems_own_message_the_bot_does_not_ask_how_it_was_again(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact, trial_followup_reminder_sent_at=at(2026, 10, 11, 19, 0))
        self.assertEqual(self.after_(contact), ('trial_done', 'bot'))
        (row,) = self.rows(contact)
        self.assertEqual((row.step, row.template_key, row.due_at), (2, 'trial_feedback_2', at(2026, 10, 14)))
        self.assertEqual(self.get(f'leads/{contact.id}/').data['next']['what'], 'הבוט ישלח את הקישור להרשמה')    # not "how was it"
        self.play(at(2026, 10, 21, 10, 29))
        self.assert_sent(
            self.template(at(2026, 10, 14), 'trial_feedback_2'),     # three days after the trial — the link, not "how was it"
            self.template(at(2026, 10, 18), 'trial_feedback_3'),     # a week after it
        )
        self.assertEqual([row.step for row in self.rows(contact)], [2, 3])
        self.assertEqual(self.after_(contact), ('trial_done', 'bot'))            # three days to answer the last one
        self.assertIn('הבוט חזר אליו פעמיים', self.fresh(contact).lead_reason)
        self.play(at(2026, 10, 21, 10, 30))
        self.assertEqual(self.after_(contact), ('trial_done', 'office'))

    def after_(self, contact):
        fresh = self.fresh(contact)
        return fresh.lead_stage, fresh.lead_owner

    def test_a_first_step_already_planned_is_passed_over_when_the_systems_message_goes_out(self):
        """
        A late lesson: `after-test` leaves only the next morning, after the bot
        has already planned its own "how was it" for 10:30.
        """
        self.clock.to(2026, 10, 12, 7, 5)
        contact = self.lead(3 * DAY)
        self.family_with_a_trial(contact)
        (planned,) = self.rows(contact)
        self.assertEqual((planned.step, planned.status, planned.due_at), (1, 'planned', at(2026, 10, 12)))
        # 7:10: the cron of apps/enrollments sends `after-test` and notes it on the enrollment.
        type(self.trial).objects.filter(pk=self.trial.pk).update(trial_followup_reminder_sent_at=at(2026, 10, 12, 7, 10))
        # The bot's own moment comes; the registrations are read again first, as before every send.
        self.play(at(2026, 10, 12))
        self.assert_sent()
        first, second = self.rows(contact)
        self.assertEqual((first.status, first.outcome_note),
                         ('skipped', 'ההורה כבר קיבל את ההודעה האוטומטית שאחרי שיעור הניסיון — הבוט לא שואל שוב'))
        self.assertEqual((second.step, second.status, second.due_at), (2, 'planned', at(2026, 10, 14)))
        round_ = self.fresh(contact).lead_bot
        self.assertEqual((round_['step'], round_['passed'], round_['attempts'], round_['done']), (2, 1, 0, False))
        self.assertEqual(self.after_(contact), ('trial_done', 'bot'))            # not handed to the office: nothing failed
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_followup').latest('id').text,
                         'אחרי שיעור ניסיון, פנייה 1: בוטלה — ההורה כבר קיבל את ההודעה האוטומטית שאחרי שיעור הניסיון — הבוט לא שואל שוב')
        self.play(at(2026, 10, 21, 10, 29))
        self.assert_sent(
            self.template(at(2026, 10, 14), 'trial_feedback_2'),     # three days after the trial
            self.template(at(2026, 10, 18), 'trial_feedback_3'),     # a week after it
        )
        self.assertEqual(self.after_(contact), ('trial_done', 'bot'))
        self.play(at(2026, 10, 21, 10, 30))
        self.assertEqual(self.after_(contact), ('trial_done', 'office'))

    def test_the_stage_passes_the_step_over_as_soon_as_the_registrations_are_matched_again(self):
        self.clock.to(2026, 10, 12, 7, 5)
        contact = self.lead(3 * DAY)
        self.family_with_a_trial(contact)
        type(self.trial).objects.filter(pk=self.trial.pk).update(trial_followup_reminder_sent_at=at(2026, 10, 12, 7, 10))
        self.clock.to(2026, 10, 12, 7, 15)
        matching.recheck_contact(self.fresh(contact))                # the cron's own match
        lead_stage.scan()
        first, second = self.rows(contact)
        self.assertEqual((first.step, first.status, second.step, second.status), (1, 'skipped', 2, 'planned'))
        # The screen already says when the bot comes back: in three days, not this morning.
        self.assertEqual(datetime.fromisoformat(self.get(f'leads/{contact.id}/').data['next']['when']), at(2026, 10, 14))
        lead_stage.scan()                                            # and another look changes nothing more
        self.assertEqual(len(self.rows(contact)), 2)

    def test_without_the_systems_message_the_bot_asks_how_it_was(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact)
        self.play(at(2026, 10, 12, 12, 0))
        self.assert_sent(self.template(at(2026, 10, 12, 12, 0), 'trial_feedback_1'))

    def test_a_family_already_told_they_did_not_come_is_not_told_again(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact, outcome='no_show', didnt_arrive_whatsapp_sent_at=at(2026, 10, 11, 19, 0))
        self.assertEqual(self.after_(contact), ('trial_no_show', 'bot'))
        self.play(at(2026, 10, 30))
        self.assert_sent(self.template(at(2026, 10, 14), 'trial_no_show_2'))     # only "there are other days", three days after
        self.assertEqual([row.step for row in self.rows(contact)], [2])
        self.assertEqual(self.after_(contact), ('trial_no_show', 'office'))

    def test_the_message_after_the_lesson_does_not_stand_for_you_did_not_come(self):
        contact = self.lead(4 * DAY)
        self.family_with_a_trial(contact, outcome='no_show', trial_followup_reminder_sent_at=at(2026, 10, 11, 19, 0))
        self.play(at(2026, 10, 12, 12, 0))
        self.assert_sent(self.template(at(2026, 10, 12, 12, 0), 'trial_no_show_1'))


# --- the words already filed are brought to the new ones, unless a person edited them --------------------

class SeedRevisionTests(LeadsCase):
    FIRST_FILED = 'היי, כאן קוגומלו 🙂 שלחתי קישור ל{חוג}[ ב{סניף}]. הצלחתם להירשם, או שמשהו נתקע באתר?'

    def filed_with_the_first_words(self):
        """The rows as the deploy of 11.10 left them: version 1, the first wording."""
        from apps.wahub.models import KnowledgeItem

        lead_followup_seed.run()
        for item in KnowledgeItem.objects.filter(kind='phrasing', key__startswith='lead_'):
            data = {**item.data, 'variants': [f'היי, {text}' for text in item.data.get('variants') or []]}
            KnowledgeItem.objects.filter(pk=item.pk).update(body=f'היי, כאן קוגומלו 🙂 {item.body}', data=data)
        KnowledgeItem.objects.filter(key='lead_link_reminder').update(body=self.FIRST_FILED)
        return {item.key: item for item in KnowledgeItem.objects.filter(kind='phrasing', key__startswith='lead_')}

    def test_rows_nobody_edited_get_the_new_words_once(self):
        from apps.wahub.models import KnowledgeHistory, KnowledgeItem

        rows = self.filed_with_the_first_words()
        result = lead_followup_seed.run()
        self.assertEqual(result, {'created': 0, 'skipped': len(rows), 'revised': len(rows), 'revision_left': [], 'reworded': 0})
        for key, draft in lead_followup_seed.DEFAULTS.items():
            item = KnowledgeItem.objects.get(key=key)
            with self.subTest(key=key):
                self.assertEqual([item.body, *item.data['variants']], draft['texts'])
                self.assertEqual((item.version, item.is_active, item.title), (2, draft['active'], draft['title']))
                last = KnowledgeHistory.objects.filter(item=item).latest('version')
                self.assertEqual((last.note, last.changed_by), (lead_followup_seed.REVISED, None))
        self.assertEqual(lead_followups.phrasing('link_reminder', 1)[0], lead_followup_seed.DEFAULTS['lead_link_reminder']['texts'][0])
        # The next deploy finds nothing to do.
        self.assertEqual(lead_followup_seed.run(), {'created': 0, 'skipped': len(rows), 'revised': 0, 'revision_left': [], 'reworded': 0})
        self.assertEqual(KnowledgeItem.objects.get(key='lead_link_reminder').version, 2)

    def test_follow_ups_already_planned_with_the_first_words_say_the_new_ones(self):
        self.filed_with_the_first_words()
        waiting = self.staged(self.lead(HOUR, lead_link_sent_at=self.clock() - HOUR))
        acting = self.staged(self.lead(HOUR, lead_link_sent_at=self.clock() - HOUR))
        gone = self.staged(self.lead(HOUR, lead_link_sent_at=self.clock() - HOUR))
        planned = LeadFollowUp.objects.get(contact=waiting)
        self.assertEqual(planned.text, 'היי, כאן קוגומלו 🙂 שלחתי קישור לקפוארה בראש העין. הצלחתם להירשם, או שמשהו נתקע באתר?')
        LeadFollowUp.objects.filter(contact=acting).update(claimed_at=self.clock())          # a cron call holds it this moment
        LeadFollowUp.objects.filter(contact=gone).update(status='sent')                      # history is history
        result = lead_followup_seed.run()
        self.assertEqual(result['reworded'], 1)
        new = 'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם לקפוארה בראש העין או שמשהו נתקע בדרך'
        self.assertEqual(LeadFollowUp.objects.get(contact=waiting).text, new)
        self.assertTrue(LeadFollowUp.objects.get(contact=acting).text.startswith('היי, כאן קוגומלו'))
        self.assertTrue(LeadFollowUp.objects.get(contact=gone).text.startswith('היי, כאן קוגומלו'))
        self.assertEqual(self.get('leads/automation/').data['upcoming'][0]['text'], new)       # what the screen shows
        self.assertEqual(lead_followup_seed.run()['reworded'], 0)

    def test_what_a_person_edited_or_turned_off_is_left_as_it_is(self):
        from apps.wahub.models import KnowledgeItem

        rows = self.filed_with_the_first_words()
        knowledge.update_item(rows['lead_will_check'], {'body': 'היי, יש חדש לגבי {חוג}?'}, self.manager)
        knowledge.soft_delete(rows['lead_opening'], self.manager)
        result = lead_followup_seed.run()
        self.assertEqual(result['revised'], len(rows) - 2)
        self.assertEqual(sorted(result['revision_left']), ['חזרה לליד: אמר שיבדוק ויחזור', 'חזרה לליד: פנה ולא כתב מה הוא מחפש'])
        self.assertEqual(KnowledgeItem.objects.get(key='lead_will_check').body, 'היי, יש חדש לגבי {חוג}?')
        opening = KnowledgeItem.objects.get(key='lead_opening')
        self.assertEqual((opening.is_active, opening.version), (False, 2))
        self.assertTrue(opening.body.startswith('היי, כאן קוגומלו'))
        # Left again on every deploy, and said so every time — it is the owner's to look at.
        self.assertEqual(len(lead_followup_seed.run()['revision_left']), 2)

    def test_a_row_only_this_file_has_rewritten_before_counts_as_untouched(self):
        from apps.wahub.models import KnowledgeItem

        rows = self.filed_with_the_first_words()
        earlier = 'נוסח קודם של אותו קובץ'
        knowledge.update_item(rows['lead_opening'], {'body': 'היי, נוסח ביניים'}, None, note=earlier)
        with patch.object(lead_followup_seed, 'REVISED_BEFORE', ()):
            self.assertIn('חזרה לליד: פנה ולא כתב מה הוא מחפש', lead_followup_seed.run(dry_run=False)['revision_left'])
        KnowledgeItem.objects.filter(key='lead_opening').update(body='היי, נוסח ביניים')
        with patch.object(lead_followup_seed, 'REVISED_BEFORE', (earlier,)):
            lead_followup_seed.run()
        item = KnowledgeItem.objects.get(key='lead_opening')
        self.assertEqual((item.body, item.version), (lead_followup_seed.DEFAULTS['lead_opening']['texts'][0], 3))

    def test_a_dry_run_changes_nothing_and_the_deploy_command_says_what_it_revised(self):
        from io import StringIO

        from django.core.management import call_command

        from apps.wahub.models import KnowledgeItem

        rows = self.filed_with_the_first_words()
        knowledge.update_item(rows['lead_will_check'], {'body': 'שלי'}, self.manager)
        lead_followup_seed.run(dry_run=True)
        self.assertEqual(KnowledgeItem.objects.get(key='lead_link_reminder').body, self.FIRST_FILED)
        out = StringIO()
        call_command('wahub_import_old_knowledge', stdout=out)
        self.assertIn(f'נוסחי החזרה ללידים עודכנו לנוסח החדש: {len(rows) - 1}; חזרות מתוכננות שנוסחו מחדש: 0.', out.getvalue())
        self.assertIn('"חזרה לליד: אמר שיבדוק ויחזור" נערך מאז ונשאר כפי שהוא.', out.getvalue())
        self.assertNotIn('כאן קוגומלו', KnowledgeItem.objects.get(key='lead_link_reminder').body)
