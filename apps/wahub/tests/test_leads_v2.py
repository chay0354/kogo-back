"""
Leads, version 2 (docs/WAHUB-CONTRACT-LEADS-V2.md, א, ב, ד, ח): what the
conversation says about a lead — by Claude behind a mock, and by the keyword
rules — the day to come back as arithmetic, the stages and who makes the next
move, and the endpoints of the leads screen. The bot's own follow-ups are in
test_lead_followups.py.

The clock is the test's: every test starts on Monday 12.10.2026 at noon
(Israel), and moves only when the test moves it.
"""
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import analysis, cron, knowledge, lead_ai, lead_stage, state
from apps.wahub.analysis import Line, Place
from apps.wahub.models import Contact, ContactEvent, LeadFollowUp, Message
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user
from apps.wahub.tests.test_analysis import CARD, NO_LEAD, POST, claude

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
ZONE = timezone.get_default_timezone()
START = timezone.make_aware(datetime(2026, 10, 12, 12, 0), ZONE)        # a Monday
TODAY = START.date()
FOLLOWUP_MARKS = ('followup_status', 'followup_due', 'followup_note', 'followup_by', 'followup_at')


class Clock:
    """`timezone.now()` for one test: it stands still until the test moves it."""

    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def forward(self, **delta):
        self.moment += timedelta(**delta)
        return self.moment

    def to(self, year, month, day, hour=12, minute=0):
        self.moment = timezone.make_aware(datetime(year, month, day, hour, minute), ZONE)
        return self.moment


class LeadsCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(START)
        frozen = patch('django.utils.timezone.now', self.clock)
        frozen.start()
        self.addCleanup(frozen.stop)
        # ManyChat has been copying the old bot's replies for a year: an unanswered message means what it says.
        seen = patch.object(lead_stage, 'replies_copied_since', return_value=START - timedelta(days=365))
        seen.start()
        self.addCleanup(seen.stop)
        self.made = 0

    def lead(self, wrote=HOUR, *, answer='bot', said=None, **fields) -> Contact:
        """
        Somebody who asked about capoeira in Rosh HaAyin `wrote` ago and is not in
        the registrations. `answer`: who answered a minute later ('bot',
        'office'), or None for a message still waiting. `said`: what the summary
        kept about the lead (read five minutes after their message).
        """
        self.made += 1
        asked_at = self.clock() - wrote
        row = dict(
            name=f'ליד {self.made}', kogo_outcome='not_found',
            known_topic='info', known_course_type='קפוארה', known_city='ראש העין', known_branch_name='ראש העין',
            first_inbound_at=asked_at, last_inbound_at=asked_at,
        )
        if answer:
            row.update(
                last_message_at=asked_at + MINUTE, last_message_direction='out', last_message_sender=answer,
                lead_human_touched=answer == 'office',
            )
        else:
            row.update(
                last_message_at=asked_at, last_message_direction='in', last_message_sender='customer', waiting_since=asked_at,
            )
        if said is not None:
            row['lead_ai'] = {**lead_ai._blank('ai'), 'intent': 'class', **said}
            row['analyzed_at'] = asked_at + 5 * MINUTE
        row.update(fields)
        return Contact.objects.create(phone=f'9725055504{self.made:02d}', **row)

    def stage(self, contact, now=None) -> dict:
        return lead_stage.compute(contact, now or self.clock())

    def staged(self, contact) -> Contact:
        lead_stage.refresh(contact)
        return Contact.objects.get(pk=contact.pk)


def said(*texts, who='customer', when=START):
    return [Line(who=who, text=text, sent_at=when) for text in texts]


def special_day(title, first, last=None, state_='closed'):
    return knowledge.create_item({
        'kind': 'special_day', 'title': title, 'date_from': first.isoformat(),
        'date_to': (last or first).isoformat(), 'state': state_,
    })


# --- the day to come back: arithmetic, never the model's ---------------------------------------------

class ComeBackDayTests(LeadsCase):
    def day(self, raw, **kwargs):
        return lead_ai.come_back_day(raw, said_on=TODAY, **kwargs)

    def test_a_child_too_young_comes_back_on_the_third_birthday(self):
        # The contract's own example: two years and seven months on 12.10 — three on 12.3.
        self.assertEqual(self.day({'kind': 'age', 'years': 2, 'months': 7}), date(2027, 3, 12))
        self.assertEqual(self.day({'kind': 'age', 'years': 2, 'months': None}), date(2027, 10, 12))
        self.assertEqual(self.day({'kind': 'age', 'years': 0, 'months': 10}), date(2028, 12, 12))
        # The age the class starts at, when the conversation named another.
        self.assertEqual(self.day({'kind': 'age', 'years': 4, 'months': 0, 'until_years': 5}), date(2027, 10, 12))
        # A birth date that was said is better than an age.
        self.assertEqual(self.day({'kind': 'age'}, child={'birth_date': '2024-05-20'}), date(2027, 5, 20))
        # The child's own age answers when the raw datum has none.
        self.assertEqual(self.day({'kind': 'age'}, child={'age_years': 2, 'age_months': 6}), date(2027, 4, 12))

    def test_a_child_who_is_old_enough_has_nothing_to_wait_for(self):
        self.assertIsNone(self.day({'kind': 'age', 'years': 3, 'months': 0}))
        self.assertIsNone(self.day({'kind': 'age', 'years': 5, 'months': 2}))
        self.assertIsNone(self.day({'kind': 'age'}))                           # no age at all: no date is invented

    def test_so_long_after_the_day_it_was_said(self):
        self.assertEqual(self.day({'kind': 'offset', 'months': 6}), date(2027, 4, 12))      # "בעוד חצי שנה"
        self.assertEqual(self.day({'kind': 'offset', 'weeks': 2}), date(2026, 10, 26))
        self.assertEqual(self.day({'kind': 'offset', 'days': 3}), date(2026, 10, 15))
        self.assertEqual(self.day({'kind': 'offset', 'months': 1, 'days': 2}), date(2026, 11, 14))
        self.assertIsNone(self.day({'kind': 'offset'}))
        self.assertIsNone(self.day({'kind': 'offset', 'months': 0, 'days': 0}))
        # Counted from the day it was said, not from today; a month with no such day ends on its last.
        self.assertEqual(
            lead_ai.come_back_day({'kind': 'offset', 'months': 1}, said_on=date(2027, 1, 31)), date(2027, 2, 28),
        )
        self.assertEqual(lead_ai.add_months(date(2026, 12, 15), 2), date(2027, 2, 15))

    def test_after_the_holidays_is_the_day_after_the_nearest_special_days(self):
        self.assertIsNone(self.day({'kind': 'after', 'what': 'החגים'}))         # an empty list: no date is invented
        special_day('חנוכה', date(2026, 12, 6), date(2026, 12, 12), 'hours')
        special_day('ערב פסח', date(2027, 4, 21))
        special_day('פסח', date(2027, 4, 22), date(2027, 4, 28))
        special_day('יום העצמאות', date(2027, 5, 12))
        # The nearest one ahead.
        self.assertEqual(self.day({'kind': 'after', 'what': 'החגים'}), date(2026, 12, 13))
        self.assertEqual(self.day({'kind': 'after', 'what': 'אחרי החג'}), date(2026, 12, 13))
        # One named by its name — and the days that follow it closely are one season.
        self.assertEqual(self.day({'kind': 'after', 'what': 'פסח'}), date(2027, 4, 29))
        self.assertEqual(self.day({'kind': 'after', 'what': 'אחרי הפסח'}), date(2027, 4, 29))
        self.assertEqual(self.day({'kind': 'after', 'what': 'חנוכה'}), date(2026, 12, 13))
        # Said in the middle of a season: after its end.
        self.assertEqual(
            lead_ai.come_back_day({'kind': 'after', 'what': 'החגים'}, said_on=date(2027, 4, 22)), date(2027, 4, 29),
        )
        # A holiday that is not on the list, and a list with nothing ahead.
        self.assertIsNone(self.day({'kind': 'after', 'what': 'סוכות'}))
        self.assertIsNone(lead_ai.come_back_day({'kind': 'after', 'what': 'החגים'}, said_on=date(2027, 6, 1)))

    def test_a_special_day_that_is_off_or_of_one_branch_is_not_a_holiday_of_the_business(self):
        off = special_day('חג שבוטל', date(2026, 11, 1))
        knowledge.soft_delete(off)
        knowledge.create_item({
            'kind': 'special_day', 'title': 'שיפוץ בסניף', 'date_from': '2026-11-05', 'date_to': '2026-11-05',
            'state': 'closed', 'scope_level': 'branch', 'scope_id': 'b-1', 'scope_label': 'ראש העין',
        })
        self.assertIsNone(self.day({'kind': 'after', 'what': 'החגים'}))

    def test_a_date_that_was_named(self):
        self.assertEqual(self.day({'kind': 'date', 'iso': '2026-11-01'}), date(2026, 11, 1))
        self.assertIsNone(self.day({'kind': 'date', 'iso': '2026-10-12'}))      # the very day it was said
        self.assertIsNone(self.day({'kind': 'date', 'iso': '2026-09-01'}))      # behind us
        self.assertIsNone(self.day({'kind': 'date', 'iso': 'אחרי החגים'}))
        self.assertIsNone(self.day({'kind': 'date', 'iso': '2040-01-01'}))      # nobody is asked to come back in 2040
        self.assertIsNone(self.day({'kind': 'someday'}))
        self.assertIsNone(self.day(None))

    def test_the_first_of_september_after_a_child_who_did_not_want_to(self):
        self.assertEqual(lead_ai.next_school_year(date(2026, 10, 12)), date(2027, 9, 1))
        self.assertEqual(lead_ai.next_school_year(date(2027, 5, 3)), date(2027, 9, 1))
        self.assertEqual(lead_ai.next_school_year(date(2027, 9, 1)), date(2028, 9, 1))


# --- Claude's answer, checked field by field --------------------------------------------------------------

class Card:
    """What analysis.Known gives lead_ai: only the fields it reads."""
    branch_id = None
    branch_name = ''
    city = ''
    course_type = ''
    child_age = ''
    callback_on = None
    topic = 'info'
    interest = 'warm'
    flags = ()

    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


def model_lead(**fields) -> dict:
    return {**NO_LEAD, 'intent': 'class', **fields}


class FromModelTests(LeadsCase):
    LINES = (
        said('היי, אפשר פרטים על קפוארה?', when=START - 3 * HOUR)
        + said('בשמחה! באיזו עיר?', who='bot', when=START - 3 * HOUR)
        + said('ראש העין. הוא בן שנתיים ושבעה חודשים', when=START - 2 * HOUR)
        + said('אצלנו מתחילים מגיל 3', who='bot', when=START - 2 * HOUR)
        + said('אוקיי תודה, לא רלוונטי כרגע', when=START - HOUR)
    )

    def read(self, lines=None, known=None, **fields):
        return lead_ai.from_model(
            model_lead(**fields), lines=self.LINES if lines is None else lines, known=known or Card(), today=TODAY,
        )

    def test_every_blocker_of_the_contract_is_kept_with_its_sentence(self):
        for blocker in lead_ai.BLOCKERS:
            with self.subTest(blocker=blocker):
                lead = self.read(blocker=blocker, blocker_text='  אמרה שתבדוק \n עם הבת שלה ותחזור  ')
                self.assertEqual((lead['blocker'], lead['blocker_text']), (blocker, 'אמרה שתבדוק עם הבת שלה ותחזור'))
        self.assertEqual(set(lead_ai.BLOCKERS), {
            'will_check', 'ask_someone', 'too_young', 'later', 'no_branch', 'no_class', 'price', 'schedule', 'full',
            'commitment', 'group', 'reschedule_trial', 'child_not_ready', 'instructor', 'site_problem', 'open_question',
            'call_someone', 'other',
        })
        # A blocker with no sentence says at least what it is; one outside the contract is nothing.
        self.assertEqual(self.read(blocker='price')['blocker_text'], 'המחיר עצר אותו')
        unknown = self.read(blocker='mood', blocker_text='לא במצב רוח')
        self.assertEqual((unknown['blocker'], unknown['blocker_text']), ('', ''))

    def test_not_interested_stands_only_on_the_customers_own_words(self):
        closed = self.read(interest='no', quote='לא רלוונטי כרגע')
        self.assertEqual((closed['interest'], closed['quote']), ('no', 'לא רלוונטי כרגע'))
        self.assertEqual(closed['quote_at'], (START - HOUR).isoformat())
        # No quote; words nobody wrote; words the bot wrote: the lead stays open.
        for quote in ('', 'אני לא מעוניינת בכלל', 'אצלנו מתחילים מגיל 3'):
            with self.subTest(quote=quote):
                lead = self.read(interest='no', quote=quote)
                self.assertEqual((lead['interest'], lead['quote'], lead['quote_at']), ('unsure', '', None))
        # Punctuation and spacing do not make the words somebody else's.
        self.assertEqual(self.read(interest='no', quote='"אוקיי תודה,  לא רלוונטי כרגע."')['interest'], 'no')

    def test_an_old_no_does_not_close_a_customer_who_wrote_again_since(self):
        lines = (
            said('לא מעוניינת, תודה', when=START - 5 * DAY) + said('אה רגע', when=START - HOUR)
            + said('בעצם', when=START - HOUR) + said('יש לכם חוג בכפר סבא?', when=START - HOUR)
        )
        self.assertEqual(self.read(lines=lines, interest='no', quote='לא מעוניינת, תודה')['interest'], 'unsure')

    def test_already_registered_is_taken_on_the_customers_word(self):
        lines = said('אפשר פרטים?', when=START - 3 * DAY) + said('בסוף רשמנו אותה, תודה', when=START - HOUR)
        lead = self.read(lines=lines, interest='registered', quote='בסוף רשמנו אותה')
        self.assertEqual((lead['interest'], lead['quote']), ('registered', 'בסוף רשמנו אותה'))
        self.assertEqual(self.read(lines=lines, interest='registered', quote='')['interest'], 'unsure')

    def test_values_outside_the_contract_are_dropped(self):
        lead = lead_ai.from_model(
            {
                'intent': 'sales', 'empty_opening': 'yes', 'interest': 'very', 'blocker': 7,
                'child': {'name': ' נועה ', 'age_years': 40, 'age_months': 13, 'grade': 'כיתה ב', 'birth_date': '2031-01-01'},
                'come_back': 'בקרוב', 'come_back_why': 'סתם', 'summary': '  שורה \n ארוכה ' + 'א' * 400,
            },
            lines=self.LINES, known=Card(), today=TODAY,
        )
        self.assertEqual((lead['intent'], lead['empty_opening'], lead['interest'], lead['blocker']), ('', False, '', ''))
        self.assertEqual(lead['child'], {'name': 'נועה', 'age_years': None, 'age_months': None, 'grade': 'כיתה ב', 'birth_date': ''})
        self.assertEqual((lead['come_back'], lead['come_back_on'], lead['come_back_why']), ({}, None, ''))
        self.assertLessEqual(len(lead['summary']), 240)
        self.assertNotIn('\n', lead['summary'])
        self.assertEqual(lead_ai.from_model(None, lines=[], known=Card(), today=TODAY)['source'], 'ai')
        self.assertEqual(lead_ai.from_model('nonsense', lines=[], known=Card(), today=TODAY)['interest'], '')

    def test_the_day_is_worked_out_here_from_what_was_said(self):
        lead = self.read(
            blocker='too_young', child={**NO_LEAD['child'], 'name': 'איתי', 'age_years': 2, 'age_months': 7},
            come_back={**NO_LEAD['come_back'], 'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'},
            come_back_why='הילד יהיה בן 3',
        )
        self.assertEqual((lead['come_back_on'], lead['come_back_why']), ('2027-03-12', 'הילד יהיה בן 3'))
        self.assertEqual(lead['come_back'], {'kind': 'age', 'said_on': '2026-10-12', 'years': 2, 'months': 7})
        # No sentence from the model: one is made.
        bare = self.read(come_back={**NO_LEAD['come_back'], 'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'})
        self.assertEqual(bare['come_back_why'], 'הילד יהיה בן 3')
        half_a_year = self.read(come_back={**NO_LEAD['come_back'], 'kind': 'offset', 'months': 6, 'said_on': '2026-10-12'})
        self.assertEqual(half_a_year['come_back_on'], '2027-04-12')
        # A day of the conversation is believed; a day outside it is not — the customer's last message decides.
        elsewhere = self.read(come_back={**NO_LEAD['come_back'], 'kind': 'offset', 'weeks': 1, 'said_on': '2025-01-01'})
        self.assertEqual(elsewhere['come_back_on'], '2026-10-19')

    def test_a_day_only_the_card_named_is_the_day_and_a_child_who_did_not_want_to_waits_for_september(self):
        named = self.read(known=Card(callback_on=date(2026, 10, 26)))
        self.assertEqual((named['come_back_on'], named['come_back']['kind']), ('2026-10-26', 'date'))
        not_ready = self.read(blocker='child_not_ready', blocker_text='הוא לא רצה, אולי בשנה הבאה')
        self.assertEqual(not_ready['come_back_on'], '2027-09-01')
        self.assertIn('השנה הבאה', not_ready['come_back_why'])
        self.assertIsNone(self.read()['come_back_on'])

    def test_where_and_what_come_from_the_card_and_nothing_is_guessed(self):
        types = [('t-capoeira', 'קפוארה'), ('t-dance', 'מחול'), ('t-dance-young', 'מחול גיל הרך')]
        lead = lead_ai.from_model(
            model_lead(), lines=self.LINES, known=Card(branch_id='b-rosh', branch_name='ראש העין', city='ראש העין', course_type='קפוארה'),
            course_types=types, today=TODAY, link_sent=True,
        )
        self.assertEqual((lead['branch_id'], lead['branch_name'], lead['city']), ('b-rosh', 'ראש העין', 'ראש העין'))
        self.assertEqual((lead['course_type'], lead['course_type_id'], lead['course_id']), ('קפוארה', 't-capoeira', None))
        self.assertTrue(lead['link_sent'])
        # Two kinds that fit the word: neither is picked.
        vague = lead_ai.from_model(model_lead(), lines=self.LINES, known=Card(course_type='מחול מודרני'), course_types=types, today=TODAY)
        self.assertEqual((vague['course_type'], vague['course_type_id']), ('מחול מודרני', 't-dance'))
        none = lead_ai.from_model(model_lead(), lines=self.LINES, known=Card(course_type='ג׳ודו'), course_types=types, today=TODAY)
        self.assertEqual((none['course_type'], none['course_type_id']), ('ג׳ודו', None))

    def test_an_external_branch_is_known_as_one(self):
        outside = [Place(id='b-shoham', name='שוהם — מתנ"ס', city='שוהם')]
        lead = lead_ai.from_model(
            model_lead(), lines=self.LINES, known=Card(branch_name='שוהם', city='שוהם'), external_places=outside, today=TODAY,
        )
        self.assertEqual((lead['external'], lead['branch_id'], lead['branch_name']), (True, 'b-shoham', 'שוהם — מתנ"ס'))
        ours = lead_ai.from_model(
            model_lead(), lines=self.LINES, known=Card(branch_id='b-rosh', branch_name='ראש העין', city='ראש העין'),
            external_places=outside, today=TODAY,
        )
        self.assertFalse(ours['external'])


# --- the keyword rules, for a day with no key -------------------------------------------------------------

class RulesTests(LeadsCase):
    def read(self, *texts, lines=None, **known):
        lines = said(*texts) if lines is None else lines
        return lead_ai.from_rules(lines, Card(**known), today=TODAY)

    def test_what_holds_them_back(self):
        cases = [
            ('will_check', ('אוקיי, אבדוק ואחזור אליכם',), {}),
            ('will_check', ('תודה, אחשוב על זה',), {}),
            ('ask_someone', ('אבדוק עם בעלי ואחזור',), {}),
            ('ask_someone', ('אני צריכה לשאול את הבת שלי',), {}),
            ('call_someone', ('עדיף שתדברו עם בעלי',), {}),
            ('later', ('תודה, לא עכשיו',), {}),
            ('schedule', ('הימים האלה לא מתאימים לנו',), {}),
            ('commitment', ('ומה אם זה לא יתאים לו? מחזירים כסף?',), {}),
            ('group', ('כמה ילדים יש בקבוצה?',), {}),
            ('child_not_ready', ('הוא לא רצה להמשיך, אולי בשנה הבאה',), {}),
            ('site_problem', ('הקישור לא עובד לי',), {}),
            ('site_problem', ('ניסיתי אבל החוג לא מופיע ברשימה',), {}),
            ('site_problem', ('כתוב שהקבוצה מלא, אין דרך להיכנס?',), {}),
            ('reschedule_trial', ('אפשר לשנות את המועד של הניסיון?',), {}),
            ('no_class', ('אין לכם חוג למבוגרים?',), {}),
            ('price', ('מה המחיר?',), {'flags': ['price']}),
            ('full', ('רציתי להירשם',), {'flags': ['class_full']}),
            ('no_branch', ('יש לכם בנתניה?',), {'flags': ['no_branch_nearby']}),
            ('too_young', ('הוא קטן מדי?',), {'flags': ['child_too_young']}),
            ('too_young', ('היא בת שנתיים וחצי',), {}),
        ]
        for blocker, texts, known in cases:
            with self.subTest(text=texts[0]):
                lead = self.read(*texts, **known)
                self.assertEqual(lead['blocker'], blocker)
                self.assertTrue(lead['blocker_text'])
        self.assertEqual(self.read('אפשר פרטים על חוג קפוארה?')['blocker'], '')
        self.assertEqual(self.read('אבדוק עם בעלי ואחזור')['blocker_text'], 'צריך לבדוק עם מישהו אחר: "אבדוק עם בעלי ואחזור"')

    def test_a_question_that_got_no_answer(self):
        lines = (
            said('כמה ילדים יש בקבוצה?') + said('אין לי את הפרט הזה כרגע, אפשר לבדוק במשרד', who='bot')
        )
        lead = self.read(lines=lines)
        self.assertEqual((lead['blocker'], lead['blocker_text']), ('open_question', 'כמה ילדים יש בקבוצה?'))
        # Answered after all: no longer open.
        answered = lines + said('עד 12 ילדים', who='office')
        self.assertNotEqual(self.read(lines=answered)['blocker'], 'open_question')

    def test_not_interested_only_when_it_is_said_outright(self):
        for text in ('לא מעוניינת, תודה', 'תודה, לא', 'טעות', 'לא רלוונטי', 'מצאנו מקום אחר', 'תורידו אותי בבקשה',
                     'יקר לי, אני מוותרת'):
            with self.subTest(text=text):
                lead = self.read('אפשר פרטים על קפוארה?', text)
                self.assertEqual((lead['interest'], lead['quote']), ('no', text))
                self.assertEqual(lead['blocker'], '')
        # "לא" alone answers whatever was asked last; a question is no goodbye; an option turned down is not the class.
        for text in ('לא', 'לא!!', 'לא מעוניינת בקפוארה, יש מחול?', 'יום שלישי לא רלוונטי לנו', 'לא כרגע', 'אבדוק ואחזור'):
            with self.subTest(text=text):
                self.assertNotEqual(self.read('אפשר פרטים על קפוארה?', text)['interest'], 'no')

    def test_silence_is_not_a_no(self):
        lead = self.read('אפשר פרטים על חוג קפוארה?')
        self.assertEqual((lead['interest'], lead['quote']), ('unsure', ''))
        self.assertEqual(self.read('רוצה להירשם', interest='hot', topic='registration')['interest'], 'yes')

    def test_we_already_signed_up(self):
        for text in ('בסוף רשמנו אותה, תודה', 'אנחנו כבר אצלכם 🙂', 'כבר נרשמנו'):
            with self.subTest(text=text):
                lead = self.read('אפשר פרטים?', text)
                self.assertEqual((lead['interest'], lead['quote']), ('registered', text))
        # A trial lesson is not a registration, and a question is not a statement.
        self.assertNotEqual(self.read('כבר נרשמנו לשיעור ניסיון')['interest'], 'registered')
        self.assertNotEqual(self.read('כבר נרשמנו?')['interest'], 'registered')

    def test_what_the_conversation_is_about(self):
        self.assertEqual(self.read('אפשר פרטים על חוג קפוארה?')['intent'], 'class')
        self.assertEqual(self.read('אפשר שיעור ניסיון?', topic='trial')['intent'], 'trial')
        self.assertEqual(self.read('הבן שלי רשום, רוצים להוסיף לו גם חוג אקרובטיקה')['intent'], 'add_class')
        for intent, text in (
            ('birthday_greeting', 'אשמח לקבל ברכה אישית מקוגומלו'), ('birthday_party', 'אתם עושים ימי הולדת?'),
            ('rental', 'אפשר לשכור את הסטודיו?'), ('store', 'איפה קונים חליפה?'),
            ('auto_reply', 'תודה שפנית אלינו! נחזור אליך בהקדם. הודעה אוטומטית'),
        ):
            with self.subTest(intent=intent):
                lead = self.read(text, topic='other')
                self.assertEqual(lead['intent'], intent)
                self.assertNotIn(lead['intent'], lead_ai.LEAD_INTENTS)
                self.assertEqual(lead['summary'], '')
        # Nothing the keywords can tell: left empty — and an empty intent is a lead.
        self.assertEqual(self.read('יש לי שאלה קטנה', topic='other')['intent'], '')

    def test_an_opening_with_nothing_of_their_own(self):
        for texts in (('היי',), ('שלום! אפשר לקבל מידע נוסף על זה?',), ('היי', 'אפשר לקבל מידע נוסף על זה?')):
            with self.subTest(texts=texts):
                lead = self.read(*texts, topic='other')
                self.assertEqual((lead['empty_opening'], lead['intent']), (True, 'class'))
        self.assertFalse(self.read('היי', 'לילדה בת 5')['empty_opening'])

    def test_an_age_said_in_words(self):
        for text, age in (
            ('הוא בן שנתיים ושבעה חודשים', (2, 7)), ('בת 2 ו-7 חודשים', (2, 7)), ('בן שנתיים וחצי', (2, 6)),
            ('היא בת 2.5', (2, 6)), ('בת שנה ו-8', (1, 8)), ('בן 10 חודשים', (0, 10)), ('הבת שלי בת 5', (5, 0)),
            ('בן 5 ו-7', (5, 0)), ('חוג לגיל 4', (4, 0)), ('בן שלוש', (3, 0)),
        ):
            with self.subTest(text=text):
                self.assertEqual(lead_ai.spoken_age(text), age)
        self.assertIsNone(lead_ai.spoken_age('אפשר פרטים?'))

    def test_the_day_to_come_back_by_rules(self):
        young = self.read('היא בת שנתיים ושבעה חודשים, אפשר?')
        self.assertEqual((young['blocker'], young['come_back_on'], young['come_back_why']), ('too_young', '2027-03-12', 'הילד יהיה בן 3'))
        self.assertEqual(young['child'], {'name': '', 'age_years': 2, 'age_months': 7, 'grade': '', 'birth_date': ''})
        self.assertEqual(self.read('נחזור אליכם בעוד חצי שנה')['come_back_on'], '2027-04-12')
        self.assertEqual(self.read('אולי בעוד 3 חודשים')['come_back_on'], '2027-01-12')
        self.assertEqual(self.read('נדבר ב-1.11')['come_back_on'], '2026-11-01')
        self.assertEqual(self.read('נדבר ב-1.9')['come_back_on'], '2027-09-01')          # behind us this year: the next one
        special_day('חנוכה', date(2026, 12, 6), date(2026, 12, 12))
        after = self.read('מתאים, רק שנוכל להגיע אחרי החגים')
        self.assertEqual((after['come_back_on'], after['come_back']['kind']), ('2026-12-13', 'after'))
        # A stretch of time that is no answer to "when shall we talk": nothing.
        self.assertIsNone(self.read('החוג מתחיל בעוד חצי שנה?')['come_back_on'])
        self.assertIsNone(self.read('אפשר פרטים?')['come_back_on'])
        # The card's own "they said when" (the older rules) is the day when nothing else is.
        self.assertEqual(self.read('אחזור אליכם', callback_on=date(2026, 10, 26))['come_back_on'], '2026-10-26')

    def test_one_sentence_of_why_this_is_a_lead(self):
        lead = lead_ai.from_rules(
            said('אפשר פרטים על קפוארה? הוא בן 5'), Card(course_type='קפוארה', branch_name='ראש העין', child_age='5'),
            today=TODAY, link_sent=True,
        )
        self.assertEqual(lead['summary'], 'שאלו על קפוארה בראש העין, גיל 5; קיבלו קישור ולא נרשמו')
        self.assertEqual(self.read('אבדוק עם בעלי', course_type='מחול')['summary'], 'שאלו על מחול; צריך לבדוק עם מישהו אחר')
        self.assertEqual(lead_ai.from_rules([], Card(), today=TODAY)['summary'], '')

    def test_the_child_as_a_chip_says_it(self):
        self.assertEqual(lead_ai.child_words({'name': 'נועה', 'age_years': 5}), 'נועה, גיל 5')
        self.assertEqual(lead_ai.child_words({'age_years': 2, 'age_months': 7}), 'גיל שנתיים ו-7 חודשים')
        self.assertEqual(lead_ai.child_words({'age_years': 0, 'age_months': 8}), 'גיל 8 חודשים')
        self.assertEqual(lead_ai.child_words({'grade': 'כיתה ב'}), 'כיתה ב')
        self.assertEqual(lead_ai.child_words({}), '')
        self.assertEqual(lead_ai.child_words(None), '')


class RegistrationLinkTests(LeadsCase):
    LINK = 'https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/'
    ENCODED = 'https://cogomelo.co.il/%D7%94%D7%A8%D7%A9%D7%9E%D7%94-%D7%9C%D7%97%D7%95%D7%92%D7%99%D7%9D-%D7%A9%D7%99%D7%A2%D7%95%D7%A8-%D7%A0%D7%99%D7%A1%D7%99%D7%95%D7%9F/'

    def test_the_link_is_known_however_whatsapp_spells_it(self):
        self.assertTrue(lead_ai.has_registration_link(f'נרשמים כאן: {self.LINK}'))
        self.assertTrue(lead_ai.has_registration_link(f'נרשמים כאן: {self.ENCODED}'))
        self.assertTrue(lead_ai.has_registration_link('cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון'))
        self.assertFalse(lead_ai.has_registration_link('האתר שלנו: https://cogomelo.co.il/'))
        self.assertFalse(lead_ai.has_registration_link('הרשמה מול העירייה: https://shoham.example/register'))
        self.assertFalse(lead_ai.has_registration_link('שלום'))
        self.assertFalse(lead_ai.has_registration_link(''))

    def test_the_link_the_owner_filed_is_the_one_looked_for(self):
        knowledge.create_item({'kind': 'link', 'key': 'registration', 'title': 'הרשמה', 'url': 'https://kogo.example/signup/'})
        self.assertTrue(lead_ai.has_registration_link('להרשמה: https://kogo.example/signup'))
        self.assertFalse(lead_ai.has_registration_link(self.LINK))

    def test_a_link_that_goes_out_is_marked_on_the_contact(self):
        self.incoming('אפשר פרטים על קפוארה?')
        self.assertIsNone(self.contact().lead_link_sent_at)
        self.bot_reply('בשמחה, הכתובת רפאל איתן 5')
        self.assertIsNone(self.contact().lead_link_sent_at)
        self.clock.forward(minutes=1)
        self.bot_reply(f'נרשמים כאן: {self.LINK}')
        contact = self.contact()
        self.assertEqual(contact.lead_link_sent_at, self.clock())
        self.assertEqual(contact.lead_stage, 'link_sent')            # at once: the last word is ours
        # The customer's own message never marks it, whatever it carries.
        self.incoming(f'זה הקישור? {self.LINK}', phone='972505550102')
        self.assertIsNone(self.contact('972505550102').lead_link_sent_at)

    @override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
    def test_the_offices_own_message_marks_it_and_a_send_that_failed_does_not(self):
        self.incoming('אפשר פרטים?', subscriber_id='4242')
        contact = self.contact()
        from apps.core.manychat_service import ManyChatError
        with patch('apps.core.manychat_service.ManyChatService._request', side_effect=ManyChatError('no', status_code=400)):
            self.post(f'contacts/{contact.id}/send/', {'text': f'להרשמה: {self.LINK}'})
        self.assertIsNone(self.contact().lead_link_sent_at)
        with patch('apps.core.manychat_service.ManyChatService._request', return_value={'status': 'success'}):
            response = self.post(f'contacts/{contact.id}/send/', {'text': f'להרשמה: {self.LINK}'})
        self.assertEqual(response.data['message']['status'], 'sent')
        self.assertIsNotNone(self.contact().lead_link_sent_at)
        self.assertEqual(response.data['contact']['lead']['stage'], 'link_sent')

    def test_the_summary_finds_a_link_that_went_out_before_anybody_marked_it(self):
        self.incoming('אפשר פרטים על קפוארה?')
        contact = self.contact()
        sent_at = self.clock() - 3 * DAY
        Message.objects.create(
            contact=contact, direction='out', sender='bot', text=f'נרשמים כאן: {self.ENCODED}', status='sent', sent_at=sent_at,
        )
        Message.objects.create(
            contact=contact, direction='out', sender='office', text=f'שוב: {self.LINK}', status='failed', sent_at=self.clock(),
        )
        self.assertEqual(lead_ai.link_sent_at(contact), sent_at)
        analysis.analyze_contact(contact, use_ai=False)
        contact = self.contact()
        self.assertEqual(contact.lead_link_sent_at, sent_at)
        self.assertTrue(contact.lead_ai['link_sent'])


# --- the summary pass: the same call, one more object -----------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-anthropic-key', WAHUB_AI_MODEL='claude-sonnet-5-5')
class SummaryPassTests(LeadsCase):
    def setUp(self):
        super().setUp()
        city = TestDataFactory.create_city('ראש העין')
        self.branch = TestDataFactory.create_branch(name='קרל וגרטי קורי 8', city=city)
        self.outside = TestDataFactory.create_branch(
            name='שוהם — מתנ"ס', city=TestDataFactory.create_city('שוהם'), is_external=True,
        )
        self.kind = TestDataFactory.create_course_type('קפוארה')
        self.incoming('היי, אפשר פרטים על קפוארה בראש העין?', name='רותם')
        self.bot_reply('בשמחה! בן כמה הילד?')
        self.clock.forward(minutes=1)
        self.incoming('הוא בן שנתיים ושבעה חודשים')

    def card(self, **lead):
        return dict(CARD, callback_on='', flags=[], lead=model_lead(**lead))

    def analyze(self, card):
        with patch(POST, return_value=claude(card)) as post:
            analysis.analyze_contact(self.contact())
        return post

    def test_one_call_fills_the_card_and_the_lead(self):
        post = self.analyze(self.card(
            interest='unsure', blocker='too_young', blocker_text='הילד עוד לא בן 3',
            child={**NO_LEAD['child'], 'age_years': 2, 'age_months': 7},
            come_back={**NO_LEAD['come_back'], 'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'},
            come_back_why='הילד יהיה בן 3', summary='אמא לילד בן שנתיים ושבעה חודשים בראש העין, שאלה על קפוארה',
        ))
        post.assert_called_once()                       # the very call that fills "מה ידוע" — never a second one
        body = post.call_args.kwargs['json']
        schema = body['output_config']['format']['schema']
        self.assertEqual(schema['properties']['lead'], lead_ai.LEAD_SCHEMA)
        self.assertIn('lead', schema['required'])
        for part in (schema['properties']['lead'], *schema['properties']['lead']['properties'].values()):
            if part.get('type') == 'object':
                self.assertFalse(part['additionalProperties'])
                self.assertEqual(set(part['required']), set(part['properties']))
        self.assertIn('- lead:', body['system'])
        self.assertNotIn('{lead}', body['system'])
        prompt = body['messages'][0]['content']
        self.assertIn('התחומים של העסק', prompt)
        self.assertIn('קפוארה', prompt)
        self.assertIn('שוהם — מתנ"ס — שוהם', prompt)               # the external branch, said to be one

        contact = self.contact()
        lead = contact.lead_ai
        self.assertEqual((lead['source'], lead['intent'], lead['interest'], lead['blocker']), ('ai', 'class', 'unsure', 'too_young'))
        self.assertEqual((lead['branch_id'], lead['course_type_id']), (str(self.branch.id), str(self.kind.id)))
        self.assertEqual(lead['come_back_on'], '2027-03-12')
        self.assertEqual(lead['summary'], 'אמא לילד בן שנתיים ושבעה חודשים בראש העין, שאלה על קפוארה')
        # The card's own "they said when" follows the day worked out in code: one date on both screens.
        self.assertEqual(contact.known_callback_on, date(2027, 3, 12))
        self.assertEqual(contact.known_topic, 'trial')
        self.assertEqual(ContactEvent.objects.filter(kind='analyzed').count(), 1)

    def test_the_models_own_arithmetic_is_not_believed_over_what_was_said(self):
        card = self.card(come_back={**NO_LEAD['come_back'], 'kind': 'offset', 'months': 6, 'said_on': '2026-10-12'})
        card['callback_on'] = '2027-01-01'              # the model's own date, and wrong
        self.analyze(card)
        contact = self.contact()
        self.assertEqual(contact.lead_ai['come_back_on'], '2027-04-12')
        self.assertEqual(contact.known_callback_on, date(2027, 4, 12))

    def test_with_no_key_the_rules_fill_the_lead(self):
        with override_settings(ANTHROPIC_API_KEY=''), patch(POST) as post:
            analysis.analyze_contact(self.contact())
        post.assert_not_called()
        self.network.assert_not_called()
        lead = self.contact().lead_ai
        self.assertEqual((lead['source'], lead['blocker'], lead['come_back_on']), ('rules', 'too_young', '2027-03-12'))
        self.assertEqual(self.contact().known_callback_on, date(2027, 3, 12))

    def test_claude_failing_falls_back_to_the_rules_for_the_lead_too(self):
        import requests
        with patch(POST, side_effect=requests.Timeout('slow')):
            analysis.analyze_contact(self.contact())
        self.assertEqual(self.contact().lead_ai['source'], 'rules')

    def test_the_summary_never_touches_a_mark_a_person_made(self):
        Contact.objects.update(
            followup_status='later', followup_due=TODAY + timedelta(days=3), followup_note='לחזור אחרי החגים',
            followup_by=self.manager, followup_at=self.clock(), lead_manual={'owner': {'value': 'office'}},
        )
        marks = Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get()
        self.analyze(self.card(interest='no', quote='הוא בן שנתיים ושבעה חודשים'))
        with override_settings(ANTHROPIC_API_KEY=''):
            analysis.analyze_contact(self.contact())
        self.assertEqual(Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get(), marks)

    def test_the_same_answer_again_wakes_no_screen(self):
        card = self.card(interest='unsure')
        self.analyze(card)
        touched = self.contact().touched_at
        self.clock.forward(minutes=5)
        self.analyze(card)
        self.assertEqual(self.contact().touched_at, touched)
        self.assertEqual(ContactEvent.objects.filter(kind='analyzed').count(), 1)

    def test_older_conversations_are_read_as_leads_a_few_a_call(self):
        Contact.objects.update(needs_analysis=False, analyzed_at=self.clock())
        for index in range(5):
            self.lead(index * DAY + HOUR)
        old = self.lead(120 * DAY)                                      # too long ago to be worth a call
        customer = self.lead(HOUR, kogo_outcome='customer_before')      # a registered customer is no lead
        Contact.objects.update(kogo_checked_at=self.clock())            # matched a moment ago: the registrations stand as written here
        with override_settings(ANTHROPIC_API_KEY=''):
            counts = cron.tick()
            self.assertEqual(counts['lead_understood'], 3)
            self.assertEqual(counts['pending_lead_understanding'], 3)
            self.assertEqual(cron.tick()['lead_understood'], 3)
            self.assertEqual(cron.tick()['lead_understood'], 0)
        self.assertEqual(Contact.objects.filter(lead_ai={}).count(), 2)
        for contact in (old, customer):
            self.assertEqual(Contact.objects.get(pk=contact.pk).lead_ai, {})
        with override_settings(WAHUB_LEAD_BACKFILL_PER_TICK=0):
            self.lead(HOUR)
            self.assertEqual(cron.tick()['lead_understood'], 0)

    def test_the_pass_over_older_conversations_asks_claude_only_when_the_owner_said_so(self):
        Contact.objects.update(needs_analysis=False, analyzed_at=self.clock(), kogo_checked_at=self.clock())
        with patch(POST, return_value=claude(self.card(interest='unsure'))) as post:
            self.assertEqual(cron.tick()['lead_understood'], 1)
        post.assert_not_called()                        # there is a key — and still the rules: Claude is money
        self.assertEqual(self.contact().lead_ai['source'], 'rules')
        Contact.objects.update(lead_ai={})
        with override_settings(WAHUB_LEAD_BACKFILL_AI=True), patch(POST, return_value=claude(self.card(interest='unsure'))) as post:
            self.assertEqual(cron.tick()['lead_understood'], 1)
        post.assert_called_once()
        self.assertEqual(self.contact().lead_ai['source'], 'ai')


# --- the stages, and who makes the next move ----------------------------------------------------------------

class StageTests(LeadsCase):
    def got(self, contact, now=None):
        result = self.stage(contact, now)
        return result['stage'], result['owner']

    def test_what_holds_them_back_decides_the_stage_and_the_owner(self):
        link = dict(lead_link_sent_at=self.clock() - HOUR)
        for blocker, expected in (
            ('will_check', ('will_check', 'bot')), ('ask_someone', ('will_check', 'bot')),
            ('no_branch', ('nothing_to_offer', '')), ('no_class', ('nothing_to_offer', '')), ('full', ('nothing_to_offer', '')),
            ('site_problem', ('signup_stuck', 'office')),
            ('open_question', ('needs_reply', 'office')), ('reschedule_trial', ('needs_reply', 'office')),
            ('call_someone', ('needs_reply', 'office')),
            # Nothing of their own to decide the stage: the conversation's clock does.
            ('later', ('in_conversation', '')), ('too_young', ('in_conversation', '')), ('group', ('in_conversation', '')),
            ('instructor', ('in_conversation', '')), ('other', ('in_conversation', '')), ('', ('in_conversation', '')),
        ):
            with self.subTest(blocker=blocker):
                self.assertEqual(self.got(self.lead(said={'blocker': blocker, 'blocker_text': 'כך נאמר'})), expected)
        for blocker, expected in (
            ('', ('link_sent', 'bot')), ('commitment', ('link_sent', 'bot')), ('group', ('link_sent', 'bot')),
            # The price or the days: nothing new to tell them — the bot does not come back by itself.
            ('price', ('link_sent', '')), ('schedule', ('link_sent', '')),
            ('will_check', ('will_check', 'bot')), ('full', ('nothing_to_offer', '')), ('site_problem', ('signup_stuck', 'office')),
        ):
            with self.subTest(blocker=blocker, link=True):
                self.assertEqual(self.got(self.lead(said={'blocker': blocker, 'blocker_text': 'כך נאמר'}, **link)), expected)

    def test_the_new_stages_in_words(self):
        stuck = self.stage(self.lead(said={'blocker': 'site_problem', 'blocker_text': 'הקישור לא עובד'}))
        self.assertEqual((stuck['label'], stuck['group'], stuck['follow']), ('נתקע בהרשמה', 'act', None))
        self.assertEqual(stuck['reason'], 'שאל על קפוארה בראש העין היום ב-11:00. הקישור לא עובד.')
        self.assertEqual(stuck['next_step'], 'להתקשר היום ולסיים איתו את ההרשמה')

        question = self.stage(self.lead(said={'blocker': 'open_question', 'blocker_text': 'כמה ילדים יש בקבוצה?'}))
        self.assertEqual((question['stage'], question['label']), ('needs_reply', 'שאל ולא קיבל מענה'))
        self.assertEqual(question['reason'], 'שאל על קפוארה בראש העין היום ב-11:00. שאל ולא קיבל תשובה: כמה ילדים יש בקבוצה?')

        checking = self.stage(self.lead(said={'blocker': 'ask_someone', 'blocker_text': 'אמרה שתבדוק עם הבת שלה ותחזור'}))
        self.assertEqual((checking['label'], checking['group']), ('אמר שיבדוק ויחזור', 'wait'))
        self.assertEqual(checking['reason'], 'שאל על קפוארה בראש העין היום ב-11:00. אמרה שתבדוק עם הבת שלה ותחזור.')
        self.assertEqual((checking['follow']['kind'], checking['follow']['delays'], checking['follow']['hot']), ('will_check', (DAY,), True))

        nothing = self.stage(self.lead(said={'blocker': 'full', 'blocker_text': 'הקבוצה מלאה'}))
        self.assertEqual((nothing['label'], nothing['group'], nothing['follow']), ('אין כרגע מה להציע', 'wait', None))
        self.assertEqual(nothing['next_step'], 'אין מה לעשות עכשיו — לחזור אליו כשייפתח משהו מתאים')

        link = self.stage(self.lead(lead_link_sent_at=self.clock() - HOUR))
        self.assertEqual((link['label'], link['group']), ('קיבל קישור ועוד לא נרשם', 'act'))
        self.assertEqual(link['reason'], 'שאל על קפוארה בראש העין היום ב-11:00. קיבל קישור להרשמה היום ב-11:00. לא נרשם.')
        self.assertEqual((link['follow']['kind'], link['follow']['delays']), ('link_reminder', (DAY, 3 * DAY, 7 * DAY)))
        self.assertEqual((link['follow']['hot'], link['follow']['variant']), (False, ''))

    def test_what_only_a_person_can_settle_is_settled_once_a_person_answered(self):
        for blocker in ('site_problem', 'open_question', 'call_someone'):
            with self.subTest(blocker=blocker):
                handled = self.lead(answer='office', said={'blocker': blocker, 'blocker_text': 'כך נאמר'})
                self.assertEqual(self.got(handled), ('in_conversation', ''))
                spoke = self.lead(
                    said={'blocker': blocker, 'blocker_text': 'כך נאמר'}, followup_status='answered', followup_at=self.clock(),
                )
                self.assertEqual(self.got(spoke)[0], 'in_conversation')
                # A mark from before their message answered nothing of it.
                earlier = self.lead(
                    said={'blocker': blocker, 'blocker_text': 'כך נאמר'}, followup_status='answered',
                    followup_at=self.clock() - 2 * DAY,
                )
                self.assertEqual(self.got(earlier)[1], 'office')

    def test_not_interested_only_with_the_customers_words(self):
        quote = {'interest': 'no', 'quote': 'תודה, לא רלוונטי', 'quote_at': (self.clock() - HOUR).isoformat()}
        closed = self.stage(self.lead(said=quote))
        self.assertEqual((closed['stage'], closed['group'], closed['owner'], closed['follow']), ('not_interested', 'closed', '', None))
        self.assertEqual(closed['reason'], f'שאל על קפוארה בראש העין ב-12.10. כתב: "תודה, לא רלוונטי".')
        self.assertEqual(closed['since'], self.clock() - HOUR)
        # "no" with no words behind it never reaches the contact (lead_ai.from_model) — and if it did, it would not close.
        self.assertEqual(self.got(self.lead(said={'interest': 'no', 'quote': ''}))[0], 'in_conversation')
        # Silence, "unsure", the older summary's "not now": open.
        self.assertEqual(self.got(self.lead(5 * DAY, said={'interest': 'unsure'}))[0], 'answered_silent')
        self.assertEqual(self.got(self.lead(5 * DAY, known_interest='cold'))[0], 'answered_silent')
        self.assertEqual(self.got(self.lead(5 * DAY))[0], 'answered_silent')
        # It closes whatever else is true of the lead — nobody goes on writing to them.
        trial = self.lead(said=quote, kogo_outcome='trial_only', kogo_trial={'on': '2026-10-11', 'outcome': 'attended', 'course': ''})
        self.assertEqual(self.got(trial)[0], 'not_interested')

    def test_a_closed_lead_opens_again_when_the_customer_writes(self):
        quote = {'interest': 'no', 'quote': 'לא מעוניינת', 'quote_at': (self.clock() - 3 * DAY).isoformat()}
        contact = self.lead(3 * DAY, said=quote)
        self.assertEqual(self.got(contact)[0], 'not_interested')
        # A new message, newer than the summary: its "no" is about a conversation that has moved on.
        Contact.objects.filter(pk=contact.pk).update(
            last_inbound_at=self.clock(), last_message_at=self.clock(), last_message_direction='in', waiting_since=self.clock(),
        )
        contact.refresh_from_db()
        self.assertEqual(self.got(contact)[0], 'in_conversation')
        # ... and the next summary decides anew: the same words again close it again.
        Contact.objects.filter(pk=contact.pk).update(analyzed_at=self.clock() + MINUTE)
        contact.refresh_from_db()
        self.assertEqual(self.got(contact, self.clock() + 2 * MINUTE)[0], 'not_interested')

        # The office's own "לא רלוונטי" — and the close of the leads screen — open the same way.
        marked = self.lead(3 * DAY, followup_status='not_relevant', followup_at=self.clock() - 2 * DAY)
        self.assertEqual(self.got(marked)[0], 'not_interested')
        Contact.objects.filter(pk=marked.pk).update(last_inbound_at=self.clock(), last_message_at=self.clock())
        marked.refresh_from_db()
        self.assertEqual(self.got(marked)[0], 'in_conversation')
        self.assertEqual(marked.followup_status, 'not_relevant')           # the mark itself is a person's: not touched

    def test_already_registered_on_the_customers_word(self):
        result = self.stage(self.lead(said={
            'interest': 'registered', 'quote': 'בסוף רשמנו אותה, תודה', 'quote_at': (self.clock() - HOUR).isoformat(),
        }))
        self.assertEqual((result['stage'], result['group'], result['owner']), ('registered', 'closed', ''))
        self.assertEqual(result['reason'], 'הלקוח אמר שכבר רשום: "בסוף רשמנו אותה, תודה".')

    def test_a_conversation_that_is_not_about_a_class_is_no_lead(self):
        for intent, label in (
            ('birthday_greeting', 'ברכת יום הולדת'), ('birthday_party', 'הפעלה ליום הולדת'), ('rental', 'השכרת סטודיו'),
            ('supplier', 'ספק שמציע שירות'), ('store', 'קנייה בחנות'), ('auto_reply', 'מענה אוטומטי של עסק'),
            ('existing_question', 'לקוח קיים — שאלה תפעולית'), ('other', 'פנייה שאינה על חוג'),
        ):
            with self.subTest(intent=intent):
                result = self.stage(self.lead(5 * DAY, said={'intent': intent}, known_topic='other', needs_human=True))
                self.assertEqual((result['stage'], result['group'], result['owner'], result['follow']), ('', '', '', None))
                self.assertEqual(result['reason'], f'לא ליד לחוגים: {label}.')
                self.assertIsNone(result['recheck_at'])
        # The intent and the topic of the card have to agree: one of them calling it a class keeps the lead.
        self.assertEqual(self.got(self.lead(5 * DAY, said={'intent': 'rental'}, known_topic='info'))[0], 'answered_silent')
        for intent in ('class', 'trial', 'add_class', ''):
            with self.subTest(intent=intent):
                self.assertEqual(self.got(self.lead(5 * DAY, said={'intent': intent}, known_topic='other'))[0], 'answered_silent')
        # A person's own decision still shows, and so does a registration.
        deleted = self.lead(said={'intent': 'rental'}, known_topic='other', lead_manual={
            'closed': {'reason': 'deleted', 'at': self.clock().isoformat(), 'by_name': 'דנה'},
        })
        self.assertEqual(self.got(deleted)[0], 'deleted')
        self.assertEqual(self.got(self.lead(said={'intent': 'other'}, known_topic='other', kogo_outcome='customer_before'))[0], 'customer')

    def test_a_customer_who_wants_one_more_class_is_a_lead(self):
        asks = self.lead(5 * DAY, said={'intent': 'add_class'}, kogo_outcome='customer_before', kogo_detail='לקוח רשום מ-1.9 · נועה לוי')
        self.assertEqual(self.got(asks), ('answered_silent', 'bot'))
        plain = self.lead(5 * DAY, said={'intent': 'existing_question'}, kogo_outcome='customer_before')
        self.assertEqual(self.got(plain), ('customer', ''))
        self.assertEqual(self.got(self.lead(5 * DAY, kogo_outcome='customer_before')), ('customer', ''))

    def test_a_registration_link_makes_a_lead_of_its_own_once_the_last_word_is_ours(self):
        link = dict(lead_link_sent_at=self.clock() - 2 * HOUR)
        self.assertEqual(self.got(self.lead(HOUR, **link)), ('link_sent', 'bot'))
        self.assertEqual(self.got(self.lead(4 * DAY, **link)), ('link_sent', 'bot'))
        # The customer has just written: still talking. Quiet for two days: a link nobody used.
        self.assertEqual(self.got(self.lead(HOUR, answer=None, **link), self.clock() - 50 * MINUTE)[0], 'in_conversation')
        with patch.object(lead_stage, 'replies_copied_since', return_value=None):
            self.assertEqual(self.got(self.lead(HOUR, answer=None, **link))[0], 'in_conversation')
            self.assertEqual(self.got(self.lead(3 * DAY, answer=None, **link))[0], 'link_sent')
        # Registered, a trial ahead, a month of silence: other stages.
        self.assertEqual(self.got(self.lead(HOUR, kogo_outcome='registered_after', **link))[0], 'registered')
        self.assertEqual(self.got(self.lead(HOUR, kogo_outcome='trial_upcoming', **link))[0], 'trial_booked')
        self.assertEqual(self.got(self.lead(40 * DAY, **link))[0], 'cold')

    def test_a_day_to_come_back_is_the_bots_when_the_conversation_named_it_and_the_offices_when_it_set_it(self):
        in_march = date(2027, 3, 12)
        too_young = {
            'blocker': 'too_young', 'come_back': {'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'},
            'come_back_on': '2027-03-12', 'come_back_why': 'הילד יהיה בן 3',
        }
        waiting = self.stage(self.lead(said=too_young, known_callback_on=in_march))
        self.assertEqual((waiting['stage'], waiting['label'], waiting['owner']), ('waiting_date', 'מחכה למועד', 'bot'))
        self.assertEqual(waiting['reason'], 'שאל על קפוארה בראש העין היום ב-11:00. לפי השיחה, לחזור אליו ב-12.3.2027 (הילד יהיה בן 3).')
        self.assertEqual(waiting['come_back_on'], in_march)
        # The follow-up is planned ahead, for the day itself: that day and a week later — not three times.
        self.assertEqual(waiting['follow']['kind'], 'date_arrived')
        self.assertEqual((waiting['follow']['delays'], waiting['follow']['variant']), ((timedelta(0), 7 * DAY), 'age'))
        self.assertEqual(timezone.localtime(waiting['follow']['anchor']).date(), in_march)

        said_later = self.stage(self.lead(known_callback_on=TODAY + timedelta(days=9)))
        self.assertEqual((said_later['owner'], said_later['follow']['delays']), ('bot', (timedelta(0), 3 * DAY, 7 * DAY)))
        not_ready = self.stage(self.lead(
            said={'blocker': 'child_not_ready', 'come_back_on': '2027-09-01'}, known_callback_on=date(2027, 9, 1),
        ))
        self.assertEqual((not_ready['follow']['delays'], not_ready['follow']['variant']), ((timedelta(0),), 'not_ready'))

        # The day has come: the bot goes back to them — it is not the office's to call.
        arrived = self.stage(self.lead(8 * DAY, known_callback_on=TODAY - timedelta(days=1)))
        self.assertEqual((arrived['stage'], arrived['owner'], arrived['follow']['kind']), ('callback_due', 'bot', 'date_arrived'))
        # Long past, and nobody ever went back: the day is over, and the conversation's clock decides.
        self.assertEqual(self.got(self.lead(50 * DAY, known_callback_on=TODAY - timedelta(days=30)))[0], 'cold')

        # The office's own day: nobody has a step until it comes, and then it is the office's.
        put_off = self.stage(self.lead(followup_status='later', followup_due=TODAY + timedelta(days=4), followup_at=self.clock()))
        self.assertEqual((put_off['stage'], put_off['owner'], put_off['follow']), ('waiting_date', '', None))
        due = self.stage(self.lead(3 * DAY, followup_status='later', followup_due=TODAY, followup_at=self.clock() - 2 * DAY))
        self.assertEqual((due['stage'], due['owner'], due['follow']), ('callback_due', 'office', None))
        owed = self.stage(self.lead(followup_status='waiting_us', followup_at=self.clock()))
        self.assertEqual((owed['stage'], owed['owner']), ('callback_due', 'office'))

    def test_what_a_person_marked_stands_above_what_the_registrations_show(self):
        trial = dict(kogo_outcome='trial_only', kogo_trial={'on': '2026-10-11', 'outcome': 'attended', 'course': ''})
        self.assertEqual(self.got(self.lead(2 * DAY, **trial)), ('trial_done', 'bot'))
        put_off = self.lead(2 * DAY, followup_status='later', followup_due=TODAY + timedelta(days=10), followup_at=self.clock(), **trial)
        self.assertEqual(self.got(put_off), ('waiting_date', ''))            # the bot does not write to somebody the office put off
        owed = self.lead(2 * DAY, followup_status='waiting_us', followup_at=self.clock(), **trial)
        self.assertEqual(self.got(owed), ('callback_due', 'office'))
        # A day the customer named after the trial comes before "how was it"; one named before the trial does not.
        after = self.lead(
            2 * DAY, known_callback_on=TODAY + timedelta(days=20),
            said={'come_back': {'kind': 'after', 'what': 'החגים', 'said_on': '2026-10-12'}, 'come_back_on': '2026-11-01'}, **trial,
        )
        self.assertEqual(self.got(after), ('waiting_date', 'bot'))
        before = self.lead(
            2 * DAY, known_callback_on=TODAY + timedelta(days=20),
            said={'come_back': {'kind': 'after', 'what': 'החגים', 'said_on': '2026-10-01'}, 'come_back_on': '2026-11-01'}, **trial,
        )
        self.assertEqual(self.got(before), ('trial_done', 'bot'))

    def test_hot_leads_are_the_bots_first_and_the_offices_when_they_are_talking_or_too_old(self):
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        came = dict(kogo_outcome='trial_only', kogo_detail='ניסיון ב-11.10, הגיע · נועה לוי',
                    kogo_trial={'on': yesterday, 'outcome': 'attended', 'course': 'קפוארה גילאי 5–7'})
        done = self.stage(self.lead(4 * DAY, **came))
        self.assertEqual((done['stage'], done['owner']), ('trial_done', 'bot'))
        self.assertEqual((done['follow']['kind'], done['follow']['hot'], done['follow']['delays']), ('trial_feedback', True, (DAY, 3 * DAY, 7 * DAY)))
        self.assertEqual(done['next_step'], 'להתקשר: איך היה, ולהציע הרשמה')      # what a person would do, when it is theirs
        missed = self.stage(self.lead(4 * DAY, **{**came, 'kogo_trial': {'on': yesterday, 'outcome': 'no_show', 'course': ''}}))
        self.assertEqual((missed['stage'], missed['owner'], missed['follow']['delays']), ('trial_no_show', 'bot', (DAY, 3 * DAY)))
        started = self.stage(self.lead(4 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום ב-11.10 · נועה לוי'))
        self.assertEqual((started['stage'], started['owner'], started['follow']['kind']), ('signup_started', 'bot', 'link_reminder'))
        self.assertTrue(started['follow']['hot'])

        # Wrote on a later day than the trial: they are in touch, and a person carries on.
        self.assertEqual(self.got(self.lead(HOUR, **came)), ('trial_done', 'office'))
        # A trial of three weeks ago, a registration begun a month ago, a trial with no day: the office's, as before.
        old = (TODAY - timedelta(days=21)).isoformat()
        self.assertEqual(self.got(self.lead(30 * DAY, **{**came, 'kogo_trial': {'on': old, 'outcome': 'attended', 'course': ''}})), ('trial_done', 'office'))
        self.assertEqual(self.got(self.lead(40 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום ב-10.9 · נועה לוי')), ('signup_started', 'office'))
        self.assertEqual(self.got(self.lead(4 * DAY, kogo_outcome='trial_only', kogo_detail='עשה שיעור ניסיון, לא סומן · נועה לוי')), ('trial_done', 'office'))
        # The charge failed: a person, at once.
        self.assertEqual(self.got(self.lead(4 * DAY, kogo_outcome='signup_declined')), ('signup_failed', 'office'))

    def test_leads_the_bot_never_comes_back_to_by_itself(self):
        link = dict(lead_link_sent_at=self.clock() - HOUR)
        complaint = self.stage(self.lead(known_flags=['complaint'], **link))
        self.assertEqual((complaint['stage'], complaint['owner'], complaint['follow']), ('link_sent', 'office', None))
        self.assertIn('יש תלונה פתוחה', complaint['reason'])
        external = self.stage(self.lead(said={'external': True, 'branch_name': 'שוהם'}, **link))
        self.assertEqual((external['stage'], external['owner'], external['follow']), ('link_sent', '', None))
        self.assertIn('סניף חיצוני — ההרשמה מול העירייה', external['reason'])
        silent = self.stage(self.lead(5 * DAY, said={'external': True}))
        self.assertEqual((silent['stage'], silent['owner']), ('answered_silent', ''))
        price = self.stage(self.lead(said={'blocker': 'price', 'blocker_text': 'יקר לה'}, **link))
        self.assertEqual((price['owner'], price['follow']), ('', None))
        self.assertIn('יקר לה — הבוט לא חוזר אליו לבד', price['reason'])
        # A hot lead the bot will not write to is not left with nobody.
        trial = dict(kogo_outcome='trial_only', kogo_trial={'on': (TODAY - timedelta(days=1)).isoformat(), 'outcome': 'attended', 'course': ''})
        self.assertEqual(self.got(self.lead(4 * DAY, said={'blocker': 'price', 'blocker_text': 'יקר'}, **trial)), ('trial_done', 'office'))

    def test_a_person_who_took_the_conversation_or_the_lead_holds_it(self):
        link = dict(lead_link_sent_at=self.clock() - HOUR)
        for held in (dict(handled_by='human'), dict(lead_manual={'owner': {'value': 'office', 'by_name': 'דנה'}})):
            with self.subTest(held=held):
                result = self.stage(self.lead(**link, **held))
                self.assertEqual((result['stage'], result['owner'], result['follow']), ('link_sent', 'office', None))
                self.assertEqual(self.got(self.lead(**held)), ('in_conversation', 'office'))
                self.assertEqual(self.got(self.lead(40 * DAY, **held)), ('cold', ''))              # closed is nobody's

    def test_closed_by_a_person_on_the_leads_screen(self):
        at = (self.clock() - 10 * MINUTE).isoformat()
        deleted = self.stage(self.lead(lead_manual={'closed': {'reason': 'deleted', 'note': 'ספק', 'at': at, 'by_name': 'דנה מנהלת'}}))
        self.assertEqual((deleted['stage'], deleted['label'], deleted['group'], deleted['owner']), ('deleted', 'נמחק', 'closed', ''))
        self.assertEqual(deleted['reason'], 'דנה מנהלת מחק את הליד ב-12.10. ספק.')
        closed = self.stage(self.lead(lead_manual={'closed': {'reason': 'not_interested', 'note': '', 'at': at, 'by_name': 'דנה מנהלת'}}))
        self.assertEqual((closed['stage'], closed['reason']), ('not_interested', 'דנה מנהלת סגר את הליד ב-12.10: לא מעוניין.'))
        # Opened again by a person: the customer's own "no" from before that no longer closes it.
        reopened = self.lead(
            said={'interest': 'no', 'quote': 'לא מעוניינת', 'quote_at': (self.clock() - HOUR).isoformat()},
            lead_manual={'reopened_at': self.clock().isoformat()},
        )
        self.assertEqual(self.got(reopened)[0], 'in_conversation')

    def test_every_open_stage_has_an_owner_by_the_table(self):
        self.assertEqual(lead_stage.STAGE_OWNERS, {
            'signup_stuck': 'office', 'needs_reply': 'office', 'signup_failed': 'office', 'callback_due': 'office',
            'signup_started': 'bot', 'link_sent': 'bot', 'will_check': 'bot', 'trial_no_show': 'bot', 'trial_done': 'bot',
            'waiting_date': 'bot', 'answered_silent': 'bot',
            'trial_booked': '', 'nothing_to_offer': '', 'in_conversation': '',
            'registered': '', 'customer': '', 'not_interested': '', 'deleted': '', 'cold': '',
        })


class KeepingTests(LeadsCase):
    def test_the_owner_and_the_columns_to_filter_by_are_kept_with_the_stage(self):
        branch = TestDataFactory.create_branch(name='ראש העין', city=TestDataFactory.create_city('ראש העין'))
        contact = self.staged(self.lead(
            said={'branch_id': str(branch.id), 'course_type': 'קפוארה', 'blocker': 'will_check', 'blocker_text': 'תבדוק'},
            known_callback_on=None,
        ))
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('will_check', 'bot'))
        self.assertEqual((contact.lead_branch_id, contact.lead_course_type, contact.lead_come_back_on), (branch.id, 'קפוארה', None))
        self.assertEqual(contact.lead_version, lead_stage.RULES_VERSION)
        waiting = self.staged(self.lead(known_callback_on=TODAY + timedelta(days=9)))
        self.assertEqual(waiting.lead_come_back_on, TODAY + timedelta(days=9))

    def test_a_contact_looked_at_under_the_older_rules_is_looked_at_again(self):
        contact = self.staged(self.lead(5 * DAY))
        self.assertEqual(lead_stage.pending_count(), 0)
        Contact.objects.filter(pk=contact.pk).update(lead_version=1, lead_stage='not_relevant', lead_owner='')
        self.assertEqual(lead_stage.pending_count(), 1)
        # Until then the old name reads like its heir, on the lists and on the contact.
        listed = self.get('contacts/', lead_stage='not_interested').data['results']
        self.assertEqual([row['lead']['stage'] for row in listed], ['not_interested'])
        self.assertEqual(listed[0]['lead']['stage_label'], 'לא מעוניין')
        self.assertEqual(self.get('leads/', owner='closed').data['counts']['closed'], 1)
        events = ContactEvent.objects.filter(kind='lead_stage_changed').count()
        self.assertEqual(lead_stage.scan()['lead_checked'], 1)
        after = Contact.objects.get(pk=contact.pk)
        self.assertEqual((after.lead_stage, after.lead_owner, after.lead_version), ('answered_silent', 'bot', 2))
        self.assertEqual(lead_stage.pending_count(), 0)
        self.assertEqual(ContactEvent.objects.filter(kind='lead_stage_changed').count(), events + 1)

    def test_a_change_of_owner_within_a_stage_is_a_line_in_the_journal(self):
        contact = self.staged(self.lead(lead_link_sent_at=self.clock() - HOUR))
        self.assertEqual(contact.lead_owner, 'bot')
        Contact.objects.filter(pk=contact.pk).update(handled_by='human')
        after = self.staged(contact)
        self.assertEqual((after.lead_stage, after.lead_owner), ('link_sent', 'office'))
        self.assertEqual(
            ContactEvent.objects.filter(kind='lead_stage_changed').latest('id').text,
            'קיבל קישור ועוד לא נרשם · הצעד הבא: הבוט ← המשרד',
        )

    def test_a_conversation_that_stops_being_a_lead_loses_its_stage(self):
        contact = self.staged(self.lead(5 * DAY, said={'intent': 'class'}))
        self.assertEqual(contact.lead_stage, 'answered_silent')
        Contact.objects.filter(pk=contact.pk).update(lead_ai={**contact.lead_ai, 'intent': 'rental'}, known_topic='other')
        after = self.staged(contact)
        self.assertEqual((after.lead_stage, after.lead_owner, after.lead_stage_since), ('', '', None))
        self.assertEqual(after.lead_reason, 'לא ליד לחוגים: השכרת סטודיו.')
        payload = self.get(f'contacts/{contact.id}/').data['lead']
        self.assertEqual((payload['stage'], payload['group'], payload['stage_label'], payload['owner']), ('', '', '', ''))
        self.assertIn('← לא ליד לחוגים', ContactEvent.objects.filter(kind='lead_stage_changed').latest('id').text)
        # In none of the lists, on neither screen.
        self.assertEqual(self.get('leads/', owner='bot').data['counts'], {'office': 0, 'bot': 0, 'closed': 0})
        self.assertEqual(sum(group['total'] for group in self.get('leads/board/').data['groups']), 0)

    def test_nothing_a_person_marked_is_written_by_the_stages(self):
        contact = self.lead(
            3 * DAY, followup_status='no_answer', followup_note='לא ענתה', followup_by=self.manager,
            followup_at=self.clock() - DAY, lead_manual={'owner': {'value': 'office', 'by_name': 'דנה'}},
        )
        marks = Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get(pk=contact.pk)
        lead_stage.refresh(contact)
        lead_stage.scan()
        self.assertEqual(Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get(pk=contact.pk), marks)


# --- the endpoints of the leads screen ---------------------------------------------------------------------

class LeadsApiTests(LeadsCase):
    def placed(self, *args, **kwargs) -> Contact:
        return self.staged(self.lead(*args, **kwargs))

    def rows(self, **params):
        response = self.get('leads/', **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_the_two_lists_and_the_closed_one(self):
        waiting = self.placed(2 * HOUR, answer=None)                                             # office: nobody answered
        link = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)                          # bot: a link reminder planned
        booked = self.placed(HOUR, kogo_outcome='trial_upcoming', kogo_detail='ניסיון ב-14.10 · נועה לוי')   # nobody's step
        registered = self.placed(HOUR, kogo_outcome='registered_after')                         # closed
        self.lead(HOUR)                                                                          # not looked at yet: on no list

        office = self.rows(owner='office')
        self.assertEqual(set(office), {'counts', 'stages', 'results', 'next_cursor'})
        self.assertEqual(office['counts'], {'office': 1, 'bot': 2, 'closed': 1})
        self.assertEqual([row['contact_id'] for row in office['results']], [waiting.id])
        self.assertEqual(office['stages'], [{'key': 'needs_reply', 'label': 'שאל ולא קיבל מענה', 'count': 1}])
        self.assertIsNone(office['next_cursor'])
        self.assertEqual(self.rows()['results'], office['results'])                              # the office's list is the default

        bot = self.rows(owner='bot')
        # Every open lead is on one of the two lists: also one with no step right now.
        self.assertEqual({row['contact_id'] for row in bot['results']}, {link.id, booked.id})
        self.assertEqual([stage['key'] for stage in bot['stages']], ['link_sent', 'trial_booked'])
        self.assertEqual([row['contact_id'] for row in self.rows(owner='closed')['results']], [registered.id])
        self.assertEqual(self.get('leads/', owner='everybody').status_code, 400)

    def test_a_row_is_the_contracts(self):
        contact = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR, said={
            'summary': 'אמא לילד בן 5 בראש העין, שאלה על קפוארה, קיבלה קישור ולא נרשמה', 'branch_name': 'ראש העין',
            'course_type': 'קפוארה', 'child': {'name': '', 'age_years': 5, 'age_months': None, 'grade': '', 'birth_date': ''},
        })
        row = self.rows(owner='bot')['results'][0]
        self.assertEqual(set(row), {
            'id', 'contact_id', 'name', 'phone', 'phone_display', 'is_demo', 'stage', 'stage_label', 'owner', 'summary',
            'chips', 'since', 'next', 'last_message_at',
        })
        self.assertEqual((row['id'], row['contact_id'], row['name'], row['phone'], row['is_demo']), (contact.id, contact.id, 'ליד 1', contact.phone, False))
        self.assertEqual((row['stage'], row['stage_label'], row['owner']), ('link_sent', 'קיבל קישור ועוד לא נרשם', 'bot'))
        self.assertEqual(row['summary'], 'אמא לילד בן 5 בראש העין, שאלה על קפוארה, קיבלה קישור ולא נרשמה')
        self.assertEqual(row['chips'], {'branch': 'ראש העין', 'course_type': 'קפוארה', 'child': 'גיל 5'})
        self.assertEqual(datetime.fromisoformat(row['since']), self.clock())
        # Tomorrow, when the office opens.
        tomorrow = timezone.make_aware(datetime.combine(TODAY + timedelta(days=1), time(10, 30)), ZONE)
        self.assertEqual(datetime.fromisoformat(row['next'].pop('when')), tomorrow)
        self.assertEqual(row['next'], {'who': 'bot', 'what': 'הבוט יזכיר את ההרשמה', 'state': 'planned', 'note': ''})
        self.assertEqual(datetime.fromisoformat(row['last_message_at']), self.clock() - HOUR + MINUTE)

    def test_the_summary_falls_back_to_the_rules_own_sentence_and_the_chips_to_the_card(self):
        self.placed(2 * HOUR, answer=None, known_child_age='5')
        row = self.rows(owner='office')['results'][0]
        self.assertEqual(row['summary'], 'שאל על קפוארה בראש העין (גיל 5) היום ב-10:00. עוד לא קיבל תשובה.')
        self.assertEqual(row['chips'], {'branch': 'ראש העין', 'course_type': 'קפוארה', 'child': 'גיל 5'})
        self.assertEqual(row['next'], {'who': 'office', 'what': 'לענות לו', 'when': None, 'state': 'none', 'note': ''})

    def test_what_happens_next_in_every_kind_of_row(self):
        def next_of(contact):
            return self.get(f'leads/{contact.id}/').data['next']

        # No step for anybody right now — and the row says why, in words.
        booked = self.placed(HOUR, kogo_outcome='trial_upcoming', kogo_trial={'on': '2026-10-13', 'outcome': '', 'course': ''})
        self.assertEqual(next_of(booked), {
            'who': 'none', 'what': 'מחכים לשיעור הניסיון ביום שלישי, 13.10', 'when': None, 'state': 'none', 'note': '',
        })
        self.assertEqual(next_of(self.placed(HOUR, kogo_outcome='trial_upcoming'))['what'], 'מחכים לשיעור הניסיון')
        kept = self.placed(HOUR, said={'blocker': 'full', 'blocker_text': 'הקבוצה מלאה'})
        self.assertEqual((next_of(kept)['who'], next_of(kept)['what']), ('none', 'נשמר: נודיע כשנפתח'))
        self.assertEqual(next_of(self.placed(HOUR))['what'], 'בשיחה עכשיו')
        outside = self.placed(5 * DAY, said={'external': True})
        self.assertEqual((next_of(outside)['who'], next_of(outside)['what']), ('none', 'סניף חיצוני — ההרשמה מול העירייה'))
        price = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR, said={'blocker': 'price', 'blocker_text': 'יקר לה'})
        self.assertEqual(next_of(price)['what'], 'הבוט לא חוזר לבד: המחיר עצר אותו')
        silent = self.placed(5 * DAY)                                      # the bot's by the table, with nothing to send
        self.assertEqual((next_of(silent)['who'], next_of(silent)['what']), ('none', 'אין חזרה מתוכננת'))
        put_off = self.placed(HOUR, followup_status='later', followup_due=TODAY + timedelta(days=4), followup_at=self.clock())
        self.assertEqual(next_of(put_off), {
            'who': 'office', 'what': 'לחזור אליו', 'state': 'planned', 'note': '',
            'when': timezone.make_aware(datetime.combine(TODAY + timedelta(days=4), time(12)), ZONE).isoformat(),
        })
        closed = self.placed(HOUR, kogo_outcome='registered_after')
        self.assertEqual(next_of(closed), {'who': 'none', 'what': '', 'when': None, 'state': 'none', 'note': ''})
        # A follow-up whose moment came and that did not leave: "the bot would have sent this now".
        link = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        LeadFollowUp.objects.filter(contact=link).update(status='due', outcome_note='השליחה ללקוחות כבויה')
        due = next_of(link)
        self.assertEqual((due['who'], due['state'], due['note']), ('bot', 'due', 'השליחה ללקוחות כבויה'))

    def test_the_offices_list_is_a_queue_with_whoever_is_stuck_in_a_registration_first(self):
        waited_long = self.placed(3 * DAY, answer=None)
        waited_less = self.placed(2 * HOUR, answer=None)
        stuck = self.placed(HOUR, said={'blocker': 'site_problem', 'blocker_text': 'הקישור לא עובד'})
        failed = self.placed(HOUR, kogo_outcome='signup_declined')
        ids = [row['contact_id'] for row in self.rows(owner='office')['results']]
        self.assertEqual(set(ids[:2]), {stuck.id, failed.id})
        self.assertEqual(ids[2:], [waited_long.id, waited_less.id])
        self.assertEqual(self.rows(owner='office')['results'][ids.index(stuck.id)]['stage_label'], 'נתקע בהרשמה')
        # The other lists read newest first.
        older = self.placed(HOUR, kogo_outcome='trial_upcoming')
        self.clock.forward(minutes=10)
        newer = self.placed(HOUR, kogo_outcome='trial_upcoming')
        self.assertEqual([row['contact_id'] for row in self.rows(owner='bot')['results']], [newer.id, older.id])

    def test_one_filter_a_search_a_branch_and_pages(self):
        branch = TestDataFactory.create_branch(name='ראש העין', city=TestDataFactory.create_city('ראש העין'))
        link = self.placed(HOUR, name='רותם לוי', lead_link_sent_at=self.clock() - HOUR, said={'branch_id': str(branch.id)})
        booked = self.placed(HOUR, name='נועה כהן', kogo_outcome='trial_upcoming')
        silent = self.placed(5 * DAY, name='דנה שחר', said={'summary': 'סבתא שמחפשת חוג אקרובטיקה לנכדה'})

        def ids(**params):
            return {row['contact_id'] for row in self.rows(owner='bot', **params)['results']}

        self.assertEqual(ids(stage='link_sent'), {link.id})
        self.assertEqual(ids(stage='link_sent,trial_booked'), {link.id, booked.id})
        self.assertEqual(ids(stage='nothing'), set())
        self.assertEqual(ids(branch=str(branch.id)), {link.id})
        self.assertEqual(ids(branch='not-a-branch'), set())
        self.assertEqual(ids(q='נועה'), {booked.id})
        self.assertEqual(ids(q='אקרובטיקה'), {silent.id})                 # the summary's sentence is searched too
        self.assertEqual(ids(q=link.phone[-7:]), {link.id})
        # The numbers on the tabs are of every lead, whatever the filter.
        self.assertEqual(self.rows(owner='bot', q='נועה')['counts'], {'office': 0, 'bot': 3, 'closed': 0})

        first = self.rows(owner='bot', limit=2)
        self.assertEqual((len(first['results']), first['next_cursor']), (2, '2'))
        second = self.rows(owner='bot', limit=2, cursor=first['next_cursor'])
        self.assertEqual((len(second['results']), second['next_cursor']), (1, None))
        self.assertEqual(len({row['contact_id'] for row in first['results'] + second['results']}), 3)

    def test_the_list_does_not_grow_queries_with_rows(self):
        for index in range(6):
            self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        with self.assertNumQueries(6):      # the user, the profile; the counts, the stages, the page, its open follow-ups
            self.rows(owner='bot')

    def test_one_lead_in_full(self):
        branch = TestDataFactory.create_branch(name='ראש העין', city=TestDataFactory.create_city('ראש העין'))
        family = TestDataFactory.create_family(name='לוי', branch=branch)
        child = TestDataFactory.create_child(family=family, first_name='נועה', last_name='לוי')
        course = TestDataFactory.create_course(name='קפוארה גילאי 5–7', branch=branch, course_type=TestDataFactory.create_course_type('קפוארה'))
        instructor = TestDataFactory.create_instructor(first_name='יואב', last_name='שדה', branch=branch)
        lesson = TestDataFactory.create_lesson(course=course, branch=branch, instructor=instructor)
        from apps.enrollments.models import LessonEnrollment
        held = TODAY - timedelta(days=1)
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active', trial_lesson_date=held, trial_outcome='attended')
        contact = self.placed(
            4 * DAY, kogo_outcome='trial_only', kogo_detail='ניסיון ב-11.10, הגיע · נועה לוי', kogo_family_id=family.id,
            kogo_child_ids=[str(child.id)], kogo_trial={'on': held.isoformat(), 'outcome': 'attended', 'course': course.name},
            said={'blocker_text': '', 'child': {'name': 'נועה', 'age_years': 6, 'age_months': None, 'grade': '', 'birth_date': ''},
                  'come_back_why': ''},
        )
        data = self.get(f'leads/{contact.id}/').data
        self.assertEqual((data['contact_id'], data['stage'], data['owner']), (contact.id, 'trial_done', 'bot'))
        details = data['details']
        self.assertEqual(set(details), {
            'reason', 'intent', 'intent_label', 'blocker', 'blocker_text', 'child', 'come_back_on', 'come_back_why',
            'link_sent_at', 'trial', 'followups', 'timeline', 'in_system', 'closed', 'taken', 'sending_enabled',
        })
        self.assertEqual(details['reason'], 'ניסיון ב-11.10, הגיע · נועה לוי · קפוארה גילאי 5–7. לא נרשם אחרי הניסיון.')
        self.assertEqual(details['trial'], {
            'course': 'קפוארה גילאי 5–7', 'branch': 'ראש העין', 'date': held.isoformat(), 'instructor': 'יואב שדה',
            'child': 'נועה', 'attended': True,
        })
        self.assertEqual(details['child']['words'], 'נועה, גיל 6')
        self.assertEqual(details['in_system'], {
            'family_id': str(family.id), 'child_ids': [str(child.id)], 'outcome': 'trial_only',
            'outcome_label': 'עשה ניסיון ולא נרשם', 'detail': 'ניסיון ב-11.10, הגיע · נועה לוי',
            'link': f'/customers?child={child.id}',
        })
        self.assertFalse(details['sending_enabled'])
        # What the bot planned, with the words it would say — the child by the name the conversation gave.
        (planned,) = details['followups']
        self.assertEqual((planned['kind'], planned['step'], planned['status'], planned['status_label']), ('trial_feedback', 1, 'planned', 'מתוכנן'))
        self.assertEqual(planned['text'], 'היי, איך היה לנועה בשיעור הניסיון? 🙂')
        self.assertEqual(data['next']['what'], 'הבוט ישאל איך היה בשיעור הניסיון')
        self.assertEqual(self.get('leads/99999/').status_code, 404)

    def test_a_lead_with_no_trial_and_nobody_in_the_system(self):
        contact = self.placed(5 * DAY, known_callback_on=TODAY + timedelta(days=30), said={
            'blocker': 'later', 'blocker_text': 'ביקשה שנחזור אחרי החגים', 'come_back_why': 'ביקשה שנחזור אחרי החגים',
            'come_back': {'kind': 'date', 'iso': (TODAY + timedelta(days=30)).isoformat()},
        })
        details = self.get(f'leads/{contact.id}/').data['details']
        self.assertEqual((details['trial'], details['in_system'], details['closed'], details['taken']), (None, None, None, None))
        self.assertEqual((details['blocker'], details['blocker_text']), ('later', 'ביקשה שנחזור אחרי החגים'))
        self.assertEqual((details['come_back_on'], details['come_back_why']), ((TODAY + timedelta(days=30)).isoformat(), 'ביקשה שנחזור אחרי החגים'))

    def test_closing_a_lead_and_opening_it_again(self):
        contact = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact, status='planned').count(), 1)

        response = self.post(f'leads/{contact.id}/close/', {'reason': 'deleted', 'note': '  ספק,  לא הורה '})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['stage'], response.data['stage_label'], response.data['owner']), ('deleted', 'נמחק', ''))
        self.assertEqual(response.data['details']['reason'], 'דנה מנהלת מחק את הליד ב-12.10. ספק, לא הורה.')
        self.assertEqual(response.data['details']['closed']['by_name'], 'דנה מנהלת')
        # The bot's planned follow-up is called off, with the reason.
        skipped = LeadFollowUp.objects.get(contact=contact)
        self.assertEqual((skipped.status, skipped.outcome_note), ('skipped', 'הליד נסגר: נמחק'))
        event = ContactEvent.objects.filter(contact=contact, kind='lead_action').latest('id')
        self.assertEqual((event.text, event.actor), ('הליד נמחק מהרשימה: ספק, לא הורה', self.manager))
        self.assertEqual(self.rows(owner='closed')['counts'], {'office': 0, 'bot': 0, 'closed': 1})

        again = self.post(f'leads/{contact.id}/reopen/')
        self.assertEqual((again.data['stage'], again.data['owner'], again.data['details']['closed']), ('link_sent', 'bot', None))
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact, status='planned').count(), 1)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_action').latest('id').text, 'הליד נפתח מחדש')

        not_interested = self.post(f'leads/{contact.id}/close/', {'reason': 'not_interested'})
        self.assertEqual((not_interested.data['stage'], not_interested.data['stage_label']), ('not_interested', 'לא מעוניין'))
        for bad in ({}, {'reason': 'boring'}):
            self.assertEqual(self.post(f'leads/{contact.id}/close/', bad).status_code, 400)
        self.assertEqual(self.post('leads/99999/close/', {'reason': 'deleted'}).status_code, 404)

    def test_a_lead_closed_by_a_person_opens_by_itself_when_the_customer_writes_again(self):
        self.incoming('אפשר פרטים על קפוארה?')
        self.bot_reply('בשמחה')
        contact = self.contact()
        self.clock.forward(minutes=5)
        self.post(f'leads/{contact.id}/close/', {'reason': 'not_interested'})
        self.assertEqual(self.contact().lead_stage, 'not_interested')
        self.clock.forward(days=3)
        self.incoming('היי, בעצם כן מעניין אותנו')
        self.assertEqual(self.contact().lead_stage, 'in_conversation')

    def test_reopening_takes_off_whatever_closed_it(self):
        quote = {'interest': 'no', 'quote': 'לא מעוניינת', 'quote_at': (self.clock() - HOUR).isoformat()}
        said_no = self.placed(HOUR, said=quote)
        self.assertEqual(said_no.lead_stage, 'not_interested')
        self.clock.forward(minutes=1)
        self.assertEqual(self.post(f'leads/{said_no.id}/reopen/').data['stage'], 'in_conversation')
        # The office's own "לא רלוונטי": pressed off, as on the older screen, by the person who opened the lead.
        marked = self.placed(HOUR, followup_status='not_relevant', followup_at=self.clock(), followup_note='הערה שנשארת')
        response = self.post(f'leads/{marked.id}/reopen/')
        self.assertEqual(response.data['stage'], 'in_conversation')
        after = Contact.objects.get(pk=marked.id)
        self.assertEqual((after.followup_status, after.followup_by, after.followup_note), ('', self.manager, 'הערה שנשארת'))

    def test_putting_a_lead_off_to_a_day(self):
        trial = dict(kogo_outcome='trial_only', kogo_trial={'on': (TODAY - timedelta(days=1)).isoformat(), 'outcome': 'attended', 'course': ''})
        contact = self.placed(4 * DAY, **trial)
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact, status='planned').count(), 1)
        until = TODAY + timedelta(days=20)
        response = self.post(f'leads/{contact.id}/snooze/', {'until': until.isoformat(), 'note': 'אחרי החגים'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['stage'], response.data['stage_label'], response.data['owner']), ('waiting_date', 'מחכה למועד', ''))
        self.assertEqual(response.data['next']['who'], 'office')
        self.assertEqual(response.data['details']['come_back_on'], until.isoformat())
        # One mark, on both screens: the office's own "בזמן אחר" with its day, made by this person.
        after = Contact.objects.get(pk=contact.id)
        self.assertEqual((after.followup_status, after.followup_due, after.followup_note, after.followup_by), ('later', until, 'אחרי החגים', self.manager))
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['followup']['status_label'], 'בזמן אחר')
        # The bot does not write to somebody a person put off.
        self.assertEqual(LeadFollowUp.objects.get(contact=contact).status, 'skipped')
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_action').latest('id').text, f'נדחה ל-{until.day}.{until.month}.{until.year}: אחרי החגים')
        # On the day, it is the office's.
        self.clock.to(until.year, until.month, until.day, 12)
        lead_stage.scan()
        on_the_day = Contact.objects.get(pk=contact.id)
        self.assertEqual((on_the_day.lead_stage, on_the_day.lead_owner), ('callback_due', 'office'))
        for bad in ({}, {'until': 'מחר'}, {'until': TODAY.isoformat()}, {'until': (TODAY - timedelta(days=1)).isoformat()}):
            self.assertEqual(self.post(f'leads/{contact.id}/snooze/', bad).status_code, 400)

    def test_taking_a_lead_and_handing_it_back_to_the_bot(self):
        contact = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        taken = self.post(f'leads/{contact.id}/owner/', {'owner': 'office'})
        self.assertEqual((taken.data['stage'], taken.data['owner']), ('link_sent', 'office'))
        self.assertEqual(taken.data['next'], {'who': 'office', 'what': 'להתקשר ולעזור להשלים את ההרשמה', 'when': None, 'state': 'none', 'note': ''})
        self.assertEqual(taken.data['details']['taken']['by_name'], 'דנה מנהלת')
        self.assertEqual(LeadFollowUp.objects.get(contact=contact).outcome_note, 'עבר לטיפול המשרד')
        self.assertEqual(self.rows(owner='office')['counts'], {'office': 1, 'bot': 0, 'closed': 0})

        back = self.post(f'leads/{contact.id}/owner/', {'owner': 'bot'})
        self.assertEqual((back.data['owner'], back.data['next']['who'], back.data['details']['taken']), ('bot', 'bot', None))
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact, status='planned').count(), 1)
        texts = list(ContactEvent.objects.filter(contact=contact, kind='lead_action').order_by('id').values_list('text', flat=True))
        self.assertEqual(texts, ['הליד נלקח לטיפול המשרד', 'הליד הוחזר לבוט'])
        # Where the stage itself asks for a person, handing it "back" changes nothing — and the answer says so.
        waiting = self.placed(2 * HOUR, answer=None)
        self.assertEqual(self.post(f'leads/{waiting.id}/owner/', {'owner': 'bot'}).data['owner'], 'office')
        self.assertEqual(self.post(f'leads/{contact.id}/owner/', {'owner': 'me'}).status_code, 400)

    def test_the_timeline_is_short_and_in_plain_words(self):
        contact = self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        self.post(f'leads/{contact.id}/owner/', {'owner': 'office'})
        for index in range(12):
            state.log_event(contact.id, 'analyzed', f'סיכום {index}')              # noise: not part of a lead's story
        timeline = self.get(f'leads/{contact.id}/').data['details']['timeline']
        self.assertLessEqual(len(timeline), 10)
        self.assertEqual(set(timeline[0]), {'at', 'kind', 'kind_label', 'text', 'by'})
        self.assertNotIn('analyzed', {event['kind'] for event in timeline})
        taken = next(event for event in timeline if event['kind'] == 'lead_action')
        self.assertEqual((taken['text'], taken['by'], taken['kind_label']), ('הליד נלקח לטיפול המשרד', 'דנה מנהלת', 'פעולה על הליד'))

    def test_the_texts_the_bot_comes_back_with(self):
        response = self.get('leads/templates/')
        self.assertEqual(response.status_code, 200)
        rows = response.data
        self.assertTrue(all({'kind', 'label', 'text', 'template_key', 'mapped'} <= set(row) for row in rows))
        keys = [row['template_key'] for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        for key in ('link_reminder_1', 'link_reminder_2', 'link_reminder_3', 'will_check_1', 'trial_feedback_1',
                    'trial_no_show_2', 'date_arrived_age_1', 'date_arrived_not_ready_1', 'opening_1', 'dormant_1'):
            self.assertIn(key, keys)
        first = next(row for row in rows if row['template_key'] == 'link_reminder_1')
        self.assertEqual((first['kind'], first['step'], first['label'], first['mapped'], first['active'], first['days']),
                         ('link_reminder', 1, 'קיבל קישור ועוד לא נרשם', False, True, 1))
        self.assertIn('{חוג}', first['text'])
        self.assertFalse(next(row for row in rows if row['kind'] == 'dormant')['active'])
        # A template the owner connected shows as connected.
        from apps.wahub import settings_store
        settings_store.put(settings_store.LEAD_FOLLOWUP_TEMPLATES, {'link_reminder_1': 'content20261012', 'will_check_1': ' '})
        mapped = {row['template_key'] for row in self.get('leads/templates/').data if row['mapped']}
        self.assertEqual(mapped, {'link_reminder_1'})

    def test_only_a_manager(self):
        contact = self.placed(HOUR)
        reads = ('leads/', f'leads/{contact.id}/', 'leads/templates/')
        writes = (f'leads/{contact.id}/close/', f'leads/{contact.id}/reopen/', f'leads/{contact.id}/snooze/', f'leads/{contact.id}/owner/')
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            client = client_for(make_user(f'{role}@leads.test', role))
            for path in reads:
                self.assertEqual(client.get(f'{BASE}/{path}').status_code, 403, path)
            for path in writes:
                self.assertEqual(client.post(f'{BASE}/{path}', {'reason': 'deleted'}, format='json').status_code, 403, path)
        anonymous = APIClient()
        for path in reads:
            self.assertIn(anonymous.get(f'{BASE}/{path}').status_code, (401, 403), path)
        for path in writes:
            self.assertIn(anonymous.post(f'{BASE}/{path}', {'reason': 'deleted'}, format='json').status_code, (401, 403), path)
        self.assertEqual(Contact.objects.get(pk=contact.id).lead_manual, {})
        self.assertEqual(self.client.post(f'{BASE}/leads/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.get(f'{BASE}/leads/{contact.id}/close/').status_code, 405)

    def test_the_older_board_and_its_lists_still_answer(self):
        self.placed(HOUR, lead_link_sent_at=self.clock() - HOUR)
        self.placed(2 * HOUR, answer=None)
        board = self.get('leads/board/')
        self.assertEqual(board.status_code, 200)
        self.assertEqual([group['key'] for group in board.data['groups']], ['act', 'wait', 'closed'])
        counted = {stage['key']: stage['count'] for group in board.data['groups'] for stage in group['stages']}
        self.assertEqual((counted['link_sent'], counted['needs_reply']), (1, 1))
        for stage in (row for group in board.data['groups'] for row in group['stages']):
            self.assertTrue(stage['label'] and stage['hint'])
        listed = self.get('contacts/', view='chats', lead_stage='link_sent').data['results']
        self.assertEqual([(row['lead']['stage'], row['lead']['group'], row['lead']['owner']) for row in listed], [('link_sent', 'act', 'bot')])
        self.assertEqual(len(self.get('contacts/', lead_group='act').data['results']), 2)
        self.assertEqual(self.get('summary/').data['lead_act']['total'], 2)
