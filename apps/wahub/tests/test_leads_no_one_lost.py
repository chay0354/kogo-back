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
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import (
    bot_mode, cron, lead_automation, lead_followup_seed, lead_followups, lead_openings, lead_stage, lead_templates, leads,
    sending, settings_store, shadow_tools,
)
from apps.wahub.models import Contact, ContactEvent, KnowledgeItem, LeadFollowUp
from apps.wahub.tests.test_lead_automation import AutomationCase
from apps.wahub.tests.test_lead_followups import SENDING_ON, at
from apps.wahub.tests.test_leads_v2 import DAY, HOUR, MINUTE, TODAY

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


# --- ג. an external branch: asked once whether it worked out with the municipality ----------------------------

EXTERNAL = {'external': True, 'branch_name': 'שוהם'}


class ExternalRoundTests(NoOneLostCase):
    def test_a_link_to_an_external_branch_is_asked_about_once_three_days_later(self):
        contact = self.link_lead(3 * HOUR, said=EXTERNAL)                 # wrote Monday 9:00, answered with a link at 9:01
        result = lead_stage.compute(contact)
        self.assertEqual((result['stage'], result['owner']), ('link_sent', 'bot'))
        self.assertIn('סניף חיצוני — ההרשמה מול העירייה', result['reason'])
        self.assertEqual((result['follow']['variant'], result['follow']['delays'], result['follow']['hot']), ('external', (3 * DAY,), False))
        [row] = self.rows(contact)
        self.assertEqual((row.kind, row.step, row.template_key), ('link_reminder', 1, 'link_reminder_external_1'))
        self.assertEqual(row.due_at, at(2026, 10, 15))                      # Thursday, when the office opens
        self.assertEqual(row.text, EXTERNAL_TEXT)
        self.assertEqual(self.next_of(contact)['what'], 'הבוט יבדוק אם הסתדרו מול העירייה')

    def test_a_quiet_lead_of_an_external_branch_gets_the_external_round_and_not_the_silent_one(self):
        contact = self.staged(self.lead(5 * DAY, said=EXTERNAL))
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('answered_silent', 'bot'))
        self.assertIn('סניף חיצוני — ההרשמה מול העירייה', contact.lead_reason)
        [row] = self.rows(contact)
        self.assertEqual((row.template_key, row.due_at), ('link_reminder_external_1', self.clock()))     # three days are behind it
        # Wrote nothing of their own, and it is an external branch: the external round all the same.
        empty = self.staged(self.lead(5 * DAY, said={**EXTERNAL, 'empty_opening': True}))
        self.assertEqual([row.template_key for row in self.rows(empty)], ['link_reminder_external_1'])

    @override_settings(**SENDING_ON)
    def test_once_sent_and_not_answered_there_is_no_other_round(self):
        self.connect('link_reminder_external_1')
        contact = self.link_lead(3 * HOUR, said=EXTERNAL)
        self.play(at(2026, 11, 5))
        self.assert_sent((at(2026, 10, 15), 'template', 'content_link_reminder_external_1'))
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner), ('link_sent', 'bot'))
        self.assertEqual(self.next_of(after)['what'], 'הבוט חזר אליו — מחכים לתשובה')

    def test_in_any_other_stage_an_external_branch_follows_the_stages_own_round(self):
        # A day the customer named is kept: the bot comes back on it, and asks nothing about the municipality before.
        later = lead_stage.compute(self.lead(said=EXTERNAL, known_callback_on=TODAY + timedelta(days=20)))
        self.assertEqual((later['stage'], later['owner']), ('waiting_date', 'bot'))
        self.assertEqual((later['follow']['kind'], later['follow']['variant']), ('date_arrived', ''))
        # "Will check": the stage's one question, and then the office calls whoever decides.
        checking = lead_stage.compute(self.lead(said={**EXTERNAL, 'blocker': 'will_check', 'blocker_text': 'נבדוק'}))
        self.assertEqual((checking['stage'], checking['follow']['kind'], checking['follow']['hot']), ('will_check', 'will_check', True))
        # A hot lead keeps its round — and with no answer, it is still the office's.
        trial = dict(kogo_outcome='trial_only', kogo_trial={'on': (TODAY - timedelta(days=1)).isoformat(), 'outcome': 'attended', 'course': ''})
        hot = self.staged(self.lead(4 * DAY, said=EXTERNAL, **trial))
        result = lead_stage.compute(hot)
        self.assertEqual((result['stage'], result['follow']['kind'], result['follow']['hot']), ('trial_done', 'trial_feedback', True))
        self.assertEqual([row.template_key for row in self.rows(hot)], ['trial_feedback_1'])
        self.play(at(2026, 11, 5))                                       # nothing leaves (sending is off); the round runs out
        self.assertEqual((self.fresh(hot).lead_stage, self.fresh(hot).lead_owner), ('trial_done', 'office'))
        self.assertFalse(LeadFollowUp.objects.filter(contact=hot, template_key='link_reminder_external_1').exists())

    def test_a_price_or_the_days_of_an_external_branch_is_a_call_of_the_office(self):
        contact = self.staged(self.lead(5 * DAY, said={**EXTERNAL, 'blocker': 'price', 'blocker_text': 'יקר לה'}))
        self.assertEqual((contact.lead_owner, contact.lead_next_step), ('office', 'להתקשר — יקר לה'))
        self.assertEqual(self.rows(contact), [])


# --- ג. answered, and went quiet: the trial lesson, once ----------------------------------------------------

class SilentRoundTests(NoOneLostCase):
    def test_the_moment_a_lead_goes_silent_the_bot_offers_a_trial_once(self):
        contact = self.staged(self.lead(HOUR))                              # wrote 11:00, answered 11:01: still talking
        self.assertEqual((contact.lead_stage, self.rows(contact)), ('in_conversation', []))
        self.clock.to(2026, 10, 14, 11, 1)                                  # two days of quiet
        lead_stage.scan()
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner), ('answered_silent', 'bot'))
        [row] = self.rows(contact)
        self.assertEqual((row.kind, row.template_key, row.due_at), ('link_reminder', 'link_reminder_silent_1', self.clock()))
        self.assertEqual(row.text, SILENT_TEXT)
        self.assertEqual(lead_stage.compute(after)['follow']['delays'], (lead_stage.SILENT_AFTER,))
        nxt = self.next_of(after)
        self.assertEqual((nxt['who'], nxt['what']), ('bot', 'הבוט יציע שיעור ניסיון'))
        # What a person would do, if it were theirs, stays the stage's.
        self.assertEqual(after.lead_next_step, 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון')

    def test_wrote_nothing_and_afraid_to_commit_go_on_as_before(self):
        nothing = lead_stage.compute(self.lead(5 * DAY, said={'empty_opening': True}))
        self.assertEqual(nothing['stage'], 'answered_silent')
        self.assertIsNone(nothing['follow'])                                # the opening's two hours are long gone
        afraid = lead_stage.compute(self.lead(5 * DAY, said={'blocker': 'commitment', 'blocker_text': 'ומה אם לא יתאים?'}))
        self.assertEqual((afraid['follow']['variant'], afraid['follow']['delays']), ('commitment', (DAY,)))

    def test_too_long_ago_to_ask(self):
        contact = self.staged(self.lead(20 * DAY))
        self.assertEqual((contact.lead_stage, self.rows(contact)), ('answered_silent', []))

    @override_settings(**SENDING_ON)
    def test_once_sent_and_not_answered_there_is_no_other_round(self):
        self.connect('link_reminder_silent_1')
        contact = self.staged(self.lead(3 * DAY, manychat_subscriber_id='4242'))
        self.play(at(2026, 11, 5))
        self.assert_sent((at(2026, 10, 12, 12, 0), 'template', 'content_link_reminder_silent_1'))
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])
        self.assertEqual(self.next_of(contact)['what'], 'הבוט חזר אליו — מחכים לתשובה')


# --- ה. the price or the days, in a lead that is not hot: the office calls ----------------------------------

class PriceAndDaysTests(NoOneLostCase):
    def test_a_lead_that_is_not_hot_goes_to_the_office_with_a_call_to_make(self):
        for blocker, words in (('price', 'המחיר עצר אותו'), ('schedule', 'הימים או השעות לא מתאימים')):
            with self.subTest(blocker=blocker):
                quiet = self.staged(self.lead(5 * DAY, said={'blocker': blocker}))
                self.assertEqual((quiet.lead_stage, quiet.lead_owner), ('answered_silent', 'office'))
                self.assertEqual(quiet.lead_next_step, f'להתקשר — {words}')
                self.assertIn(f'{words} — הבוט לא חוזר אליו לבד', quiet.lead_reason)
                self.assertEqual(self.rows(quiet), [])
                self.assertEqual(self.next_of(quiet), {
                    'who': 'office', 'what': f'להתקשר — {words}', 'when': None, 'state': 'none', 'note': '', 'hold': '',
                })

    def test_a_hot_lead_keeps_the_step_of_its_stage(self):
        trial = dict(kogo_outcome='trial_only', kogo_trial={'on': (TODAY - timedelta(days=1)).isoformat(), 'outcome': 'attended', 'course': ''})
        hot = self.staged(self.lead(4 * DAY, said={'blocker': 'price', 'blocker_text': 'יקר'}, **trial))
        self.assertEqual((hot.lead_stage, hot.lead_owner, hot.lead_next_step), ('trial_done', 'office', 'להתקשר: איך היה, ולהציע הרשמה'))


# --- ג. the two texts: filed with the others, and their templates in stage 2 --------------------------------

class TextsTests(NoOneLostCase):
    def test_the_two_texts_are_filed_and_listed_with_their_templates(self):
        rows = {row['template_key']: row for row in lead_automation.templates()}
        for key, flow, days, when in (
            ('link_reminder_external_1', 'kogo_lead_external_1', 3,
             '3 ימים אחרי השיחה האחרונה, למי שפנה על סניף חיצוני (ההרשמה מול העירייה). פעם אחת בלבד'),
            ('link_reminder_silent_1', 'kogo_lead_silent_1', 2, 'יומיים אחרי שקיבל תשובה ושתק, בלי קישור ובלי סיבה ידועה. פעם אחת בלבד'),
        ):
            with self.subTest(key=key):
                row = rows[key]
                self.assertEqual((row['stage'], row['flow_name'], row['days'], row['when_label']), (2, flow, days, when))
                self.assertTrue(row['required'] and row['active'])
                self.assertEqual(lead_templates.FLOW_NAMES[key], flow)
        self.assertEqual(lead_templates.STAGE_ONE, ('link_reminder_1', 'will_check_1', 'trial_feedback_1', 'trial_no_show_1', 'date_arrived_age_1'))
        lead_followup_seed.run()
        for key in ('lead_link_reminder_external', 'lead_link_reminder_silent'):
            self.assertTrue(KnowledgeItem.objects.get(kind='phrasing', key=key).is_active)

    def test_the_owners_edit_is_what_is_planned_and_what_leaves(self):
        lead_followup_seed.run()
        item = KnowledgeItem.objects.get(key='lead_link_reminder_silent')
        from apps.wahub import knowledge

        knowledge.update_item(item, {'body': 'היי רק בודקת לגבי {חוג} 🙂 רוצים לנסות שיעור אחד?'}, self.manager)
        contact = self.staged(self.lead(4 * DAY))
        [row] = self.rows(contact)
        self.assertEqual(row.text, 'היי רק בודקת לגבי קפוארה 🙂 רוצים לנסות שיעור אחד?')
        self.assertEqual(lead_followups.variant_of(row), 'silent')
        self.assertEqual(lead_followups.words_now(row, contact), row.text)


# --- ד. nothing to offer: looked at every day --------------------------------------------------------------

class OpeningsTests(NoOneLostCase):
    def setUp(self):
        super().setUp()
        self.city = TestDataFactory.create_city('ראש העין')
        self.branch = TestDataFactory.create_branch(name='ראש העין', city=self.city)
        self.capoeira = TestDataFactory.create_course_type('קפוארה')

    def open_a_class(self, name='קפוארה גן חובה', branch=None):
        """A class of capoeira for ages 4.5–6 (a five-year-old fits it), with a lesson and seats free."""
        course = TestDataFactory.create_course(
            name=name, branch=branch or self.branch, course_type=self.capoeira, price=Decimal('260.00'), min_age=2, max_age=2,
        )
        TestDataFactory.create_lesson(course=course, day_of_week=2, start_time=time(17, 0), end_time=time(17, 45))
        return course

    def kept(self, wrote=HOUR, **fields) -> Contact:
        """Asked about capoeira for a five-year-old in Rosh HaAyin, and nothing fit."""
        row = dict(known_child_age='5', said={'blocker': 'no_class', 'blocker_text': 'אין חוג לגיל שלו'})
        row.update(fields)
        return self.staged(self.lead(wrote, **row))

    def test_a_class_that_opened_moves_the_lead_to_the_office_with_what_to_offer(self):
        contact = self.kept()
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('nothing_to_offer', ''))
        self.assertEqual(self.next_of(contact)['what'], 'נבדק כל יום — אם ייפתח חוג שמתאים, הליד יחזור אליך')
        # Nothing open today: only the day of the look is kept.
        self.assertEqual(lead_openings.check(), {'openings_checked': 1, 'openings_found': 0})
        self.assertEqual(self.fresh(contact).lead_opened, {'checked_on': '2026-10-12'})
        self.assertEqual((self.fresh(contact).lead_owner, self.fresh(contact).touched_at), ('', contact.touched_at))
        # The next day a class that fits has opened.
        self.open_a_class()
        self.clock.forward(days=1)
        self.assertEqual(lead_openings.check(), {'openings_checked': 1, 'openings_found': 1})
        after = self.fresh(contact)
        self.assertEqual(after.lead_opened, {
            'checked_on': '2026-10-13', 'found_at': self.clock().isoformat(), 'what': 'נפתח: קפוארה גן חובה בראש העין',
        })
        # Open again, the office's question (15.10.2026) — from the moment the class was found.
        self.assertEqual((after.lead_stage, after.lead_owner), ('needs_reply', 'office'))
        self.assertEqual(after.lead_stage_since, self.clock())
        self.assertEqual(after.lead_next_step, 'נפתח: קפוארה גן חובה בראש העין — להציע לו')
        self.assertIn('נפתח: קפוארה גן חובה בראש העין', after.lead_reason)
        self.assertEqual(self.next_of(after), {
            'who': 'office', 'what': 'נפתח: קפוארה גן חובה בראש העין — להציע לו', 'when': None, 'state': 'none', 'note': '',
            'hold': '',
        })
        self.assertTrue(ContactEvent.objects.filter(contact=contact, text='הבדיקה היומית: נפתח: קפוארה גן חובה בראש העין').exists())
        # Nothing is sent to the customer: what to tell them is the office's.
        self.assertEqual((self.rows(contact), self.manychat.asked), ([], []))

    def test_not_looked_at_twice_on_one_day(self):
        self.kept()
        self.assertEqual(lead_openings.check()['openings_checked'], 1)
        with patch.object(shadow_tools, 'find_courses') as search:
            self.assertEqual(lead_openings.check(), {'openings_checked': 0, 'openings_found': 0})
            self.clock.forward(hours=11)                                     # 23:00, the same day in Israel
            self.assertEqual(lead_openings.check()['openings_checked'], 0)
        search.assert_not_called()
        self.clock.forward(hours=2)                                          # the next day
        self.assertEqual(lead_openings.check()['openings_checked'], 1)

    def test_a_lead_with_no_field_age_or_place_is_not_looked_at_and_says_what_is_missing(self):
        missing = {
            'age': dict(known_child_age=''),
            'field': dict(known_course_type='', lead_course_type=''),
            'place': dict(known_city='', known_branch_name=''),
        }
        for what, fields in missing.items():
            with self.subTest(missing=what):
                contact = self.kept(**fields)
                self.assertIsNone(lead_openings.search_terms(contact))
                self.assertEqual(self.next_of(contact)['what'], 'אין כרגע מה להציע — חסר תחום, גיל או מקום כדי לבדוק כל יום')
        with patch.object(shadow_tools, 'find_courses') as search:
            self.assertEqual(lead_openings.check(), {'openings_checked': 0, 'openings_found': 0})
        search.assert_not_called()
        # Marked as looked at all the same: they do not stand in line before the others on every call.
        self.assertEqual(Contact.objects.filter(lead_stage='nothing_to_offer', lead_opened__checked_on='2026-10-12').count(), 3)

    def test_what_is_looked_for(self):
        self.assertEqual(lead_openings.search_terms(self.kept()), {
            'course_type': 'קפוארה', 'age': 5.0, 'grade': '', 'city': 'ראש העין', 'branch': 'ראש העין',
        })
        said = {'blocker': 'no_branch', 'course_type': 'ג׳ודו', 'city': 'שוהם', 'branch_name': '',
                'child': {'name': '', 'age_years': 4, 'age_months': 6, 'grade': '', 'birth_date': ''}}
        terms = lead_openings.search_terms(self.kept(said=said, known_child_age='', known_branch_name='', known_course_type=''))
        self.assertEqual((terms['course_type'], terms['age'], terms['city'], terms['branch']), ('ג׳ודו', 4.5, 'שוהם', ''))
        graded = lead_openings.search_terms(self.kept(known_child_age='כיתה ב'))
        self.assertEqual((graded['age'], graded['grade']), (None, 'כיתה ב'))

    def test_a_class_with_every_lesson_full_is_not_open_and_a_question_is_not_an_answer(self):
        terms = {'course_type': 'קפוארה', 'age': 5.0, 'grade': '', 'city': 'ראש העין', 'branch': ''}
        full = {'need': '', 'courses': [{'name': 'קפוארה גן חובה', 'branch': 'ראש העין', 'lessons': [{'is_full': True}]}]}
        some = {'need': '', 'courses': [
            {'name': 'קפוארה גן חובה', 'branch': 'ראש העין', 'lessons': [{'is_full': True}, {'is_full': False}]},
            {'name': 'קפוארה מלאה', 'branch': 'ראש העין', 'lessons': [{'is_full': True}]},
        ]}
        asked = {'need': 'branch', 'courses': []}
        for answer, expected in ((full, ''), (some, 'נפתח: קפוארה גן חובה בראש העין'), (asked, '')):
            with patch.object(shadow_tools, 'find_courses', return_value=answer) as search:
                self.assertEqual(lead_openings.what_is_open(terms), expected)
            self.assertIs(search.call_args.kwargs['show_all'], True)

    def test_two_branches_of_the_city_are_both_named(self):
        other = TestDataFactory.create_branch(name='ראש העין צפון', city=self.city)
        self.open_a_class()
        self.open_a_class('קפוארה צפון', branch=other)
        contact = self.kept(known_branch_name='')
        lead_openings.check()
        self.assertEqual(self.fresh(contact).lead_next_step, 'נפתח: קפוארה גן חובה בראש העין; קפוארה צפון בראש העין צפון — להציע לו')

    def test_once_a_person_dealt_with_it_the_same_classes_do_not_bring_it_back(self):
        self.open_a_class()
        contact = self.kept()
        lead_openings.check()
        self.assertEqual(self.fresh(contact).lead_owner, 'office')
        # The office called and marked it: the ordinary rules decide again.
        self.clock.forward(minutes=30)
        Contact.objects.filter(pk=contact.pk).update(followup_status='answered', followup_at=self.clock(), followup_by=self.manager)
        lead_stage.refresh(contact)
        self.assertEqual(self.fresh(contact).lead_owner, '')
        # Tomorrow the same class is still there: not news.
        self.clock.forward(days=1)
        self.assertEqual(lead_openings.check(), {'openings_checked': 1, 'openings_found': 0})
        self.assertEqual(self.fresh(contact).lead_owner, '')
        # Another class opened: that is news.
        self.open_a_class('קפוארה גן חובה ב')
        self.clock.forward(days=1)
        self.assertEqual(lead_openings.check()['openings_found'], 1)
        self.assertEqual(self.fresh(contact).lead_owner, 'office')

    def test_the_customer_writing_ends_it_too(self):
        self.open_a_class()
        contact = self.kept()
        lead_openings.check()
        self.assertIsNotNone(lead_stage.opening_found(self.fresh(contact)))
        Contact.objects.filter(pk=contact.pk).update(last_inbound_at=self.clock() + MINUTE)
        self.assertIsNone(lead_stage.opening_found(self.fresh(contact)))

    def test_the_cron_does_it(self):
        self.open_a_class()
        contact = self.kept()
        Contact.objects.update(kogo_checked_at=self.clock())                 # matched a moment ago
        with override_settings(ANTHROPIC_API_KEY=''):
            counts = cron.tick()
        self.assertEqual((counts['openings_checked'], counts['openings_found']), (1, 1))
        self.assertEqual(self.fresh(contact).lead_owner, 'office')
        with override_settings(ANTHROPIC_API_KEY=''):
            self.assertEqual(cron.tick()['openings_checked'], 0)


# --- ט. nothing to offer is closed: "לא רלוונטי · אין מה להציע" (15.10.2026) ------------------------------

class NothingToOfferClosedTests(NoOneLostCase):
    """
    The owner, 15.10.2026: a lead with nothing to offer is no lead the bot handles. It is closed — "לא רלוונטי ·
    אין מה להציע", under "נסגרו", nobody's step and no follow-up of the bot's — and the daily look goes on: a class
    that fits opens, and it is an open question of the office's again ("נפתח: … — להציע לו").
    """

    def setUp(self):
        super().setUp()
        self.branch = TestDataFactory.create_branch(name='ראש העין', city=TestDataFactory.create_city('ראש העין'))
        self.capoeira = TestDataFactory.create_course_type('קפוארה')

    def open_a_class(self, name='קפוארה גן חובה'):
        course = TestDataFactory.create_course(
            name=name, branch=self.branch, course_type=self.capoeira, price=Decimal('260.00'), min_age=2, max_age=2,
        )
        TestDataFactory.create_lesson(course=course, day_of_week=2, start_time=time(17, 0), end_time=time(17, 45))

    def kept(self, wrote=HOUR, **fields) -> Contact:
        """Asked about capoeira for a five-year-old in Rosh HaAyin; the group is full."""
        row = dict(known_child_age='5', said={'blocker': 'full', 'blocker_text': 'הקבוצה מלאה'})
        row.update(fields)
        return self.staged(self.lead(wrote, **row))

    def listed(self, owner, **params) -> dict:
        return {row['contact_id']: row for row in self.get('leads/', owner=owner, **params).data['results']}

    def test_closed_in_its_new_words_and_the_bubble_says_what_becomes_of_it(self):
        contact = self.kept()
        result = lead_stage.compute(contact, self.clock())
        self.assertEqual(
            (result['stage'], result['group'], result['label'], result['owner'], result['follow'], result['next_step']),
            ('nothing_to_offer', 'closed', 'לא רלוונטי · אין מה להציע', '', None, ''),
        )
        # No clock moves a closed lead: it is not looked at again at midnight.
        self.assertEqual((contact.lead_owner, contact.lead_next_step, contact.lead_recheck_at), ('', '', None))
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['stage_label'], row['list'], row['list_label']), (
            'nothing_to_offer', 'לא רלוונטי · אין מה להציע', 'closed', 'נסגרו',
        ))
        # Closed, and nobody's step — but not over: the row says it is looked at every day, and comes back.
        self.assertEqual(row['next'], {
            'who': 'none', 'what': 'נבדק כל יום — אם ייפתח חוג שמתאים, הליד יחזור אליך', 'when': None, 'state': 'none',
            'note': '', 'hold': '',
        })
        # The conversation's own copy of the stage says the same.
        lead = self.get(f'contacts/{contact.id}/').data['lead']
        self.assertEqual((lead['stage'], lead['stage_label'], lead['group']), ('nothing_to_offer', 'לא רלוונטי · אין מה להציע', 'closed'))
        board = {group['key']: {stage['key']: stage for stage in group['stages']} for group in lead_stage.board()['groups']}
        self.assertNotIn('nothing_to_offer', board['wait'])
        self.assertEqual(board['closed']['nothing_to_offer']['label'], 'לא רלוונטי · אין מה להציע')
        self.assertEqual(
            board['closed']['nothing_to_offer']['hint'],
            'אין סניף באזור, אין חוג לגיל, או שהחוג מלא. אם ייפתח חוג שמתאים, הליד יחזור אליך',
        )

    def test_not_under_the_bot_nor_waiting_for_you_and_counted_with_the_closed(self):
        by_the_bot = self.link_lead(3 * HOUR)
        for_you = self.staged(self.lead(2 * HOUR, answer=None))
        contact = self.kept()
        for apart in ({}, {'waiting': 'apart'}):
            with self.subTest(**apart):
                answer = self.get('leads/', owner='bot', **apart).data
                self.assertEqual(
                    {key: answer['counts'][key] for key in ('office', 'bot', 'closed')}, {'office': 1, 'bot': 1, 'closed': 1},
                )
                self.assertEqual(set(self.listed('bot', **apart)), {by_the_bot.id})
                self.assertEqual(set(self.listed('office', **apart)), {for_you.id})
        self.assertEqual(set(self.listed('closed')), {contact.id})
        self.assertEqual(self.get('leads/', owner='closed').data['stages'], [
            {'key': 'nothing_to_offer', 'label': 'לא רלוונטי · אין מה להציע', 'count': 1},
        ])
        # The conversations' filter by group agrees.
        groups = {
            group: [row['id'] for row in self.get('contacts/', lead_group=group).data['results']]
            for group in ('wait', 'closed')
        }
        self.assertIn(contact.id, groups['closed'])
        self.assertNotIn(contact.id, groups['wait'])

    def test_no_follow_up_ever_and_it_does_not_go_cold(self):
        # Even with a registration link and a ManyChat contact: nothing to offer comes first, and the bot starts nothing.
        contact = self.kept(lead_link_sent_at=self.clock() - HOUR, manychat_subscriber_id='4242')
        self.assertEqual(contact.lead_stage, 'nothing_to_offer')
        self.assertEqual(self.rows(contact), [])
        self.clock.forward(days=40)
        lead_stage.refresh(contact)
        lead_followups.run()
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner, self.rows(contact)), ('nothing_to_offer', '', []))
        self.assertEqual(self.manychat.asked, [])

    def test_the_message_to_the_dormant_is_still_only_for_the_dormant(self):
        from apps.wahub import knowledge

        cold = self.staged(self.lead(40 * DAY, is_demo=True, source='demo'))
        contact = self.kept(40 * DAY, is_demo=True, source='demo')
        self.assertEqual((cold.lead_stage, contact.lead_stage), ('cold', 'nothing_to_offer'))
        settings_store.put(settings_store.LEAD_DORMANT_ON, TODAY.isoformat())
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_dormant', 'title': 'נפתחה הרשמה', 'body': 'היי, נפתחה ההרשמה 🙂'})
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 1)
        self.assertEqual([row.kind for row in self.rows(cold)], ['dormant'])
        self.assertEqual(self.rows(contact), [])

    def test_the_waiting_list_stands_above_it_as_it_did(self):
        waiting = {'status': 'waiting', 'label': 'קפוארה גן חובה, יום שלישי 17:00', 'since': self.clock().isoformat()}
        contact = self.kept(lead_waiting=waiting)
        self.assertEqual(lead_stage.compute(contact, self.clock())['group'], 'wait')
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['list']), ('waiting_list', 'waiting'))

    def test_a_class_that_opened_is_an_open_question_of_the_offices_and_stays_one(self):
        self.open_a_class()
        contact = self.kept()
        self.assertEqual(lead_openings.check()['openings_found'], 1)
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner, after.lead_stage_since), ('needs_reply', 'office', self.clock()))
        self.assertEqual(after.lead_next_step, 'נפתח: קפוארה גן חובה בראש העין — להציע לו')
        row = self.get(f'leads/{contact.id}/').data
        self.assertEqual((row['stage'], row['stage_label'], row['list']), ('needs_reply', 'נפתח חוג שמתאים', 'office'))
        self.assertEqual(set(self.listed('office')), {contact.id})
        counts = self.get('leads/', owner='office').data['counts']
        self.assertEqual((counts['office'], counts['bot'], counts['closed']), (1, 0, 0))
        self.assertTrue(ContactEvent.objects.filter(
            contact=contact, kind='lead_stage_changed', text__startswith='לא רלוונטי · אין מה להציע ← נפתח חוג שמתאים',
        ).exists())
        # It stays the office's: after midnight, and when the rules are run again.
        self.clock.forward(days=1)
        lead_stage.scan()
        lead_stage.refresh(contact)
        again = self.fresh(contact)
        self.assertEqual((again.lead_stage, again.lead_owner, again.lead_stage_since), ('needs_reply', 'office', after.lead_stage_since))
        # ...and the daily look leaves it alone while it is: it is no longer "nothing to offer".
        self.assertEqual(lead_openings.check(), {'openings_checked': 0, 'openings_found': 0})
        # Nothing goes to the customer by itself.
        self.assertEqual((self.rows(contact), self.manychat.asked), ([], []))

    def test_once_the_office_dealt_with_it_it_is_closed_again_and_looked_at_every_day(self):
        self.open_a_class()
        contact = self.kept()
        lead_openings.check()
        self.clock.forward(minutes=30)
        Contact.objects.filter(pk=contact.pk).update(followup_status='answered', followup_at=self.clock(), followup_by=self.manager)
        lead_stage.refresh(contact)
        self.assertEqual((self.fresh(contact).lead_stage, self.fresh(contact).lead_owner), ('nothing_to_offer', ''))
        self.assertEqual(set(self.listed('closed')), {contact.id})
        self.clock.forward(days=1)
        self.assertEqual(lead_openings.check(), {'openings_checked': 1, 'openings_found': 0})
        self.assertEqual(self.fresh(contact).lead_stage, 'nothing_to_offer')

    def test_version_6_of_the_rules_reaches_the_leads_already_there(self):
        self.assertEqual(lead_stage.RULES_VERSION, 6)
        # As version 5 left them: one waiting for a class, and one whose class had opened (still "nothing to offer",
        # the office's), both in the group "בתהליך".
        waiting = self.kept()
        opened = self.kept()
        found_at = self.clock() - HOUR
        Contact.objects.filter(pk=waiting.pk).update(lead_version=5, lead_recheck_at=self.clock() + DAY)
        Contact.objects.filter(pk=opened.pk).update(
            lead_version=5, lead_owner='office', lead_stage_since=self.clock() - DAY,
            lead_opened={'checked_on': TODAY.isoformat(), 'found_at': found_at.isoformat(), 'what': 'נפתח: קפוארה גן חובה בראש העין'},
            lead_next_step='נפתח: קפוארה גן חובה בראש העין — להציע לו',
        )
        self.assertEqual(lead_stage.pending_count(), 2)
        lead_stage.scan()
        self.assertEqual(lead_stage.pending_count(), 0)
        waiting, opened = self.fresh(waiting), self.fresh(opened)
        self.assertEqual((waiting.lead_stage, waiting.lead_owner, waiting.lead_version, waiting.lead_recheck_at), ('nothing_to_offer', '', 6, None))
        self.assertEqual((opened.lead_stage, opened.lead_owner, opened.lead_version), ('needs_reply', 'office', 6))
        self.assertEqual((opened.lead_stage_since, opened.lead_next_step), (found_at, 'נפתח: קפוארה גן חובה בראש העין — להציע לו'))


# --- the templates found by name, kept fresh by the cron for the leads list ---------------------------------

@override_settings(**SENDING_ON)
class TemplatesKeptFreshTests(NoOneLostCase):
    def test_a_template_made_in_manychat_reaches_the_leads_list_through_the_cron(self):
        contact = self.link_lead(3 * HOUR)
        Contact.objects.update(kogo_checked_at=self.clock())             # matched a moment ago
        self.assertEqual(self.listed_next(contact)['hold'], 'חסרה תבנית')
        self.manychat.named('link_reminder_1')                            # made in ManyChat under its fixed name
        self.assertEqual(self.listed_next(contact)['hold'], 'חסרה תבנית')
        self.assertEqual(self.manychat.lists, 0)                         # the list itself never asks ManyChat
        with patch.object(lead_templates, 'manychat_flows', wraps=lead_templates.manychat_flows) as asked, \
                override_settings(ANTHROPIC_API_KEY=''):
            self.assertEqual(cron.tick()['lead_templates_connected'], 1)
            self.assertEqual(self.listed_next(contact)['hold'], '')
            cron.tick()
            self.assertEqual(asked.call_count, 1)                        # not again within ten minutes
            self.clock.forward(minutes=11)
            cron.tick()
            self.assertEqual(asked.call_count, 2)


# --- the words of the leads screen --------------------------------------------------------------------------

class WordsTests(NoOneLostCase):
    def test_what_the_bot_will_do_by_the_variant_of_its_round(self):
        self.assertEqual(leads.NEXT_BY_VARIANT, {
            'external': 'הבוט יבדוק אם הסתדרו מול העירייה', 'silent': 'הבוט יציע שיעור ניסיון',
            # A child who came to a lesson without registering (tests/test_walk_in_leads.py).
            'walk_in': 'הבוט יכתוב שראינו אותם בשיעור ויציע מקום קבוע',
        })
        for variant, key in (('external', 'link_reminder_external_1'), ('silent', 'link_reminder_silent_1'),
                             ('', 'link_reminder_1'), ('signup', 'link_reminder_signup_1')):
            self.assertEqual(lead_followups.variant_of(LeadFollowUp(kind='link_reminder', template_key=key)), variant)
        self.assertEqual(lead_followups.variant_of(LeadFollowUp(kind='date_arrived', template_key='date_arrived_not_ready_1')), 'not_ready')
        link = self.link_lead(3 * HOUR)
        self.assertEqual(self.listed_next(link)['what'], 'הבוט יזכיר את ההרשמה')
        external = self.link_lead(3 * HOUR, said=EXTERNAL)
        self.assertEqual(self.listed_next(external)['what'], 'הבוט יבדוק אם הסתדרו מול העירייה')
        silent = self.staged(self.lead(3 * DAY))
        self.assertEqual(self.listed_next(silent)['what'], 'הבוט יציע שיעור ניסיון')
