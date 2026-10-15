"""
"מצב הליד" (docs/WAHUB-CONTRACT-LEAD-STAGES.md; the stages, labels and owners of
docs/WAHUB-CONTRACT-LEADS-V2.md are tried in test_leads_v2.py): every stage with its sentence of
why and of what to do, the order of the rules, the thresholds under a clock the
test holds, what is written and when, the cron's look, the board, the lists and
the "today" tab.

The rules are tried at noon today (Israel) — `self.now` — so "yesterday" and
"four days ago" never depend on the hour the tests run.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment
from apps.enrollments.models import LessonEnrollment
from apps.wahub import cron, lead_stage, matching, office_clock, state
from apps.wahub.models import (
    LEAD_GROUP_CHOICES,
    LEAD_STAGE_CHOICES,
    LEAD_STAGE_GROUPS,
    Contact,
    ContactEvent,
    Message,
)
from apps.wahub.tests.base import BASE, PHONE, WahubTestCase, client_for, make_user

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
ASKED = 'שאל על קפוארה בראש העין'
LEAD_FIELDS = (
    'lead_stage', 'lead_stage_since', 'lead_reason', 'lead_next_step', 'lead_human_touched',
    'lead_checked_at', 'lead_recheck_at',
    # leads, version 2: who makes the next move, the columns the lists filter by, the rules' version
    'lead_owner', 'lead_branch_id', 'lead_course_type', 'lead_come_back_on', 'lead_version',
)


class LeadStageTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        # The world these tests describe: ManyChat has been copying the old bot's replies for a year, so
        # an unanswered message means what it says. RepliesNotCopiedTests is the other world.
        self.replies_seen = patch.object(
            lead_stage, 'replies_copied_since', return_value=timezone.now() - timedelta(days=365),
        )
        self.replies_seen.start()
        self.addCleanup(self.replies_seen.stop)
        self.today = state.now_israel_date()
        self.now = timezone.make_aware(datetime.combine(self.today, time(12)))
        self.made = 0

    def day(self, value) -> str:
        return lead_stage._day(value, self.today)

    def lead(self, wrote=HOUR, *, answer='bot', now=None, **fields) -> Contact:
        """
        Somebody who asked about capoeira in Rosh HaAyin `wrote` ago and is not
        in the registrations. `answer`: who answered a minute later ('bot',
        'office'), or None for a message still waiting.
        """
        self.made += 1
        asked_at = (now or self.now) - wrote
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
                last_message_at=asked_at, last_message_direction='in', last_message_sender='customer',
                waiting_since=asked_at,
            )
        row.update(fields)
        return Contact.objects.create(phone=f'9725055503{self.made:02d}', **row)

    def stage(self, contact, now=None) -> dict:
        return lead_stage.compute(contact, now or self.now)

    def staged(self, contact, now=None) -> Contact:
        """The contact after its stage was worked out and kept."""
        lead_stage.refresh(contact, now=now or self.now)
        return Contact.objects.get(pk=contact.pk)


# --- every stage: the facts → the stage, why, what to do ------------------------------------------------

class ActStageTests(LeadStageTestCase):
    def test_asked_and_got_no_answer_for_half_an_hour(self):
        result = self.stage(self.lead(40 * MINUTE, answer=None))
        self.assertEqual((result['stage'], result['group'], result['label']), ('needs_reply', 'act', 'שאל ולא קיבל מענה'))
        self.assertEqual(result['reason'], f'{ASKED} היום ב-11:20. עוד לא קיבל תשובה.')
        self.assertEqual(result['next_step'], 'לענות לו')
        self.assertEqual(result['since'], self.now - 40 * MINUTE)

    def test_asked_for_a_person_even_though_the_bot_answered(self):
        result = self.stage(self.lead(
            3 * HOUR, needs_human=True, needs_human_reason='ביקש נציג', needs_human_at=self.now - 3 * HOUR,
        ))
        self.assertEqual(result['stage'], 'needs_reply')
        self.assertEqual(result['reason'], f'{ASKED} היום ב-09:00. מחכה לנציג (ביקש נציג).')
        self.assertEqual(result['since'], self.now - 3 * HOUR)

    def test_tried_to_register_and_the_charge_failed(self):
        result = self.stage(self.lead(
            2 * DAY, kogo_outcome='signup_declined', kogo_detail='ניסה להירשם — החיוב נכשל · נועה לוי',
        ))
        self.assertEqual((result['stage'], result['group'], result['label']), ('signup_failed', 'act', 'ניסה להירשם והחיוב נכשל'))
        self.assertEqual(result['reason'], 'ניסה להירשם — החיוב נכשל · נועה לוי. לא נרשם.')
        self.assertEqual(result['next_step'], 'לברר מה קרה ולשלוח קישור חדש')

    def test_started_to_register_and_did_not_finish(self):
        result = self.stage(self.lead(2 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום ב-2.10 · נועה לוי'))
        self.assertEqual((result['stage'], result['group'], result['label']), ('signup_started', 'act', 'התחיל הרשמה ולא סיים'))
        self.assertEqual(result['reason'], 'התחיל רישום ב-2.10 · נועה לוי. לא שילם.')
        self.assertEqual(result['next_step'], 'להתקשר ולעזור לסיים')

    def test_booked_a_trial_and_did_not_come(self):
        held = self.today - timedelta(days=3)
        result = self.stage(self.lead(
            9 * DAY, kogo_outcome='trial_only', kogo_detail='ניסיון ב-8.10, לא הגיע · נועה לוי',
            kogo_trial={'on': held.isoformat(), 'outcome': 'no_show', 'course': 'קפוארה גילאי 5–7'},
        ))
        self.assertEqual((result['stage'], result['group'], result['label']), ('trial_no_show', 'act', 'נרשם לניסיון ולא הגיע'))
        self.assertEqual(result['reason'], 'ניסיון ב-8.10, לא הגיע · נועה לוי · קפוארה גילאי 5–7. לא נרשם.')
        self.assertEqual(result['next_step'], 'לתאם מועד חדש')
        self.assertEqual(timezone.localtime(result['since']).date(), held)      # since the trial, not since we noticed

    def test_did_a_trial_and_did_not_register_whether_he_came_or_nobody_marked(self):
        held = self.today - timedelta(days=6)
        for outcome, words in (('attended', 'הגיע'), ('', 'לא סומן')):
            with self.subTest(outcome=outcome):
                result = self.stage(self.lead(
                    9 * DAY, kogo_outcome='trial_only', kogo_detail=f'ניסיון ב-5.10, {words} · נועה לוי',
                    kogo_trial={'on': held.isoformat(), 'outcome': outcome, 'course': ''},
                ))
                self.assertEqual((result['stage'], result['group'], result['label']), ('trial_done', 'act', 'עשה ניסיון ולא נרשם'))
                self.assertEqual(result['reason'], f'ניסיון ב-5.10, {words} · נועה לוי. לא נרשם אחרי הניסיון.')
                self.assertEqual(result['next_step'], 'להתקשר: איך היה, ולהציע הרשמה')
                self.assertEqual(timezone.localtime(result['since']).date(), held)
        # Matched before the facts were kept beside the sentence: the matching's own words decide, until its next match.
        for words, expected in (('לא הגיע', 'trial_no_show'), ('הגיע', 'trial_done'), ('לא סומן', 'trial_done')):
            with self.subTest(words=words):
                older = self.stage(self.lead(9 * DAY, kogo_outcome='trial_only', kogo_detail=f'ניסיון ב-5.10, {words} · נועה לוי'))
                self.assertEqual((older['stage'], older['since']), (expected, None))
        # A trial known only from the child's status: no day, no outcome — still "did a trial".
        bare = self.stage(self.lead(9 * DAY, kogo_outcome='trial_only', kogo_detail='עשה שיעור ניסיון, לא סומן · נועה לוי'))
        self.assertEqual((bare['stage'], bare['since']), ('trial_done', None))

    def test_the_day_to_come_back_has_come(self):
        yesterday = self.today - timedelta(days=1)
        said = self.stage(self.lead(8 * DAY, known_callback_on=yesterday))
        self.assertEqual((said['stage'], said['group'], said['label']), ('callback_due', 'act', 'הגיע הזמן לחזור אליו'))
        self.assertEqual(said['reason'], f'{ASKED} לפני 8 ימים. לפי השיחה, לחזור אליו ב-{self.day(yesterday)}.')
        self.assertEqual(said['next_step'], 'לחזור אליו היום')
        self.assertEqual(timezone.localtime(said['since']).date(), yesterday)

        marked = self.stage(self.lead(
            8 * DAY, followup_status='later', followup_due=self.today, followup_at=self.now - 7 * DAY,
        ))
        self.assertEqual(marked['stage'], 'callback_due')
        self.assertEqual(marked['reason'], f'{ASKED} לפני 8 ימים. הצוות קבע לחזור אליו ב-{self.day(self.today)}.')

        waiting = self.stage(self.lead(8 * DAY, followup_status='waiting_us', followup_at=self.now - 5 * DAY))
        self.assertEqual(waiting['stage'], 'callback_due')
        self.assertEqual(
            waiting['reason'], f'{ASKED} לפני 8 ימים. הצוות סימן "מחכה לנו" ב-{self.day(self.today - timedelta(days=5))}.',
        )
        self.assertEqual(waiting['since'], self.now - 5 * DAY)

    def test_was_answered_and_did_not_register(self):
        # The contract's own example, word for word.
        result = self.stage(self.lead(4 * DAY))
        self.assertEqual((result['stage'], result['group'], result['label']), ('answered_silent', 'act', 'קיבל תשובה ולא נרשם'))
        self.assertEqual(result['reason'], f'{ASKED} לפני 4 ימים. רק הבוט ענה. לא נרשם.')
        self.assertEqual(result['next_step'], 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון')
        self.assertEqual(result['since'], self.now - 4 * DAY + MINUTE)       # quiet since the answer

        by_person = self.stage(self.lead(4 * DAY, answer='office'))
        self.assertEqual(by_person['reason'], f'{ASKED} לפני 4 ימים. נציג ענה לו. לא נרשם.')
        self.assertEqual(by_person['next_step'], 'להתקשר ולהציע שיעור ניסיון')

        no_answer = self.stage(self.lead(6 * DAY, followup_status='no_answer', followup_at=self.now - 3 * DAY))
        self.assertEqual(no_answer['stage'], 'answered_silent')
        self.assertEqual(
            no_answer['reason'],
            f'{ASKED} לפני 6 ימים. הצוות סימן "לא ענה" ב-{self.day(self.today - timedelta(days=3))}. לא נרשם.',
        )
        self.assertEqual(no_answer['next_step'], 'ניסו להשיג אותו ולא ענה — לנסות שוב')


class WaitStageTests(LeadStageTestCase):
    def test_a_trial_is_still_ahead(self):
        result = self.stage(self.lead(
            3 * DAY, kogo_outcome='trial_upcoming', kogo_detail='ניסיון ב-14.10 · נועה לוי',
            kogo_trial={'on': (self.today + timedelta(days=3)).isoformat(), 'outcome': '', 'course': 'קפוארה גילאי 5–7'},
        ))
        self.assertEqual((result['stage'], result['group'], result['label']), ('trial_booked', 'wait', 'רשום לשיעור ניסיון'))
        self.assertEqual(result['reason'], 'ניסיון ב-14.10 · נועה לוי · קפוארה גילאי 5–7.')
        self.assertEqual(result['next_step'], 'אין מה לעשות עכשיו — מחכים לשיעור הניסיון')

    def test_waiting_for_a_day_that_is_still_ahead(self):
        after_the_holidays = self.today + timedelta(days=9)
        said = self.stage(self.lead(3 * DAY, known_callback_on=after_the_holidays))
        self.assertEqual((said['stage'], said['group'], said['label']), ('waiting_date', 'wait', 'מחכה למועד'))
        self.assertEqual(said['reason'], f'{ASKED} לפני 3 ימים. לפי השיחה, לחזור אליו ב-{self.day(after_the_holidays)}.')
        self.assertEqual(said['next_step'], f'לחזור אליו ב-{self.day(after_the_holidays)}')

        marked = self.stage(self.lead(
            3 * DAY, followup_status='later', followup_due=self.today + timedelta(days=2), followup_at=self.now - DAY,
            known_callback_on=after_the_holidays,
        ))
        self.assertEqual(marked['stage'], 'waiting_date')
        # Two days are known: the nearer one is the one to wait for.
        self.assertEqual(marked['next_step'], f'לחזור אליו ב-{self.day(self.today + timedelta(days=2))}')
        self.assertIn('הצוות קבע לחזור אליו', marked['reason'])

    def test_in_conversation(self):
        result = self.stage(self.lead(HOUR))
        self.assertEqual((result['stage'], result['group'], result['label']), ('in_conversation', 'wait', 'בשיחה'))
        self.assertEqual(result['reason'], f'{ASKED} היום ב-11:00. רק הבוט ענה.')
        self.assertEqual(result['next_step'], 'אין מה לעשות עכשיו')

        just_wrote = self.stage(self.lead(10 * MINUTE, answer=None))
        self.assertEqual(just_wrote['stage'], 'in_conversation')
        self.assertEqual(just_wrote['reason'], f'{ASKED} היום ב-11:50. עוד לא קיבל תשובה.')


class ClosedStageTests(LeadStageTestCase):
    def test_registered_by_the_registrations_or_by_a_mark(self):
        result = self.stage(self.lead(6 * DAY, kogo_outcome='registered_after', kogo_detail='נרשם ב-7.10 · נועה לוי'))
        self.assertEqual((result['stage'], result['group'], result['label']), ('registered', 'closed', 'נרשם'))
        self.assertEqual((result['reason'], result['next_step']), ('נרשם ב-7.10 · נועה לוי.', ''))

        marked = self.stage(self.lead(6 * DAY, followup_status='registered', followup_at=self.now - 2 * DAY))
        self.assertEqual(marked['stage'], 'registered')
        self.assertEqual(marked['reason'], f'הצוות סימן "נרשם" ב-{self.day(self.today - timedelta(days=2))}.')
        self.assertEqual(marked['since'], self.now - 2 * DAY)

    def test_a_customer_from_before_is_not_a_lead(self):
        result = self.stage(self.lead(HOUR, kogo_outcome='customer_before', kogo_detail='לקוח רשום מ-1.9 · נועה לוי'))
        self.assertEqual((result['stage'], result['group'], result['label']), ('customer', 'closed', 'לקוח קיים'))
        self.assertEqual((result['reason'], result['next_step']), ('לקוח רשום מ-1.9 · נועה לוי.', ''))

    def test_closed_by_the_offices_mark_and_what_the_older_summary_knew_no_longer_closes(self):
        # The office's "לא רלוונטי" closes the lead, under the stage's name of 12.10.2026.
        marked = self.stage(self.lead(3 * HOUR, followup_status='not_relevant', followup_at=self.now - 10 * MINUTE))
        self.assertEqual((marked['stage'], marked['group'], marked['label']), ('not_interested', 'closed', 'לא מעוניין'))
        self.assertEqual((marked['reason'], marked['next_step']), (f'הצוות סימן "לא רלוונטי" ב-{self.day(self.today)}.', ''))

        # No branch nearby is closed since 15.10.2026 — "לא רלוונטי · אין מה להציע" — and looked at every day
        # (docs/WAHUB-CONTRACT-LEADS-NO-ONE-LOST.md, ט).
        asked_on = self.day(self.today - timedelta(days=1))
        far = self.stage(self.lead(DAY, known_city='חולון', known_branch_name='', known_flags=['no_branch_nearby']))
        self.assertEqual((far['stage'], far['group'], far['label']), ('nothing_to_offer', 'closed', 'לא רלוונטי · אין מה להציע'))
        self.assertEqual((far['owner'], far['follow'], far['next_step']), ('', None, ''))
        self.assertEqual(far['reason'], f'שאל על קפוארה בחולון ב-{asked_on}. אין סניף באזור שלו.')

        # "Not now" in the older summary is no "not interested": that takes the customer's own words.
        self.assertEqual(self.stage(self.lead(DAY, known_interest='cold'))['stage'], 'in_conversation')

    def test_nothing_else_in_the_summary_closes_a_lead(self):
        # Until the summary has read a conversation as a lead, nothing else it kept moves the stage.
        for flags in (['lives_far'], ['child_too_young'], ['price'], ['class_full'], ['says_registered']):
            with self.subTest(flags=flags):
                self.assertEqual(self.stage(self.lead(HOUR, known_flags=flags))['stage'], 'in_conversation')
        for interest in ('hot', 'warm', 'none', ''):
            with self.subTest(interest=interest):
                self.assertEqual(self.stage(self.lead(HOUR, known_interest=interest))['stage'], 'in_conversation')

    def test_an_old_lead(self):
        result = self.stage(self.lead(45 * DAY))
        self.assertEqual((result['stage'], result['group'], result['label']), ('cold', 'closed', 'רדום'))
        self.assertEqual(
            result['reason'], f'{ASKED} ב-{self.day(self.today - timedelta(days=45))}. רק הבוט ענה. לא נרשם.',
        )
        self.assertEqual(result['next_step'], '')


class WordsTests(LeadStageTestCase):
    def test_what_they_asked_is_said_from_what_the_summary_knows(self):
        def asked(**known):
            blank = dict(known_topic='', known_course_type='', known_city='', known_branch_name='', known_child_age='')
            return lead_stage._asked(Contact(**{**blank, **known}))

        self.assertEqual(asked(), 'פנה בוואטסאפ')
        self.assertEqual(asked(known_topic='other'), 'פנה בוואטסאפ')
        self.assertEqual(asked(known_topic='info'), 'שאל על חוגים')
        self.assertEqual(asked(known_city='שוהם'), 'שאל על חוגים בשוהם')
        self.assertEqual(asked(known_course_type='בלט'), 'שאל על בלט')
        self.assertEqual(asked(known_topic='trial', known_course_type='קפוארה'), 'שאל על שיעור ניסיון בקפוארה')
        self.assertEqual(asked(known_topic='trial', known_city='יהוד'), 'שאל על שיעור ניסיון ביהוד')
        self.assertEqual(asked(known_topic='registration', known_course_type='היפ הופ'), 'שאל על הרשמה להיפ הופ')
        # The branch when it is known, else the city.
        self.assertEqual(
            asked(known_course_type='קפוארה', known_city='פתח תקווה', known_branch_name='אם המושבות'),
            'שאל על קפוארה באם המושבות',
        )
        self.assertEqual(asked(known_course_type='קפוארה', known_child_age='5'), 'שאל על קפוארה (גיל 5)')
        self.assertEqual(asked(known_course_type='קפוארה', known_child_age='כיתה ב'), 'שאל על קפוארה (כיתה ב)')

    def test_when_is_said_by_the_calendar(self):
        when = lambda ago: lead_stage._when(self.now - ago, self.now)    # noqa: E731
        self.assertEqual(when(20 * MINUTE), 'היום ב-11:40')
        self.assertEqual(when(11 * HOUR), 'היום ב-01:00')
        self.assertEqual(when(13 * HOUR), 'אתמול')                        # 23:00 last night
        self.assertEqual(when(2 * DAY), 'לפני יומיים')
        self.assertEqual(when(9 * DAY), 'לפני 9 ימים')

    def test_a_contact_nobody_exchanged_a_message_with(self):
        manual = Contact.objects.create(phone='972505550399', name='ידני', source='manual')
        Contact.objects.filter(pk=manual.pk).update(created_at=self.now - 2 * HOUR)
        manual.refresh_from_db()
        fresh = self.stage(manual)
        self.assertEqual((fresh['stage'], fresh['reason']), ('in_conversation', 'אין שיחה בוואטסאפ; נוסף היום ב-10:00.'))
        later = self.stage(manual, self.now + 5 * DAY)
        self.assertEqual(later['stage'], 'answered_silent')
        self.assertEqual(later['next_step'], 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון')

    def test_the_sentences_fit_their_columns(self):
        long = 'א' * 400
        result = self.stage(self.lead(HOUR, kogo_outcome='pending', kogo_detail=long[:300]))
        self.assertLessEqual(len(result['reason']), 300)
        self.assertLessEqual(len(result['next_step']), 200)

    def test_every_stage_has_a_group_a_label_a_hint_and_a_next_step(self):
        keys = [stage for stage, _label in LEAD_STAGE_CHOICES]
        self.assertEqual(len(keys), 20)
        self.assertEqual(set(LEAD_STAGE_GROUPS), set(keys))
        self.assertEqual(set(lead_stage.HINTS), set(keys))
        self.assertEqual(set(lead_stage.NEXT_STEPS), set(keys))
        by_group = {group: [key for key in keys if LEAD_STAGE_GROUPS[key] == group] for group, _label in LEAD_GROUP_CHOICES}
        self.assertEqual(by_group, {
            'act': [
                'signup_stuck', 'needs_reply', 'signup_failed', 'signup_started', 'link_sent', 'trial_no_show', 'trial_done',
                'callback_due', 'answered_silent',
            ],
            'wait': ['will_check', 'trial_booked', 'waiting_date', 'waiting_list', 'in_conversation'],
            'closed': ['nothing_to_offer', 'registered', 'customer', 'not_interested', 'deleted', 'cold'],
        })
        self.assertEqual(set(lead_stage.STAGE_OWNERS), set(keys))
        # Every stage that asks for something says what; a closed one asks for nothing.
        for key in keys:
            self.assertTrue(lead_stage.HINTS[key])
            self.assertEqual(bool(lead_stage.NEXT_STEPS[key]), LEAD_STAGE_GROUPS[key] != 'closed', key)


# --- the order of the rules -----------------------------------------------------------------------------

class RuleOrderTests(LeadStageTestCase):
    def test_the_first_rule_that_fits_decides(self):
        yesterday = self.today - timedelta(days=1)
        tomorrow = self.today + timedelta(days=1)
        trial = {'on': yesterday.isoformat(), 'outcome': 'no_show', 'course': ''}
        waiting = dict(wrote=2 * HOUR, answer=None)                     # a message nobody answered for two hours
        due = dict(known_callback_on=yesterday)                         # the day to come back has come
        far = dict(known_flags=['no_branch_nearby'])
        steps = [
            # (what is true about the contact, the stage)
            (dict(followup_status='not_relevant', kogo_outcome='registered_after', needs_human=True), 'not_interested'),
            (dict(followup_status='registered', kogo_outcome='customer_before', needs_human=True), 'registered'),
            (dict(kogo_outcome='registered_after', needs_human=True, **waiting), 'registered'),
            (dict(kogo_outcome='customer_before', needs_human=True, **waiting), 'customer'),
            (dict(kogo_outcome='signup_declined', needs_human=True), 'needs_reply'),
            (dict(kogo_outcome='signup_declined', **waiting), 'needs_reply'),
            (dict(kogo_outcome='trial_upcoming', **waiting), 'needs_reply'),
            (dict(kogo_outcome='signup_declined', **due, **far), 'signup_failed'),
            (dict(kogo_outcome='pending', **due, **far), 'signup_started'),
            (dict(kogo_outcome='trial_upcoming', **due, **far), 'trial_booked'),
            (dict(kogo_outcome='trial_only', kogo_trial=trial, **due, **far), 'trial_no_show'),
            (dict(kogo_outcome='trial_only', **due, **far), 'trial_done'),
            (dict(**due, **far, known_interest='cold'), 'callback_due'),
            (dict(followup_status='waiting_us', followup_at=self.now - 3 * HOUR, **far), 'callback_due'),
            (dict(known_callback_on=tomorrow, **far, known_interest='cold'), 'waiting_date'),
            (dict(wrote=HOUR, **far), 'nothing_to_offer'),
            (dict(wrote=5 * DAY, known_interest='cold'), 'answered_silent'),
            (dict(wrote=HOUR), 'in_conversation'),
            (dict(wrote=5 * DAY), 'answered_silent'),
            (dict(wrote=40 * DAY), 'cold'),
        ]
        for facts, expected in steps:
            with self.subTest(facts=facts):
                self.assertEqual(self.stage(self.lead(**{'wrote': 3 * DAY, **facts}))['stage'], expected)

    def test_a_contact_the_matching_has_not_reached_and_a_family_with_nothing_running_follow_the_conversation(self):
        for outcome in ('', 'not_found', 'in_system'):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.stage(self.lead(HOUR, kogo_outcome=outcome))['stage'], 'in_conversation')
                self.assertEqual(self.stage(self.lead(5 * DAY, kogo_outcome=outcome))['stage'], 'answered_silent')

    def test_a_later_day_the_office_set_beats_an_older_day_the_conversation_named(self):
        last_week = self.today - timedelta(days=7)
        in_a_week = self.today + timedelta(days=7)
        # The customer said "after the holidays"; the day came; the office spoke to them and set a new one.
        rescheduled = self.stage(self.lead(
            20 * DAY, known_callback_on=last_week, followup_status='later', followup_due=in_a_week,
            followup_at=self.now - 3 * DAY,
        ))
        self.assertEqual((rescheduled['stage'], rescheduled['next_step']), ('waiting_date', f'לחזור אליו ב-{self.day(in_a_week)}'))
        # Both days still ahead, the customer's the nearer one: the office's is the one that is waited for,
        # so the screen never promises a day on which nothing will happen.
        both_ahead = self.stage(self.lead(
            3 * DAY, known_callback_on=self.today + timedelta(days=3), followup_status='later', followup_due=in_a_week,
            followup_at=self.now - DAY,
        ))
        self.assertEqual((both_ahead['stage'], both_ahead['next_step']), ('waiting_date', f'לחזור אליו ב-{self.day(in_a_week)}'))
        self.assertIn('הצוות קבע לחזור אליו', both_ahead['reason'])
        # The office's day has come.
        today_it_is = self.stage(self.lead(
            20 * DAY, known_callback_on=last_week, followup_status='later', followup_due=self.today,
            followup_at=self.now - 3 * DAY,
        ))
        self.assertEqual(today_it_is['stage'], 'callback_due')
        self.assertIn(f'הצוות קבע לחזור אליו ב-{self.day(self.today)}', today_it_is['reason'])

    def test_the_day_the_conversation_named_is_closed_by_a_mark_made_once_it_has_come(self):
        yesterday = self.today - timedelta(days=1)
        due = dict(wrote=9 * DAY, known_callback_on=yesterday)
        self.assertEqual(self.stage(self.lead(**due))['stage'], 'callback_due')
        # A mark from before the day says nothing about it: still "come back to them".
        for mark in ('answered', 'no_answer', 'later'):
            with self.subTest(mark=mark, when='before'):
                early = self.stage(self.lead(followup_status=mark, followup_at=self.now - 5 * DAY, **due))
                self.assertEqual(early['stage'], 'callback_due')
        # The office went back to them today: it is off the list, and the conversation's clock takes over.
        for mark in ('answered', 'no_answer', 'later'):
            with self.subTest(mark=mark, when='after'):
                went_back = self.stage(self.lead(followup_status=mark, followup_at=self.now - HOUR, **due))
                self.assertEqual(went_back['stage'], 'in_conversation')
                self.assertEqual(self.stage(self.lead(followup_status=mark, followup_at=self.now - HOUR, **due), self.now + 2 * DAY)['stage'], 'answered_silent')
        # A day still ahead is not closed by a mark made before it.
        ahead = self.stage(self.lead(
            wrote=2 * DAY, known_callback_on=self.today + timedelta(days=5), followup_status='answered',
            followup_at=self.now - HOUR,
        ))
        self.assertEqual(ahead['stage'], 'waiting_date')

    def test_who_dealt_with_them_last_is_what_the_sentence_says(self):
        # An old mark, and the office has written since: the message is the news.
        written_since = self.stage(self.lead(
            HOUR, answer='office', followup_status='answered', followup_at=self.now - 6 * DAY,
        ))
        self.assertEqual(written_since['reason'], f'{ASKED} היום ב-11:00. נציג ענה לו.')
        # The bot answered, and a person marked after it: the mark is the news.
        marked_since = self.stage(self.lead(3 * HOUR, followup_status='no_answer', followup_at=self.now - HOUR))
        self.assertEqual(marked_since['reason'], f'{ASKED} היום ב-09:00. הצוות סימן "לא ענה" ב-{self.day(self.today)}.')

    def test_an_old_no_answer_mark_does_not_speak_once_the_customer_wrote_again(self):
        # Marked "לא ענה" ten days ago; the customer wrote five days ago and the bot answered.
        result = self.stage(self.lead(5 * DAY, followup_status='no_answer', followup_at=self.now - 10 * DAY))
        self.assertEqual(result['stage'], 'answered_silent')
        self.assertEqual(result['reason'], f'{ASKED} לפני 5 ימים. רק הבוט ענה. לא נרשם.')
        self.assertEqual(result['next_step'], lead_stage.NOBODY_SPOKE_YET)

    def test_a_mark_that_closed_the_day_to_come_back_takes_it_off(self):
        # The rule of the "לחזור אליהם" queue: what the customer said counts until somebody closes it.
        yesterday = self.today - timedelta(days=1)
        self.assertEqual(self.stage(self.lead(5 * DAY, known_callback_on=yesterday))['stage'], 'callback_due')
        self.assertEqual(
            self.stage(self.lead(
                5 * DAY, known_callback_on=yesterday, followup_status='not_relevant', followup_at=self.now - HOUR,
            ))['stage'],
            'not_interested',
        )


# --- the thresholds, with the clock in the test's hand --------------------------------------------------

class ThresholdTests(LeadStageTestCase):
    def test_the_thresholds_are_the_contracts(self):
        self.assertEqual(lead_stage.REPLY_AFTER, timedelta(minutes=30))
        self.assertEqual(lead_stage.SILENT_AFTER, timedelta(days=2))
        self.assertEqual(lead_stage.IN_CONVERSATION_FOR, timedelta(hours=48))
        self.assertEqual(lead_stage.COLD_AFTER, timedelta(days=30))

    def test_thirty_minutes_with_no_answer(self):
        contact = self.lead(timedelta(0), answer=None)
        asked_at = self.now
        before = self.stage(contact, asked_at + 29 * MINUTE)
        self.assertEqual(before['stage'], 'in_conversation')
        self.assertEqual(before['recheck_at'], asked_at + 30 * MINUTE)       # the cron comes back exactly then
        self.assertEqual(self.stage(contact, asked_at + 30 * MINUTE)['stage'], 'needs_reply')
        self.assertEqual(self.stage(contact, asked_at + 3 * DAY)['stage'], 'needs_reply')
        self.assertEqual(self.stage(contact, asked_at + 60 * DAY)['stage'], 'needs_reply')    # never "cold" unanswered

    def test_an_unanswered_message_is_watched_whatever_the_stage(self):
        # A sign-up that failed, and a new message ten minutes ago: half an hour later it needs a reply.
        contact = self.lead(10 * MINUTE, answer=None, kogo_outcome='signup_declined')
        result = self.stage(contact)
        self.assertEqual((result['stage'], result['recheck_at']), ('signup_failed', self.now + 20 * MINUTE))
        self.assertEqual(self.stage(contact, self.now + 20 * MINUTE)['stage'], 'needs_reply')
        # A customer's message is service: no stage waits on it.
        customer = self.stage(self.lead(10 * MINUTE, answer=None, kogo_outcome='customer_before'))
        self.assertEqual((customer['stage'], customer['recheck_at']), ('customer', None))

    def test_forty_eight_hours_of_conversation_then_silent_then_thirty_days(self):
        contact = self.lead(timedelta(0))
        answered_at = self.now + MINUTE
        fresh = self.stage(contact, answered_at + 47 * HOUR + 59 * MINUTE)
        self.assertEqual(fresh['stage'], 'in_conversation')
        self.assertLessEqual(fresh['recheck_at'], answered_at + 48 * HOUR)
        silent = self.stage(contact, answered_at + 48 * HOUR)
        self.assertEqual(silent['stage'], 'answered_silent')
        self.assertEqual(self.stage(contact, answered_at + 30 * DAY - MINUTE)['stage'], 'answered_silent')
        self.assertEqual(self.stage(contact, answered_at + 30 * DAY)['stage'], 'cold')

    def test_thresholds_set_apart_leave_nobody_without_a_stage(self):
        # Should "silent" ever start later than "in conversation" ends, the days between are still conversation.
        contact = self.lead(60 * HOUR)
        with patch.object(lead_stage, 'SILENT_AFTER', timedelta(days=3)):
            between = self.stage(contact)
            self.assertEqual(between['stage'], 'in_conversation')
            self.assertEqual(between['recheck_at'], timezone.make_aware(datetime.combine(self.today + timedelta(days=1), time.min)))
            self.assertEqual(self.stage(contact, self.now + DAY)['stage'], 'answered_silent')

    def test_the_day_to_come_back_arrives_at_midnight(self):
        tomorrow = self.today + timedelta(days=1)
        contact = self.lead(3 * DAY, known_callback_on=tomorrow)
        waiting = self.stage(contact)
        midnight = timezone.make_aware(datetime.combine(tomorrow, time.min))
        self.assertEqual((waiting['stage'], waiting['recheck_at']), ('waiting_date', midnight))
        self.assertEqual(self.stage(contact, midnight - MINUTE)['stage'], 'waiting_date')
        self.assertEqual(self.stage(contact, midnight + 5 * MINUTE)['stage'], 'callback_due')

    def test_every_open_lead_is_looked_at_again_after_midnight_and_a_closed_one_is_not(self):
        midnight = timezone.make_aware(datetime.combine(self.today + timedelta(days=1), time.min))
        open_leads = [
            self.lead(40 * MINUTE, answer=None), self.lead(HOUR, kogo_outcome='signup_declined'),
            self.lead(HOUR, kogo_outcome='pending'), self.lead(HOUR, kogo_outcome='trial_only'),
            self.lead(HOUR, kogo_outcome='trial_upcoming'), self.lead(HOUR, followup_status='waiting_us'),
            self.lead(5 * DAY), self.lead(HOUR), self.lead(HOUR, known_callback_on=self.today + timedelta(days=5)),
        ]
        for contact in open_leads:
            result = self.stage(contact)
            with self.subTest(stage=result['stage']):
                self.assertNotEqual(result['group'], 'closed')
                self.assertLessEqual(result['recheck_at'], midnight)     # "אתמול" becomes "לפני יומיים"
                self.assertGreater(result['recheck_at'], self.now)
        closed = [
            self.lead(HOUR, kogo_outcome='registered_after'), self.lead(HOUR, kogo_outcome='customer_before'),
            self.lead(HOUR, followup_status='not_relevant'), self.lead(45 * DAY),
        ]
        for contact in closed:
            result = self.stage(contact)
            with self.subTest(stage=result['stage']):
                self.assertEqual((result['group'], result['recheck_at']), ('closed', None))

    def test_a_mark_is_activity_like_a_message_is(self):
        # Quiet in WhatsApp for five days, and the office spoke to them an hour ago.
        contact = self.lead(5 * DAY, followup_status='answered', followup_at=self.now - HOUR)
        result = self.stage(contact)
        self.assertEqual(result['stage'], 'in_conversation')
        self.assertEqual(result['reason'], f'{ASKED} לפני 5 ימים. הצוות סימן "ענה" ב-{self.day(self.today)}.')
        self.assertEqual(result['since'], self.now - HOUR)
        # Two days after the mark, still not registered: it is back on the list, and a person has spoken.
        later = self.stage(contact, self.now + 2 * DAY)
        self.assertEqual((later['stage'], later['next_step']), ('answered_silent', 'להתקשר ולהציע שיעור ניסיון'))
        # A mark that was cleared says nothing: its time is not activity.
        cleared = self.lead(5 * DAY, followup_status='', followup_at=self.now - HOUR)
        self.assertEqual(self.stage(cleared)['stage'], 'answered_silent')

    def test_a_mark_that_says_they_spoke_answers_the_message_and_one_that_says_no_answer_does_not(self):
        unanswered = dict(wrote=2 * HOUR, answer=None)
        self.assertEqual(self.stage(self.lead(**unanswered))['stage'], 'needs_reply')
        for mark in ('answered', 'later'):
            with self.subTest(mark=mark):
                spoke = self.stage(self.lead(followup_status=mark, followup_at=self.now - HOUR, **unanswered))
                self.assertEqual(spoke['stage'], 'in_conversation')
                self.assertIn(f'הצוות סימן "{lead_stage.FOLLOWUP_LABELS[mark]}"', spoke['reason'])
        # "לא ענה": they tried the phone; the WhatsApp message is still waiting.
        tried = self.stage(self.lead(followup_status='no_answer', followup_at=self.now - HOUR, **unanswered))
        self.assertEqual(tried['stage'], 'needs_reply')
        # A mark from before the message is about an older conversation.
        old_mark = self.stage(self.lead(followup_status='answered', followup_at=self.now - 3 * DAY, **unanswered))
        self.assertEqual(old_mark['stage'], 'needs_reply')
        # They spoke yesterday about Monday's message — and the customer wrote again two hours ago.
        # (The "waiting since" of the row still points at Monday: a mark does not move it.)
        wrote_again = self.stage(self.lead(
            2 * HOUR, answer=None, waiting_since=self.now - 2 * DAY, followup_status='answered', followup_at=self.now - DAY,
        ))
        self.assertEqual((wrote_again['stage'], wrote_again['since']), ('needs_reply', self.now - 2 * HOUR))
        self.assertIn('עוד לא קיבל תשובה', wrote_again['reason'])
        just_wrote_again = self.stage(self.lead(
            10 * MINUTE, answer=None, waiting_since=self.now - 2 * DAY, followup_status='answered', followup_at=self.now - DAY,
        ))
        self.assertEqual(
            (just_wrote_again['stage'], just_wrote_again['recheck_at']), ('in_conversation', self.now + 20 * MINUTE),
        )
        # And a later date marked after the message makes it wait for that date.
        later = self.stage(self.lead(
            followup_status='later', followup_due=self.today + timedelta(days=4), followup_at=self.now - HOUR, **unanswered,
        ))
        self.assertEqual(later['stage'], 'waiting_date')


# --- keeping it: refresh --------------------------------------------------------------------------------

class RefreshTests(LeadStageTestCase):
    def test_the_stage_is_kept_with_why_what_to_do_and_since_when(self):
        contact = self.lead(4 * DAY)
        self.assertTrue(lead_stage.refresh(contact, now=self.now))
        contact.refresh_from_db()
        self.assertEqual(contact.lead_stage, 'answered_silent')
        self.assertEqual(contact.lead_reason, f'{ASKED} לפני 4 ימים. רק הבוט ענה. לא נרשם.')
        self.assertEqual(contact.lead_next_step, 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון')
        self.assertEqual(contact.lead_stage_since, self.now - 4 * DAY + MINUTE)
        self.assertEqual(contact.lead_recheck_at, timezone.make_aware(datetime.combine(self.today + timedelta(days=1), time.min)))
        self.assertIsNotNone(contact.lead_checked_at)
        self.assertEqual(contact.lead_checked_at, contact.touched_at)     # seen by the screen, and not picked again

    def test_a_second_look_that_finds_nothing_new_writes_nothing_the_screen_sees(self):
        contact = self.staged(self.lead(4 * DAY))
        events = ContactEvent.objects.count()
        with self.assertNumQueries(4):      # the row, "did the office write", the stamp, the bot's open follow-up
            self.assertFalse(lead_stage.refresh(contact, now=self.now))
        after = Contact.objects.get(pk=contact.pk)
        self.assertEqual((after.touched_at, after.updated_at), (contact.touched_at, contact.updated_at))
        self.assertEqual(
            [getattr(after, name) for name in LEAD_FIELDS[:5]], [getattr(contact, name) for name in LEAD_FIELDS[:5]],
        )
        self.assertGreater(after.lead_checked_at, contact.lead_checked_at)      # only "looked at" moved
        self.assertEqual(ContactEvent.objects.count(), events)

    def test_only_the_lead_fields_are_written(self):
        contact = self.lead(
            4 * DAY, followup_status='no_answer', followup_note='לא ענתה', followup_by=self.manager,
            followup_at=self.now - 3 * DAY, known_summary='שאלה על קפוארה', kogo_detail='x', unread_count=2,
        )
        # ... and the bot's round, which lead_followups.py keeps beside them: quiet four days, the bot asks once.
        moved = set(LEAD_FIELDS) | {'touched_at', 'updated_at', 'lead_bot'}
        before = {name: value for name, value in Contact.objects.values().get(pk=contact.pk).items() if name not in moved}
        self.assertTrue(lead_stage.refresh(contact, now=self.now))
        after = {name: value for name, value in Contact.objects.values().get(pk=contact.pk).items() if name not in moved}
        self.assertEqual(after, before)
        self.assertEqual(Message.objects.count(), 0)

    def test_the_first_stage_is_not_a_journal_line_and_a_change_of_stage_is(self):
        contact = self.staged(self.lead(HOUR))
        self.assertEqual(contact.lead_stage, 'in_conversation')
        self.assertFalse(ContactEvent.objects.filter(kind='lead_stage_changed').exists())

        self.assertTrue(lead_stage.refresh(contact, now=self.now + 3 * DAY))
        event = ContactEvent.objects.get(kind='lead_stage_changed')
        self.assertEqual(event.contact_id, contact.id)
        self.assertIsNone(event.actor)
        self.assertTrue(event.text.startswith('בשיחה ← קיבל תשובה ולא נרשם ('), event.text)
        self.assertIn('רק הבוט ענה', event.text)

        # The same stage with new words (a day later) is a change the screen sees, and no journal line.
        self.assertTrue(lead_stage.refresh(contact, now=self.now + 4 * DAY))
        self.assertEqual(ContactEvent.objects.filter(kind='lead_stage_changed').count(), 1)
        self.assertIn('לפני 4 ימים', Contact.objects.get(pk=contact.pk).lead_reason)

    def test_a_stage_no_fact_dates_keeps_the_moment_it_was_first_seen(self):
        contact = self.staged(self.lead(2 * DAY, kogo_outcome='pending', kogo_detail='התחיל רישום · נועה לוי'))
        self.assertEqual((contact.lead_stage, contact.lead_stage_since), ('signup_started', self.now))
        # Looked at again three days later (the words did not change): the same "since".
        Contact.objects.filter(pk=contact.pk).update(lead_reason='')
        again = self.staged(contact, self.now + 3 * DAY)
        self.assertEqual((again.lead_stage, again.lead_stage_since), ('signup_started', self.now))
        # A new stage starts counting from the moment it was seen.
        Contact.objects.filter(pk=contact.pk).update(kogo_outcome='signup_declined')
        moved = self.staged(contact, self.now + 4 * DAY)
        self.assertEqual((moved.lead_stage, moved.lead_stage_since), ('signup_failed', self.now + 4 * DAY))

    def test_a_person_wrote_is_read_from_the_messages_and_stays(self):
        contact = self.lead(4 * DAY)
        failed = Message.objects.create(contact=contact, direction='out', sender='office', text='שלום', status='failed')
        Message.objects.create(contact=contact, direction='out', sender='bot', text='שלום', status='sent')
        staged = self.staged(contact)
        self.assertFalse(staged.lead_human_touched)       # a send that failed reached nobody; the bot is not a person
        self.assertEqual(staged.lead_next_step, lead_stage.NOBODY_SPOKE_YET)

        Message.objects.filter(pk=failed.pk).update(status='sent')
        staged = self.staged(contact)
        self.assertTrue(staged.lead_human_touched)
        self.assertEqual(staged.lead_next_step, 'להתקשר ולהציע שיעור ניסיון')
        self.assertIn('נציג ענה לו', staged.lead_reason)

        Message.objects.all().delete()
        with self.assertNumQueries(3):      # the row, the stamp, the bot's open follow-up: once true, it is not asked again
            lead_stage.refresh(contact, now=self.now)
        self.assertTrue(Contact.objects.get(pk=contact.pk).lead_human_touched)

    def test_a_stage_from_an_older_row_never_covers_a_newer_fact(self):
        contact = self.lead(HOUR)
        in_hand = Contact.objects.get(pk=contact.pk)
        # Something happened between the read and the write.
        state.touch(contact.pk, kogo_outcome='signup_declined')
        self.assertFalse(lead_stage.refresh(in_hand, now=self.now, fresh=True))
        untouched = Contact.objects.get(pk=contact.pk)
        self.assertEqual((untouched.lead_stage, untouched.lead_checked_at), ('', None))
        self.assertEqual(lead_stage.pending_count(self.now), 1)           # still waiting for a look
        # The next look reads the row as it now is.
        self.assertEqual(self.staged(contact).lead_stage, 'signup_failed')

    def test_it_takes_an_id_and_ignores_one_that_is_gone(self):
        contact = self.lead(HOUR)
        self.assertTrue(lead_stage.refresh(contact.pk, now=self.now))
        self.assertEqual(Contact.objects.get(pk=contact.pk).lead_stage, 'in_conversation')
        self.assertFalse(lead_stage.refresh(987654321))

    def test_a_demo_contact_gets_a_stage_like_everybody(self):
        demo = self.staged(self.lead(4 * DAY, is_demo=True, source='demo'))
        self.assertEqual((demo.lead_stage, demo.is_demo), ('answered_silent', True))
        self.assertTrue(demo.lead_reason)


# --- the cron's look ------------------------------------------------------------------------------------

class ScanTests(LeadStageTestCase):
    def stages(self) -> dict:
        return dict(Contact.objects.order_by('id').values_list('name', 'lead_stage'))

    def test_the_first_look_gives_everybody_a_stage_and_the_second_has_nothing_to_do(self):
        self.lead(40 * MINUTE, answer=None, name='מחכה')
        self.lead(4 * DAY, name='שותק')
        self.lead(HOUR, kogo_outcome='customer_before', name='לקוח')
        self.assertEqual(lead_stage.pending_count(self.now), 3)

        counts = lead_stage.scan(now=self.now)
        self.assertEqual(counts, {'lead_checked': 3, 'lead_changed': 3})
        self.assertEqual(self.stages(), {'מחכה': 'needs_reply', 'שותק': 'answered_silent', 'לקוח': 'customer'})
        self.assertEqual(lead_stage.pending_count(self.now), 0)
        self.assertEqual(lead_stage.scan(now=self.now), {'lead_checked': 0, 'lead_changed': 0})

    def test_a_contact_something_happened_to_is_looked_at_again(self):
        waiting = self.lead(HOUR, name='א')
        self.lead(HOUR, name='ב')
        lead_stage.scan(now=self.now)
        state.touch(waiting.pk, kogo_outcome='pending')                # the matching found a sign-up
        self.assertEqual(lead_stage.pending_count(self.now), 1)
        self.assertEqual(lead_stage.scan(now=self.now), {'lead_checked': 1, 'lead_changed': 1})
        self.assertEqual(self.stages(), {'א': 'signup_started', 'ב': 'in_conversation'})

    def test_a_contact_whose_time_has_come_is_looked_at_again(self):
        self.lead(10 * MINUTE, answer=None, name='כתב עכשיו')
        self.lead(HOUR, name='נענה')
        self.lead(HOUR, kogo_outcome='customer_before', name='לקוח')
        lead_stage.scan(now=self.now)
        self.assertEqual(self.stages(), {'כתב עכשיו': 'in_conversation', 'נענה': 'in_conversation', 'לקוח': 'customer'})

        # Nineteen minutes on, nobody's time has come.
        self.assertEqual(lead_stage.scan(now=self.now + 19 * MINUTE)['lead_checked'], 0)
        # Half an hour after the message: the one still waiting, and only that one.
        self.assertEqual(lead_stage.scan(now=self.now + 20 * MINUTE), {'lead_checked': 1, 'lead_changed': 1})
        self.assertEqual(self.stages()['כתב עכשיו'], 'needs_reply')
        # After midnight every open lead is looked at again (its words are a day older); the customer is not.
        tomorrow = self.now + DAY
        self.assertEqual(lead_stage.pending_count(tomorrow), 2)
        self.assertEqual(lead_stage.scan(now=tomorrow)['lead_checked'], 2)
        self.assertIn('אתמול', Contact.objects.get(name='נענה').lead_reason)
        # Two days of quiet.
        lead_stage.scan(now=self.now + 2 * DAY + 5 * MINUTE)
        self.assertEqual(self.stages()['נענה'], 'answered_silent')

    def test_a_customer_waiting_for_an_answer_goes_before_the_mornings_wave(self):
        for index in range(4):
            self.lead(3 * HOUR, name=f'פתוח {index}')
        self.lead(10 * MINUTE, answer=None, name='מחכה')
        lead_stage.scan(now=self.now)
        # The next morning all five are due — the four since midnight, the waiting one since yesterday 12:20.
        morning = self.now + 19 * HOUR
        self.assertEqual(lead_stage.pending_count(morning), 5)
        self.assertEqual(lead_stage.scan(now=morning, batch=1)['lead_checked'], 1)
        self.assertEqual(self.stages()['מחכה'], 'needs_reply')

    def test_a_look_out_of_time_stops_and_the_next_one_carries_on(self):
        for index in range(6):
            self.lead(HOUR, name=f'ליד {index}')
        self.assertEqual(lead_stage.scan(budget_seconds=0, now=self.now), {'lead_checked': 0, 'lead_changed': 0})
        self.assertEqual(lead_stage.pending_count(self.now), 6)

        # Time runs out after two contacts: those two are kept, the rest wait.
        clock = iter([0.0] + [0.0] * 6 + [99.0] * 20)
        with patch('apps.wahub.lead_stage.time.monotonic', side_effect=lambda: next(clock)):
            partial = lead_stage.scan(budget_seconds=5, now=self.now)
        self.assertGreater(partial['lead_checked'], 0)
        self.assertLess(partial['lead_checked'], 6)
        self.assertEqual(Contact.objects.exclude(lead_stage='').count(), partial['lead_checked'])
        self.assertEqual(lead_stage.pending_count(self.now), 6 - partial['lead_checked'])

        rest = lead_stage.scan(now=self.now)
        self.assertEqual(rest['lead_checked'], 6 - partial['lead_checked'])
        self.assertEqual(lead_stage.pending_count(self.now), 0)

    def test_a_batch_at_a_time(self):
        for index in range(5):
            self.lead(HOUR, name=f'ליד {index}')
        self.assertEqual(lead_stage.scan(now=self.now, batch=2)['lead_checked'], 2)
        self.assertEqual(lead_stage.pending_count(self.now), 3)
        self.assertEqual(lead_stage.scan(now=self.now, batch=2)['lead_checked'], 2)
        self.assertEqual(lead_stage.scan(now=self.now, batch=2)['lead_checked'], 1)

    def test_the_look_does_not_ask_about_each_contact_separately(self):
        for index in range(8):
            self.lead(HOUR, name=f'ליד {index}')
        # Two lists (touched, timed), one "who did the office write to" and one "which follow-ups are open"
        # for the batch, and one write per contact.
        with self.assertNumQueries(2 + 2 + 8):
            lead_stage.scan(now=self.now)

    def test_one_contact_that_fails_does_not_stop_the_rest(self):
        broken = self.lead(HOUR, name='שבור')
        self.lead(HOUR, name='תקין')
        real = lead_stage.compute

        def compute(contact, now=None):
            if contact.pk == broken.pk:
                raise RuntimeError('boom')
            return real(contact, now)

        with patch('apps.wahub.lead_stage.compute', side_effect=compute), self.assertLogs('apps.wahub.lead_stage', level='ERROR'):
            counts = lead_stage.scan(now=self.now)
        self.assertEqual(counts, {'lead_checked': 1, 'lead_changed': 1})
        self.assertEqual(self.stages(), {'שבור': '', 'תקין': 'in_conversation'})
        # The one that failed is not first in line again five minutes later; it is tried again in an hour.
        self.assertEqual(lead_stage.pending_count(timezone.now() + 5 * MINUTE), 0)
        self.assertEqual(lead_stage.scan(now=timezone.now() + HOUR + MINUTE)['lead_checked'], 1)
        self.assertEqual(self.stages()['שבור'], 'in_conversation')


@override_settings(CRON_TOKEN='cron-secret', ANTHROPIC_API_KEY='')
class CronTests(LeadStageTestCase):
    def test_the_five_minute_slice_works_the_stages_out_after_the_matching(self):
        family = TestDataFactory.create_family(phone='050-5550101')
        TestDataFactory.create_child(family=family, status='pending')
        self.incoming('אפשר שיעור ניסיון בקפוארה? הוא בן 5')
        self.age(self.contact(), last_message_at=6)
        self.assertEqual(self.contact().lead_stage, 'in_conversation')        # at once, before the matching

        counts = cron.tick()
        self.assertEqual((counts['matched'], counts['outcomes_changed']), (1, 1))
        self.assertEqual((counts['lead_checked'], counts['lead_changed']), (1, 1))
        self.assertEqual(counts['pending_lead_stage'], 0)
        contact = self.contact()
        self.assertEqual((contact.kogo_outcome, contact.lead_stage), ('pending', 'signup_started'))
        self.assertEqual(ContactEvent.objects.filter(kind='lead_stage_changed').count(), 1)

    def test_with_a_shadow_answer_waiting_both_get_their_turn(self):
        self.incoming('אפשר פרטים על חוג קפוארה?')
        Contact.objects.update(
            last_message_at=timezone.now() - 10 * MINUTE, last_inbound_at=timezone.now() - 10 * MINUTE, lead_checked_at=None,
        )
        counts = cron.tick()
        self.assertEqual(counts['lead_checked'], 1)
        self.assertEqual((counts['shadow_proposed'], counts['pending_shadow']), (1, 0))
        self.assertEqual(self.contact().lead_stage, 'in_conversation')
        # The shadow's own bookkeeping touched the contact after the look: the next call looks once more, finds
        # nothing new, and nobody is left waiting.
        again = cron.tick()
        self.assertEqual((again['lead_checked'], again['lead_changed'], again['pending_lead_stage']), (1, 0, 0))
        # On a call too short to leave a shadow answer its time, the stages wait for the next one.
        Contact.objects.update(needs_shadow=True, lead_checked_at=None)
        short = cron.tick(budget_seconds=cron.SHADOW_ANSWER_SECONDS)
        self.assertEqual((short['lead_checked'], short['pending_lead_stage']), (0, 1))

    def test_a_call_with_no_time_does_nothing_and_says_how_many_wait(self):
        self.lead(HOUR)
        self.lead(HOUR)
        spent = cron.tick(budget_seconds=0)
        self.assertEqual((spent['lead_checked'], spent['lead_changed'], spent['pending_lead_stage']), (0, 0, 2))
        done = cron.tick()
        self.assertEqual((done['lead_checked'], done['pending_lead_stage']), (2, 0))

    def test_a_fault_in_the_stages_does_not_stop_the_slice(self):
        self.lead(HOUR)
        with patch('apps.wahub.cron.lead_stage.scan', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.wahub.cron', level='ERROR'):
            counts = cron.tick()
        self.assertEqual((counts['lead_checked'], counts['pending_lead_stage']), (0, 1))
        self.assertIn('seconds', counts)

    def test_the_endpoint_reports_the_stages(self):
        self.lead(HOUR)
        data = APIClient().get(f'{BASE}/cron/tick/', HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertEqual((data['lead_checked'], data['lead_changed'], data['pending_lead_stage']), (1, 1, 0))


class CommandTests(LeadStageTestCase):
    def test_the_local_command_fills_the_stages_and_prints_them(self):
        self.lead(40 * MINUTE, answer=None)
        self.lead(HOUR)
        out = StringIO()
        with patch('apps.wahub.demo.ensure_local_database', return_value='kogo_local_test'):
            call_command('wahub_lead_scan', stdout=out)
        self.assertEqual(Contact.objects.filter(lead_stage='').count(), 0)
        self.assertIn('נבדקו 2 אנשי קשר', out.getvalue())
        self.assertIn('שאל ולא קיבל מענה (needs_reply)', out.getvalue())

    def test_it_refuses_a_database_that_is_not_local(self):
        # The test database itself (test_…) is not a kogo_local one — nor is production's.
        self.lead(HOUR)
        with self.assertRaises(CommandError) as raised:
            call_command('wahub_lead_scan', '--all')
        self.assertIn('kogo_local', str(raised.exception))
        self.assertEqual(Contact.objects.get().lead_stage, '')


# --- the board ------------------------------------------------------------------------------------------

class ApiTestCase(LeadStageTestCase):
    """Through the API the clock is the real one: the leads are placed by whole days, at noon."""

    def noon(self, days_ago: int):
        return self.now - timedelta(days=days_ago)

    def placed(self, days_ago: int, **fields) -> Contact:
        """A lead who wrote at noon `days_ago` days back and was answered, with its stage kept as of now."""
        real_now = timezone.now()
        contact = self.lead(real_now - self.noon(days_ago), now=real_now, **fields)
        lead_stage.refresh(contact)
        return Contact.objects.get(pk=contact.pk)


class BoardTests(ApiTestCase):
    def board(self) -> dict:
        response = self.get('leads/board/')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_three_groups_in_order_and_every_stage_listed_even_with_nobody(self):
        data = self.board()
        self.assertEqual(set(data), {'groups', 'checked_at', 'pending_scan'})
        for group in data['groups']:
            self.assertEqual(set(group), {'key', 'label', 'total', 'stages'})
            for stage in group['stages']:
                self.assertEqual(set(stage), {'key', 'label', 'hint', 'next_step', 'count', 'oldest_days'})
                self.assertEqual((stage['count'], stage['oldest_days']), (0, None))      # nobody yet, and still listed
        self.assertEqual(
            [(group['key'], group['label'], group['total']) for group in data['groups']],
            [('act', 'דורש טיפול', 0), ('wait', 'בתהליך', 0), ('closed', 'סגור', 0)],
        )
        self.assertEqual(
            [[stage['key'] for stage in group['stages']] for group in data['groups']],
            [
                [
                    'signup_stuck', 'needs_reply', 'signup_failed', 'signup_started', 'link_sent', 'trial_no_show',
                    'trial_done', 'callback_due', 'answered_silent',
                ],
                ['will_check', 'trial_booked', 'waiting_date', 'waiting_list', 'in_conversation'],
                ['nothing_to_offer', 'registered', 'customer', 'not_interested', 'deleted', 'cold'],
            ],
        )
        first = data['groups'][0]['stages'][1]
        self.assertEqual(set(first), {'key', 'label', 'hint', 'next_step', 'count', 'oldest_days'})
        self.assertEqual(
            (first['label'], first['hint'], first['next_step'], first['count'], first['oldest_days']),
            ('שאל ולא קיבל מענה', 'ביקש נציג, שאל ולא קיבל תשובה, או כתב ולא נענה יותר מ-30 דקות', 'לענות לו', 0, None),
        )
        self.assertEqual((data['checked_at'], data['pending_scan']), (None, 0))

    def test_it_counts_who_is_in_each_stage_and_how_long_the_longest_has_been_there(self):
        self.placed(3)                                              # answered_silent, 3 days
        self.placed(12)                                             # answered_silent, 12 days
        self.placed(5, answer=None)                                 # needs_reply, 5 days
        self.placed(1, kogo_outcome='pending')                      # signup_started, since now
        self.placed(1, kogo_outcome='trial_upcoming')               # trial_booked
        self.placed(0, kogo_outcome='customer_before')              # customer
        self.placed(45)                                             # cold
        self.placed(9, is_demo=True, source='demo')                 # a demo contact counts like everybody
        waiting = self.lead(HOUR)                                   # not looked at yet: in no stage

        data = self.board()
        rows = {stage['key']: (stage['count'], stage['oldest_days']) for group in data['groups'] for stage in group['stages']}
        self.assertEqual(rows['answered_silent'], (3, 12))
        self.assertEqual(rows['needs_reply'], (1, 5))
        self.assertEqual(rows['signup_started'], (1, 0))
        self.assertEqual(rows['trial_booked'], (1, 0))
        self.assertEqual(rows['customer'], (1, 0))
        self.assertEqual(rows['cold'], (1, 45))
        self.assertEqual(rows['trial_done'], (0, None))
        self.assertEqual([group['total'] for group in data['groups']], [5, 1, 2])
        self.assertEqual(data['pending_scan'], 1)
        self.assertIsNotNone(data['checked_at'])

        lead_stage.refresh(waiting)
        again = self.board()
        self.assertEqual(again['pending_scan'], 0)
        self.assertEqual([group['total'] for group in again['groups']], [5, 2, 2])

    def test_one_query_whatever_the_number_of_contacts(self):
        for days in range(6):
            self.placed(days)
        with self.assertNumQueries(3):      # the user, the profile, one aggregate
            self.board()

    def test_only_a_manager(self):
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            client = client_for(make_user(f'{role}@lead.test', role))
            self.assertEqual(client.get(f'{BASE}/leads/board/').status_code, 403)
        self.assertIn(APIClient().get(f'{BASE}/leads/board/').status_code, (401, 403))
        self.assertEqual(self.client.post(f'{BASE}/leads/board/', {}, format='json').status_code, 405)


# --- the lists and the contact --------------------------------------------------------------------------

class ListTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.silent_3 = self.placed(3, name='שותק 3')
        self.silent_12 = self.placed(12, name='שותק 12', known_interest='hot')
        self.unanswered = self.placed(5, answer=None, name='לא נענה')
        self.booked = self.placed(1, kogo_outcome='trial_upcoming', name='ניסיון')
        self.customer = self.placed(0, kogo_outcome='customer_before', name='לקוח')
        self.old = self.placed(45, name='ישן')

    def names(self, **params):
        response = self.get('contacts/', **params)
        self.assertEqual(response.status_code, 200, response.data)
        return [row['name'] for row in response.data['results']]

    def test_the_people_of_one_stage_longest_there_first(self):
        self.assertEqual(self.names(lead_stage='answered_silent'), ['שותק 12', 'שותק 3'])
        self.assertEqual(self.names(lead_stage='needs_reply'), ['לא נענה'])
        self.assertEqual(self.names(lead_stage='trial_done'), [])
        self.assertEqual(self.names(lead_stage='no_such_stage'), [])
        self.assertEqual(self.names(lead_stage='needs_reply,answered_silent'), ['שותק 12', 'לא נענה', 'שותק 3'])

    def test_the_people_of_a_group(self):
        self.assertEqual(self.names(lead_group='act'), ['שותק 12', 'לא נענה', 'שותק 3'])
        self.assertEqual(self.names(lead_group='wait'), ['ניסיון'])
        # The stages that ask for nothing read newest first.
        self.assertEqual(self.names(lead_group='closed'), ['לקוח', 'ישן'])
        self.assertEqual(self.names(lead_group='nothing'), [])
        self.assertEqual(self.names(lead_group='act', lead_stage='needs_reply'), ['לא נענה'])

    def test_a_stage_asked_for_by_name_is_never_hidden_and_the_other_filters_still_hold(self):
        # The leads view leaves registered customers and trials ahead out — unless a stage asks for them.
        self.assertNotIn('ניסיון', self.names(view='leads'))
        self.assertEqual(self.names(view='leads', lead_stage='trial_booked'), ['ניסיון'])
        self.assertEqual(self.names(view='leads', lead_stage='customer'), ['לקוח'])
        self.assertEqual(self.names(lead_group='act', interest='hot'), ['שותק 12'])
        self.assertEqual(self.names(lead_group='act', search='לא נענה'), ['לא נענה'])
        # Without the filter the lists are what they were.
        self.assertEqual(len(self.names()), 6)

    def test_the_call_the_screen_makes(self):
        # kogo-front asks for a stage's people as contacts/?view=chats&lead_stage=<key> (and lead_group=),
        # with page_size and search as on every list, and reads `lead` off every row.
        registered = self.placed(2, kogo_outcome='registered_after', kogo_detail='נרשם ב-9.10 · נועה לוי', name='נרשמה')
        marked = self.placed(4, followup_status='registered', followup_at=timezone.now() - DAY, name='סומן נרשם')
        lead_keys = {'stage', 'stage_label', 'group', 'since', 'days', 'reason', 'next_step', 'human_touched', 'temperature', 'owner'}

        for view in ({'view': 'chats'}, {}, {'view': 'leads'}):
            with self.subTest(view=view):
                # A closed stage asked for by name gives its people: a registered customer is hidden by
                # default (`kogo.hidden_by_default`) from the leads list, never from the list of its own stage.
                self.assertEqual(self.names(lead_stage='customer', **view), ['לקוח'])
                self.assertEqual(self.names(lead_stage='registered', **view), ['נרשמה', 'סומן נרשם'])
                self.assertEqual(self.names(lead_stage='trial_booked', **view), ['ניסיון'])
                # Closed stages read newest first: registered a moment ago, a customer since setUp, marked yesterday, cold.
                self.assertEqual(self.names(lead_group='closed', **view), ['נרשמה', 'לקוח', 'סומן נרשם', 'ישן'])
                self.assertEqual(self.names(lead_group='wait', **view), ['ניסיון'])
                self.assertEqual(self.names(lead_group='act', **view), ['שותק 12', 'לא נענה', 'שותק 3'])
                self.assertEqual(self.names(lead_stage='answered_silent', **view), ['שותק 12', 'שותק 3'])

        # page_size and search, as on every list.
        first = self.get('contacts/', view='chats', lead_group='act', page_size=2)
        self.assertEqual((first.data['count'], [row['name'] for row in first.data['results']]), (3, ['שותק 12', 'לא נענה']))
        self.assertIsNotNone(first.data['next'])
        second = self.get('contacts/', view='chats', lead_group='act', page_size=2, page=2)
        self.assertEqual([row['name'] for row in second.data['results']], ['שותק 3'])
        self.assertEqual(self.names(view='chats', lead_group='act', search='שותק'), ['שותק 12', 'שותק 3'])
        self.assertEqual(self.names(view='chats', lead_stage='registered', search='סומן'), ['סומן נרשם'])
        self.assertEqual(self.names(view='chats', lead_stage='registered', search='אין כזה'), [])

        # Every row of every such list carries `lead`, whole.
        for params in ({'lead_group': 'act'}, {'lead_group': 'wait'}, {'lead_group': 'closed'}, {'lead_stage': 'registered'}):
            rows = self.get('contacts/', view='chats', **params).data['results']
            self.assertTrue(rows)
            for row in rows:
                with self.subTest(params=params, name=row['name']):
                    self.assertEqual(set(row['lead']), lead_keys)
                    self.assertTrue(row['lead']['stage'] and row['lead']['stage_label'] and row['lead']['reason'])
                    self.assertEqual(row['lead']['group'], LEAD_STAGE_GROUPS[row['lead']['stage']])
        by_name = {row['name']: row for row in self.get('contacts/', view='chats', lead_stage='registered').data['results']}
        self.assertEqual(by_name['נרשמה']['lead']['reason'], 'נרשם ב-9.10 · נועה לוי.')
        self.assertTrue(by_name['נרשמה']['kogo']['hidden_by_default'])                 # hidden by default, shown when asked for
        self.assertEqual((by_name['סומן נרשם']['lead']['stage'], by_name['סומן נרשם']['lead']['next_step']), ('registered', ''))
        self.assertEqual({registered.lead_stage, marked.lead_stage}, {'registered'})

    def test_a_row_carries_the_stage_as_it_is_kept(self):
        row = self.get('contacts/', lead_stage='answered_silent').data['results'][0]
        self.assertEqual(
            set(row['lead']),
            {'stage', 'stage_label', 'group', 'since', 'days', 'reason', 'next_step', 'human_touched', 'temperature', 'owner'},
        )
        lead = row['lead']
        self.assertEqual((lead['stage'], lead['stage_label'], lead['group']), ('answered_silent', 'קיבל תשובה ולא נרשם', 'act'))
        self.assertEqual(lead['days'], 12)
        self.assertEqual(lead['since'], self.silent_12.lead_stage_since.isoformat())
        self.assertEqual(lead['reason'], f'{ASKED} לפני 12 ימים. רק הבוט ענה. לא נרשם.')
        self.assertEqual(lead['next_step'], 'אף נציג עוד לא דיבר איתו — להתקשר ולהציע שיעור ניסיון')
        self.assertEqual((lead['human_touched'], lead['temperature']), (False, 'hot'))

    def test_the_contact_itself_carries_it_too_and_one_not_looked_at_yet_says_so(self):
        lead = self.get(f'contacts/{self.unanswered.id}/').data['lead']
        self.assertEqual((lead['stage'], lead['group'], lead['days'], lead['next_step']), ('needs_reply', 'act', 5, 'לענות לו'))

        fresh = self.lead(HOUR)
        self.assertEqual(self.get(f'contacts/{fresh.id}/').data['lead'], {
            'stage': '', 'stage_label': '', 'group': '', 'since': None, 'days': None, 'reason': '', 'next_step': '',
            'human_touched': False, 'temperature': 'cold', 'owner': '',
        })

    def test_the_small_dot(self):
        def dot(**fields):
            # Matched a moment ago: opening the conversation does not match it again (matching.STALE_AFTER).
            contact = self.lead(HOUR, kogo_checked_at=timezone.now(), **fields)
            return self.get(f'contacts/{contact.id}/').data['lead']['temperature']

        self.assertEqual(dot(known_interest='hot'), 'hot')
        for outcome in ('signup_declined', 'trial_upcoming', 'trial_only'):       # a step taken is hot, whatever was said
            self.assertEqual(dot(kogo_outcome=outcome, known_interest='cold'), 'hot')
        self.assertEqual(dot(known_interest='warm'), 'warm')
        for interest in ('cold', 'none', ''):
            self.assertEqual(dot(known_interest=interest), 'cold')

    def test_the_list_of_a_stage_does_not_grow_queries_with_rows(self):
        office_clock.calendar()          # the office's hours: read once a minute, whoever asks (test_overdue holds that)
        with self.assertNumQueries(5):      # the user, the profile, the count, the rows, their tags
            self.assertEqual(len(self.names(lead_group='act')), 3)

    def test_the_counts_follow_the_filter(self):
        self.assertEqual(self.get('contacts/counts/', lead_stage='answered_silent').data['boxes']['all'], 2)
        self.assertEqual(self.get('contacts/counts/', lead_group='act').data['boxes']['all'], 3)
        # ... and do not hide what the list of that stage shows (a trial ahead, a registered customer).
        self.assertEqual(self.get('contacts/counts/', lead_stage='trial_booked').data['queues']['all'], 1)
        self.assertEqual(self.get('contacts/counts/', lead_stage='customer').data['queues']['all'], 1)
        self.assertEqual(self.get('contacts/counts/').data['queues']['all'], 4)       # without it, as before


# --- the "today" tab ------------------------------------------------------------------------------------

class SummaryTests(ApiTestCase):
    def test_what_asks_for_something_with_only_the_stages_somebody_is_in(self):
        self.placed(3)
        self.placed(12)
        self.placed(5, answer=None)
        self.placed(1, kogo_outcome='signup_declined')
        self.placed(1, kogo_outcome='trial_upcoming')          # in motion: not on the card
        self.placed(0, kogo_outcome='customer_before')         # closed: not on the card
        self.lead(HOUR)                                        # not looked at yet
        with self.assertNumQueries(5):      # the user, the profile, and still three for the tab
            data = self.get('summary/').data
        self.assertEqual(data['lead_act'], {
            'total': 4,
            'stages': [
                {'key': 'needs_reply', 'label': 'שאל ולא קיבל מענה', 'count': 1},
                {'key': 'signup_failed', 'label': 'ניסה להירשם והחיוב נכשל', 'count': 1},
                {'key': 'answered_silent', 'label': 'קיבל תשובה ולא נרשם', 'count': 2},
            ],
        })
        # The same numbers as the board's first group.
        act = self.get('leads/board/').data['groups'][0]
        self.assertEqual(act['total'], data['lead_act']['total'])
        self.assertEqual(
            {stage['key']: stage['count'] for stage in act['stages'] if stage['count']},
            {stage['key']: stage['count'] for stage in data['lead_act']['stages']},
        )
        # The older card is still served, for the screen that reads it.
        self.assertEqual(set(data['unregistered_leads']), {'total', 'hot', 'oldest_days'})

    def test_with_nobody_the_card_is_empty_and_the_stage_counts_do_not_leak_into_the_tab(self):
        data = self.get('summary/').data
        self.assertEqual(data['lead_act'], {'total': 0, 'stages': []})
        self.assertFalse([key for key in data if key.startswith('lead_') and key != 'lead_act'])


# --- the stage follows what happens, at once ------------------------------------------------------------

class FollowsEventsTests(LeadStageTestCase):
    def lead_of(self, response) -> dict:
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data['lead']

    def test_a_message_and_its_answer(self):
        self.incoming('אפשר פרטים על קפוארה?')
        contact = self.contact()
        self.assertEqual(contact.lead_stage, 'in_conversation')
        self.assertIn('עוד לא קיבל תשובה', contact.lead_reason)
        self.assertEqual(contact.lead_recheck_at, contact.waiting_since + timedelta(minutes=30))
        self.assertEqual(lead_stage.pending_count(), 0)

        # Nobody answered: half an hour later the cron finds it.
        Contact.objects.filter(pk=contact.pk).update(
            waiting_since=timezone.now() - 31 * MINUTE, last_message_at=timezone.now() - 31 * MINUTE,
            last_inbound_at=timezone.now() - 31 * MINUTE, lead_recheck_at=timezone.now() - MINUTE,
        )
        lead_stage.scan()
        self.assertEqual(self.contact().lead_stage, 'needs_reply')

        # The bot's answer takes it off at once.
        self.bot_reply('בשמחה! באיזה סניף?')
        contact = self.contact()
        self.assertEqual(contact.lead_stage, 'in_conversation')
        self.assertIn('רק הבוט ענה', contact.lead_reason)
        journal = list(ContactEvent.objects.filter(kind='lead_stage_changed').order_by('id').values_list('text', flat=True))
        self.assertEqual(len(journal), 2)
        self.assertTrue(journal[0].startswith('בשיחה ← שאל ולא קיבל מענה ('), journal[0])
        self.assertTrue(journal[1].startswith('שאל ולא קיבל מענה ← בשיחה ('), journal[1])

    def test_asking_for_a_person_is_a_stage_at_once(self):
        self.incoming('אפשר לדבר עם נציג?')
        contact = self.contact()
        self.assertEqual(contact.lead_stage, 'needs_reply')
        self.assertIn('מחכה לנציג (ביקש נציג)', contact.lead_reason)
        # "טופל" from the screen, with the bot's answer already there.
        self.bot_reply('מעביר לנציג')
        lead = self.lead_of(self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': False}))
        self.assertEqual(lead['stage'], 'in_conversation')
        lead = self.lead_of(self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': True, 'reason': 'מתלבטת'}))
        self.assertEqual((lead['stage'], lead['next_step']), ('needs_reply', 'לענות לו'))

    def test_a_mark_moves_the_stage_in_the_same_answer(self):
        self.incoming('אפשר פרטים?')
        self.bot_reply('בוודאי')
        url = f'{BASE}/contacts/{self.contact().id}/followup/'

        def mark(**data):
            return self.lead_of(self.client.patch(url, data, format='json'))

        self.assertEqual(mark(status='not_relevant')['stage'], 'not_interested')
        self.assertEqual(mark(status='registered')['stage'], 'registered')
        due = (self.today + timedelta(days=6)).isoformat()
        waiting = mark(status='later', due=due)
        self.assertEqual((waiting['stage'], waiting['group']), ('waiting_date', 'wait'))
        self.assertEqual(mark(status='waiting_us')['stage'], 'callback_due')
        self.assertEqual(mark(status='')['stage'], 'in_conversation')
        # Every change of stage is in the journal, and the marks are still the person's own.
        self.assertEqual(ContactEvent.objects.filter(kind='lead_stage_changed').count(), 5)
        self.assertEqual(ContactEvent.objects.filter(kind='followup_changed').count(), 5)
        self.assertEqual(self.contact().followup_by, self.manager)

    def test_a_recheck_of_the_registrations(self):
        self.incoming('אפשר פרטים?')
        self.bot_reply('בוודאי')
        contact = self.contact()
        family = TestDataFactory.create_family(phone='050-5550101')
        TestDataFactory.create_child(family=family, status='pending')
        lead = self.lead_of(self.post(f'contacts/{contact.id}/recheck/'))
        self.assertEqual((lead['stage'], lead['next_step']), ('signup_started', 'להתקשר ולעזור לסיים'))

        # And from the customer's card.
        Contact.objects.filter(pk=contact.pk).update(kogo_outcome='', lead_stage='', kogo_detail='')
        self.assertEqual(self.post('for-customer/recheck/', {'family': str(family.id)}).status_code, 200)
        self.assertEqual(self.contact().lead_stage, 'signup_started')

    def test_a_summary_asked_for_from_the_screen(self):
        self.incoming('כמה עולה חוג קפוארה?')
        self.bot_reply('260 ₪ לחודש')
        self.incoming('לא מעוניינת, תודה')
        self.bot_reply('בסדר גמור')
        contact = self.contact()
        self.assertEqual(contact.lead_stage, 'in_conversation')       # the summary has not read it yet
        with override_settings(ANTHROPIC_API_KEY=''):
            lead = self.lead_of(self.post(f'contacts/{contact.id}/analyze/'))
        self.assertEqual((lead['stage'], lead['group']), ('not_interested', 'closed'))

    @override_settings(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
    def test_an_answer_from_the_office(self):
        self.incoming('מה המחיר?', subscriber_id='4242')
        contact = self.contact()
        Contact.objects.filter(pk=contact.pk).update(waiting_since=timezone.now() - HOUR)
        lead_stage.refresh(contact)
        self.assertEqual(self.contact().lead_stage, 'needs_reply')
        with patch('apps.core.manychat_service.ManyChatService._request', return_value={'status': 'success'}):
            response = self.post(f'contacts/{contact.id}/send/', {'text': '260 ₪ לחודש'})
        lead = response.data['contact']['lead']
        self.assertEqual((lead['stage'], lead['human_touched']), ('in_conversation', True))
        self.assertIn('נציג ענה לו', lead['reason'])

    def test_a_send_that_did_not_go_out_answers_nobody(self):
        # The switch is off (the default): the attempt is kept as failed, and the customer is still waiting.
        self.incoming('מה המחיר?')
        contact = self.contact()
        Contact.objects.filter(pk=contact.pk).update(waiting_since=timezone.now() - HOUR)
        response = self.post(f'contacts/{contact.id}/send/', {'text': '260 ₪ לחודש'})
        self.assertEqual(response.data['message']['status'], 'failed')
        lead = response.data['contact']['lead']
        self.assertEqual((lead['stage'], lead['human_touched']), ('needs_reply', False))

    def test_a_contact_added_by_hand_and_a_demo_contact(self):
        manual = self.lead_of(self.post('contacts/', {'phone': '050-5550177', 'name': 'ליד מהטלפון'}))
        self.assertEqual((manual['stage'], manual['group']), ('in_conversation', 'wait'))
        self.assertIn('אין שיחה בוואטסאפ', manual['reason'])

        created = self.post('contacts/', {'phone': '050-5550178', 'name': 'דמו', 'is_demo': True})
        self.assertTrue(created.data['is_demo'])
        typed = self.post(f"contacts/{created.data['id']}/simulate-inbound/", {'text': 'אפשר נציג?'})
        self.assertEqual((typed.data['is_demo'], typed.data['lead']['stage']), (True, 'needs_reply'))
        # On the board like everybody.
        board = self.get('leads/board/').data
        self.assertEqual(board['groups'][0]['stages'][1]['count'], 1)       # needs_reply, after "נתקע בהרשמה"

    def test_a_database_fault_in_the_stage_does_not_take_the_callers_work_with_it(self):
        def broken(contact, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute('SELECT no_such_column FROM wahub_contacts')      # aborts the transaction it runs in

        # The test itself is one transaction, as a caller's may be: the message kept before the fault stays readable.
        with patch('apps.wahub.lead_stage.refresh', side_effect=broken), self.assertLogs('apps.wahub.lead_stage', level='ERROR'):
            result = self.incoming('שלום')
        self.assertTrue(result.stored)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.contact().messages_count, 1)

    def test_a_fault_in_the_stage_never_fails_the_message(self):
        with patch('apps.wahub.lead_stage.compute', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.wahub.lead_stage', level='ERROR'):
            result = self.incoming('שלום')
        self.assertTrue(result.stored)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.contact().lead_stage, '')
        # ... nor a mark.
        with patch('apps.wahub.lead_stage.compute', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.wahub.lead_stage', level='ERROR'):
            response = self.client.patch(
                f'{BASE}/contacts/{self.contact().id}/followup/', {'status': 'answered'}, format='json',
            )
        self.assertEqual((response.status_code, response.data['followup']['status']), (200, 'answered'))


# --- the trial, as the matching keeps it ----------------------------------------------------------------

class TrialFactsTests(LeadStageTestCase):
    def setUp(self):
        super().setUp()
        self.lesson = TestDataFactory.create_lesson()
        self.incoming('אשמח לפרטים')
        self.bot_reply('בשמחה')
        family = TestDataFactory.create_family(name='לוי', branch=self.lesson.course.branch, phone='050-5550101')
        self.child = TestDataFactory.create_child(family=family, first_name='נועה', last_name='לוי', status='trial_completed')

    def recheck(self) -> Contact:
        matching.recheck_contact(self.contact())
        lead_stage.refresh(self.contact())
        return self.contact()

    def trial(self, **fields) -> LessonEnrollment:
        return LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active', **fields)

    def test_a_trial_marked_did_not_come(self):
        held = self.today - timedelta(days=4)
        self.trial(trial_held_on=held, trial_outcome='no_show')
        contact = self.recheck()
        self.assertEqual(contact.kogo_outcome, 'trial_only')
        self.assertEqual(contact.kogo_trial, {'on': held.isoformat(), 'outcome': 'no_show', 'course': self.lesson.course.name})
        self.assertEqual(contact.lead_stage, 'trial_no_show')
        self.assertEqual(contact.lead_reason, f'{contact.kogo_detail} · {self.lesson.course.name}. לא נרשם.')
        self.assertIn('לא הגיע', contact.lead_reason)
        self.assertEqual(timezone.localtime(contact.lead_stage_since).date(), held)
        self.assertEqual(contact.lead_next_step, 'לתאם מועד חדש')

    def test_a_trial_that_was_attended_or_never_marked(self):
        held = self.today - timedelta(days=4)
        enrollment = self.trial(trial_held_on=held, trial_outcome='attended')
        contact = self.recheck()
        self.assertEqual((contact.kogo_trial['outcome'], contact.lead_stage), ('attended', 'trial_done'))
        for stored in ('unmarked', ''):
            with self.subTest(stored=stored):
                LessonEnrollment.objects.filter(pk=enrollment.pk).update(trial_outcome=stored, trial_lesson_date=held)
                contact = self.recheck()
                self.assertEqual((contact.kogo_trial['outcome'], contact.lead_stage), ('', 'trial_done'))
        # Marked later as "did not come": the stage follows.
        LessonEnrollment.objects.filter(pk=enrollment.pk).update(trial_outcome='no_show')
        self.assertEqual(self.recheck().lead_stage, 'trial_no_show')
        self.assertTrue(
            ContactEvent.objects.filter(kind='lead_stage_changed', text__startswith='עשה ניסיון ולא נרשם ← נרשם לניסיון ולא הגיע').exists()
        )

    def test_a_trial_still_ahead_and_what_is_left_when_it_is_not_about_a_trial(self):
        ahead = self.today + timedelta(days=3)
        enrollment = self.trial(trial_lesson_date=ahead)
        contact = self.recheck()
        self.assertEqual(contact.kogo_outcome, 'trial_upcoming')
        self.assertEqual(contact.kogo_trial, {'on': ahead.isoformat(), 'outcome': '', 'course': self.lesson.course.name})
        self.assertEqual(contact.lead_stage, 'trial_booked')
        self.assertEqual(contact.lead_reason, f'{contact.kogo_detail} · {self.lesson.course.name}.')

        enrollment.delete()
        type(self.child).objects.filter(pk=self.child.pk).update(status='pending')
        contact = self.recheck()
        self.assertEqual((contact.kogo_outcome, contact.kogo_trial, contact.lead_stage), ('pending', {}, 'signup_started'))

    def test_a_change_of_the_trial_alone_is_a_change_of_the_answer(self):
        held = self.today - timedelta(days=4)
        self.trial(trial_held_on=held, trial_outcome='attended')
        contact = self.contact()
        self.assertTrue(matching.recheck_contact(contact))
        # A row matched before this field existed: the same sentence, no facts yet.
        Contact.objects.filter(pk=contact.pk).update(kogo_trial={})
        self.assertTrue(matching.recheck_contact(self.contact()))
        self.assertEqual(self.contact().kogo_trial['outcome'], 'attended')
        self.assertFalse(matching.recheck_contact(self.contact()))

    def test_a_paying_child_is_registered_whatever_the_trial_was(self):
        self.trial(trial_held_on=self.today - timedelta(days=4), trial_outcome='no_show')
        type(self.child).objects.filter(pk=self.child.pk).update(status='active')
        Payment.objects.create(
            child=self.child, family=self.child.family, lesson=self.lesson, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('260'), final_amount=Decimal('260'),
            description='מנוי חודשי - קפוארה', payment_date=timezone.now(),
        )
        contact = self.recheck()
        self.assertEqual((contact.kogo_outcome, contact.kogo_trial, contact.lead_stage), ('registered_after', {}, 'registered'))


# --- the schema, for the code that is already running ---------------------------------------------------

class SchemaTests(LeadStageTestCase):
    def test_the_database_itself_fills_the_new_columns_for_code_that_does_not_know_them(self):
        # A branch's build migrates production while the previous code still creates a contact for every
        # new number (migrations 0006 and 0007): the defaults must be the database's own, not only Django's.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'wahub_contacts' AND (column_name LIKE 'lead\\_%' OR column_name = 'kogo_trial') "
                "ORDER BY column_name"
            )
            self.assertEqual(cursor.fetchall(), [
                ('kogo_trial', 'NO', "'{}'::jsonb"),
                ('lead_ai', 'NO', "'{}'::jsonb"),
                ('lead_bot', 'NO', "'{}'::jsonb"),
                ('lead_branch_id', 'YES', None),
                ('lead_checked_at', 'YES', None),
                ('lead_come_back_on', 'YES', None),
                ('lead_course_type', 'NO', "''::character varying"),
                ('lead_human_touched', 'NO', 'false'),
                ('lead_link_sent_at', 'YES', None),
                ('lead_manual', 'NO', "'{}'::jsonb"),
                ('lead_next_step', 'NO', "''::character varying"),
                ('lead_opened', 'NO', "'{}'::jsonb"),
                ('lead_owner', 'NO', "''::character varying"),
                ('lead_reason', 'NO', "''::character varying"),
                ('lead_recheck_at', 'YES', None),
                ('lead_stage', 'NO', "''::character varying"),
                ('lead_stage_since', 'YES', None),
                ('lead_version', 'NO', '0'),
                ('lead_waiting', 'NO', "'{}'::jsonb"),
            ])
            # The insert the previous code makes: none of the new columns named.
            cursor.execute(
                "INSERT INTO wahub_contacts (phone, name, manychat_subscriber_id, source, last_message_text, "
                "last_message_direction, last_message_sender, messages_count, unread_count, handled_by, needs_human, "
                "needs_human_reason, known_topic, known_course_type, known_city, known_branch_name, known_child_age, "
                "known_interest, known_flags, known_summary, needs_analysis, analysis_source, kogo_outcome, kogo_child_ids, "
                "kogo_detail, needs_shadow, is_demo, live_test, followup_status, followup_note, touched_at, created_at, updated_at) "
                "VALUES (%s, '', '', 'whatsapp', '', '', '', 0, 0, 'bot', false, '', '', '', '', '', '', '', '[]', '', false, '', "
                "'', '[]', '', false, false, false, '', '', now(), now(), now())",
                [PHONE],
            )
        contact = Contact.objects.get(phone=PHONE)
        self.assertEqual((contact.lead_stage, contact.lead_reason, contact.lead_next_step), ('', '', ''))
        self.assertEqual((contact.lead_human_touched, contact.kogo_trial), (False, {}))
        self.assertEqual((contact.lead_ai, contact.lead_bot, contact.lead_manual, contact.lead_opened), ({}, {}, {}, {}))
        self.assertEqual((contact.lead_owner, contact.lead_course_type, contact.lead_version), ('', '', 0))
        # ... and the new code picks such a contact up on its next look.
        self.assertEqual(lead_stage.scan()['lead_checked'], 1)


class RepliesNotCopiedTests(WahubTestCase):
    """
    ManyChat copies the customers' messages and not the old bot's replies (production,
    11.10.2026): a message with no answer on record is not "asked and got no answer".
    """

    def setUp(self):
        super().setUp()
        lead_stage._seen.update(at=None, since=None)
        self.addCleanup(lambda: lead_stage._seen.update(at=None, since=None))
        self.now = timezone.now()

    def waiting(self, **fields):
        self.incoming('יש חוג קפוארה לבן 6?')
        contact = self.contact()
        two_hours_ago = self.now - timedelta(hours=2)
        Contact.objects.filter(pk=contact.pk).update(
            waiting_since=two_hours_ago, last_inbound_at=two_hours_ago, last_message_at=two_hours_ago, **fields,
        )
        return Contact.objects.get(pk=contact.pk)

    def test_a_real_lead_is_not_called_unanswered_while_the_bots_replies_are_not_copied(self):
        result = lead_stage.compute(self.waiting(), self.now)
        self.assertEqual(result['stage'], 'in_conversation')
        self.assertNotIn('עוד לא קיבל תשובה', result['reason'])

    def test_asking_for_a_person_still_needs_a_reply(self):
        result = lead_stage.compute(self.waiting(needs_human=True, needs_human_at=self.now), self.now)
        self.assertEqual(result['stage'], 'needs_reply')

    def test_a_contact_kogo_answers_itself_is_still_measured(self):
        for fields in ({'is_demo': True}, {'live_test': True}, {'handled_by': 'human'}):
            with self.subTest(fields=fields):
                contact = self.waiting(**fields)
                self.assertEqual(lead_stage.compute(contact, self.now)['stage'], 'needs_reply')
                Contact.objects.filter(pk=contact.pk).delete()

    def test_the_old_bots_line_in_a_demo_conversation_is_not_a_copy_from_manychat(self):
        # "כתוב כבוט הישן" on a demo contact goes through the very door ManyChat's copies use, and a demo
        # scenario's lines are kept with the same source: neither says ManyChat copies anything.
        self.incoming('שלום', phone='972505550188')
        Contact.objects.filter(phone='972505550188').update(is_demo=True, source='demo')
        self.bot_reply('היי, כאן הבוט הישן', phone='972505550188')
        lead_stage._seen.update(at=None)
        self.assertFalse(lead_stage.old_bot_replies_seen(self.now))
        self.assertEqual(lead_stage.compute(self.waiting(), self.now)['stage'], 'in_conversation')

    def test_a_message_from_before_the_copying_began_is_not_judged_and_one_written_since_is(self):
        waited_before = self.waiting()                      # wrote two hours ago, when nothing was copied
        # ManyChat began copying an hour ago: its first copy, in somebody else's conversation.
        self.incoming('שלום', phone='972505550177')
        self.bot_reply('היי', phone='972505550177')
        began = self.now - timedelta(hours=1)
        Message.objects.filter(sender='bot').update(created_at=began)
        lead_stage._seen.update(at=None)
        self.assertEqual(lead_stage.replies_copied_since(self.now), began)

        # Whether the bot answered the older message is not on record: it is not "asked and got no answer".
        before = lead_stage.compute(waited_before, self.now)
        self.assertEqual(before['stage'], 'in_conversation')
        self.assertNotIn('עוד לא קיבל תשובה', before['reason'])
        # The same customer wrote again forty minutes ago, and that one is measured — from its own moment.
        forty_minutes_ago = self.now - timedelta(minutes=40)
        Contact.objects.filter(pk=waited_before.pk).update(last_inbound_at=forty_minutes_ago, last_message_at=forty_minutes_ago)
        since = lead_stage.compute(Contact.objects.get(pk=waited_before.pk), self.now)
        self.assertEqual((since['stage'], since['since']), ('needs_reply', forty_minutes_ago))
        # And when the copies stop for more than a week, nothing is judged again.
        self.assertIsNone(lead_stage.replies_copied_since(self.now + timedelta(days=8)))

    def test_once_manychat_copies_a_reply_the_rule_is_back(self):
        contact = self.waiting()
        Message.objects.create(
            contact=contact, direction='out', sender='bot', source='manychat', text='היי', sent_at=self.now - timedelta(days=1),
        )
        # Kept a day ago, before the message that is waiting: from then on Kogo sees the answers.
        Message.objects.filter(sender='bot').update(created_at=self.now - timedelta(days=1))
        lead_stage._seen.update(at=None)
        self.assertTrue(lead_stage.old_bot_replies_seen(self.now))
        self.assertEqual(lead_stage.compute(Contact.objects.get(pk=contact.pk), self.now)['stage'], 'needs_reply')

