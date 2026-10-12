"""
The bot's self-checks about trial dates (docs/WAHUB-BOT-ACCURACY-2026-10-12.md):
a date the bot offers for a trial lesson is one the registration form offers
now. The checks, on answers written here; the facts the scripts are held
against, read from the system; and the scripts themselves, played with the stub
and with Claude behind a mock.
"""
from django.test import SimpleTestCase, override_settings
from unittest.mock import patch

from apps.enrollments.models import LessonEnrollment, TrialBlockedDate
from apps.wahub import course_tools
from apps.wahub.evals import checks, rules, runner, scenarios, world
from apps.wahub.evals.checks import Moment
from apps.wahub.shadow_tools import ToolContext, run_tool
from apps.wahub.tests.test_evals import MODEL, POST, EvalTestCase, FakeClaude

CARD = {
    'name': 'course_card', 'input': {'course_id': 'x'},
    'result': {
        'found': True,
        'course': {
            'name': 'קפוארה כיתות ג-ד',
            'lessons': [{'day': 'ראשון', 'start': '16:00', 'end': '17:00', 'instructor': 'אורי דוגמאי', 'trial_dates': ['25.10.2026', '01.11.2026']}],
            'trial': {
                'is_paid': False, 'can_book': True,
                'dates': [{'date': '25.10.2026', 'day': 'ראשון', 'start': '16:00'}, {'date': '01.11.2026', 'day': 'ראשון', 'start': '16:00'}],
                'full_dates': [{'date': '18.10.2026', 'day': 'ראשון', 'start': '16:00'}],
                'not_offered': [{'date': '14.10.2026', 'day': 'רביעי', 'start': '16:00', 'why': 'אין שיעורי ניסיון בתאריך הזה (חופשה לדוגמה)'}],
                'dates_text': 'אפשר להגיע לשיעור ניסיון ביום ראשון בשעה 16:00, בתאריכים 25.10 או 01.11',
            },
        },
    },
}
WORLD = {'facts': {'full_trial': {'trial_dates': {
    'can_book': True, 'open': ['25.10', '01.11'], 'full': ['18.10'], 'skipped': ['14.10'], 'today_open': False, 'this_week_open': False,
}}}}


class TrialDateChecksTests(SimpleTestCase):
    def moment(self, reply, **extra):
        return Moment(reply=reply, calls=[CARD], world=WORLD, **extra)

    def general(self, reply) -> str:
        check = next(check for check in checks.GENERAL if check.id == 'open_trial_dates')
        return check.test(self.moment(reply))

    def test_what_the_tools_returned_as_open_and_as_closed(self):
        self.assertEqual(checks.trial_dates_evidence([CARD]), ({(25, 10), (1, 11)}, {(18, 10), (14, 10)}))
        # A date open in one class and full in another is not for a hard check to decide.
        other = {'name': 'course_card', 'result': {'course': {'trial': {'dates': [{'date': '18.10.2026'}]}}}}
        self.assertEqual(checks.trial_dates_evidence([CARD, other])[1], {(14, 10)})

    def test_on_every_answer_a_full_or_closed_date_is_not_offered(self):
        self.assertEqual(self.general('אפשר להגיע לשיעור ניסיון ביום ראשון בשעה 16:00, בתאריכים 25.10 או 01.11'), '')
        self.assertIn('18.10', self.general('אפשר להגיע ביום ראשון 18.10 בשעה 16:00'))
        self.assertIn('מלא או לא פתוח לשיעור ניסיון', self.general('נתראה ביום רביעי 14.10'))
        # Said as what it is, a date that is taken is the truth and not an offer.
        self.assertEqual(self.general('ביום ראשון 18.10 כבר מלא\nאפשר להגיע ב-25.10'), '')
        self.assertEqual(self.general('ב-14.10 אין שיעורי ניסיון, התאריך הקרוב הוא 25.10'), '')
        self.assertEqual(self.general('היי, במה אפשר לעזור'), '')
        self.assertIn('open_trial_dates', checks.GENERAL_IDS)
        self.assertEqual(rules.GENERAL_COVERS['open_trial_dates'], ('A1', 'A2'))

    def test_only_a_date_the_tools_returned_as_open(self):
        check = checks.only_open_trial_dates()
        self.assertEqual(check.test(self.moment('אפשר להגיע ביום ראשון 25.10 בשעה 16:00')), '')
        told = check.test(self.moment('אפשר להגיע ביום ראשון 08.11 בשעה 16:00'))          # a date the bot worked out itself
        self.assertIn('08.11', told)
        self.assertIn('25.10, 01.11', told)
        self.assertIn('18.10', check.test(self.moment('אפשר להגיע ב-18.10')))
        self.assertEqual(check.test(self.moment('ב-31.12 אי אפשר, אבל אפשר להגיע ב-25.10\nנרשמים מראש באתר')), '')
        # `grounded_dates` alone is content with a full date: a tool did return it.
        grounded = next(check for check in checks.GENERAL if check.id == 'grounded_dates')
        self.assertEqual(grounded.test(self.moment('אפשר להגיע ב-18.10')), '')
        nothing_open = Moment(reply='אפשר להגיע ב-25.10', calls=[{'name': 'course_card', 'result': {'course': {'trial': {'can_book': False, 'dates': []}}}}])
        self.assertIn('אין כרגע תאריך פנוי', check.test(nothing_open))

    def test_the_checks_that_read_the_system_itself(self):
        says = checks.says_open_trial_date('full_trial')
        self.assertEqual(says.test(self.moment('אפשר ב-25.10')), '')
        self.assertIn('25.10, 01.11', says.test(self.moment('אפשר להגיע ביום ראשון')))
        # A class the site offers no date for: the answer says that there is none, and offers none.
        nothing = {'facts': {'full_trial': {'trial_dates': {'can_book': False, 'open': [], 'full': ['18.10']}}}}
        self.assertIn('הבוט הציע: 25.10', says.test(Moment(reply='אפשר ב-25.10', world=nothing)))
        self.assertIn('ציפינו שהבוט יאמר זאת', says.test(Moment(reply='נרשמים מראש באתר', world=nothing)))
        self.assertEqual(says.test(Moment(reply='כרגע כל התאריכים הקרובים מלאים, גם 18.10', world=nothing)), '')
        closed = checks.not_closed_trial_date('full_trial')
        self.assertEqual(closed.test(self.moment('אפשר ב-25.10')), '')
        self.assertIn('התאריך הזה מלא', closed.test(self.moment('אפשר להגיע ב-18.10')))
        self.assertIn('חופשה או שיעור שבוטל', closed.test(self.moment('אפשר להגיע ב-14.10')))
        self.assertEqual(closed.test(self.moment('ב-18.10 מלא, אפשר ב-25.10')), '')

    def test_today_and_this_week_only_when_the_system_has_them(self):
        today = checks.today_as_the_system_has_it('full_trial')
        self.assertIn('אי אפשר להירשם היום', today.test(self.moment('כן, אפשר להגיע היום בשעה 16:00')))
        self.assertEqual(today.test(self.moment('היום אין שיעור, התאריך הקרוב הוא 25.10')), '')
        self.assertEqual(today.test(self.moment('התאריך הקרוב הוא 25.10')), '')
        week = checks.this_week_as_the_system_has_it('full_trial')
        self.assertIn('אין השבוע תאריך פנוי', week.test(self.moment('בטח, אפשר השבוע ביום ראשון')))
        self.assertEqual(week.test(self.moment('השבוע אין תאריך פנוי\nאפשר להגיע ביום ראשון 25.10')), '')
        open_world = {'facts': {'full_trial': {'trial_dates': {'today_open': True, 'this_week_open': True}}}}
        self.assertEqual(today.test(Moment(reply='אפשר להגיע היום', world=open_world)), '')
        self.assertEqual(week.test(Moment(reply='אפשר להגיע השבוע', world=open_world)), '')


@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class TrialDatesWorldTests(EvalTestCase):
    def test_the_invented_catalogue_has_a_full_date_and_a_closed_day(self):
        built = world.build()
        words, facts = built['words'], built['facts']
        self.assertEqual(built['missing'], {})
        self.assertEqual((words['trial_field'], words['trial_child']), (words['field'], words['child']))     # the main class can be booked
        self.assertEqual((words['full_field'], words['full_child']), ('קפוארה', 'בכיתה ג'))
        self.assertEqual((words['skipped_field'], words['skipped_child']), ('קפוארה', 'בכיתה ג'))
        trial = facts['trial']['trial_dates']
        self.assertTrue(trial['can_book'] and len(trial['open']) == 3 and not trial['full'])
        full = facts['full_trial']['trial_dates']
        self.assertEqual((len(full['full']), len(full['skipped'])), (1, 1))
        self.assertFalse(set(full['open']) & (set(full['full']) | set(full['skipped'])))
        # The facts are the form's dates at the scripts' clock — the same ones the tool returns on it.
        from datetime import datetime

        ctx = ToolContext(None, now=datetime.fromisoformat(built['clocks']['open']))
        card = run_tool('course_card', {'course_id': facts['full_trial']['id']}, ctx)['course']['trial']
        self.assertEqual([row['date'][:5] for row in card['dates']], full['open'])
        self.assertEqual([row['date'][:5] for row in card['full_dates']], full['full'])
        self.assertEqual([(row['date'][:5], row['why']) for row in card['not_offered']], [(full['skipped'][0], 'אין שיעורי ניסיון בתאריך הזה (חופשה לדוגמה)')])
        self.assertNotIn(full['full'][0], card['dates_text'])
        self.assertNotIn(full['skipped'][0], card['dates_text'])

    def test_a_system_with_no_such_class_skips_those_scripts_and_says_why(self):
        TrialBlockedDate.objects.all().delete()
        LessonEnrollment.objects.filter(trial_lesson_date__isnull=False).delete()
        built = world.build()
        self.assertEqual(set(built['missing']), {'full_field', 'full_child', 'skipped_field', 'skipped_child'})
        self.assertIn('trial_field', built['words'])
        run = runner.start('trial_dates_a_date_is_full')
        self.assertEqual(run.status, 'skipped')
        self.assertIn('אין כרגע חוג שאחד התאריכים הקרובים שלו לשיעור ניסיון מלא', run.detail)
        self.assertIn('חופשה או שיעור שבוטל', runner.start('trial_dates_a_week_with_a_holiday').detail)


@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class TrialDatesStubRunTests(EvalTestCase):
    def test_the_scripts_are_there_and_run_to_a_verdict(self):
        ids = [scenario.id for scenario in scenarios.TRIAL_DATES]
        self.assertEqual(len(ids), 6)
        self.assertTrue(all(scenarios.BY_ID[name] in scenarios.SCENARIOS for name in ids))
        for name in ids:
            run = runner.run_to_end(name)
            with self.subTest(scenario=name):
                self.assertIn(run.status, ('passed', 'failed'), run.detail)
                self.assertFalse(any('הבדיקה עצמה נכשלה' in row['explain'] for row in run.checks))
                # The stub recites no date, so nothing it says can be a date that is not offered.
                failed = {row['id'] for row in run.checks if row['status'] == 'failed'}
                self.assertFalse(failed & {'open_trial_dates', 'only_open_trial_dates', 'not_closed_trial_date:full_trial', 'not_closed_trial_date:skipped_trial'})
        # …and a script that asks "when" is not passed by an answer with no date in it.
        self.assertEqual(runner.run_to_end('trial_dates_a_date_is_full').status, 'failed')


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL, REGISTRATION_FEE_ILS=120)
class TrialDatesClaudeRunTests(EvalTestCase):
    """The class of grades ג–ד in the invented catalogue: one Sunday full, one Wednesday closed for a holiday."""

    def setUp(self):
        super().setUp()
        self.find = {'city': 'ראש העין', 'course_type': 'קפוארה', 'grade': 'כיתה ג'}

    def play(self, scenario_id, say):
        claude = FakeClaude(('tool', 'find_courses', self.find), ('say', say))
        with patch(POST, side_effect=claude):
            run = runner.run_to_end(scenario_id, with_judge=False)
        return run, self.failed(run)

    def test_a_bot_that_quotes_the_ready_sentence_passes(self):
        run, failed = self.play('trial_dates_a_date_is_full', lambda found: found['courses'][0]['trial']['dates_text'])
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])
        self.assertTrue(run.turns[0]['reply'].startswith('אפשר להגיע לשיעור ניסיון'))
        self.assertEqual(failed, set())

    def test_a_bot_that_offers_the_full_date_fails_and_is_told_why(self):
        def say(found):
            taken = found['courses'][0]['trial']['full_dates'][0]
            return f'אפשר להגיע ביום {taken["day"]} {taken["date"][:5]} בשעה {taken["start"]}'

        run, failed = self.play('trial_dates_a_date_is_full', say)
        self.assertEqual(run.status, 'failed')
        self.assertLessEqual({'open_trial_dates', 'only_open_trial_dates', 'not_closed_trial_date:full_trial'}, failed)
        self.assertIn('התאריך הזה מלא', self.explain(run, 'not_closed_trial_date:full_trial'))
        self.assertIn('מלא או לא פתוח לשיעור ניסיון', runner.run_payload(run)['headline'])

    def test_a_bot_that_offers_the_day_of_the_holiday_fails(self):
        def say(found):
            closed = found['courses'][0]['trial']['not_offered'][0]
            return f'התאריך הכי קרוב הוא יום {closed["day"]} {closed["date"][:5]} בשעה {closed["start"]}'

        run, failed = self.play('trial_dates_a_week_with_a_holiday', say)
        self.assertEqual(run.status, 'failed')
        self.assertIn('not_closed_trial_date:skipped_trial', failed)
        self.assertIn('חופשה או שיעור שבוטל', self.explain(run, 'not_closed_trial_date:skipped_trial'))

    def test_a_bot_that_works_a_date_out_by_itself_fails(self):
        run, failed = self.play('trial_dates_a_date_the_customer_names', 'כן, אפשר להגיע ב-31.12 בשעה 16:00')
        self.assertEqual(run.status, 'failed')
        self.assertIn('only_open_trial_dates', failed)
        self.assertIn('31.12', self.explain(run, 'only_open_trial_dates'))

    def test_the_tool_gives_the_bot_the_rule_with_the_dates(self):
        found = run_tool('find_courses', self.find, ToolContext(None))
        self.assertEqual(found['trial_dates_rule'], course_tools.TRIAL_DATES_RULE)
        trial = found['courses'][0]['trial']
        self.assertEqual({'can_book', 'dates', 'dates_text'} - set(trial), set())
        self.assertLessEqual(len(trial['dates']), course_tools.LIST_DATES)
