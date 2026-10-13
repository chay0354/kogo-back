"""
The money guard (shadow.py; the owner, 14.10.2026): no answer leaves with a sum
of money the system did not return. In a run of 161 test conversations against
the real bot it wrote ₪360 — a price it worked out by itself — and the rule in
its instructions had not stopped it.

Held here: what counts as a sum with a source and what does not (read by the
one function the self-checks read it with), every branch of what the guard does
— nothing, one more wording, the office's line and a mark for a person — on
every path an answer leaves by (the live answer, "נסה שאלה", the self-checks,
the stub), and that the guard breaking is never "send it anyway".

And what the same run showed about the checks themselves: the judge is given
what the bot read, a line of the system's names is not a sentence of Hebrew, a
customer who asked four questions is owed more than three lines, and a parent
at a locked door is not asked how old the child is.
"""
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.wahub import grounding, knowledge_import, live_test, shadow, unknowns
from apps.wahub.evals import checks, judge, runner, scenarios
from apps.wahub.evals.checks import Moment
from apps.wahub.models import Contact, KnowledgeItem, ShadowReply
from apps.wahub.tests.test_bot_sharper import MAIN, PLACE, THE_BUSY, THE_MAIN, find, lesson, person, price, say
from apps.wahub.tests.test_evals import MODEL, POST, EvalTestCase, FakeClaude
from apps.wahub.tests.test_live_test import OK, OWNER_SUBSCRIBER, REQUEST, LiveTestCase, sends
from apps.wahub.tests.test_shadow import catalogue

MISSING = unknowns.DEFAULT_MISSING
CARD = {'name': 'course_card', 'input': {}, 'result': {'course': {
    'name': 'קפוארה גיל 4.5-6', 'price_monthly': '235.00', 'registration_fee': '120.00',
    'trial': {'is_paid': True, 'price': '30.00', 'credit_rule': '₪30 יורדים מהתשלום הראשון'},
}}}
CEREMONY = 'בסוף השנה יש טקס 🎉 העלות היא *₪60*, עבור חגורה, תעודה ומתנה קטנה'


# --- what a sum with a source is --------------------------------------------------------------------------------

class SourcesTests(SimpleTestCase):
    def found(self, reply, *, evidence=(CARD,), knowledge=CEREMONY, customer='') -> tuple:
        invented, affirmed = grounding.unsourced_amounts(reply, evidence=list(evidence), knowledge=knowledge, customer=customer)
        return grounding.shown(invented), grounding.shown(affirmed)

    def test_what_the_tools_returned_and_what_the_knowledge_holds(self):
        for reply in (
            'המחיר *₪235* לחודש',                                       # bold, as `polish` leaves every sum
            'המחיר ₪235 לחודש, כלומר ₪235 בכל חודש',                     # the same sum twice
            'המחיר 235 ש"ח לחודש ודמי הרישום 120 שקלים',                 # written without the sign
            'שיעור הניסיון בעלות ₪30',                                   # the trial's own price, from the tool
            '₪30 יורדים מהתשלום הראשון',                                 # a sum inside a sentence a tool returned
            'הטקס בסוף השנה עולה ₪60',                                   # the knowledge's own sum: no tool returns it
            'ב-17:00, ₪235',                                             # the minutes of an hour are not money
            'באיזו עיר אתם?',                                            # no sum at all
        ):
            self.assertEqual(self.found(reply), ('', ''), reply)

    def test_a_sum_of_the_system_however_it_is_written_there(self):
        # The owner writes the knowledge himself, and a tool's sentence is a person's too: "80 ש"ח" is a sum as "₪80" is.
        self.assertEqual(self.found('התלבושת עולה ₪80', knowledge='עלות התלבושת 80 ש״ח, פעם אחת'), ('', ''))
        self.assertEqual(self.found('התלבושת עולה 80 שקלים', knowledge='עלות התלבושת 80₪'), ('', ''))
        said = {'name': 'course_card', 'input': {}, 'result': {'course': {'discounts_available': [{'value': '10 שקלים הנחה בחודש'}]}}}
        self.assertEqual(self.found('לילד שני יש ₪10 הנחה בחודש', evidence=(said,)), ('', ''))
        # A number that is not a sum there is not a sum here: the 80 of an address, of an age, of an hour.
        self.assertEqual(self.found('התלבושת עולה ₪80', knowledge='הרצל 80, עד גיל 80, ב-8:00'), ('₪80', ''))
        # A negative line of a quote (a trial that was paid, coming off) is the sum it is.
        quote = {'name': 'quote_registration', 'input': {}, 'result': {'first_charge': {'amount': '350.00', 'breakdown': [{'label': 'קיזוז', 'amount': '-30.00'}]}}}
        self.assertEqual(self.found('מהתשלום הראשון יורדים ₪30, ונשאר ₪350', evidence=(quote,)), ('', ''))

    def test_thousands_and_agorot_read_as_the_number_they_are(self):
        dear = {'name': 'course_card', 'input': {}, 'result': {'course': {'price_monthly': '1250.50'}}}
        self.assertEqual(self.found('המחיר *₪1,250.50* לחודש', evidence=(dear,)), ('', ''))
        self.assertEqual(self.found('המחיר ₪1250.5 לחודש', evidence=(dear,)), ('', ''))
        # Rounded by the bot, it is a sum nobody returned.
        self.assertEqual(self.found('המחיר ₪1,251 לחודש', evidence=(dear,)), ('₪1251', ''))

    def test_a_sum_the_bot_worked_out(self):
        # The one of the run: the monthly price and the registration fee, added up.
        self.assertEqual(self.found('המחיר ₪235 לחודש ודמי רישום ₪120, סך הכול ₪355'), ('₪355', ''))
        self.assertEqual(self.found('לשנה זה יוצא בערך ₪2,820'), ('₪2820', ''))
        self.assertEqual(self.found('לשני ילדים 470 שקלים'), ('₪470', ''))
        # A sum only an example of the knowledge holds is nobody's: the examples are not read (a "wrong" one is what must not be said).
        self.assertEqual(self.found('₪99 לחודש', knowledge=CEREMONY), ('₪99', ''))

    def test_the_customers_own_sum_said_back_and_confirmed(self):
        wrote = 'בחוג השני לוקחים 180'
        # Said back to him — a question about it, a denial of it, a line that only names it.
        for reply in ('₪180 זה של החוג השני\nאצלנו המחיר *₪235* לחודש', 'אצלנו זה לא ₪180', 'התכוונתם ל-₪180 בחוג אחר?'):
            self.assertEqual(self.found(reply, customer=wrote), ('', ''), reply)
        # Confirmed, it is not his any more.
        self.assertEqual(self.found('נכון, ₪180 לחודש', customer=wrote), ('', '₪180'))
        self.assertEqual(self.found('המחיר ₪180', customer=wrote), ('', '₪180'))
        # And a sum he never wrote is not his at all.
        self.assertEqual(self.found('נכון, ₪190 לחודש', customer=wrote), ('₪190', ''))

    def test_the_self_checks_ask_the_same_function(self):
        moment = Moment(reply='סך הכול ₪355', calls=[CARD], knowledge_text=CEREMONY, customer_all='כמה יוצא?')
        self.assertIn('₪355', checks._grounded_amounts(moment))
        with patch('apps.wahub.evals.checks.unsourced_amounts', return_value=([], [])) as asked:
            self.assertEqual(checks._grounded_amounts(moment), '')
        asked.assert_called_once_with('סך הכול ₪355', evidence=[CARD], knowledge=CEREMONY, customer='כמה יוצא?')
        # One reading of money for both: the checks hold no copy of their own.
        for name in ('amounts_in', 'money_evidence', '_only_the_customers', 'affirms', '_numbers_written', 'MONEY_KEYS'):
            self.assertIs(getattr(checks, name), getattr(grounding, name), name)

    def test_what_reads_as_money_at_all(self):
        for text in ('₪5', '5 ש"ח', 'חמישה שקלים', 'שקל אחד'):
            self.assertTrue(grounding.mentions_money(text), text)
        for text in ('', 'יום שלישי 17:00', 'בן 5', None):
            self.assertFalse(grounding.mentions_money(text), text)


# --- the guard, with Claude behind a mock -------------------------------------------------------------------------

def then(claude, error):
    """The script of `claude`, and after its last step — this failure."""
    def post(url, headers=None, json=None, timeout=None):
        if not claude.steps:
            claude.requests.append(json)
            raise error
        return claude(url, headers=headers, json=json, timeout=timeout)
    return post


TOTAL = lambda found: f'{price(found)} ודמי רישום ₪{found["courses"][0]["registration_fee"].split(".")[0]}, סך הכול ₪370'        # noqa: E731
APART = lambda found: f'{price(found)}\nדמי הרישום ₪{found["courses"][0]["registration_fee"].split(".")[0]}, פעם אחת'            # noqa: E731
# The same two sums as a second wording: after the guard's line there is no tool's answer for a script to read them from.
AGAIN = 'המחיר ₪250 לחודש\nדמי הרישום ₪120, פעם אחת'


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL, REGISTRATION_FEE_ILS=120)
class GuardTests(EvalTestCase):
    """The catalogue of the self-checks: היפ הופ for a child of 5 in ראש העין costs ₪250 a month, and ₪120 to register."""

    def ctx(self, *says, before=()):
        now = timezone.now()
        history = [{'who': 'customer', 'text': text, 'sent_at': now - timedelta(minutes=2)} for text in before]
        return shadow.scripted_context(history, [shadow.Target(id=None, text=text, sent_at=now) for text in says], now=now)

    def draft(self, steps, *says, before=(), post=None, **options):
        claude = FakeClaude(*steps)
        with patch(POST, side_effect=post(claude) if post else claude):
            result = shadow.draft(self.ctx(*says, before=before), fallback=False, **options)
        return result, claude

    # ---- nothing to stop ----

    def test_an_answer_with_no_sum_costs_no_second_call(self):
        result, claude = self.draft([say('באיזו עיר אתם?')], 'יש לכם חוגים?')
        self.assertEqual((result.text, len(claude.requests), result.request_human), ('באיזו עיר אתם?', 1, False))
        self.assertNotIn('שומר הסכומים', result.reasoning)
        with patch('apps.wahub.shadow._money_correction') as told:
            self.draft([THE_MAIN, say(lesson)], f'מתי יש {MAIN} ב{PLACE} לילד בן 5?')
        told.assert_not_called()

    def test_sums_the_system_returned_leave_as_they_are(self):
        # The monthly price and the registration fee of the class, each as the tool returned it — bold, and twice.
        result, claude = self.draft([THE_MAIN, say(lambda found: APART(found) + '\n' + price(found))], f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?')
        self.assertEqual(result.text, 'המחיר *₪250* לחודש\nדמי הרישום *₪120*, פעם אחת\nהמחיר *₪250* לחודש')
        self.assertEqual((len(claude.requests), result.request_human), (2, False))
        # The price of a paid trial, from the tool.
        result, claude = self.draft([THE_BUSY, say('שיעור הניסיון בעלות ₪30')], 'כמה עולה שיעור ניסיון בקפוארה בראש העין לבן 5?')
        self.assertEqual((result.text, len(claude.requests)), ('שיעור הניסיון בעלות *₪30*', 2))
        # The knowledge's own sum — the ceremony at the end of the year — with no tool at all.
        result, claude = self.draft([say('בסוף השנה יש טקס, והעלות ₪60')], 'כמה עולה חגורה?')
        self.assertEqual((result.text, len(claude.requests)), ('בסוף השנה יש טקס, והעלות *₪60*', 1))
        # A price in thousands with agorot.
        course = self.rows['young_hip_hop']
        course.price = Decimal('1250.50')
        course.save(update_fields=['price'])
        result, claude = self.draft([THE_MAIN, say('המחיר ₪1,250.50 לחודש')], f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?')
        self.assertEqual((result.text, len(claude.requests)), ('המחיר *₪1,250.50* לחודש', 2))

    def test_the_customers_own_sum_said_back_to_him(self):
        result, claude = self.draft(
            [THE_MAIN, say(lambda found: '₪180 זה של החוג השני\nאצלנו ' + price(found))],
            f'בחוג השני לוקחים 180. כמה עולה {MAIN} ב{PLACE} לילד בן 5?',
        )
        self.assertEqual((result.text, len(claude.requests), result.request_human), ('*₪180* זה של החוג השני\nאצלנו המחיר *₪250* לחודש', 2, False))
        # Also when he wrote it a turn earlier.
        result, claude = self.draft([THE_MAIN, say(lambda found: 'לא ₪180 — ' + price(found))], 'אז כמה אצלכם?', before=['בחוג השני לוקחים 180'])
        self.assertEqual(len(claude.requests), 2)

    # ---- a sum nobody returned ----

    def test_it_is_sent_back_once_and_the_second_wording_is_the_answer(self):
        with self.assertLogs('apps.wahub.shadow', level='WARNING') as logs:
            result, claude = self.draft([THE_MAIN, say(TOTAL), say(AGAIN)], 'וכמה זה יוצא לי לכל השנה?', before=[f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?'])
        self.assertEqual(result.text, 'המחיר *₪250* לחודש\nדמי הרישום *₪120*, פעם אחת')
        self.assertEqual((result.request_human, result.model, len(claude.requests)), (False, MODEL, 3))
        self.assertIn('the money guard sent a wording back to Claude (הבוט ניסח סכום שלא חזר מהמערכת: ₪370)', logs.output[0])
        # In the same conversation of tools: the wording that was stopped is Claude's own turn, and after it what was wrong with it.
        again = claude.requests[2]['messages']
        self.assertEqual([row['role'] for row in again], ['user', 'assistant', 'user', 'assistant', 'user'])
        self.assertIn('סך הכול ₪370', again[3]['content'][0]['text'])
        told = again[4]['content']
        for words in ('₪370', 'לא חזר מאף כלי', 'התשובה לא נשלחה', 'quote_registration', 'request_human', 'missing_info', 'לא מזכירים ללקוח'):
            self.assertIn(words, told)
        self.assertNotIn('₪250', told)                                  # only the sum with no source is named
        # The owner reads what happened, and what the first wording was.
        self.assertIn('• שומר הסכומים: בתשובה נכתב ₪370 — סכום שלא חזר מהכלים בתשובה הזאת', result.reasoning)
        self.assertIn('סך הכול *₪370*', result.reasoning)
        self.assertIn('זה הניסוח השני', result.reasoning)

    def test_a_tool_called_for_the_second_wording_is_its_source(self):
        # The sum was right and only unchecked: told so, the bot asks the system — and then it is a sum that was returned.
        result, claude = self.draft([say('המחיר ₪250 לחודש'), THE_MAIN, say(price)], 'אז כמה זה?', before=[f'{MAIN} ב{PLACE} לילד בן 5'])
        self.assertEqual((result.text, result.request_human, len(claude.requests)), ('המחיר *₪250* לחודש', False, 3))
        self.assertEqual([tool['name'] for tool in result.tools_used], ['find_courses'])

    def test_a_second_wording_with_a_sum_nobody_returned_is_replaced_by_the_offices_line(self):
        with self.assertLogs('apps.wahub.shadow', level='WARNING') as logs:
            result, claude = self.draft(
                [THE_MAIN, say(TOTAL), say('בסך הכול ₪370 בחודש הראשון')], 'וכמה זה יוצא לי?', before=[f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?'],
            )
        self.assertEqual(result.text, MISSING)
        self.assertEqual((result.request_human, result.request_human_reason), (True, 'הבוט ניסח סכום שלא חזר מהמערכת: ₪370'))
        self.assertEqual(len(claude.requests), 3)                       # one more wording, never a third
        self.assertIn('the money guard stopped an answer', logs.output[-1])
        for words in ('שומר הסכומים עצר את התשובה', 'ללקוח נכתב במקומה שבודקים מול המשרד', 'הניסוח שנעצר: "בסך הכול *₪370* בחודש הראשון"', 'זה הניסוח השני'):
            self.assertIn(words, result.reasoning)
        # The phrasing is the knowledge's own, and the answer says it rests on it; the tools that were read stay on the record.
        phrasing = KnowledgeItem.objects.get(key='missing_info')
        self.assertIn(phrasing.id, result.knowledge_used)
        self.assertEqual([tool['name'] for tool in result.tools_used], ['find_courses'])
        # "מה הבוט לא ידע" reads it as a case of its own.
        self.assertEqual(
            unknowns.detect(result.text, unknown=result.unknown, request_human=True, reason=result.request_human_reason, phrasing=phrasing.body),
            (unknowns.UNKNOWN_BY_PHRASING, 'הבוט ניסח סכום שלא חזר מהמערכת: ₪370'),
        )

    def test_with_no_time_left_nobody_is_asked_again(self):
        result, claude = self.draft([THE_MAIN, say(TOTAL)], 'כמה יוצא?', deadline=time.monotonic() + shadow.MONEY_RETRY_SECONDS - 1)
        self.assertEqual((result.text, result.request_human, len(claude.requests)), (MISSING, True, 2))
        self.assertIn('לא נשאר זמן לבקש מהמודל ניסוח נוסף', result.reasoning)
        # With time for it, the same answer is asked for again inside the same budget.
        result, claude = self.draft([THE_MAIN, say(TOTAL), say(AGAIN)], 'כמה יוצא?', deadline=time.monotonic() + 40)
        self.assertEqual((result.request_human, len(claude.requests)), (False, 3))

    def test_a_second_wording_that_does_not_come_is_not_an_error_and_not_the_first_one(self):
        for error, why in (
            (requests.Timeout(), 'ניסוח נוסף התבקש מהמודל ולא התקבל (Claude לא ענה (Timeout))'),
            (KeyError('ours'), 'ניסוח נוסף התבקש מהמודל ונכשל (KeyError)'),
        ):
            with self.subTest(error=type(error).__name__), self.assertLogs('apps.wahub.shadow', level='WARNING'):
                result, claude = self.draft([THE_MAIN, say(TOTAL)], 'כמה יוצא?', post=lambda claude, error=error: then(claude, error))
                self.assertEqual((result.text, result.request_human, len(claude.requests)), (MISSING, True, 3))
                self.assertIn(why, result.reasoning)
        # An empty second answer is no answer either.
        with self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result, _claude = self.draft([THE_MAIN, say(TOTAL), say('')], 'כמה יוצא?')
        self.assertEqual((result.text, result.request_human), (MISSING, True))

    def test_a_customer_who_wrote_again_meanwhile_starts_it_all_over(self):
        looks = iter([False, True])
        claude = FakeClaude(THE_MAIN, say(TOTAL), say(AGAIN))
        with patch(POST, side_effect=claude), self.assertLogs('apps.wahub.shadow', level='WARNING'), self.assertRaises(shadow.Superseded):
            shadow.draft(self.ctx('כמה יוצא?'), fallback=False, superseded=lambda: next(looks))
        self.assertEqual(len(claude.requests), 2)                       # the second wording was never asked for

    def test_confirming_a_sum_only_the_customer_wrote(self):
        with self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result, claude = self.draft([say('נכון, ₪99 לחודש'), say('על איזה חוג ובאיזו עיר מדובר?')], 'שמעתי שזה 99 שקל לחודש, נכון?')
        self.assertEqual((result.text, result.request_human, len(claude.requests)), ('על איזה חוג ובאיזו עיר מדובר?', False, 2))
        told = claude.requests[1]['messages'][-1]['content']
        self.assertIn('אישרת את הסכום ₪99 שהלקוח כתב', told)
        self.assertIn('לא מאשרים סכום רק כי הלקוח כתב אותו', told)
        with self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result, _claude = self.draft([say('נכון, ₪99 לחודש'), say('כן, ₪99')], 'שמעתי שזה 99 שקל לחודש, נכון?')
        self.assertEqual((result.text, result.request_human_reason), (MISSING, 'הבוט אישר סכום שהלקוח כתב ושלא חזר מהמערכת: ₪99'))

    # ---- the guard itself breaking ----

    def test_a_guard_that_breaks_does_not_send_a_sum_nobody_verified(self):
        broken = patch('apps.wahub.shadow.grounding.unsourced_amounts', side_effect=RuntimeError('broken'))
        with broken, self.assertLogs('apps.wahub.shadow', level='ERROR') as logs:
            result, claude = self.draft([THE_MAIN, say(price)], f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?')
        self.assertEqual((result.text, result.request_human), (MISSING, True))
        self.assertEqual(result.request_human_reason, 'שומר הסכומים נכשל (RuntimeError): בתשובה של הבוט היה סכום שלא אומת')
        self.assertEqual(len(claude.requests), 2)                       # Claude is not asked again: there is nothing to tell it
        self.assertIn('the money guard itself failed', logs.output[0])
        self.assertIn('הניסוח שנעצר: "המחיר *₪250* לחודש"', result.reasoning)
        # An answer with no sign of money in it stands: a fault of the guard does not silence the bot.
        with broken, self.assertLogs('apps.wahub.shadow', level='ERROR'):
            result, _claude = self.draft([say('באיזו עיר אתם?')], 'יש לכם חוגים?')
        self.assertEqual((result.text, result.request_human), ('באיזו עיר אתם?', False))

    def test_a_replacement_that_breaks_still_sends_no_sum(self):
        with patch('apps.wahub.shadow._office_instead', side_effect=RuntimeError('broken')), self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result, _claude = self.draft([THE_MAIN, say(TOTAL), say(TOTAL)], 'כמה יוצא?')
        self.assertEqual((result.text, result.request_human), (MISSING, True))
        self.assertIn('שומר הסכומים נכשל', result.request_human_reason)

    # ---- what would have followed the answer that was stopped ----

    def test_nothing_follows_an_answer_that_was_replaced(self):
        board = {'name': 'holiday_dates', 'input': {'branch': 'מינץ'}, 'summary': '…', 'send_flow': {'name': 'Kogo-Holidays', 'ns': 'content-1', 'label': 'לוח החופשות'}}
        waiting = {'name': 'join_waiting_list', 'input': {'lesson_id': 'x', 'child': ''}, 'summary': 'סומן לרשימת ההמתנה'}
        looked = {'name': 'find_courses', 'input': {}, 'summary': '1 סניפים, 1 חוגים'}
        ctx = self.ctx('יש שיעורים בחנוכה? וכמה עולה?')
        stopped = shadow.Draft(text='שולחת את לוח החופשות, והמחיר ₪999', reasoning='כי', tools_used=[looked, board, waiting], model=MODEL)
        self.assertEqual(len(shadow.flows_to_send(stopped)), 1)
        with self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result = shadow.keep_money_honest(stopped, ctx)
        self.assertEqual((result.text, result.request_human), (MISSING, True))
        self.assertEqual(shadow.flows_to_send(result), [])
        self.assertEqual(result.tools_used, [looked, {key: value for key, value in board.items() if key != 'send_flow'}])
        # The line that is dropped is the one the live answer would have made a row of the waiting list from.
        from apps.wahub import waiting_list

        self.assertEqual(waiting['name'], waiting_list.TOOL_NAME)
        with patch('apps.wahub.waiting_list.join') as joined:
            self.assertEqual(waiting_list.apply_bot_marks(None, result.tools_used), 0)
        joined.assert_not_called()
        self.assertIn('מה שהיה יוצא אחריו לא ייצא (holiday_dates, join_waiting_list)', ctx.notes[-1])
        # An answer that stands keeps everything of its own.
        fine = shadow.Draft(text='שולחת את לוח החופשות', reasoning='כי', tools_used=[looked, board, waiting], model=MODEL)
        self.assertIs(shadow.keep_money_honest(fine, ctx), fine)

    def test_a_phrasing_somebody_gave_a_sum_to_is_not_said_in_its_place(self):
        KnowledgeItem.objects.filter(key='missing_info').update(body='את זה אני בודקת מול המשרד, בדרך כלל ₪100')
        with self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result, _claude = self.draft([say('₪999'), say('₪999')], 'כמה?')
        self.assertEqual(result.text, MISSING)

    # ---- every path an answer leaves by ----

    def test_try_a_question_is_held_to_it_too(self):
        claude = FakeClaude(say('לשנה זה יוצא ₪3,000'), say('בערך ₪3,000 לשנה'))
        with patch(POST, side_effect=claude), self.assertLogs('apps.wahub.shadow', level='WARNING'):
            result = shadow.answer('כמה יוצא לשנה?')
        self.assertEqual((result.text, result.request_human, result.model), (MISSING, True, MODEL))
        self.assertEqual(shadow.draft_payload(result)['request_human_reason'], 'הבוט ניסח סכום שלא חזר מהמערכת: ₪3000')

    def test_the_stub_is_held_to_it_too(self):
        with override_settings(ANTHROPIC_API_KEY=''):
            # What the stub builds from the tools passes, as before.
            result = shadow.draft(self.ctx(f'כמה עולה {MAIN} ב{PLACE} לילד בן 5?'))
            self.assertIn('*₪250*', result.text)
            self.assertFalse(result.request_human)
            # And were it ever to write a sum of its own, it would not leave either.
            made_up = shadow.Draft(text='המחיר ₪777 לחודש', reasoning='• כלל', model='stub')
            with patch('apps.wahub.shadow._stub', return_value=made_up), self.assertLogs('apps.wahub.shadow', level='WARNING'):
                result = shadow.draft(self.ctx('כמה עולה?'))
            self.assertEqual((result.text, result.request_human, result.model), (MISSING, True, 'stub'))

    def test_a_self_check_is_played_against_what_the_customer_would_get(self):
        # The conversation of the run: "וכמה זה יוצא לי לכל השנה?" — a bot that multiplies, is told, and words it again.
        steps = [THE_MAIN, say(price), THE_MAIN, say('החוג פעם בשבוע, ביום חמישי 16:30'), THE_MAIN, say('לשנה זה יוצא ₪3,000'), say(AGAIN)]
        claude = FakeClaude(*steps)
        with patch(POST, side_effect=claude), self.assertLogs('apps.wahub.shadow', level='WARNING'):
            run = runner.run_to_end('edge_no_arithmetic', with_judge=False)
        self.assertEqual((run.status, self.failed(run)), ('passed', set()))
        self.assertEqual(run.turns[2]['reply'], 'המחיר *₪250* לחודש\nדמי הרישום *₪120*, פעם אחת')
        self.assertIn('שומר הסכומים', run.turns[2]['reasoning'])
        # A bot that multiplies twice: the customer gets the office's line, and that is what the checks see.
        claude = FakeClaude(*steps[:5], say('לשנה זה יוצא ₪3,000'), say('₪3,000 לשנה, בערך'))
        with patch(POST, side_effect=claude), self.assertLogs('apps.wahub.shadow', level='WARNING'):
            run = runner.run_to_end('edge_no_arithmetic', with_judge=False)
        self.assertEqual((run.turns[2]['reply'], run.turns[2]['request_human']), (MISSING, True))
        self.assertEqual(run.turns[2]['request_human_reason'], 'הבוט ניסח סכום שלא חזר מהמערכת: ₪3000')
        self.assertNotIn('grounded_amounts', self.failed(run))


# --- the live answer: once, and never the sum -----------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL)
class LiveAnswerTests(LiveTestCase):
    REAL_CLAUDE = True

    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.turn_on()

    def claude(self, *texts):
        return FakeClaude(*[say(text) for text in texts])

    def test_the_customer_is_sent_the_offices_line_once_and_a_person_is_called(self):
        self.manychat_message('כמה יוצא לשנה?')
        claude = self.claude('לשנה זה יוצא ₪3,120', 'בערך ₪3,120 לשנה')
        with patch(POST, side_effect=claude), patch(REQUEST, return_value=OK) as request, self.assertLogs('apps.wahub.shadow', level='WARNING'):
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, MISSING)])  # one message, and no ₪ in it
        self.assertEqual(len(claude.requests), 2)
        proposal = ShadowReply.objects.get(pk=response.data['shadow_id'])
        self.assertEqual((proposal.text, proposal.request_human, proposal.request_human_reason), (MISSING, True, 'הבוט ניסח סכום שלא חזר מהמערכת: ₪3120'))
        self.assertIn('הניסוח שנעצר: "בערך *₪3,120* לשנה"', proposal.reasoning)
        contact = Contact.objects.get(pk=self.contact_id)
        self.assertEqual((contact.needs_human, contact.needs_human_reason), (True, 'הבוט ניסח סכום שלא חזר מהמערכת: ₪3120'))
        self.assertEqual([event.text for event in self.events('needs_human_changed')], ['הבוט ניסח סכום שלא חזר מהמערכת: ₪3120'])
        # Nothing waits: a second call sends nothing more.
        with patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'nothing')
        request.assert_not_called()

    def test_a_second_wording_inside_the_time_of_the_live_answer_is_what_is_sent(self):
        self.manychat_message('כמה יוצא לשנה?')
        claude = self.claude('לשנה זה יוצא ₪3,120', 'על איזה חוג ובאיזו עיר מדובר?')
        with patch(POST, side_effect=claude), patch(REQUEST, return_value=OK) as request, self.assertLogs('apps.wahub.shadow', level='WARNING'):
            response = self.reply()
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, 'על איזה חוג ובאיזו עיר מדובר?')])
        self.assertFalse(Contact.objects.get(pk=self.contact_id).needs_human)
        self.assertEqual(response.data['restarts'], 0)
        self.assertEqual(live_test.REPLY_SECONDS, 50)                   # the budget the second wording is inside of


# --- the instructions ---------------------------------------------------------------------------------------------

class InstructionsTests(SimpleTestCase):
    def test_what_the_run_of_13_10_added_to_the_instructions(self):
        task = shadow.SYSTEM_TASK
        for line in (
            # a total is only what the system computed
            'סכום כולל — "כמה יוצא", "לשנה", "לשני ילדים", "מחיר ועוד דמי רישום" — נאמר רק כפי שחזר מ-quote_registration',
            'לא מחברים, לא מכפילים ולא כותבים "בערך"',
            'כששואלים על שנה: המחיר החודשי ודמי הרישום כפי שחזרו מהכלי, כל אחד בשורה משלו ובלי סכום שלישי',
            'כל סכום בתשובה נבדק לפני השליחה',
            # a list of five, a line said again, a tag nobody marked
            'רשימה — לכל היותר 4 שורות של 🔹, גם כשהכלי החזיר יותר',
            'שורה חמישית של 🔹 לא נכתבת',
            'לא חוזרים על פרט שכבר נאמר בתשובה הקודמת שלך',
            'נושא שכתוב בו "בסיום לתייג"',
            'קוראים ל-tag_conversation עם התגית לפני שעונים',
            # several questions are owed several lines
            'ללקוח ששאל כמה שאלות מפורשות בהודעה אחת — שורה, לכל היותר שתיים, לכל שאלה',
        ):
            self.assertIn(line, task)
        self.assertLess(len(task.splitlines()), 40)
        from apps.wahub.shadow_tools import TOOLS

        tag = {tool['name']: tool['description'] for tool in TOOLS}['tag_conversation']
        self.assertIn('נושא שכולו תשובה אחת (הפעלת יום הולדת: הנוסח של גלעד) מתויג באותה תשובה', tag)


# --- the judge reads what the bot read -----------------------------------------------------------------------------------

BRANCH = {'branches': [{
    'id': '11111111-2222-3333-4444-555555555555', 'name': 'סטודיו הדגמה', 'city': 'ראש העין', 'address': 'הרצל 1, ראש העין',
    'directions': 'הכניסה מהחניון האחורי, קומה 2', 'phone': '0509424755', 'missing': [], 'note': 'הוראה לבוט',
}], 'note': 'הוראה לבוט'}


class JudgeReadsTests(SimpleTestCase):
    def turn(self, reply, **extra):
        return {'turn': 1, 'customer': [{'kind': 'text', 'text': 'איך מגיעים?'}], 'reply': reply, **extra}

    def test_a_tools_answer_and_the_knowledge_behind_an_answer_are_in_what_the_judge_reads(self):
        told = judge._conversation([self.turn(
            'הכניסה מהחניון האחורי, קומה 2\nוהטקס בסוף השנה עולה *₪60*', at_text='יום רביעי 14.10.2026 11:00',
            tools=[{'name': 'branch_info', 'input': {'branch': 'ראש העין'}, 'summary': 'סטודיו הדגמה (שלנו; וויפי לא רשום)', 'result': BRANCH}],
            knowledge=[{'id': 7, 'title': 'טקס סוף שנה — חגורה, תעודה ומתנה קטנה', 'text': CEREMONY}],
            tags=[{'tag': 'הפעלת יום הולדת', 'details': ''}],
        )])
        for words in (
            '(עכשיו, בשעון של השיחה: יום רביעי 14.10.2026 11:00)',
            '(הבוט בדק במערכת — branch_info: סטודיו הדגמה (שלנו; וויפי לא רשום))',
            '"directions": "הכניסה מהחניון האחורי, קומה 2"', '"address": "הרצל 1, ראש העין"',
            '(מהידע של הבוט — טקס סוף שנה — חגורה, תעודה ומתנה קטנה: ' + CEREMONY + ')',
            '(הבוט סימן על השיחה את התגית "הפעלת יום הולדת")',
        ):
            self.assertIn(words, told)
        # Not the tool's own ids, nor its instructions to the bot.
        self.assertNotIn('11111111', told)
        self.assertNotIn('הוראה לבוט', told)
        # A run kept before this — a line of summary and no more — reads as it did.
        old = judge._conversation([self.turn('שלום', tools=[{'name': 'branch_info', 'summary': 'סטודיו הדגמה'}])])
        self.assertEqual(old, '--- תור 1 ---\nלקוח: איך מגיעים?\n(הבוט בדק במערכת — branch_info: סטודיו הדגמה)\nהבוט: שלום')
        for words in ('"חזר:"', '"מהידע של הבוט"', 'זו לא המצאה', 'השדות שהתשובה מצטטת מופיעים ראשונים'):
            self.assertIn(words, judge.SYSTEM)

    def test_a_long_answer_of_a_tool_is_cut_with_what_the_bot_quoted_first(self):
        courses = [{
            'id': f'0000000{index}-2222-3333-4444-555555555555', 'name': f'חוג מספר {index}', 'price_monthly': f'{200 + index}.00',
            'description': 'תיאור ארוך של החוג, ' * 12,
            'lessons': [{'day': 'שלישי', 'start': '17:00', 'seats_text': f'ביום שלישי רשומים {index} מתוך 14'}],
            'trial': {'dates': [{'date': f'{10 + index}.11.2026'}], 'dates_text': f'אפשר להירשם ל-{10 + index}.11'},
        } for index in range(1, 10)]
        reply = 'ביום שלישי רשומים 9 מתוך 14\nהמחיר *₪209* לחודש'
        cut = judge._returned({'courses': courses}, reply, 900)
        self.assertLessEqual(len(cut), 1000)
        lines = cut.split('\n')
        # What the answer says — how many are registered, the price — comes first, with whose it is: the class, its day.
        head = '\n'.join(lines[:8])
        for words in ('courses[8].lessons[0].seats_text: ביום שלישי רשומים 9 מתוך 14', 'courses[8].price_monthly: 209.00', 'courses[8].name: חוג מספר 9', 'courses[8].lessons[0].day: שלישי'):
            self.assertIn(words, head)
        self.assertRegex(lines[-1], r'^… \(קוצר: עוד \d+ שדות שלא נכנסו\)$')
        self.assertNotIn('0000000', cut)
        # Short, it is given whole.
        self.assertEqual(judge._returned(BRANCH, reply), json.dumps(judge._clean(BRANCH), ensure_ascii=False))
        # And a whole conversation that is too long shows less of each answer, not less of the conversation.
        turns = [{**self.turn(reply), 'turn': number, 'tools': [{'name': 'find_courses', 'summary': '…', 'result': {'courses': courses}}] * 4} for number in range(1, 9)]
        with patch.object(judge, 'MAX_CONVERSATION_CHARS', 20000):
            fitted = judge.conversation_for_judge(turns)
        self.assertLess(len(fitted), len(judge._conversation(turns)))
        self.assertEqual(fitted.count('הבוט: '), 8)
        self.assertEqual(fitted.count('רשומים 9 מתוך 14'), 8 * 4 + 8)   # quoted in every answer shown, and said in every reply

    def test_what_was_said_before_the_script_and_the_card_of_the_writer(self):
        told = judge._conversation(
            [self.turn('יום חמישי 16:30')],
            before=[{'who': 'customer', 'text': 'יש היפ הופ בראש העין לילד בן 5?', 'hours_ago': 0.4}, {'who': 'bot', 'text': 'בן כמה הילד?', 'hours_ago': 20}],
            card={'known': True, 'name': 'רותם בדויה', 'children': [{'id': 'script-child', 'name': 'נגה בדויה', 'classes': [{'day': 'חמישי', 'start': '16:30'}]}]},
        )
        self.assertIn('--- לפני השיחה (נאמר קודם, והבוט קרא את זה) ---\n(לפני 24 דקות) לקוח: יש היפ הופ בראש העין לילד בן 5?\n(לפני 20 שעות) הבוט: בן כמה הילד?', told)
        self.assertIn('(מה המערכת יודעת על הכותב, כפי שנמסר לבוט: {"known": true, "name": "רותם בדויה"', told)
        self.assertIn('"start": "16:30"', told)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL, REGISTRATION_FEE_ILS=120)
class JudgeIsToldTests(EvalTestCase):
    def play(self, scenario_id, *steps):
        claude = FakeClaude(*steps)
        with patch(POST, side_effect=claude):
            run = runner.run_to_end(scenario_id)
        return run, claude.requests[-1]['messages'][0]['content']

    def test_the_question_that_waited_is_shown_to_the_judge(self):
        # The run of 13.10: the judge wrote that no earlier question was left unanswered — it was never shown the one that was.
        run, told = self.play('edge_question_mark_after_silence', THE_MAIN, say(lesson), ('json', {'verdicts': [{'number': 1, 'holds': True, 'why': 'ענה על השאלה שחיכתה'}]}))
        self.assertEqual(run.status, 'passed', runner.run_payload(run)['failures'])
        self.assertIn('--- לפני השיחה (נאמר קודם, והבוט קרא את זה) ---\n(לפני 24 דקות) לקוח: יש היפ הופ בראש העין לילד בן 5?', told)
        self.assertIn('(חזר: {', told)
        self.assertIn('"start": "16:30"', told)
        self.assertRegex(told, r'\(עכשיו, בשעון של השיחה: יום (ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת) \d\d\.\d\d\.\d{4} 11:00\)')

    def test_the_knowledge_an_answer_rests_on_is_kept_for_the_judge_and_not_for_the_screen(self):
        fact = KnowledgeItem.objects.get(title__startswith='טקס סוף שנה')
        scenario = next(row for row in scenarios.SCENARIOS if row.id == 'ceremony_belt_cost_and_date')
        steps = [say('בסוף השנה יש טקס 🎉 העלות *₪60*, עבור חגורה, תעודה ומתנה קטנה')] + [say('התאריך יעודכן בהמשך השנה')] * (len(scenario.turns) - 1)
        rubric = sum(len(turn.rubric) for turn in scenario.turns)
        if rubric:
            steps.append(('json', {'verdicts': [{'number': number, 'holds': True, 'why': 'מהידע'} for number in range(1, rubric + 1)]}))
        run, told = self.play('ceremony_belt_cost_and_date', *steps)
        # The bot did not say which item it read: the ₪60 it wrote is found in the knowledge all the same.
        self.assertEqual([item['id'] for item in run.turns[0]['knowledge']], [fact.id])
        self.assertIn('₪60', run.turns[0]['knowledge'][0]['text'])
        self.assertEqual(run.turns[1]['knowledge'], [])
        if rubric:
            self.assertIn(f'(מהידע של הבוט — {fact.title}: ', told)
        payload = runner.run_payload(run)
        self.assertEqual(set(payload['conversation'][0]), {'turn', 'customer', 'last_outbound', 'bot', 'reasoning', 'tools', 'request_human', 'request_human_reason', 'tags'})
        self.assertNotIn('טקס', json.dumps(payload['conversation'][0]['tools'], ensure_ascii=False))


# --- four checks that were stricter than the rules ------------------------------------------------------------------------

CITIES = {'name': 'find_courses', 'input': {}, 'result': {
    'need': 'city', 'ask': 'באיזו עיר מדובר?', 'cities': ['ראש העין', 'פתח תקווה', 'רמת גן', 'יהוד'], 'branches': [], 'courses': [],
}}
ARABIC_FIRST = 'مرحبا، هل يوجد عندكم دورات للأطفال بعمر خمس سنوات؟'
ARABIC_AGAIN = 'لم أفهم، هل يمكن أن تخبروني أين توجد الدورات؟'


class StricterThanTheRulesTests(SimpleTestCase):
    def moment(self, reply, **extra):
        return Moment(reply=reply, **extra)

    def test_a_line_of_the_systems_names_is_not_a_sentence_of_hebrew(self):
        arabic = dict(customer=[ARABIC_AGAIN], customer_now=f'{ARABIC_FIRST}\n{ARABIC_AGAIN}', customer_before=[ARABIC_FIRST], calls=[CITIES])
        listed = 'أهلاً! في أي مدينة تبحثون عن دورة؟\nראש העין, פתח תקווה, רמת גן, יהוד'
        self.assertEqual(checks._hebrew_only(self.moment(listed, **arabic)), '')
        self.assertEqual(checks.answers_in('arabic').test(self.moment(listed, **arabic)), '')
        bullets = 'أهلاً! في أي مدينة تبحثون عن دورة؟\n🔹 ראש העין\n🔹 פתח תקוה ורמת גן\n🔹 יהוד'
        self.assertEqual(checks._hebrew_only(self.moment(bullets, **arabic)), '')
        # A sentence of Hebrew is still one — with the names in it or without.
        for line in ('יש לנו סניפים בערים ראש העין, פתח תקווה, רמת גן ויהוד, באיזו עיר נוח לכם', 'באיזו עיר אתם מחפשים חוג לילד שלכם עכשיו'):
            self.assertIn('משפט שלם בעברית', checks._hebrew_only(self.moment(f'أهلاً! في أي مدينة تبحثون عن دورة؟\n{line}', **arabic)), line)
        # And a name the system does not hold is not passed over.
        self.assertIn('משפט שלם בעברית', checks._hebrew_only(self.moment('أهلاً! في أي مدينة تبحثون عن دورة؟\nחיפה, נתניה, אשדוד, אילת, טבריה', **arabic)))

    def test_several_questions_asked_outright_are_owed_a_line_or_two_each(self):
        four = (
            'היי, אני מתעניינת בהיפ הופ בראש העין לבן שלי, הוא בן 5. רציתי לדעת באיזה יום ושעה זה, כמה זה עולה לחודש, '
            'אם יש שיעור ניסיון, ומה צריך להביא. תודה מראש!'
        )
        self.assertEqual(checks.questions_asked([four]), 4)
        self.assertEqual(checks.questions_asked(['כמה שאלות: איפה אתם? כמה זה עולה? יש שיעור ניסיון? מה מדיניות הביטול? עד מתי השנה? ומה מביאים?']), 6)
        self.assertEqual(checks.questions_asked(['יש לכם חוגים?']), 1)
        self.assertEqual(checks.questions_asked(['היי', 'תודה רבה!']), 0)
        eight = '\n'.join(f'שורה {number} של תשובה' for number in range(1, 9))
        self.assertEqual(checks._short(self.moment(eight, customer=[four])), '')
        # To one question the same eight lines are still too many, and to four — nine are.
        self.assertIn('הכלל הוא 1–3 שורות', checks._short(self.moment(eight, customer=['כמה זה עולה?'])))
        nine = eight + '\nועוד שורה'
        self.assertIn('הלקוח שאל 4 שאלות, ומותר עד שורה-שתיים לכל שאלה (8 שורות)', checks._short(self.moment(nine, customer=[four])))
        # However many were asked, a message is still a message.
        self.assertIn('ארוכה מדי', checks._short(self.moment('\n'.join(['שורה'] * 13), customer=['מה? ' * 9])))
        self.assertIn('ארוכה מדי', checks._short(self.moment('מילה ' * 200, customer=[four])))

    def test_somebody_at_the_door_is_handed_to_a_person_and_not_asked_the_next_question(self):
        gated = {'name': 'find_courses', 'input': {'city': 'ראש העין', 'course_type': 'קפוארה'}, 'result': {
            'branches': [{'name': 'סטודיו הדגמה'}], 'need': 'age', 'ask': 'בן כמה הילד, או באיזו כיתה?', 'courses': [],
        }}
        said = 'אמור להיות שיעור ב-11:00 — בודקים מיד מול המשרד'
        wrote = 'אמור להיות עכשיו שיעור של קפוארה ב-11:00 ואין אף אחד'
        self.assertEqual(checks._order_kept(self.moment(said, calls=[gated], request_human=True, customer_all=wrote)), '')
        # With nobody called, the question the system said to ask is still owed.
        self.assertIn('והבוט לא שאל את זה', checks._order_kept(self.moment(said, calls=[gated], customer_all=wrote)))
        # And a person being called is no leave to offer a class before the question.
        offered = checks._order_kept(self.moment('יש קפוארה ביום שלישי ב-17:00, ונציג יחזור אליכם', calls=[gated], request_human=True, customer_all=wrote))
        self.assertIn('קודם מבררים, אחר כך מציעים', offered)
