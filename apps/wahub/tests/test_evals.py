"""
"בדיקות הבוט" (docs/WAHUB-CONTRACT-BOT-EVALS.md): the scripts, the world they
are filled from, the hard checks, the runner — with the stub and with Claude
behind a mock — the judge, the endpoints and the command. And what holds all
of it honest: a run makes no contact and no message, sends nothing and reaches
no network.
"""
import json
import re
import time
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.wahub.evals import checks, fixtures, judge, rules, runner, scenarios, world
from apps.wahub.evals.checks import Moment
from apps.wahub.models import BotEvalRun, Contact, ContactEvent, KnowledgeItem, Message, ShadowReply
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user

POST = 'apps.wahub.shadow.requests.post'
SEND = 'apps.wahub.sending._deliver'
HANDOFF = 'apps.wahub.handoff._in_manychat'
MODEL = 'claude-opus-5-5'


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


class FakeClaude:
    """
    Claude behind a mock, playing a script: ('tool', name, input) asks for a
    tool, ('say', text) is the bot's answer — a callable is given what the
    last tool returned — and ('json', payload) is any other JSON answer (the
    judge's). The requests it was sent are kept.
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.requests.append(json)
        step = self.steps.pop(0)
        if step[0] == 'tool':
            block = {'type': 'tool_use', 'id': f'toolu_{len(self.requests)}', 'name': step[1], 'input': step[2]}
            return _response({'model': MODEL, 'stop_reason': 'tool_use', 'content': [block]})
        if step[0] == 'json':
            return _response({'model': MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': globals()['json'].dumps(step[1], ensure_ascii=False)}]})
        text = step[1]
        if callable(text):
            text = text(self._last_tool_result(json))
        answer = {'text': text, 'reasoning': 'בדיקה', 'knowledge_ids': []}
        return _response({'model': MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': globals()['json'].dumps(answer, ensure_ascii=False)}]})

    @staticmethod
    def _last_tool_result(body) -> dict:
        last = body['messages'][-1]['content']
        return globals()['json'].loads(last[0]['content']) if isinstance(last, list) else {}


class EvalTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.rows = fixtures.build()
        # A run may reach neither of these, ever.
        self.deliver = patch(SEND, side_effect=AssertionError('a bot check tried to send')).start()
        self.addCleanup(patch.stopall)
        self.handoff = patch(HANDOFF, side_effect=AssertionError('a bot check tried to touch ManyChat')).start()

    def failed(self, run) -> set:
        return {row['id'] for row in run.checks if row['status'] == 'failed'}

    def explain(self, run, check_id) -> str:
        return next(row['explain'] for row in run.checks if row['id'] == check_id and row['status'] == 'failed')


# --- the scripts and the rules --------------------------------------------------------------------------------

class ScriptsTests(SimpleTestCase):
    WORLD_KEYS = {
        'place', 'field', 'child', 'course', 'instructor', 'paid_field', 'paid_child', 'free_field', 'free_child', 'second_field',
        'second_child', 'other_field', 'bring_field', 'bring_child', 'no_class_child', 'boundary_field', 'missing_field', 'multi_city',
        'external_place', 'far_city', 'young_child',
        'trial_field', 'trial_child', 'full_field', 'full_child', 'skipped_field', 'skipped_child',
        # the words of the seats and edge-case conversations (12.10.2026)
        'place_typo', 'place_glued', 'way_place', 'field_typo', 'main_day', 'busy_field', 'busy_child', 'busy_day',
        'packed_field', 'packed_child', 'packed_day', 'empty_field', 'empty_child', 'empty_day', 'two_day_field', 'two_day_child',
        'two_day_day', 'two_day_other', 'sibling_child', 'wifi_place', 'no_wifi_place', 'short_city', 'birth_date', 'off_day', 'trial_day',
        # the words of the owner's answers of 13.10.2026
        'holiday_place', 'holiday_other_place', 'holiday_none_place', 'camp_place', 'no_camp_place', 'manager_place',
    }

    def test_the_scripts_are_well_formed(self):
        ids = [scenario.id for scenario in scenarios.SCENARIOS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 30)
        for scenario in scenarios.SCENARIOS:
            with self.subTest(scenario=scenario.id):
                self.assertIn(scenario.group, scenarios.GROUP_TITLES)
                self.assertIn(scenario.clock, ('open', 'closed', 'weekend', 'before_cancel_deadline', 'after_cancel_deadline'))
                self.assertTrue(scenario.turns and scenario.title and scenario.what and scenario.rules)
                self.assertLessEqual(set(scenario.needs), self.WORLD_KEYS)
                texts = [said if isinstance(said, str) else said.text for turn in scenario.turns for said in turn.says]
                texts += [turn.last_outbound for turn in scenario.turns] + [text for _who, text, _ago in scenario.before]
                # A word a script is filled with is one the script says it needs (or the one every world has).
                used = set(re.findall(r'\{(\w+)\}', ' '.join(texts)))
                self.assertLessEqual(used, set(scenario.needs) | {'young_child'})
                for turn in scenario.turns:
                    self.assertTrue(all(isinstance(check, checks.Check) for check in turn.expect))
                    self.assertLessEqual(set(turn.allow), set(checks.SITUATIONAL))

    def test_every_rule_has_a_script_a_general_check_or_a_reason(self):
        in_scripts = {rule for scenario in scenarios.SCENARIOS for rule in scenario.rules}
        in_general = {rule for ids in rules.GENERAL_COVERS.values() for rule in ids}
        self.assertLessEqual(in_scripts | in_general | set(rules.NO_SCRIPT), set(rules.RULES))
        self.assertEqual(set(rules.RULES) - in_scripts - in_general - set(rules.NO_SCRIPT), set())
        self.assertEqual(set(rules.NO_SCRIPT) & (in_scripts | in_general), set())
        self.assertEqual(set(rules.GENERAL_COVERS), set(checks.GENERAL_IDS))

    def test_the_audit_table_names_every_rule(self):
        """docs/WAHUB-BOT-RULES-AUDIT-2026-10-12.md is where each id is explained; beside the main checkout or two levels up."""
        base = Path(settings.BASE_DIR).resolve()
        found = next((
            folder / 'docs' / 'WAHUB-BOT-RULES-AUDIT-2026-10-12.md' for folder in (base.parents[0], base.parents[1])
            if (folder / 'docs' / 'WAHUB-BOT-RULES-AUDIT-2026-10-12.md').is_file()
        ), None)
        if found is None:
            self.skipTest('the audit table is not beside this checkout')
        table = found.read_text(encoding='utf-8')
        missing = [rule for rule in rules.RULES if f'| {rule} |' not in table]
        self.assertEqual(missing, [])
        self.assertEqual([scenario.id for scenario in scenarios.SCENARIOS if f'`{scenario.id}`' not in table], [])


# --- the hard checks, on answers written here ---------------------------------------------------------------------

FIND = {
    'name': 'find_courses', 'input': {'city': 'ראש העין', 'course_type': 'קפוארה', 'age': 5},
    'result': {
        'branches': [{'name': 'סטודיו הדגמה', 'city': 'ראש העין'}], 'need': '',
        'courses': [
            {
                'id': '11111111-2222-3333-4444-555555555555', 'name': 'קפוארה גיל 4.5-6', 'course_type': 'קפוארה', 'price_monthly': '260.00',
                'registration_fee': '120.00', 'trial': {'is_paid': True, 'price': '30.00', 'credit_rule': '₪30 יורדים מהתשלום הראשון'},
                'lessons': [{'day': 'שלישי', 'start': '17:00', 'end': '17:45', 'instructor': 'אורי דוגמאי', 'next_dates': [{'date': '13.10.2026'}]}],
            },
            {
                'id': '99999999-2222-3333-4444-555555555555', 'name': 'קפוארה כיתות ג-ד', 'course_type': 'קפוארה', 'price_monthly': '270.00',
                'registration_fee': '60.00', 'trial': {'is_paid': False, 'price': None},
                'lessons': [{'day': 'ראשון', 'start': '16:00', 'end': '17:00', 'instructor': 'נועה לדוגמה', 'next_dates': []}],
            },
        ],
    },
}
GATED = {'name': 'find_courses', 'input': {'city': 'ראש העין'}, 'result': {
    'branches': [{'name': 'סטודיו הדגמה'}], 'need': 'course_type', 'ask': 'באיזה תחום מדובר?', 'course_types': ['היפ הופ', 'קפוארה'], 'courses': [],
}}


class _Names:
    courses = {'קפוארה גיל 4.5-6', 'קפוארה כיתות ג-ד', 'היפ הופ גיל 4.5-6'}
    fields = {'קפוארה', 'היפ הופ'}
    instructors = {'אורי דוגמאי', 'נועה לדוגמה', 'דני בדוי'}
    branches = {'סטודיו הדגמה', 'מינץ - הדגמה'}
    cities = {'ראש העין', 'פתח תקווה'}


class ChecksTests(SimpleTestCase):
    def moment(self, reply, calls=(FIND,), **extra):
        defaults = {
            'customer_all': 'יש קפוארה בראש העין לילד בן 5? כמה עולה?', 'customer_now': 'יש קפוארה בראש העין לילד בן 5? כמה עולה?',
            'knowledge_text': 'המשרד: רפאל איתן 5, פתח תקווה\nטלפון 0509424755\nhttps://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/\nעד 01.04',
            'office_days': ('ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי'), 'catalog': _Names(), 'world': {'facts': {'stage': {'fields': ['היפ הופ', 'קפוארה']}}},
        }
        return Moment(reply=reply, calls=list(calls), **{**defaults, **extra})

    def failed(self, reply, calls=(FIND,), **extra) -> dict:
        rows = checks.run(checks.GENERAL, self.moment(reply, calls, **extra), turn=1)
        return {row['id']: row['explain'] for row in rows if row['status'] == 'failed'}

    def test_an_answer_built_from_what_the_tools_returned_passes_everything(self):
        reply = 'ל *קפוארה* בגיל הזה:\n🔹 יום שלישי 17:00–17:45 עם אורי דוגמאי\nהמחיר ₪260 לחודש, ושיעור הניסיון בעלות ₪30'
        self.assertEqual(self.failed(reply), {})

    def test_an_amount_an_hour_a_day_and_a_date_nobody_returned(self):
        failed = self.failed('יש ביום חמישי 18:30, המחיר ₪199 לחודש, מתחילים ב *05.11*')
        self.assertIn('₪199', failed['grounded_amounts'])
        self.assertIn('18:30', failed['grounded_times'])
        self.assertIn('יום חמישי', failed['grounded_days'].replace('ראשון', ''))
        self.assertIn('05.11', failed['grounded_dates'])
        # The same things, returned by the tools or written in the knowledge, pass: ₪260 as "260 שקלים", a date of the knowledge.
        self.assertEqual(self.failed('יום שלישי 17:00, 260 שקלים לחודש, אפשר לבטל עד *01.04*'), {})

    def test_listing_the_fields_to_choose_from_is_not_offering_a_class(self):
        # From the first run against the real bot: a class is named "היפהופ", and the field it was listing is "היפהופ - מחול".
        class Names(_Names):
            courses = {'היפהופ', 'קפוארה גיל 4.5-6'}
            fields = {'קפוארה', 'היפהופ - מחול'}

        check = checks.offers_nothing()
        asking = self.moment('באיזה תחום מדובר?\n🔹 קפוארה\n🔹 היפהופ - מחול', calls=(GATED,), catalog=Names(), customer_all='ראש העין')
        self.assertEqual(check.test(asking), '')
        # The class itself, offered before the field was chosen, is still an offer.
        offering = self.moment('יש לנו את החוג קפוארה גיל 4.5-6, מתאים?', calls=(GATED,), catalog=Names(), customer_all='ראש העין')
        self.assertIn('קפוארה גיל 4.5-6', check.test(offering))

    def test_asking_which_city_is_not_the_campaigns_line(self):
        # From the same run: "באיזו עיר אתם מחפשים חוג?" is a question anybody may be asked; the campaign's line opens with "מעולה".
        self.assertNotIn('phrasing_in_place', self.failed('היי 🙂 באיזו עיר אתם מחפשים חוג?', calls=(GATED,), customer_all='פרטים', customer_now='פרטים'))
        self.assertIn('phrasing_in_place', self.failed('מעולה, באיזו עיר אתם מחפשים חוג', calls=(GATED,), customer_all='פרטים', customer_now='פרטים'))

    def test_nobody_is_addressed_as_a_man_or_a_woman_by_a_guess(self):
        asked = dict(calls=(GATED,), customer_now='פרטים')
        guess = self.failed('היי, תוכל לכתוב באיזו עיר?', customer_all='היי, אפשר פרטים על החוגים?', **asked)
        self.assertIn('לשון זכר', guess['neutral_address'])
        self.assertNotIn('neutral_address', self.failed('היי, באיזו עיר אתם?', customer_all='היי, אפשר פרטים על החוגים?', **asked))
        # She said who she is: the masculine is a mistake, the feminine is not.
        mother = 'היי אני מעוניינת בחוג לבת שלי'
        self.assertIn('בלשון נקבה, והבוט פנה אליה בלשון זכר', self.failed('בשמחה, תוכל לכתוב באיזו עיר?', customer_all=mother, **asked)['neutral_address'])
        self.assertNotIn('neutral_address', self.failed('בשמחה, תוכלי לכתוב באיזו עיר?', customer_all=mother, **asked))
        # Words that only look like it: "לך", "את", a class that is "רשום" in the system's own line are not an address.
        self.assertNotIn('neutral_address', self.failed('שולחת לך את הקישור', customer_all='היי', **asked))

    def test_a_sum_that_is_also_the_asked_class_own_is_not_a_mix_up(self):
        # From the full run of 12.10: the first class cost ₪360 a month, and so did the second one twice a week.
        world = {'facts': {'main': {'name': 'היפ הופ נוער', 'price_monthly': '360'}, 'second': {'name': 'קפוארה כיתות ג-ד', 'price_monthly': '260'}}}
        check = checks.not_amount_of_other('main.price_monthly', 'second.price_monthly', 'המחיר של החוג הראשון')
        own = {'name': 'course_card', 'input': {}, 'result': {'course': {
            'name': 'קפוארה כיתות ג-ד', 'price_monthly': '260.00', 'tracks': [{'name': 'פעמיים בשבוע', 'price_monthly': '360.00'}],
        }}}
        reply = '🔹 *₪260* לחודש, פעם בשבוע\n🔹 פעמיים בשבוע *₪360* לחודש'
        self.assertEqual(check.test(self.moment(reply, calls=(own,), world=world)), '')
        # The same sum with nothing of the asked class behind it is the first class's price, carried over.
        other = {'name': 'course_card', 'input': {}, 'result': {'course': {'name': 'היפ הופ נוער', 'price_monthly': '360.00'}}}
        self.assertIn('פרט של חוג אחד נכנס לתשובה על חוג אחר', check.test(self.moment('המחיר *₪360* לחודש', calls=(other,), world=world)))

    def test_a_weekday_word_that_is_not_a_day(self):
        self.assertEqual(self.failed('לילד שני במשפחה יש הנחה, והתשלום הראשון יורד'), {})
        self.assertEqual(checks.days_in('ביום שלישי ורביעי, וגם ראשון 16:00'), ['שלישי', 'רביעי', 'ראשון'])
        self.assertEqual(checks.days_in('הילד השני והתשלום הראשון'), [])

    def test_a_name_a_phone_and_a_link_from_nowhere(self):
        failed = self.failed('השיעור עם דני בדוי במינץ - הדגמה, ויש גם בלט. טלפון 052-1234567, הרשמה ב-https://example.com/x')
        self.assertIn('דני בדוי', failed['grounded_names'])
        self.assertIn('מינץ - הדגמה', failed['grounded_names'])
        self.assertIn('בלט', failed['grounded_names'])
        self.assertIn('0521234567', failed['grounded_phones'])
        self.assertIn('https://example.com/x', failed['grounded_links'])
        # What the customer wrote himself is not an invention; the knowledge's phone and link are the knowledge's.
        ok = self.failed('אין לנו בלט. אפשר לבדוק במשרד 📞 050-9424755, הרשמה: https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/',
                         customer_all='יש בלט?', customer_now='יש בלט? כמה עולה ההרשמה?')
        self.assertEqual(ok, {})

    def test_a_detail_of_one_class_beside_another(self):
        mixed = self.failed('🔹 יום ראשון 17:00 עם אורי דוגמאי')
        self.assertIn('השעה 17:00', mixed['own_details'])
        wrong_teacher = self.failed('🔹 יום שלישי 17:00 עם נועה לדוגמה')
        self.assertIn('המדריך', wrong_teacher['own_details'])
        wrong_price = self.failed('🔹 יום שלישי 17:00 — ₪270 לחודש')
        self.assertIn('₪270', wrong_price['own_details'])
        # The end of one lesson is not the start of another: "16:00–17:00" on Sunday is Sunday's own.
        self.assertEqual(self.failed('🔹 יום ראשון 16:00–17:00 עם נועה לדוגמה — ₪270 לחודש'), {})

    def test_the_order_of_the_conversation(self):
        offered = self.failed('יש קפוארה ביום שלישי 17:00', calls=(GATED,))
        self.assertIn('קודם צריך לשאול באיזה תחום מדובר', offered['order_kept'])
        silent = self.failed('נשמח לראות אתכם', calls=(GATED,))
        self.assertIn('לא שאל', silent['order_kept'])
        self.assertEqual(self.failed('באיזה תחום מדובר?', calls=(GATED,), customer_now='יש חוגים בראש העין?'), {})
        self.assertEqual(self.failed('יש לנו שם היפ הופ וקפוארה, מה מעניין אתכם?', calls=(GATED,), customer_now='יש חוגים בראש העין?'), {})
        # A class the bot went on to look up after the question is no longer "before the question".
        self.assertNotIn('order_kept', self.failed('יום שלישי 17:00', calls=(GATED, FIND)))

    def test_everything_only_for_who_asked_and_details_only_from_this_answer(self):
        everything = {'name': 'find_courses', 'input': {'city': 'ראש העין', 'show_all': True}, 'result': FIND['result']}
        self.assertIn('show_all_only_when_pressed', self.failed('יום שלישי 17:00', calls=(everything,), customer_now='יש חוגים בראש העין?'))
        self.assertNotIn('show_all_only_when_pressed', self.failed('יום שלישי 17:00', calls=(everything,), customer_now='פשוט תגידו מה יש'))
        from_memory = self.failed('יום שלישי 17:00, ₪260', calls=(), earlier_calls=[FIND])
        self.assertEqual(set(from_memory), {'fresh_details'})

    def test_cost_is_not_generalised_and_not_volunteered(self):
        self.assertIn('no_generalisation', self.failed('בכל האפשרויות האלה שיעור הניסיון ללא עלות'))
        self.assertIn('no_generalisation', self.failed('בסניף הזה שיעור הניסיון ללא עלות'))
        volunteered = self.failed('יום שלישי 17:00, ושיעור הניסיון ללא עלות', customer_now='מתי יש קפוארה בראש העין לבן 5?')
        self.assertIn('cost_only_when_asked', volunteered)
        self.assertNotIn('cost_only_when_asked', self.failed('שיעור הניסיון בעלות ₪30', customer_now='יש שיעור ניסיון?'))

    def test_a_phrasing_out_of_its_place_and_an_answer_said_again(self):
        self.assertIn('phrasing_in_place', self.failed('סליחה על העיכוב בימים האחרונים, באיזו עיר אתם?', calls=()))
        self.assertEqual(self.failed('היה לנו קצת עומס בימים האחרונים אז סליחה על העיכוב, באיזו עיר רלוונטי לכם', calls=(), allow=('campaign_not_answered',)), {})
        again = 'באיזה תחום מדובר?\n🔹 היפ הופ לכל הגילים\n🔹 קפוארה לכל הגילים'
        repeated = self.failed('לא ניתן לשמוע הודעות קוליות\n🔹 היפ הופ לכל הגילים\n🔹 קפוארה לכל הגילים', calls=(), previous_reply=again, allow=('voice_message',))
        self.assertIn('no_repeat', repeated)
        # A phrasing of the knowledge said twice is the same line for another question, not an answer said again.
        missing = 'אין לי את הפרט הזה כרגע\nאפשר לבדוק במשרד 📞 0509424755'
        world_with = {'facts': {'phrasings': {'missing_info': 'אין לי את הפרט הזה כרגע\nאפשר לבדוק במשרד 📞 {טלפון_משרד}'}}}
        self.assertEqual(self.failed(missing, calls=(), previous_reply=missing, world=world_with), {})

    def test_the_rules_of_form(self):
        failed = self.failed('**קפוארה** ביום שלישי 17:00, המחיר 260₪\nפרטים ב-cogomelo.co.il, נתראה בשיעור.')
        self.assertIn('נקודה בסוף משפט', failed['form'])
        self.assertIn('כוכבית כפולה', failed['form'])
        self.assertIn('₪ אחרי המספר', failed['form'])
        self.assertIn('בלי https://', failed['form'])
        self.assertIn('hebrew_only', self.failed('Sure, יום שלישי 17:00'))
        self.assertIn('short', self.failed('\n'.join(f'שורה {index}' for index in range(10)), calls=()))
        self.assertIn('no_courtesy', self.failed('יום שלישי 17:00, יש עוד משהו שאוכל לעזור בו?'))

    def test_a_tool_that_hands_over_what_it_must_not(self):
        leaking = {'name': 'instructor_info', 'input': {}, 'result': {'instructors': [{'name': 'אורי דוגמאי', 'salary_per_lesson': '100'}]}}
        self.assertIn('salary_per_lesson', self.failed('אורי דוגמאי מלמד קפוארה', calls=(leaking,))['no_internal_money'])

    def test_the_specific_checks_say_what_was_expected_and_what_was_written(self):
        facts = {'facts': {'main': {'price_monthly': '250.00', 'field': 'היפ הופ', 'child': 'בן 5', 'lessons': [{'day': 'חמישי', 'start': '16:30', 'end': '17:15', 'instructor': 'נועה לדוגמה'}]},
                           'second': {'price_monthly': '280.00'}, 'stage': {'fields': ['היפ הופ', 'קפוארה']},
                           'links': {'registration': 'https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/'}}}
        m = self.moment('המחיר ₪280 לחודש', world=facts)
        self.assertEqual(checks.says_amount('main.price_monthly', 'המחיר החודשי').test(m), 'ציפינו להמחיר החודשי שבמערכת: ₪250. הבוט ענה: "המחיר ₪280 לחודש"')
        self.assertEqual(checks.says_amount('second.price_monthly', 'המחיר החודשי').test(m), '')
        self.assertIn('פרט של חוג אחד נכנס לתשובה על חוג אחר', checks.not_amount_of_other('second.price_monthly', 'main.price_monthly', 'המחיר של החוג השני').test(m))
        self.assertEqual(checks.says_word('main.lessons.0.instructor', 'שם המדריך').test(self.moment('עם נועה לדוגמה', world=facts)), '')
        self.assertEqual(checks.tells_lesson('main').test(self.moment('יום חמישי 16:30', world=facts)), '')
        self.assertIn('יום חמישי 16:30', checks.tells_lesson('main').test(self.moment('יום שני 17:00', world=facts)))
        self.assertIn('17:00', checks.only_lessons_of('main').test(self.moment('יום חמישי 16:30 וגם 17:00', world=facts)))
        self.assertEqual(checks.asks('field').test(self.moment('יש לנו היפ הופ וקפוארה', world=facts)), '')
        self.assertIn('ציפינו שהבוט ישאל באיזה תחום מדובר', checks.asks('field').test(self.moment('נשמח לעזור', world=facts)))
        self.assertEqual(checks.link('registration', 'ההרשמה').test(self.moment('https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/', world=facts)), '')
        self.assertIn('ציפינו שלא ייאמר שום סכום', checks.no_amounts().test(m))
        self.assertIn('לפני שבירר', checks.not_missing_info().test(self.moment('אין לי את הפרט הזה כרגע')))

    def test_a_check_that_breaks_is_a_failed_check_and_not_a_broken_run(self):
        broken = checks.Check('broken', 'בדיקה שבורה', lambda moment: 1 / 0)
        rows = checks.run((broken,), self.moment('שלום'), turn=2)
        self.assertEqual((rows[0]['status'], rows[0]['turn'], rows[0]['kind']), ('failed', 2, 'hard'))
        self.assertIn('ZeroDivisionError', rows[0]['explain'])


# --- the world --------------------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class WorldTests(EvalTestCase):
    def test_the_words_and_the_facts_are_read_from_the_system(self):
        built = world.build()
        words, facts = built['words'], built['facts']
        self.assertEqual(built['missing'], {})
        self.assertEqual((words['place'], words['field'], words['child']), ('ראש העין', 'היפ הופ', 'בן 5'))
        self.assertEqual((words['paid_field'], words['paid_child'], words['free_field'], words['free_child']), ('קפוארה', 'בן 5', 'היפ הופ', 'בן 5'))
        self.assertEqual((words['multi_city'], words['external_place'], words['boundary_field'], words['missing_field']), ('פתח תקווה', 'רמת גן', 'היפ הופ', 'ג׳ודו'))
        self.assertIn(words['far_city'], world.FAR_CITIES)
        self.assertEqual((facts['main']['name'], facts['main']['price_monthly'], facts['main']['trial_is_paid']), ('היפ הופ גיל 4.5-6', '250.00', False))
        self.assertEqual((facts['paid']['trial_price'], facts['paid']['registration_fee'], facts['paid']['bring']), ('30.00', '120.00', 'בגדים נוחים ובקבוק מים'))
        self.assertEqual(facts['main']['lessons'], [{'day': 'חמישי', 'start': '16:30', 'end': '17:15', 'instructor': 'נועה לדוגמה'}])
        self.assertNotEqual((facts['second']['field'], facts['second']['child']), (facts['main']['field'], facts['main']['child']))
        self.assertEqual(facts['links']['registration'], 'https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/')
        self.assertEqual(facts['stage']['fields'], ['היפ הופ', 'קפוארה'])
        # How full each lesson is, the network of the branch, and the words a hurried parent writes.
        self.assertEqual((words['busy_field'], words['busy_child'], words['busy_day']), ('קפוארה', 'בן 5', 'שלישי'))
        self.assertEqual(facts['busy']['seats'], {'enrolled': 9, 'capacity': 14, 'seats_left': 5, 'is_full': False})
        self.assertEqual((words['packed_child'], words['packed_day'], facts['packed']['seats']['is_full']), ('בכיתה ה', 'שני', True))
        self.assertEqual((words['empty_field'], words['empty_day'], facts['empty']['seats']['enrolled']), ('היפ הופ', 'חמישי', 0))
        self.assertEqual((words['two_day_day'], words['two_day_other'], facts['two_day']['seats']['enrolled']), ('רביעי', 'ראשון', 6))
        self.assertEqual((words['wifi_place'], facts['wifi']['name'], facts['wifi']['code']), ('ראש העין', fixtures.WIFI_NAME, fixtures.WIFI_CODE))
        self.assertEqual((words['no_wifi_place'], words['short_city'], words['off_day']), ('אם המושבות - הדגמה', 'פ"ת', 'שבת'))
        self.assertEqual((words['place_typo'], words['place_glued'], words['field_typo']), ('רשא העין', 'ראשהעין', 'הפי הופ'))
        self.assertEqual((words['sibling_child'], words['main_day'], words['trial_day'], words['way_place']), ('בכיתה א', 'חמישי', 'חמישי', 'ראש העין'))
        self.assertEqual({key: entry['child'] for key, entry in facts['boundary'].items()}, {'young': 'בן 5', 'old': 'בכיתה א'})
        self.assertRegex(words['birth_date'], r'^\d{2}\.\d{2}\.\d{4}$')
        json.dumps(built)                                   # plain JSON: it is kept on the run

    def test_the_facts_follow_the_system_when_it_changes(self):
        self.rows['young_hip_hop'].price = Decimal('310.00')
        self.rows['young_hip_hop'].save(update_fields=['price'])
        self.rows['young_capoeira'].trial_lesson_price = Decimal('45.00')
        self.rows['young_capoeira'].save(update_fields=['trial_lesson_price'])
        facts = world.build()['facts']
        self.assertEqual((facts['main']['price_monthly'], facts['paid']['trial_price']), ('310.00', '45.00'))

    def test_the_clocks_are_where_the_scripts_need_them(self):
        from datetime import datetime

        from apps.wahub import knowledge

        clocks = {name: datetime.fromisoformat(value) for name, value in world.build()['clocks'].items()}
        self.assertTrue(knowledge.office_hours_now(clocks['open'])['open'])
        self.assertFalse(knowledge.office_hours_now(clocks['closed'])['open'])
        self.assertEqual((clocks['weekend'].weekday(), clocks['weekend'].hour), (5, 10))            # a Saturday morning
        after, before = clocks['after_cancel_deadline'], clocks['before_cancel_deadline']
        self.assertTrue((after.month, after.day) >= (4, 2) and after.month < 9)
        self.assertTrue(before.month >= 9 or before.month < 4)

    def test_a_system_with_nothing_to_ask_about_says_what_is_missing(self):
        from apps.courses.models import Course

        Course.objects.all().update(is_active=False)
        built = world.build()
        self.assertIn('stage', built['missing'])
        self.assertNotIn('place', built['words'])
        run = runner.start('order_city_only')
        self.assertEqual(run.status, 'skipped')
        self.assertIn('אי אפשר להריץ כאן', run.detail)
        self.assertIn('אין במערכת סניף שלנו', run.detail)
        # A script that needs nothing of the catalogue still runs.
        self.assertEqual(runner.run_to_end('order_no_details').status, 'passed')

    def test_a_bot_with_no_knowledge_is_not_checked(self):
        KnowledgeItem.objects.all().delete()
        run = runner.start('order_no_details')
        self.assertEqual(run.status, 'skipped')
        self.assertIn('ידע הבוט ריק', run.detail)


# --- the runner, with the stub --------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class StubRunTests(EvalTestCase):
    # The order of the conversation is kept by the tool, so even the rule-built stub passes these.
    ORDER = (
        'order_no_details', 'order_city_only', 'order_field_no_age', 'order_all_given', 'order_step_by_step', 'order_age_first',
        'order_place_and_age_no_field', 'order_presses_to_see_all', 'order_age_on_the_line', 'order_city_with_several_branches',
    )

    def test_every_script_runs_to_a_verdict_and_writes_nothing_but_its_run(self):
        before = [model.objects.count() for model in (Contact, Message, ShadowReply, ContactEvent)]
        written = set()

        def watching(execute, sql, params, many, context):
            verb = sql.lstrip().split(None, 1)[0].upper()
            if verb in ('INSERT', 'UPDATE', 'DELETE'):
                written.add(re.search(r'"(\w+)"', sql).group(1))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(watching):
            runs = {scenario.id: runner.run_to_end(scenario.id) for scenario in scenarios.SCENARIOS}
        self.assertEqual(written, {'wahub_bot_eval_runs'})
        self.assertEqual([model.objects.count() for model in (Contact, Message, ShadowReply, ContactEvent)], before)
        self.assertEqual(self.network.call_count, 0)
        self.deliver.assert_not_called()
        self.handoff.assert_not_called()
        self.assertEqual({run.status for run in runs.values()}, {'passed', 'failed'})        # the invented catalogue fills every script
        for scenario in scenarios.SCENARIOS:
            run = runs[scenario.id]
            with self.subTest(scenario=scenario.id):
                self.assertEqual((run.engine, len(run.turns)), ('stub', len(scenario.turns)))
                self.assertTrue(all(turn['reply'] for turn in run.turns))
                self.assertFalse(any('הבדיקה עצמה נכשלה' in row['explain'] for row in run.checks), [row for row in run.checks if 'הבדיקה עצמה' in row['explain']])
                self.assertTrue(all(row['status'] == 'not_judged' for row in run.checks if row['kind'] == 'rubric'))
                json.dumps(runner.run_payload(run))
        for name in self.ORDER:
            with self.subTest(order=name):
                self.assertEqual(runs[name].status, 'passed', runner.run_payload(runs[name])['failures'])

    def test_a_conversation_is_played_turn_by_turn_with_what_was_said_before(self):
        run = runner.run_to_end('order_step_by_step')
        asked = [turn['tools'][0]['input'] if turn['tools'] else None for turn in run.turns]
        self.assertIsNone(asked[0])                                              # no city yet: nothing to look up
        self.assertEqual((asked[1]['city'], asked[1]['course_type']), ('ראש העין', ''))
        self.assertEqual(asked[2]['course_type'], 'היפ הופ')
        self.assertEqual((asked[3]['course_type'], asked[3]['age']), ('היפ הופ', 5.0))
        self.assertEqual([turn['tools'][0]['result']['need'] for turn in run.turns[1:]], ['course_type', 'age', ''])
        self.assertIn('יום חמישי 16:30', run.turns[3]['reply'])
        self.assertEqual(run.turns[3]['customer'], [{'kind': 'text', 'text': 'הילד בן 5'}])
        # The run keeps what the tool returned; what the screen gets is the line about it.
        payload = runner.run_payload(run)
        self.assertEqual(set(payload['conversation'][3]['tools'][0]), {'name', 'input', 'summary'})
        self.assertEqual(payload['conversation'][1]['tools'][0]['summary'], 'קודם שואלים תחום; חוגים לא הוחזרו (היפ הופ, קפוארה)')

    def test_an_earlier_conversation_is_background_for_the_stub_too(self):
        run = runner.run_to_end('context_new_opening_after_hours')
        self.assertEqual((run.status, run.turns[0]['reply'], run.turns[0]['tools']), ('passed', 'באיזו עיר אתם?', []))

    def test_the_pretend_clock_of_a_script(self):
        closed = runner.run_to_end('handoff_asks_for_a_person_when_closed')
        self.assertEqual(closed.status, 'passed', runner.run_payload(closed)['failures'])
        self.assertTrue(closed.turns[0]['request_human'])
        after = runner.run_to_end('topic_cancel_after_deadline')
        self.assertEqual(after.status, 'passed', runner.run_payload(after)['failures'])
        self.assertNotIn('studio-cancellation-form', after.turns[0]['reply'])
        before = runner.run_to_end('topic_cancel_before_deadline')
        self.assertIn('https://studio-cancellation-form.vercel.app/', before.turns[0]['reply'])

    def test_a_failure_is_explained_in_plain_words_beside_the_answer(self):
        run = runner.run_to_end('money_paid_trial_then_free_trial')                # the stub keeps answering about the first child
        payload = runner.run_payload(run)
        self.assertEqual((payload['status'], payload['status_label'], payload['done']), ('failed', 'נכשל', True))
        mixed = next(row for row in payload['failures'] if row['id'].startswith('not_amount_of_other'))
        self.assertEqual(mixed['turn'], 2)
        self.assertIn('פרט של חוג אחד נכנס לתשובה על חוג אחר', mixed['explain'])
        self.assertEqual(mixed['bot'], run.turns[1]['reply'])
        self.assertEqual(mixed['customer'][0]['text'], 'ויש לי גם ילד בן 5. שיעור ניסיון בהיפ הופ עולה כסף?')
        self.assertEqual(payload['headline'], payload['failures'][0]['explain'])
        self.assertEqual(payload['counts']['failed'], len(payload['failures']))
        self.assertTrue(payload['engine_is_stub'])

    def test_a_run_too_long_for_one_request_continues_in_the_next(self):
        run = runner.start('order_step_by_step')
        with patch.object(runner, 'MIN_TURN_SECONDS', 10_000):
            run = runner.advance(run, deadline=time.monotonic() + 30)
            self.assertEqual((run.status, len(run.turns)), ('running', 1))
            self.assertFalse(runner.run_payload(run)['done'])
            again = BotEvalRun.objects.get(pk=run.pk)                               # another request, the row alone
            again = runner.advance(again, deadline=time.monotonic() + 30)
            self.assertEqual((again.status, len(again.turns)), ('running', 2))
        done = runner.advance(BotEvalRun.objects.get(pk=run.pk), deadline=time.monotonic() + 30)
        self.assertEqual((done.status, len(done.turns)), ('passed', 4))
        self.assertIsNotNone(done.finished_at)
        self.assertEqual(runner.advance(done).status, 'passed')                    # a finished run is not played again
        self.assertEqual(BotEvalRun.objects.count(), 1)


# --- the runner, with Claude behind a mock ----------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL, REGISTRATION_FEE_ILS=120)
class ClaudeRunTests(EvalTestCase):
    HERE = {'city': 'ראש העין'}

    def play(self, scenario_id, *steps):
        claude = FakeClaude(*steps)
        with patch(POST, side_effect=claude):
            run = runner.run_to_end(scenario_id)
        return run, claude

    def test_a_bot_that_keeps_the_order_passes_and_the_judge_is_asked(self):
        run, claude = self.play(
            'order_step_by_step',
            ('tool', 'find_courses', {}), ('say', 'באיזו עיר אתם?'),
            ('tool', 'find_courses', self.HERE), ('say', 'באיזה תחום מדובר?\n🔹 היפ הופ\n🔹 קפוארה'),
            ('tool', 'find_courses', {**self.HERE, 'course_type': 'היפ הופ'}), ('say', 'בן כמה הילד, או באיזו כיתה?'),
            ('tool', 'find_courses', {**self.HERE, 'course_type': 'היפ הופ', 'age': 5}),
            ('say', lambda found: f'ל *היפ הופ* בגיל הזה:\n🔹 יום {found["courses"][0]["lessons"][0]["day"]} 16:30–17:15 עם נועה לדוגמה'),
            ('json', {'verdicts': [{'number': 1, 'holds': True, 'why': 'הוצג רק היפ הופ לגיל 5'}]}),
        )
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])
        self.assertEqual(run.engine, MODEL)
        rubric = [row for row in run.checks if row['kind'] == 'rubric']
        self.assertEqual([(row['turn'], row['status'], row['explain']) for row in rubric], [(4, 'passed', 'הוצג רק היפ הופ לגיל 5')])
        # The bot was given the turns before as its own side of the conversation, and nothing of any real conversation.
        last_turn = claude.requests[6]['messages'][0]['content']
        self.assertIn('את (הבוט החדש): בן כמה הילד, או באיזו כיתה?', last_turn)
        self.assertIn('לקוח: היפ הופ', last_turn)
        self.assertIn('- הילד בן 5', last_turn)
        # The judge read the conversation as it was played, and the sentence it was asked about.
        asked = claude.requests[-1]
        self.assertEqual((asked['model'], asked['output_config']['effort'], asked['output_config']['format']['type']), (MODEL, 'low', 'json_schema'))
        told = asked['messages'][0]['content']
        self.assertIn('הבוט: באיזו עיר אתם?', told)
        self.assertIn('1. (על התשובה בתור 4) בסוף השיחה הבוט הציג רק את החוג שמתאים', told)
        self.assertIn('(הבוט בדק במערכת — find_courses: קודם שואלים תחום; חוגים לא הוחזרו (היפ הופ, קפוארה))', told)

    def test_a_bot_that_offers_before_it_asks_fails_and_is_told_why(self):
        run, _claude = self.play(
            'order_city_only', ('tool', 'find_courses', self.HERE), ('say', 'יש לנו קפוארה ביום שלישי ב-17:00, ₪260 לחודש'),
            ('json', {'verdicts': [{'number': 1, 'holds': False, 'why': 'הבוט הציע חוג עם יום ושעה במקום לשאול על התחום'}]}),
        )
        self.assertEqual(run.status, 'failed')
        failed = self.failed(run)
        self.assertLessEqual({'order_kept', 'asks_field', 'offers_nothing', 'grounded_amounts', 'grounded_times', 'grounded_days', 'rubric_1_1'}, failed)
        self.assertIn('קודם מבררים, אחר כך מציעים', self.explain(run, 'order_kept'))
        self.assertIn('₪260', self.explain(run, 'grounded_amounts'))
        self.assertEqual(self.explain(run, 'rubric_1_1'), 'הבוט הציע חוג עם יום ושעה במקום לשאול על התחום')

    def test_a_bot_that_asks_for_everything_unasked(self):
        run, _claude = self.play(
            'order_city_only', ('tool', 'find_courses', {**self.HERE, 'show_all': True}), ('say', 'באיזה תחום מדובר?'),
            ('json', {'verdicts': [{'number': 1, 'holds': True, 'why': 'שאל על התחום'}]}),
        )
        self.assertEqual(self.failed(run), {'show_all_only_when_pressed'})
        self.assertIn('בלי שהלקוח ביקש לראות הכול', self.explain(run, 'show_all_only_when_pressed'))

    def test_the_price_expected_is_the_one_in_the_system_now(self):
        ask = {**self.HERE, 'course_type': 'היפ הופ', 'age': 5}
        price = lambda found: f'המחיר ₪{found["courses"][0]["price_monthly"].split(".")[0]} לחודש'       # noqa: E731
        run, _claude = self.play('money_price_of_a_class', ('tool', 'find_courses', ask), ('say', price))
        self.assertEqual((run.status, run.turns[0]['reply']), ('passed', 'המחיר *₪250* לחודש'))
        course = self.rows['young_hip_hop']
        course.price = Decimal('310.00')
        course.save(update_fields=['price'])
        run, _claude = self.play('money_price_of_a_class', ('tool', 'find_courses', ask), ('say', price))
        self.assertEqual((run.status, run.turns[0]['reply']), ('passed', 'המחיר *₪310* לחודש'))
        # A bot that says yesterday's price fails on both counts: not what the system holds, and not what any tool returned.
        run, _claude = self.play('money_price_of_a_class', ('tool', 'find_courses', ask), ('say', 'המחיר ₪250 לחודש'))
        self.assertEqual(self.failed(run), {'grounded_amounts', 'says_amount:main.price_monthly'})
        self.assertIn('₪310', self.explain(run, 'says_amount:main.price_monthly'))

    def test_one_classs_price_in_the_answer_about_another(self):
        first = {**self.HERE, 'course_type': 'היפ הופ', 'age': 5}
        second = {**self.HERE, 'course_type': 'היפ הופ', 'grade': 'כיתה א'}
        run, _claude = self.play(
            'money_two_classes_in_one_conversation',
            ('tool', 'find_courses', first), ('say', 'המחיר ₪250 לחודש'),
            ('tool', 'find_courses', second), ('say', 'גם שם המחיר ₪250 לחודש, ביום שני 17:00–18:00'),
        )
        # Caught twice over: by what this script expects, and by the check that runs on every answer — ₪250 beside Monday 17:00
        # is the price of another class.
        self.assertEqual(self.failed(run), {'says_amount:second.price_monthly', 'not_amount_of_other:main.price_monthly', 'own_details'})
        self.assertIn('שייך לחוג אחר', self.explain(run, 'own_details'))
        run, _claude = self.play(
            'money_two_classes_in_one_conversation',
            ('tool', 'find_courses', first), ('say', 'המחיר ₪250 לחודש'),
            ('tool', 'find_courses', second), ('say', 'לכיתה א המחיר ₪280 לחודש, ביום שני 17:00–18:00'),
        )
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])

    def test_a_tag_and_a_handoff_are_marks_the_checks_can_read(self):
        run, _claude = self.play(
            'topic_birthday_party', ('tool', 'tag_conversation', {'tag': 'הפעלת יום הולדת', 'details': 'שאלו על מחיר'}),
            ('say', 'לבירורים לגבי *ימי הולדת* אפשר ליצור קשר עם *גלעד*, *מנהל האירועים הפרטיים* שלנו\n📞 *050-722-3419*'),
        )
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])
        self.assertEqual(run.turns[0]['tags'], [{'tag': 'הפעלת יום הולדת', 'details': 'שאלו על מחיר'}])
        run, _claude = self.play(
            'handoff_charged_twice', ('tool', 'request_human', {'reason': 'חיוב כפול'}), ('say', 'מצטערת לשמוע, נציג מהמשרד יחזור אליך בהקדם'),
        )
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])
        self.assertEqual(runner.run_payload(run)['conversation'][0]['request_human_reason'], 'חיוב כפול')

    def test_claude_that_does_not_answer_is_not_a_pass_and_not_a_fail(self):
        run = runner.start('order_no_details')
        with patch(POST, return_value=_response({'error': {'message': 'overloaded'}}, status_code=529)), self.assertLogs('apps.wahub.shadow', level='WARNING'):
            run = runner.advance(run)
            self.assertEqual((run.status, run.attempts, run.turns), ('running', 1, []))     # tried again by the next request
            run = runner.advance(run)
            run = runner.advance(run)
        self.assertEqual((run.status, run.attempts), ('error', runner.MAX_ATTEMPTS))
        self.assertIn('הבוט לא ענה בתור 1', run.detail)
        payload = runner.run_payload(run)
        self.assertEqual((payload['status_label'], payload['done'], payload['headline']), ('לא רץ', True, run.detail))
        self.assertEqual(run.checks, [])

    def test_a_judge_that_fails_leaves_the_sentences_unjudged(self):
        steps = (('tool', 'find_courses', self.HERE), ('say', 'באיזה תחום מדובר?\n🔹 היפ הופ\n🔹 קפוארה'))
        with patch(POST, side_effect=[FakeClaude(*steps)(None, json={'messages': []}), FakeClaude(steps[1])(None, json={'messages': [{'content': ''}]}),
                                      _response({}, status_code=500)]), self.assertLogs('apps.wahub.evals.judge', level='WARNING'):
            run = runner.run_to_end('order_city_only')
        self.assertEqual(run.status, 'passed')
        rubric = [row for row in run.checks if row['kind'] == 'rubric']
        self.assertEqual([(row['status'], row['explain']) for row in rubric], [('not_judged', 'לא נשפט: Claude ענה 500')])
        self.assertEqual(runner.run_payload(run)['counts']['not_judged'], 1)


class JudgeTests(WahubTestCase):
    TURNS = [{'turn': 1, 'customer': [{'kind': 'voice', 'text': ''}], 'tools': [], 'reply': 'אפשר לכתוב לי בבקשה?', 'request_human': False}]
    SENTENCES = [{'turn': 1, 'text': 'הבוט ביקש לכתוב'}, {'turn': 1, 'text': 'הבוט לא חזר על תשובה קודמת'}]

    @override_settings(ANTHROPIC_API_KEY='')
    def test_without_a_key_nothing_is_judged_and_nothing_is_called(self):
        verdicts = judge.judge(self.TURNS, self.SENTENCES)
        self.assertEqual(verdicts, [{'status': 'not_judged', 'explain': judge.NO_KEY}] * 2)
        self.assertFalse(judge.configured())

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_each_sentence_gets_its_verdict_by_its_number(self):
        answer = {'verdicts': [{'number': 2, 'holds': False, 'why': 'חזר על הרשימה'}, {'number': 1, 'holds': True, 'why': 'ביקש לכתוב'}]}
        payload = {'model': MODEL, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}
        with patch('apps.wahub.evals.judge.requests.post', return_value=_response(payload)) as post:
            verdicts = judge.judge(self.TURNS, self.SENTENCES)
        self.assertEqual(verdicts, [{'status': 'passed', 'explain': 'ביקש לכתוב'}, {'status': 'failed', 'explain': 'חזר על הרשימה'}])
        body = post.call_args.kwargs['json']
        self.assertIn('לקוח: [הקלטה שלא תומללה]', body['messages'][0]['content'])
        self.assertEqual(body['fallbacks'], 'default')
        self.assertEqual(post.call_args.kwargs['headers']['anthropic-beta'], judge.FALLBACK_BETA)

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_a_refusal_a_missing_verdict_and_no_time_left(self):
        with patch('apps.wahub.evals.judge.requests.post', return_value=_response({'stop_reason': 'refusal', 'content': []})):
            self.assertEqual({row['status'] for row in judge.judge(self.TURNS, self.SENTENCES)}, {'not_judged'})
        partial = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps({'verdicts': [{'number': 1, 'holds': True, 'why': 'כן'}]})}]}
        with patch('apps.wahub.evals.judge.requests.post', return_value=_response(partial)):
            self.assertEqual([row['status'] for row in judge.judge(self.TURNS, self.SENTENCES)], ['passed', 'not_judged'])
        with patch('apps.wahub.evals.judge.requests.post', side_effect=AssertionError('no time: must not be called')):
            late = judge.judge(self.TURNS, self.SENTENCES, deadline=time.monotonic() + 1)
        self.assertEqual({row['explain'] for row in late}, {'לא נשפט: לא נשאר זמן בבקשה הזאת'})


# --- the endpoints ---------------------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class ApiTests(EvalTestCase):
    def test_the_list_before_anything_ran(self):
        data = self.get('bot/evals/').data
        self.assertEqual(data['engine'], {'configured': False, 'model': 'stub', 'judge': False, 'label': 'בוט מדומה (אין מפתח של Claude במערכת הזאת)'})
        total = len(scenarios.SCENARIOS)
        self.assertEqual(data['summary'], {'total': total, 'passed': 0, 'failed': 0, 'skipped': 0, 'not_run': 0, 'never_run': total, 'last_run_at': None})
        self.assertEqual([group['id'] for group in data['groups']], [group for group, _title in scenarios.GROUPS])
        self.assertEqual(sum(group['total'] for group in data['groups']), total)
        first = data['scenarios'][0]
        self.assertEqual(set(first), {'id', 'title', 'group', 'group_title', 'what', 'rules', 'turns', 'last', 'in_progress'})
        self.assertEqual((first['id'], first['group_title'], first['last'], first['in_progress']), ('order_no_details', 'סדר השיחה', None, None))
        self.assertEqual(first['rules'][0], {'id': 'P1', 'title': rules.RULES['P1']})

    def test_one_conversation_is_played_per_request_and_the_list_shows_its_result(self):
        response = self.post('bot/evals/run/', {'scenario_id': 'order_city_only'})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(set(data), {
            'run_id', 'scenario', 'status', 'status_label', 'done', 'turns_played', 'turns_total', 'engine', 'engine_is_stub',
            'started_at', 'finished_at', 'took_ms', 'detail', 'headline', 'counts', 'conversation', 'checks', 'failures',
        })
        self.assertEqual((data['status'], data['status_label'], data['done'], data['turns_played'], data['turns_total']), ('passed', 'עבר', True, 1, 1))
        self.assertEqual(data['scenario']['id'], 'order_city_only')
        self.assertEqual(data['conversation'][0]['customer'], [{'kind': 'text', 'text': 'יש לכם חוגים בראש העין?'}])
        self.assertEqual(data['conversation'][0]['bot'], 'באיזה תחום מדובר?\n🔹 היפ הופ\n🔹 קפוארה')
        self.assertEqual(set(data['checks'][0]), {'id', 'turn', 'kind', 'title', 'status', 'status_label', 'explain'})
        self.assertEqual({row['status_label'] for row in data['checks'] if row['kind'] == 'rubric'}, {'לא נשפט'})
        self.assertEqual(data['failures'], [])
        self.assertEqual(BotEvalRun.objects.get().created_by, self.manager)

        self.post('bot/evals/run/', {'scenario_id': 'money_paid_trial_then_free_trial'})
        listed = self.get('bot/evals/').data
        self.assertEqual((listed['summary']['passed'], listed['summary']['failed'], listed['summary']['never_run']), (1, 1, len(scenarios.SCENARIOS) - 2))
        self.assertIsNotNone(listed['summary']['last_run_at'])
        by_id = {row['id']: row for row in listed['scenarios']}
        last = by_id['order_city_only']['last']
        self.assertEqual(set(last), {
            'run_id', 'status', 'status_label', 'finished_at', 'engine', 'engine_is_stub', 'took_ms', 'passed_checks', 'failed_checks',
            'not_judged', 'headline',
        })
        self.assertEqual((last['run_id'], last['status'], last['failed_checks'], last['headline']), (data['run_id'], 'passed', 0, ''))
        failed = by_id['money_paid_trial_then_free_trial']['last']
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['headline'])
        self.assertEqual({group['id']: (group['passed'], group['failed']) for group in listed['groups']}['money'], (0, 1))
        # The last run is the newest finished one.
        again = self.post('bot/evals/run/', {'scenario_id': 'order_city_only'}).data
        self.assertEqual({row['id']: row for row in self.get('bot/evals/').data['scenarios']}['order_city_only']['last']['run_id'], again['run_id'])

    def test_a_long_conversation_is_continued_by_its_run_id(self):
        with patch.object(runner, 'MIN_TURN_SECONDS', 10_000):
            first = self.post('bot/evals/run/', {'scenario_id': 'order_step_by_step'}).data
            self.assertEqual((first['status'], first['done'], first['turns_played'], first['turns_total']), ('running', False, 1, 4))
            listed = {row['id']: row for row in self.get('bot/evals/').data['scenarios']}['order_step_by_step']
            self.assertEqual((listed['last'], listed['in_progress']), (None, {'run_id': first['run_id'], 'turns_played': 1}))
            second = self.post('bot/evals/run/', {'scenario_id': 'order_step_by_step', 'run_id': first['run_id']}).data
            self.assertEqual((second['run_id'], second['turns_played']), (first['run_id'], 2))
        while not second['done']:
            second = self.post('bot/evals/run/', {'run_id': first['run_id']}).data
        self.assertEqual((second['status'], second['turns_played']), ('passed', 4))
        self.assertEqual(BotEvalRun.objects.count(), 1)
        kept = self.get(f'bot/evals/runs/{first["run_id"]}/')
        self.assertEqual((kept.status_code, kept.data['status'], len(kept.data['conversation'])), (200, 'passed', 4))

    def test_what_is_refused(self):
        self.assertEqual(self.post('bot/evals/run/', {}).status_code, 400)
        self.assertEqual(self.post('bot/evals/run/', {'scenario_id': 'no_such_script'}).data['detail'], 'אין תרחיש כזה.')
        self.assertEqual(self.post('bot/evals/run/', {'scenario_id': 'order_no_details', 'run_id': 999999}).data['detail'], 'הריצה לא נמצאה.')
        run_id = self.post('bot/evals/run/', {'scenario_id': 'order_no_details'}).data['run_id']
        other = self.post('bot/evals/run/', {'scenario_id': 'order_city_only', 'run_id': run_id})
        self.assertEqual((other.status_code, other.data['detail']), (400, 'הריצה הזאת שייכת לתרחיש אחר.'))
        self.assertEqual(self.get('bot/evals/runs/999999/').status_code, 404)
        self.assertEqual(BotEvalRun.objects.count(), 1)

    def test_managers_only(self):
        run_id = self.post('bot/evals/run/', {'scenario_id': 'order_no_details'}).data['run_id']
        partner = client_for(make_user('partner@wahub.test', UserProfile.ROLE_PARTNER))
        instructor = client_for(make_user('worker@wahub.test', UserProfile.ROLE_WORKER))
        calls = (('get', 'bot/evals/', None), ('post', 'bot/evals/run/', {'scenario_id': 'order_no_details'}), ('get', f'bot/evals/runs/{run_id}/', None))
        for name, client, allowed in (('partner', partner, (403,)), ('instructor', instructor, (403,)), ('stranger', APIClient(), (401, 403))):
            for method, path, data in calls:
                with self.subTest(who=name, call=path):
                    response = getattr(client, method)(f'{BASE}/{path}', data, format='json') if data is not None else getattr(client, method)(f'{BASE}/{path}')
                    self.assertIn(response.status_code, allowed)
        self.assertEqual(BotEvalRun.objects.count(), 1)


# --- the command ---------------------------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='', REGISTRATION_FEE_ILS=120)
class CommandTests(WahubTestCase):
    def test_on_an_invented_catalogue_nothing_is_left_behind(self):
        from apps.core.models import Branch

        out = StringIO()
        call_command('wahub_eval', '--fixtures', '--only', 'order_city_only,order_no_details', '--show', stdout=out)
        text = out.getvalue()
        self.assertIn('מי עונה: בוט מדומה', text)
        self.assertIn('עבר  order_city_only', text)
        self.assertIn('בוט:  באיזה תחום מדובר?', text)
        self.assertIn('סה"כ 2: עברו 2, נכשלו 0, דולגו 0, לא רצו 0.', text)
        self.assertIn('מסד הנתונים נשאר כפי שהיה', text)
        self.assertEqual((Branch.objects.count(), KnowledgeItem.objects.count(), BotEvalRun.objects.count()), (0, 0, 0))

    def test_a_failure_is_printed_and_whose_failure_it_is(self):
        out = StringIO()
        call_command('wahub_eval', '--fixtures', '--only', 'money_paid_trial_then_free_trial', stdout=out)
        self.assertIn('✗ תור 2', out.getvalue())
        self.assertIn('ענה הבוט המדומה', out.getvalue())                   # the stub's failure is not the bot's: the command ends well
        with self.assertRaises(CommandError) as raised:
            call_command('wahub_eval', '--fixtures', '--strict', '--only', 'money_paid_trial_then_free_trial', stdout=StringIO())
        self.assertEqual(str(raised.exception), '1 תרחישים נכשלו.')
        self.assertEqual(BotEvalRun.objects.count(), 0)

    @override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL)
    def test_with_the_real_bot_a_failed_script_fails_the_command(self):
        fixtures.build()
        claude = FakeClaude(('tool', 'find_courses', {'city': 'ראש העין'}), ('say', 'יש לנו קפוארה ביום שלישי ב-17:00'))
        with patch(POST, side_effect=claude), self.assertRaises(CommandError):
            call_command('wahub_eval', '--only', 'order_city_only', '--no-judge', stdout=StringIO())
        self.assertEqual(BotEvalRun.objects.get().status, 'failed')

    def test_listing_a_group_and_refusing_a_name_that_does_not_exist(self):
        out = StringIO()
        call_command('wahub_eval', '--list', '--group', 'limits', stdout=out)
        self.assertEqual(len(out.getvalue().strip().splitlines()), len([scenario for scenario in scenarios.SCENARIOS if scenario.group == 'limits']))
        with self.assertRaises(CommandError):
            call_command('wahub_eval', '--only', 'nope')
        with self.assertRaises(CommandError):
            call_command('wahub_eval', '--group', 'nope')

    def test_without_fixtures_the_runs_are_kept(self):
        fixtures.build()
        call_command('wahub_eval', '--only', 'order_city_only', '--no-judge', stdout=StringIO())
        run = BotEvalRun.objects.get()
        self.assertEqual((run.scenario_id, run.status), ('order_city_only', 'passed'))
        self.assertEqual({row['explain'] for row in run.checks if row['kind'] == 'rubric'}, {'לא נשפט: הריצה הזאת בלי שיפוט'})
