"""
The owner's answers of 13.10.2026 (docs/CHANGE-IMPACT-2026-10-13-WAHUB-OWNER-ANSWERS.md).

What is held here:
  * the holidays board of a branch: which automation a branch is on, what
    ManyChat is heard to have under the boards' names, and that the bot's tool
    only reads;
  * the automation leaves after the answer's text — in the live test, once,
    through the same doors as every send — and nowhere else: not from the
    shadow, not from "נסה שאלה", not from a self-check;
  * the status says which of the four automations ManyChat has;
  * the language an answer is owed in: Hebrew, until the customer goes on in
    his own language or asks for it;
  * his answers are filed in the knowledge, and a rewritten record reaches the
    row production already holds — with its new title;
  * every conversation added that day is passed by a bot that answers well and
    failed by one that answers badly.
"""
import json
from datetime import date, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.core.manychat_service import ManyChatError
from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import LessonEnrollment
from apps.wahub import cron, holiday_flows, knowledge, knowledge_import, knowledge_seed, sending, settings_store, shadow, shadow_tools
from apps.wahub.evals import checks, rules, runner, scenarios, world
from apps.wahub.evals.checks import Moment
from apps.wahub.models import Contact, KnowledgeHistory, KnowledgeItem, Message, ShadowReply
from apps.wahub.shadow_tools import ToolContext, run_tool, summarize_result
from apps.wahub.tests.base import WahubTestCase
from apps.wahub.tests.test_bot_sharper import LOUD, MISSING, PLACE, QUIET, find, person, say
from apps.wahub.tests.test_evals import MODEL, POST, EvalTestCase, FakeClaude
from apps.wahub.tests.test_live_test import OK, OWNER_SUBSCRIBER, REQUEST, LiveTestCase, asked_to_do, sends
from apps.wahub.tests.test_shadow import catalogue

MAIN, YAHUD, OR_YEHUDA, ZAMIR = 'kogo_Holidays', 'kogo_Holidays_yahud', 'kogo_Holidays_oryehuda', 'kogo_Holidays_zamir'
# ManyChat's own list, as its API answers getFlows.
LISTED = {'status': 'success', 'data': [
    {'ns': 'content-1', 'name': 'kogo_Holidays'},
    {'ns': 'content-2', 'name': 'Kogo Holidays-Yahud'},          # a person's spelling of the same name
    {'ns': 'content-3', 'name': 'kogo_holidays_zamir'},
    {'ns': 'content-4', 'name': 'kogo_Holidays_zamir'},          # twice: nobody's
    {'ns': 'content-9', 'name': 'kogo_lead_link_1'},
]}
HEARD = [{'id': 'content-1', 'name': 'kogo_Holidays'}, {'id': 'content-2', 'name': 'Kogo Holidays-Yahud'}]


def branch(name, city, **extra):
    return TestDataFactory.create_branch(name=name, city=TestDataFactory.create_city(city), **extra)


class Branches:
    """The branches as production names them — the names are the owner's, nothing else of them is anybody's."""

    def __init__(self):
        self.mintz = branch('מרכז העיר - מינץ 24', 'פתח תקווה')
        self.em = branch('אם המושבות - רפאל איתן 5', 'פתח תקווה')
        self.zamir = branch('מרכז זמיר - אויערבך משה 3', 'פתח תקווה')
        self.karl = branch('קרל וגרטי קורי 8', 'ראש העין')
        self.psagot = branch('פסגות אפק', 'ראש העין')
        self.dimri = branch('דימרי סנטר', 'כפר סבא')
        self.shoham = branch('מרכז ניצנים', 'שוהם')
        self.yahud = branch('מרכז קהילתי', 'יהוד')
        self.or_yehuda = branch('אור יהודה - בית בפארק', 'אור יהודה', is_external=True)
        self.ramat_gan = branch('רמת גן - גאולים', 'רמת גן', is_external=True)


# --- which board a branch is on, and what ManyChat has ----------------------------------------------------------------

class BoardsTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.b = Branches()

    def board(self, row):
        found = holiday_flows.board_of(row)
        return found.flow if found else None

    def test_each_branch_is_on_the_board_the_owner_named(self):
        b = self.b
        self.assertEqual([self.board(row) for row in (b.mintz, b.em, b.karl, b.dimri, b.shoham)], [MAIN] * 5)
        self.assertEqual((self.board(b.yahud), self.board(b.or_yehuda), self.board(b.zamir)), (YAHUD, OR_YEHUDA, ZAMIR))
        # The second branch of a city, and a branch nobody listed, are on no board.
        self.assertEqual((self.board(b.psagot), self.board(b.ramat_gan)), (None, None))
        # אור יהודה is not יהוד, and the three names of one centre are one board.
        self.assertEqual(self.board(branch('סניף חדש', 'אור יהודה')), OR_YEHUDA)
        self.assertEqual([self.board(branch(name, 'פתח תקווה')) for name in ('כפר גנים', 'אויערבך', 'זמיר')], [ZAMIR] * 3)
        self.assertEqual([board.flow for board in holiday_flows.BOARDS], [MAIN, YAHUD, OR_YEHUDA, ZAMIR])

    def test_the_names_are_found_whatever_their_spelling_and_a_name_two_carry_is_nobodys(self):
        flows = [{'id': row['ns'], 'name': row['name']} for row in LISTED['data']]
        found, twice = holiday_flows.by_name(flows)
        self.assertEqual(found, {MAIN: {'flow': 'content-1', 'name': 'kogo_Holidays'}, YAHUD: {'flow': 'content-2', 'name': 'Kogo Holidays-Yahud'}})
        self.assertEqual(twice, [ZAMIR])

    def test_nothing_kept_is_nothing_found_and_the_tool_never_asks(self):
        self.assertEqual(holiday_flows.found(ask=False), {})
        self.assertIsNone(holiday_flows.flow_of(holiday_flows.BY_FLOW[MAIN]))
        self.assertEqual(holiday_flows.why_missing(holiday_flows.BY_FLOW[MAIN]), holiday_flows.NOT_ASKED_YET)
        holiday_flows.remember(HEARD)
        self.assertEqual(holiday_flows.flow_of(holiday_flows.BY_FLOW[YAHUD]), {'name': YAHUD, 'ns': 'content-2', 'label': 'לוח החופשות של יהוד'})
        self.assertEqual(holiday_flows.why_missing(holiday_flows.BY_FLOW[ZAMIR]), holiday_flows.NOT_IN_MANYCHAT)
        self.network.assert_not_called()

    @override_settings(MANYCHAT_KEY='test-key')
    def test_manychat_is_asked_once_in_ten_minutes_and_its_last_answer_stands_when_it_does_not_answer(self):
        now = timezone.now()
        with patch(REQUEST, return_value=LISTED) as request:
            first = holiday_flows.found(now)
            again = holiday_flows.found(now + timedelta(minutes=9))
        self.assertEqual(first, again)
        self.assertEqual(set(first), {MAIN, YAHUD})
        self.assertEqual([call.args[:2] for call in request.call_args_list], [('GET', '/fb/page/getFlows')])
        self.assertEqual(holiday_flows.why_missing(holiday_flows.BY_FLOW[ZAMIR]), holiday_flows.TWICE_IN_MANYCHAT)
        # Ten minutes later it is asked again; it does not answer, and what it said last still stands — for ten more minutes.
        with patch(REQUEST, side_effect=ManyChatError('Server error')) as request, self.assertLogs('apps.wahub.lead_templates', level='WARNING'):
            later = holiday_flows.found(now + timedelta(minutes=11))
            holiday_flows.found(now + timedelta(minutes=12))
        self.assertEqual((set(later), request.call_count), ({MAIN, YAHUD}, 1))
        self.assertFalse(settings_store.get(settings_store.HOLIDAY_FLOWS_FOUND)['ok'])

    def test_with_no_manychat_or_no_branch_on_a_board_nobody_is_asked(self):
        with patch(REQUEST) as request:
            self.assertEqual(holiday_flows.found(), {})                                    # no key on this server
            with override_settings(MANYCHAT_KEY='test-key', DEBUG=True, WAHUB_SIMULATE_SEND=True):
                self.assertEqual(holiday_flows.found(), {})                                # a developer's machine that only records
            type(self.b.mintz).objects.exclude(pk=self.b.ramat_gan.pk).update(is_active=False)
            with override_settings(MANYCHAT_KEY='test-key'):
                self.assertEqual(holiday_flows.found(), {})                                # no branch a board would be sent for
        request.assert_not_called()

    @override_settings(MANYCHAT_KEY='test-key')
    def test_the_status_says_which_of_the_four_manychat_has(self):
        with patch(REQUEST, return_value=LISTED):
            data = self.get('status/').data['holiday_flows']
        self.assertEqual([(row['name'], row['found']) for row in data['flows']], [(MAIN, True), (YAHUD, True), (OR_YEHUDA, False), (ZAMIR, False)])
        self.assertEqual(data['missing'], [OR_YEHUDA, ZAMIR])
        by_name = {row['name']: row for row in data['flows']}
        self.assertEqual(by_name[MAIN]['branches'], ['אם המושבות - רפאל איתן 5', 'דימרי סנטר', 'מרכז העיר - מינץ 24', 'מרכז ניצנים', 'קרל וגרטי קורי 8'])
        self.assertEqual((by_name[OR_YEHUDA]['problem'], by_name[ZAMIR]['problem']), (holiday_flows.NOT_IN_MANYCHAT, holiday_flows.TWICE_IN_MANYCHAT))
        self.assertEqual(data['branches_without_a_board'], ['פסגות אפק', 'רמת גן - גאולים'])
        self.assertTrue(data['manychat_answered'] and data['asked_at'])
        # Asked a moment ago: the next look at the status reads what is kept.
        with patch(REQUEST) as request:
            self.assertEqual(self.get('status/').data['holiday_flows']['missing'], [OR_YEHUDA, ZAMIR])
        request.assert_not_called()

    def test_the_status_with_no_manychat_says_nothing_was_asked(self):
        data = self.get('status/').data['holiday_flows']
        self.assertEqual((data['asked_at'], data['manychat_answered'], len(data['missing'])), (None, False, 4))
        self.assertEqual({row['problem'] for row in data['flows']}, {holiday_flows.NOT_ASKED_YET})

    @override_settings(MANYCHAT_KEY='test-key', CRON_TOKEN='cron-secret')
    def test_the_cron_keeps_the_answer_fresh_and_a_fault_there_does_not_stop_the_slice(self):
        with patch(REQUEST, return_value=LISTED) as request:
            counts = cron.tick()
            cron.tick()
        self.assertEqual(counts['holiday_flows_found'], 2)
        self.assertEqual(asked_to_do(request), [])                                         # a read of the list, and nothing else
        # Once for the boards — and once for the templates of the bot's follow-ups to leads, which the cron keeps fresh
        # the same way (cron 6b): two reads in ten minutes, however many calls.
        self.assertEqual(request.call_count, 2)
        with patch.object(holiday_flows, 'found', side_effect=RuntimeError('boom')), self.assertLogs('apps.wahub.cron', level='ERROR'):
            self.assertIn('seconds', cron.tick())
        # With no time left the list is not asked for.
        settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {})
        with patch(REQUEST) as request:
            self.assertNotIn('holiday_flows_found', cron.tick(budget_seconds=0))
        request.assert_not_called()


# --- the bot's tool ----------------------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='')
class HolidayDatesToolTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.b = Branches()
        holiday_flows.remember(HEARD)

    def ask(self, contact=None, **arguments):
        return run_tool('holiday_dates', arguments, ToolContext(contact))

    def test_a_branch_whose_board_manychat_has(self):
        for words in ({'branch': 'מינץ'}, {'branch': 'קרל וגרטי'}, {'branch': 'אם המושבות'}, {'city': 'שוהם'}, {'branch': 'כפר סבא'}, {'branch': 'דמרי'}):
            with self.subTest(words=words):
                result = self.ask(**words)
                self.assertEqual((result['found'], result['need'], result['flow_name']), (True, '', MAIN), result)
                self.assertEqual(result['send_flow'], {'name': MAIN, 'ns': 'content-1', 'label': 'לוח החופשות'})
                self.assertEqual(result['say'], 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים')
        yahud = self.ask(city='יהוד')
        self.assertEqual((yahud['found'], yahud['send_flow']['ns'], yahud['branches']), (True, 'content-2', [{'name': 'מרכז קהילתי', 'city': 'יהוד'}]))
        self.assertIn('אחרי התשובה נשלחת האוטומציה kogo_Holidays_yahud (לוח החופשות של יהוד) למרכז קהילתי', summarize_result('holiday_dates', yahud))
        self.assertIn('בתשובה בצל, ב"נסה שאלה" ובבדיקות לא נשלח דבר', summarize_result('holiday_dates', yahud))

    def test_a_board_manychat_does_not_have_and_a_branch_on_no_board_go_to_the_office(self):
        zamir = self.ask(branch='זמיר')
        self.assertEqual((zamir['found'], zamir['need'], zamir['flow_name'], zamir['why']), (False, '', ZAMIR, holiday_flows.NOT_IN_MANYCHAT))
        self.assertNotIn('send_flow', zamir)
        self.assertIn('missing_info', zamir['note'])
        self.assertEqual(summarize_result('holiday_dates', zamir), 'מרכז זמיר - אויערבך משה 3: האוטומציה kogo_Holidays_zamir: לא נמצאה ב-ManyChat אוטומציה בשם הזה — לא נשלח דבר; בודקים מול המשרד')
        for words in ({'branch': 'פסגות אפק'}, {'city': 'רמת גן'}):
            result = self.ask(**words)
            self.assertEqual((result['found'], result['need'], result['flow_name']), (False, '', ''), result)
            self.assertNotIn('send_flow', result)
        # What only the tool's line of `tools_used` carries: the automation, and only when there is one to send.
        self.assertNotIn('send_flow', shadow_tools.used('holiday_dates', {'branch': 'זמיר'}, zamir))
        self.assertEqual(shadow_tools.used('holiday_dates', {}, self.ask(branch='מינץ'))['send_flow']['ns'], 'content-1')

    def test_branches_of_different_boards_are_a_question_and_so_is_no_branch_at_all(self):
        petah_tikva = self.ask(city='פתח תקווה')                         # מינץ and אם המושבות on one board, זמיר on another
        self.assertEqual((petah_tikva['found'], petah_tikva['need'], petah_tikva['ask']), (False, 'branch', 'באיזה סניף מדובר?'))
        self.assertEqual(len(petah_tikva['branches']), 3)
        rosh_haayin = self.ask(branch='ראש העין')                        # one on the main board, one on none
        self.assertEqual((rosh_haayin['need'], [row['name'] for row in rosh_haayin['branches']]), ('branch', ['פסגות אפק', 'קרל וגרטי קורי 8']))
        nothing = self.ask()
        self.assertEqual((nothing['found'], nothing['need'], nothing['branches']), (False, 'branch', []))
        self.assertIn('ראש העין', nothing['cities'])
        self.assertEqual(summarize_result('holiday_dates', nothing), 'קודם שואלים באיזה סניף; לוח חופשות לא נשלח')
        unknown = self.ask(city='אילת')
        self.assertEqual((unknown['found'], unknown['need'], unknown['hint']), (False, '', 'לא נמצא סניף כזה במערכת.'))
        for result in (petah_tikva, rosh_haayin, nothing, unknown):
            self.assertNotIn('send_flow', result)

    def test_a_registered_parent_who_names_no_branch_gets_the_board_of_the_childs_class(self):
        course = TestDataFactory.create_course(name='קפוארה צעירים', branch=self.b.yahud, course_type=TestDataFactory.create_course_type('קפוארה'))
        lesson = TestDataFactory.create_lesson(course=course, day_of_week=2)
        family = TestDataFactory.create_family(name='משפחת בדויה', branch=self.b.yahud, phone='050-5550101')
        child = TestDataFactory.create_child(family=family, first_name='נגה', last_name='בדויה', status='active')
        LessonEnrollment.objects.create(child=child, lesson=lesson, status='active')
        self.incoming('יש שיעור בחנוכה?')
        from apps.wahub import matching

        matching.recheck_contact(self.contact())
        contact = self.contact()
        self.assertEqual(shadow_tools.customer_card(ToolContext(contact))['children'][0]['classes'][0]['branch'], 'מרכז קהילתי')
        result = self.ask(contact)
        self.assertEqual((result['found'], result['flow_name']), (True, YAHUD))
        # A branch he names wins over the one on the card.
        self.assertEqual(self.ask(contact, branch='מינץ')['flow_name'], MAIN)

    def test_the_tool_reads_only(self):
        def reading_only(execute, sql, params, many, context):
            if sql.lstrip().split(None, 1)[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
                raise AssertionError(f'the tool wrote to the database: {sql[:120]}')
            return execute(sql, params, many, context)

        with override_settings(MANYCHAT_KEY='test-key'), connection.execute_wrapper(reading_only), patch(REQUEST) as request:
            settings_stale = holiday_flows.found(ask=False)
            for words in ({'branch': 'מינץ'}, {'branch': 'זמיר'}, {'city': 'פתח תקווה'}, {}):
                self.assertNotIn('error', self.ask(**words))
        request.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(set(settings_stale), {MAIN, YAHUD})

    def test_the_tool_is_described_to_the_bot_and_the_instructions_send_it_there(self):
        description = {tool['name']: tool['description'] for tool in shadow_tools.TOOLS}
        for words in ('יש שיעורים בחנוכה?', 'found=true', 'need=branch', 'לא מפרטים תאריכי חופשה בעצמנו', 'missing_info', 'רק כשהלקוח שאל על חגים או על חופשות'):
            self.assertIn(words, description['holiday_dates'])
        # No manager, one phone (the owner, 13.10.2026 at noon): the tool says so, and names nobody.
        self.assertIn('phone — הטלפון של המשרד, שהוא הטלפון של כל הסניפים', description['branch_info'])
        self.assertIn('לסניף אין מנהל', description['branch_info'])
        self.assertNotIn('manager_name', description['branch_info'])
        task = shadow.SYSTEM_TASK
        for line in (
            'כל שאלה על חגים וחופשות', 'קראי ל-holiday_dates עם הסניף', 'תאריכי חופשה לא מפרטים בעצמך, גם לא מהימים המיוחדים שבידע',
            'שולחים את לוח החופשות של הסניף', 'עונים בעברית — גם להודעה ראשונה בשפה אחרת',
            'כשהוא ממשיך לכתוב בה אחרי שקיבל תשובה בעברית', 'או כשביקש במפורש או כתב שאינו מבין עברית',
        ):
            self.assertIn(line, task)
        self.assertNotIn('עונים בשפה שבה הלקוח כותב', task)
        # The special days are still read to the bot — for the office's hours, not to recite vacations from.
        knowledge_import.run(today=date(2026, 10, 13))
        told = knowledge.describe_items(knowledge.relevant_items(today=date(2026, 12, 1)))      # a week before Hanukkah
        self.assertIn('חנוכה', told)
        self.assertIn('ימים מיוחדים קרובים (לשעות המשרד בלבד.', told)
        self.assertNotIn('להציג רק עתידיים', told)


# --- what an answer sends after its text ------------------------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='', CRON_TOKEN='cron-secret')
class FlowsToSendTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        self.ours, _course, _lesson, _external = catalogue()            # "קרל וגרטי קורי 8", in ראש העין
        holiday_flows.remember(HEARD)
        self.deliver = patch('apps.wahub.sending._deliver', side_effect=AssertionError('an answer that is only kept tried to send')).start()
        self.addCleanup(patch.stopall)

    def test_the_answers_own_record_names_the_automation_once(self):
        flow = {'name': MAIN, 'ns': 'content-1', 'label': 'לוח החופשות'}
        line = {'name': 'holiday_dates', 'input': {'branch': 'מינץ'}, 'summary': '…', 'send_flow': flow}
        draft = shadow.Draft(text='שולחת', reasoning='', tools_used=[{'name': 'find_courses', 'input': {}, 'summary': ''}, line, dict(line)])
        self.assertEqual(shadow.flows_to_send(draft), [flow])
        # A line written before there was such a thing, a tool that found no board, and an answer with no tools at all.
        # …and a line of any other tool: only the holidays tool names an automation, whatever a record says.
        other = {'name': 'branch_info', 'input': {}, 'summary': '', 'send_flow': flow}
        self.assertNotIn('send_flow', shadow_tools.used('branch_info', {}, {'found': True, 'send_flow': flow}))
        for used in ([{'name': 'holiday_dates', 'input': {}, 'summary': ''}], [{'name': 'holiday_dates', 'send_flow': {'name': MAIN, 'ns': ''}}], [other], [], None, ['x']):
            self.assertEqual(shadow.flows_to_send(shadow.Draft(text='', reasoning='', tools_used=used)), [])

    def test_try_a_question_shows_what_would_follow_and_sends_nothing(self):
        answer = shadow.answer('יש שיעורים בחנוכה בקרל וגרטי?')
        self.assertEqual(answer.text, 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים')
        self.assertFalse(answer.request_human)
        [line] = answer.tools_used
        self.assertEqual((line['name'], line['send_flow']['name'], line['send_flow']['ns']), ('holiday_dates', MAIN, 'content-1'))
        self.assertIn('אחרי התשובה נשלחת האוטומציה kogo_Holidays (לוח החופשות)', line['summary'])
        self.assertEqual(shadow.flows_to_send(answer), [{'name': MAIN, 'ns': 'content-1', 'label': 'לוח החופשות'}])
        self.assertEqual(shadow.draft_payload(answer)['tools_used'], answer.tools_used)
        self.deliver.assert_not_called()
        self.assertEqual(Message.objects.count(), 0)

    def test_the_shadow_keeps_the_answer_with_its_automation_and_sends_nothing(self):
        self.incoming('מתי החופשה של פסח בראש העין?')
        contact = self.contact()
        self.age(contact, last_inbound_at=1)
        with override_settings(MANYCHAT_KEY='test-key'), patch(REQUEST, return_value=LISTED) as request:
            self.assertEqual(cron.tick()['shadow_proposed'], 1)
        self.assertEqual(asked_to_do(request), [])
        reply = ShadowReply.objects.get(contact=contact)
        self.assertEqual(reply.text, 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים')
        self.assertEqual([flow['name'] for flow in shadow.flows_to_send(reply)], [MAIN])
        self.assertEqual(shadow.reply_payload(reply)['tools_used'][0]['send_flow']['name'], MAIN)
        self.deliver.assert_not_called()
        self.assertEqual(Message.objects.filter(direction='out').count(), 0)

    def test_the_stub_asks_which_branch_and_goes_to_the_office_when_there_is_no_board(self):
        asked = shadow.answer('היי, יש שיעור בחג?')
        self.assertEqual((asked.text, asked.request_human, shadow.flows_to_send(asked)), ('באיזה סניף מדובר?', False, []))
        settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {})
        none = shadow.answer('יש שיעורים בחנוכה בראש העין?')
        self.assertEqual((none.text, none.request_human, shadow.flows_to_send(none)), (MISSING, True, []))
        self.assertEqual([tool['name'] for tool in none.tools_used], ['holiday_dates', 'request_human'])
        # The summer is not a holiday: the season's end, and the camps, are another question.
        self.assertNotIn('holiday_dates', [tool['name'] for tool in shadow.answer('יש חוגים בחופש הגדול בראש העין?').tools_used])


# --- the live test: the text, and then the board ------------------------------------------------------------------------

def flows_sent(request):
    """The sendFlow calls ManyChat got, as (subscriber_id, flow)."""
    return [
        (call.kwargs['json_body']['subscriber_id'], call.kwargs['json_body']['flow_ns'])
        for call in request.call_args_list if call.args[1] == '/fb/sending/sendFlow'
    ]


class TheBoardLeavesAfterTheAnswerTests(LiveTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        catalogue()
        self.turn_on()
        holiday_flows.remember(HEARD)

    def test_the_text_and_then_the_automation_once_as_the_new_bots_lines(self):
        self.manychat_message('יש שיעורים בחנוכה בראש העין?')
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent', response.data)
        self.assertEqual(sends(request), [(OWNER_SUBSCRIBER, 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים')])
        self.assertEqual(flows_sent(request), [(OWNER_SUBSCRIBER, 'content-1')])
        # In that order: the sentence, and the picture after it.
        self.assertEqual([call.args[1] for call in request.call_args_list], ['/fb/sending/sendContent', '/fb/sending/sendFlow'])
        text, board = Message.objects.filter(contact_id=self.contact_id, direction='out').order_by('id')
        self.assertEqual((text.message_type, text.sender, text.new_bot, text.status), ('text', 'bot', True, 'sent'))
        self.assertEqual((board.message_type, board.sender, board.new_bot, board.status, board.text), ('template', 'bot', True, 'sent', 'לוח החופשות (kogo_Holidays)'))
        self.assertEqual(response.data['flows'], [{'name': MAIN, 'sent': True, 'message_id': board.id, 'error': ''}])
        [event] = self.answers()
        self.assertTrue(event.text.startswith('תשובה חיה נשלחה: '), event.text)
        self.assertIn('אחריה נשלחה האוטומציה kogo_Holidays', event.text)
        self.assertFalse(self.owner().needs_human)
        # Answered: a second call sends neither again.
        with patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'nothing')
        request.assert_not_called()

    def test_an_automation_that_does_not_leave_is_said_on_the_contact(self):
        self.manychat_message('יש שיעורים בחנוכה בראש העין?')

        def manychat(method, path, **kwargs):
            if path == '/fb/sending/sendFlow':
                raise ManyChatError('Validation error', status_code=400, payload={'details': 'Flow not found'})
            return OK

        with patch(REQUEST, side_effect=manychat) as request, self.assertLogs('apps.wahub.sending', level='WARNING'):
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')                      # the answer itself did leave
        self.assertEqual(len(flows_sent(request)), 1)                             # tried once, never again by itself
        board = Message.objects.filter(contact_id=self.contact_id, direction='out').order_by('-id').first()
        self.assertEqual((board.message_type, board.status), ('template', 'failed'))
        self.assertIn('ManyChat', board.error)
        self.assertFalse(response.data['flows'][0]['sent'])
        contact = self.owner()
        self.assertTrue(contact.needs_human)
        self.assertIn('האוטומציה שאחרי התשובה לא נשלחה (בדיקה חיה): kogo_Holidays', contact.needs_human_reason)
        [event] = self.answers()
        self.assertTrue(event.text.startswith('תשובה חיה נשלחה: '), event.text)   # the cron's look reads this line as an answer that left
        self.assertIn('האוטומציה kogo_Holidays לא נשלחה — ', event.text)

    def test_a_text_that_did_not_leave_is_followed_by_nothing(self):
        self.manychat_message('יש שיעורים בחנוכה בראש העין?')
        with patch(REQUEST, side_effect=ManyChatError('Server error', status_code=500)) as request, self.assertLogs('apps.wahub.sending', level='WARNING'):
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'failed')
        self.assertEqual(flows_sent(request), [])
        self.assertNotIn('flows', response.data)
        self.assertEqual(Message.objects.filter(contact_id=self.contact_id, direction='out', message_type='template').count(), 0)

    def test_no_board_no_automation_and_the_office_is_asked(self):
        settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {})              # ManyChat was never heard to have it
        self.manychat_message('יש שיעורים בחנוכה בראש העין?')
        with patch(REQUEST, return_value=OK) as request:
            response = self.reply()
        self.assertEqual(response.data['outcome'], 'sent')
        self.assertEqual(flows_sent(request), [])
        self.assertEqual(len(sends(request)), 1)                                  # "בודקת מול המשרד", or the office's closed line
        self.assertTrue(self.owner().needs_human)

    def test_the_new_bot_sends_an_automation_only_to_a_live_test_contact_and_through_every_door(self):
        self.incoming('שלום', phone='972505550102', subscriber_id='9')
        plain = Contact.objects.get(phone='972505550102')
        demo = Contact.objects.create(phone='972505559001', is_demo=True, live_test=True, last_inbound_at=timezone.now())
        with patch(REQUEST) as request:
            for contact in (plain, demo):
                with self.assertRaises(ValueError):
                    sending.send_new_bot_flow(contact, 'content-1', 'לוח החופשות')
            # A developer's machine only records, also for the live-test contact.
            with override_settings(DEBUG=True, WAHUB_SIMULATE_SEND=True):
                recorded = sending.send_new_bot_flow(self.owner(), 'content-1', 'לוח החופשות (kogo_Holidays)')
        request.assert_not_called()
        self.assertEqual((recorded.status, recorded.message_type, recorded.new_bot, recorded.sender), ('simulated', 'template', True, 'bot'))
        # The test turned off in the seconds the answer took: nothing leaves at all (live_test._answer looks again before it sends).
        self.manychat_message('יש שיעורים בחנוכה בראש העין?')
        real = shadow.answer_now

        def turned_off_meanwhile(contact, **options):
            proposal = real(contact, **options)
            Contact.objects.filter(pk=contact.pk).update(live_test=False)
            return proposal

        with patch.object(shadow, 'answer_now', side_effect=turned_off_meanwhile), patch(REQUEST) as request:
            self.assertEqual(self.reply().data['outcome'], 'not_live_test')
        request.assert_not_called()


# --- the language an answer is owed in -------------------------------------------------------------------------------------

class OwedLanguageTests(SimpleTestCase):
    ENGLISH = 'Hi, do you have capoeira classes for a 5 year old?'
    ARABIC = 'مرحبا، هل يوجد عندكم دورات للأطفال بعمر خمس سنوات؟'
    AD = 'Hello! Can I get more info on this?'

    def moment(self, reply, now, before=(), **extra):
        said = dict(customer=[now], customer_all='\n'.join([*before, now]), customer_now='\n'.join([*before, now]), customer_before=list(before))
        return Moment(reply=reply, **{**said, **extra})

    def owed(self, now, before=()):
        return checks.owed_language(self.moment('', now, before))

    def failed(self, reply, now, before=()):
        rows = checks.run(checks.GENERAL, self.moment(reply, now, before), turn=1)
        return {row['id']: row['explain'] for row in rows if row['status'] == 'failed'}

    def test_hebrew_until_he_goes_on_in_his_own_language_or_asks(self):
        self.assertEqual(self.owed(self.ENGLISH), '')                                           # a first message in English
        self.assertEqual(self.owed('Could you tell me where the classes are?', [self.ENGLISH]), 'latin')   # he goes on: the second in a row
        self.assertEqual(self.owed('ok', [self.ENGLISH]), 'latin')
        self.assertEqual(self.owed('اسكن في بيتح تكفا، هل يوجد شيء قريب؟', [self.ARABIC]), 'arabic')
        self.assertEqual(self.owed(self.ARABIC), '')
        # Back to Hebrew is Hebrew; and a language is not owed for an earlier turn in another one.
        self.assertEqual(self.owed('היי יש חוגים?', [self.ENGLISH]), '')
        self.assertEqual(self.owed(self.ENGLISH, ['היי, יש לכם חוגים לילד בן חמש?']), '')
        self.assertEqual(self.owed(self.ENGLISH, [self.ARABIC]), '')
        # The line an ad writes is not a message in another language: the one after it is his first.
        self.assertEqual(self.owed(self.AD), '')
        self.assertEqual(self.owed('Do you have classes for kids aged five?', [self.AD]), '')
        self.assertEqual(self.owed('We live in the city center, anything close?', [self.AD, 'Do you have classes for kids aged five?']), 'latin')

    def test_asked_for_is_owed_from_the_first_answer(self):
        for text, language in (
            ("Hi! We just moved and I don't speak Hebrew. Do you have classes?", 'latin'), ('English please', 'latin'),
            ('Can you answer in English?', 'latin'), ('היי, אפשר לענות לי באנגלית בבקשה?', 'latin'),
            ('Здравствуйте! Можно по-русски? Есть ли у вас кружки?', 'cyrillic'), ('Я не говорю на иврите, есть кружки для ребёнка?', 'cyrillic'),
            ('مرحبا، لا أتكلم العبرية، هل توجد دورات؟', 'arabic'), ('אפשר לדבר בערבית?', 'arabic'),
        ):
            self.assertEqual(self.owed(text), language, text)
        # A class in English is not a request to be answered in English, nor is Hebrew with a few foreign words.
        self.assertEqual(self.owed('יש לכם חוג באנגלית לילדים?'), '')
        self.assertEqual(self.owed('יש לכם trial lesson בקפוארה? ה-kid בן 5'), '')
        # Asked once, it stays: a later short line is answered in it too.
        self.assertEqual(self.owed('ok thanks', ['English please']), 'latin')

    def test_the_check_holds_the_answer_to_it(self):
        hebrew, english = 'באיזו עיר מדובר?', 'Sure! Which city are you looking in?'
        self.assertEqual(self.failed(hebrew, self.ENGLISH), {})
        first = self.failed(english, self.ENGLISH)
        self.assertIn('ברירת המחדל עברית', first['hebrew_only'])
        self.assertIn('Sure', first['hebrew_only'])
        self.assertEqual(self.failed(english, 'Where are the classes?', [self.ENGLISH]), {})
        second = self.failed(hebrew, 'Where are the classes?', [self.ENGLISH])
        self.assertIn('והבוט ענה בעברית', second['hebrew_only'])
        self.assertIn('כשהוא ממשיך לכתוב בה אחרי תשובה בעברית, או כשביקש', second['hebrew_only'])
        self.assertIn('אותיות לועזיות', self.failed('في أي مدينة تبحثون عن دورة؟', self.ARABIC)['hebrew_only'])
        # The script's own check: Hebrew, and a language.
        self.assertEqual(checks.answers_in('hebrew').test(self.moment(hebrew, self.ENGLISH)), '')
        self.assertIn('Sure', checks.answers_in('hebrew').test(self.moment(english, self.ENGLISH)))
        self.assertIn('ציפינו לתשובה בעברית', checks.answers_in('hebrew').test(self.moment('👍', self.ENGLISH)))
        self.assertEqual(checks.answers_in('latin').test(self.moment(english, 'x', [self.ENGLISH])), '')
        # A question asked in the language that is owed is the question — also when he asked for the language in Hebrew.
        asked = self.moment(english, 'היי, אפשר לענות לי באנגלית בבקשה?')
        self.assertEqual(checks.asks('city').test(asked), '')
        self.assertEqual(rules.GENERAL_COVERS['hebrew_only'], ('M2', 'X1'))
        self.assertIn('ממשיך בה אחרי תשובה בעברית', rules.RULES['X1'])


# --- the checks of the holidays board ------------------------------------------------------------------------------------

class BoardChecksTests(SimpleTestCase):
    FOUND = {'name': 'holiday_dates', 'input': {'branch': 'מינץ'}, 'result': {
        'found': True, 'need': '', 'flow_name': MAIN, 'send_flow': {'name': MAIN, 'ns': 'content-1', 'label': 'לוח החופשות'},
    }}
    NONE = {'name': 'holiday_dates', 'input': {'branch': 'פסגות אפק'}, 'result': {'found': False, 'need': '', 'flow_name': ''}}
    WORLD = {'facts': {
        'holiday': {'main': {'flow': MAIN}, 'other': {'flow': YAHUD}, 'stage': {'branch': 'סטודיו', 'flow': ''}, 'has': {'branch': 'מינץ', 'flow': MAIN}},
        'office_phone': '0509424755', 'camp': {'asked': {'who': 'דני', 'phone': '050-0000001'}},
    }}

    def moment(self, reply, calls=(), **extra):
        return Moment(reply=reply, calls=list(calls), world=self.WORLD, **extra)

    def general(self, reply, calls=()):
        rows = checks.run(checks.GENERAL, self.moment(reply, calls, customer=['יש שיעורים בחנוכה?'], customer_all='יש שיעורים בחנוכה?', customer_now='יש שיעורים בחנוכה?'), turn=1)
        return {row['id']: row['explain'] for row in rows if row['status'] == 'failed'}

    def test_a_board_that_is_said_to_be_sent_is_sent(self):
        said = 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים'
        self.assertEqual(self.general(said, [self.FOUND]), {})
        for calls in ([], [self.NONE]):
            self.assertIn('לא נשלחת שום אוטומציה', self.general(said, calls)['board_really_sent'])
        self.assertIn('board_really_sent', self.general('לוח החופשות בדרך אליכם'))
        # Speaking of the board without saying it is on its way is not a promise.
        self.assertEqual(self.general('את זה אני בודקת מול המשרד, ונחזור אליכם עם תשובה', [self.NONE]), {})
        # A board for a customer who asked nothing about holidays is a picture nobody wanted.
        price = 'כמה עולה החוג במינץ?'
        unasked = checks.run(checks.GENERAL, self.moment('באיזה תחום מדובר?', [self.FOUND], customer=[price], customer_all=price, customer_now=price), turn=1)
        self.assertIn('בלי שהלקוח שאל על חגים', next(row['explain'] for row in unasked if row['id'] == 'board_really_sent'))
        later = 'יש שיעור בחג?\nמינץ'                                 # asked a turn ago, and named the branch now
        asked = checks.run(checks.GENERAL, self.moment('שולחת לכם את לוח החופשות', [self.FOUND], customer=['מינץ'], customer_all=later, customer_now=later), turn=2)
        self.assertEqual([row['explain'] for row in asked if row['status'] == 'failed'], [])
        self.assertEqual(rules.GENERAL_COVERS['board_really_sent'], ('N5',))
        self.assertIn('board_really_sent', checks.GENERAL_IDS)

    def test_the_right_board_no_board_and_the_board_of_a_registered_childs_branch(self):
        self.assertEqual(checks.sends_board('holiday.main.flow').test(self.moment('', [self.FOUND])), '')
        self.assertIn('לא נשלחת שום אוטומציה', checks.sends_board('holiday.main.flow').test(self.moment('בחנוכה יש שיעורים כרגיל')))
        self.assertIn(f'הבוט ביקש לשלוח: {MAIN}', checks.sends_board('holiday.other.flow').test(self.moment('', [self.FOUND])))
        self.assertIn('אי אפשר לבדוק', checks.sends_board('holiday.nowhere.flow').test(self.moment('', [self.FOUND])))
        self.assertEqual(checks.no_board().test(self.moment('באיזה סניף מדובר?', [self.NONE])), '')
        self.assertIn(MAIN, checks.no_board().test(self.moment('', [self.FOUND])))
        registered = checks.board_as_the_system_has_it('holiday.stage')
        self.assertEqual(registered.test(self.moment('בודקת', [self.NONE], request_human=True)), '')
        self.assertIn('יסמן לנציג', registered.test(self.moment('בודקת', [self.NONE])))
        self.assertIn('אין לוח חופשות מחובר, והבוט ביקש לשלוח', registered.test(self.moment('', [self.FOUND], request_human=True)))
        has = checks.board_as_the_system_has_it('holiday.has')
        self.assertEqual(has.test(self.moment('', [self.FOUND])), '')
        self.assertIn('לא נשלחת שום אוטומציה', has.test(self.moment('', [self.NONE], request_human=True)))

    def test_no_dates_and_phones(self):
        self.assertEqual(checks.no_dates().test(self.moment('שולחת לכם את לוח החופשות')), '')
        self.assertIn('06.12', checks.no_dates().test(self.moment('חנוכה ב-06.12 עד 11.12, הסטודיו פתוח')))
        self.assertEqual(checks.no_dates_but(r'28\.0?7').test(self.moment('הפעילות מסתיימת ב-28.07')), '')
        self.assertIn('01.07', checks.no_dates_but(r'28\.0?7').test(self.moment('הקייטנה מתחילה ב-01.07')))
        self.assertEqual(checks.no_phones().test(self.moment('המשרד יעביר אליו את הבקשה')), '')
        self.assertIn('0521234567', checks.no_phones().test(self.moment('המספר שלו 052-1234567')))
        office = checks.no_phones('office_phone')
        self.assertEqual(office.test(self.moment('אפשר גם להתקשר למשרד 📞 050-9424755')), '')
        self.assertIn('0521234567', office.test(self.moment('050-9424755 או 052-1234567')))
        camp = checks.says_phone('camp.asked.phone', 'המספר של הקייטנה')
        self.assertEqual(camp.test(self.moment('הקייטנה אצל דני 📞 050-0000001')), '')
        self.assertIn('ציפינו', camp.test(self.moment('הקייטנה אצל דני')))
        self.assertIn('0500000002', camp.test(self.moment('050-0000001 או 050-0000002')))


# --- the knowledge ----------------------------------------------------------------------------------------------------------

class TheAnswersInTheKnowledgeTests(WahubTestCase):
    SAID = 'בעל המערכת 13.10.2026'

    def test_what_is_new_is_filed_under_his_name_and_what_he_changed_is_rewritten_where_it_stood(self):
        knowledge_import.run()
        # The waiting list is his word of the same day too; it is held by its own tests (test_waiting_list).
        waiting = 'שיעור מלא — מועד אחר, ואז רשימת המתנה'
        # ... and so is what he said in the evening about "הסר": the rule and its phrasing (test_optout).
        evening = ('"הסר" — מאשרים בשורה אחת', 'אישור "הסר"')
        new = {
            item.title: item
            for item in KnowledgeItem.objects.filter(source_note__contains=self.SAID).exclude(title__in=(waiting, *evening))
        }
        ceremony_title = 'טקס סוף שנה — חגורה, תעודה ומתנה קטנה'
        self.assertEqual(set(new), {
            'חגים וחופשות — שולחים את לוח החופשות של הסניף', 'אין מנהל סניף — הכול דרך המשרד', 'קייטנה בקיץ — לפי סניף, אצל המדריך',
            ceremony_title,                                          # the same day, at noon
        })
        ceremony = new.pop(ceremony_title)
        manager, holidays, camp = (new[title] for title in sorted(new))
        # The ceremony at the end of the year: one fact, said only when asked, ready to be said as it is — short, the
        # sum in bold, no full stop — and with no date in it, because none is set.
        self.assertEqual((ceremony.kind, ceremony.when_to_say, ceremony.scope_level, ceremony.is_active), ('fact', 'if_asked', 'business', True))
        self.assertIn('בעל המערכת 13.10.2026 (צהריים)', ceremony.source_note)
        self.assertEqual(ceremony.body.splitlines(), [
            'בסוף השנה יש טקס 🎉 העלות היא *₪60*, עבור חגורה, תעודה ומתנה קטנה',
            'את התאריך וכל הפרטים, מתי ואיך, נעדכן בהמשך השנה',
        ])
        self.assertFalse(ceremony.body.endswith('.'))
        self.assertEqual((checks.amounts_in(ceremony.body), checks.dates_in(ceremony.body)), ([60], []))
        for asked in ('כמה עולה חגורה', 'מתי מקבלים חגורה', 'יש תעודה', 'טקס החגורות', 'טקס סוף שנה'):
            self.assertIn(asked, ceremony.data['question'])
        # …and the rule about selling sends a question about a belt there, not to the shop. No sum is written in it.
        selling = KnowledgeItem.objects.get(title='לא להציע רכישה מיוזמתך')
        for words in ('"טקס סוף שנה"', 'לא מפנים לחנות ולא למדריך', 'תאריך לא אומרים'):
            self.assertIn(words, selling.body)
        self.assertNotIn('₪', selling.body)
        self.assertEqual((holidays.kind, manager.kind, camp.kind, camp.when_to_say, camp.scope_level), ('behavior_rule', 'behavior_rule', 'fact', 'if_asked', 'business'))
        for words in ('holiday_dates', 'לא מפרטים תאריכי חופשה בעצמנו', 'missing_info', 'כשלא ידוע באיזה סניף — שואלים קודם'):
            self.assertIn(words, holidays.body)
        for words in ('branch_info', 'לסניפים אין מנהל', 'שם של מנהל לא אומרים ולא ממציאים', 'הטלפון של כל הסניפים הוא הטלפון של המשרד', 'request_human'):
            self.assertIn(words, manager.body)
        # Three branches with a camp, each with who runs it and a number; everywhere else there is none for now.
        self.assertEqual([row[0].strip() for row in world._CAMP_LINE.findall(camp.body)], ['אם המושבות', 'מינץ', 'ראש העין (קרל וגרטי קורי)'])
        self.assertIn('בשאר הסניפים אין כרגע קייטנה', camp.body)
        self.assertIn('מספרי הקייטנה', KnowledgeItem.objects.get(title='פרטים של אנשים אחרים לא נמסרים').body)

        rewritten = {
            'עברית בלבד': 'לשפה אחרת עוברים רק לפי הכלל',
            'שפה: עברית, עד שהלקוח ממשיך בשפה שלו או מבקש': 'התשובה הראשונה בעברית, קצרה ופשוטה',
            'חופשות: לא מפרטים תאריכים — שולחים את לוח החופשות': 'הימים המיוחדים שבידע הם לשעות המשרד בלבד',
            'היעדרות, השלמה וזיכוי': 'רק עם אישור רפואי',
            'ניסיון נוסף, ו"הילד לא רצה"': 'לשיעור ניסיון שני לא ניתן להירשם',
            'לא נשארים עם הילד בשיעור': 'השיעור הוא הרגע של הילדים',
            'דמי רישום — כל שנה ולכל ילד': 'עבור הביטוח, ועבור שמירת המקום של הילד',
            'יש שיעור היום? — לפי הביטולים שבמערכת': 'שולחים את לוח החופשות של הסניף (holiday_dates)',
            'מתי — רק מהמערכת': 'לוח החופשות של הסניף (holiday_dates)',
        }
        for title, words in rewritten.items():
            self.assertIn(words, KnowledgeItem.objects.get(title=title, is_active=True).body, title)
        # The two rules of the language no longer say what the owner did not approve; "אתה בוט?" stands as it was.
        self.assertFalse(KnowledgeItem.objects.filter(title='עונים בשפה שבה הלקוח כותב').exists())
        self.assertFalse(KnowledgeItem.objects.filter(body__contains='מקבל תשובה באותה שפה').exists())
        self.assertIn('העוזרת הדיגיטלית של קוגומלו', KnowledgeItem.objects.get(title='בוט או אדם — עונים בכנות').body)
        # No price and no hour is written into a rule: those are Kogo's.
        for record in knowledge_seed.OWNER_13_10:
            self.assertNotRegex(record['body'], r'₪|\d{1,2}:\d{2}', record['title'])

    def test_the_rows_production_holds_are_brought_to_the_new_text_and_title_and_an_edited_one_is_left(self):
        knowledge_import.run()
        language = KnowledgeItem.objects.get(title='שפה: עברית, עד שהלקוח ממשיך בשפה שלו או מבקש')
        vacations = KnowledgeItem.objects.get(title='חופשות: לא מפרטים תאריכים — שולחים את לוח החופשות')
        absence = KnowledgeItem.objects.get(title='היעדרות, השלמה וזיכוי')
        hebrew = KnowledgeItem.objects.get(title='עברית בלבד')
        trial = KnowledgeItem.objects.get(title='ניסיון נוסף, ו"הילד לא רצה"')
        # Imported under the old title: the key the import finds the row by has not moved.
        self.assertTrue(language.source_note.endswith('עונים בשפה שבה הלקוח כותב'))
        self.assertTrue(vacations.source_note.endswith('חופשות: רק עתידיות, בפורמט קבוע'))
        today = {item.pk: (item.title, item.body) for item in (language, vacations, absence, hebrew, trial)}

        # As production holds them since 12.10.2026: the old titles and texts, version 1 — and for "עברית בלבד",
        # version 2 by the seed's own rewrite of that day.
        KnowledgeItem.objects.filter(pk=language.pk).update(title='עונים בשפה שבה הלקוח כותב', body='לקוח שכותב באנגלית מקבל תשובה באותה שפה')
        KnowledgeItem.objects.filter(pk=vacations.pk).update(title='חופשות: רק עתידיות, בפורמט קבוע', body='מציגים רק אירועים שעוד לפנינו', example_good='🔹 *21.04* פסח')
        KnowledgeItem.objects.filter(pk=absence.pk).update(body='בידע אין כלל על זה')
        KnowledgeHistory.objects.filter(item=hebrew).delete()
        KnowledgeItem.objects.filter(pk=hebrew.pk).update(version=1, body='ישן')
        hebrew.refresh_from_db()
        first = 'בדיקות קיצון 12.10.2026: לקוח שכתב באנגלית שאינו קורא עברית קיבל תשובה בעברית; עונים בשפה שבה הלקוח כותב'
        knowledge.update_item(hebrew, {'body': 'לקוח שכותב בשפה אחרת מקבל תשובה בשפה שלו'}, None, note=first)
        # …and one the owner has taught the bot since, in his own words.
        trial.refresh_from_db()
        knowledge.update_item(trial, {'body': 'הנוסח של בעל המערכת'}, self.manager, note='לימוד מהמסך')

        result = knowledge_import.run()
        self.assertEqual((result['created_total'], result['revised'], result['revision_left']), (0, 4, ['ניסיון נוסף, ו"הילד לא רצה"']))
        for item in (language, vacations, absence, hebrew):
            item.refresh_from_db()
            self.assertEqual((item.title, item.body), today[item.pk])
        self.assertEqual((language.version, vacations.version, absence.version, hebrew.version, vacations.example_good), (2, 2, 2, 3, ''))
        note = KnowledgeHistory.objects.get(item=language, version=2)
        self.assertTrue(note.note.startswith(self.SAID))
        self.assertEqual(note.before['title'], 'עונים בשפה שבה הלקוח כותב')
        trial.refresh_from_db()
        self.assertEqual(trial.body, 'הנוסח של בעל המערכת')
        # Run again: nothing more to do, and no second row under the new title.
        again = knowledge_import.run()
        self.assertEqual((again['created_total'], again['revised']), (0, 0))
        self.assertEqual(KnowledgeItem.objects.filter(title__startswith='שפה: עברית').count(), 1)

    def test_every_rule_of_that_day_has_a_conversation(self):
        answers = [scenario for scenario in scenarios.SCENARIOS if scenario.group == 'answers']
        self.assertGreaterEqual(len(answers), 15)
        used = {rule for scenario in scenarios.SCENARIOS for rule in scenario.rules}
        self.assertLessEqual({'N1', 'N2', 'N3', 'N4', 'N5', 'X1', 'X6', 'E18', 'D14c', 'E14'}, used)
        self.assertEqual({rule for rule in rules.RULES if rule.startswith('N')}, {'N1', 'N2', 'N3', 'N4', 'N5', 'N6'})
        self.assertEqual(scenarios.BY_ID['ceremony_belt_cost_and_date'].rules[0], 'N6')          # the same day, at noon
        self.assertIn('N6', scenarios.BY_ID['ground_selling_a_belt'].rules)
        self.assertEqual(dict(scenarios.GROUPS)['answers'], scenarios.GROUP_TITLES['answers'])
        self.assertEqual(scenarios.BY_ID['holiday_board_of_the_branch'].group, 'answers')
        self.assertIn('answers', {group['id'] for group in runner.listing()['groups']})


# --- the conversations of 13.10.2026, with a bot that answers well and with one that answers badly -----------------------------

def board(place=None):
    return ('tool', 'holiday_dates', {'branch': place} if place else {})


def good_answers(built: dict) -> dict:
    """What a bot that answers well does in each turn of each conversation of that day, on the invented catalogue."""
    words, facts = built['words'], built['facts']
    camp = facts['camp']['asked']
    return {
        'lang_ad_line_is_not_a_sign': [
            [say('היי במה אוכל לעזור')], [find(), say('באיזו עיר מדובר?')], [find(), say('Sure — which city are you looking in?')],
        ],
        'lang_asks_in_hebrew_for_english': [[find(), say('Sure! Which city are you looking in?')]],
        'manager_who_and_how_to_reach': [[
            person('מבקשת לדבר עם מנהלת הסניף'), ('tool', 'branch_info', {'branch': words['manager_place']}),
            say('את הסניפים מלווה המשרד שלנו. העברתי את הבקשה, ויחזרו אליכם'),
        ]],
        'absence_one_time_make_up': [[person('השלמת שיעור לילד חולה'), say(
            'רפואה שלמה 🙏 בגדול אפשר להשלים בשיעור אחר באותו שבוע, באותו תחום ובאותה קבוצת גיל\nהמשרד מאשר ומתאם, ויחזרו אליכם',
        )]],
        'absence_unclear_how_often': [[say('זו היעדרות חד-פעמית, או משהו שחוזר?')]],
        'absence_again_and_again': [[person('היעדרויות שחוזרות'), say('מבינה, זה לא פשוט. העברתי את זה למשרד, ויחזרו אליכם לבדוק מה אפשר')]],
        'absence_credit_needs_a_doctor': [[person('זיכוי על היעדרות'), say('רפואה שלמה 🙏 זיכוי ניתן רק עם אישור רפואי, והמשרד מטפל בזה — העברתי אליהם, ויחזרו אליכם')]],
        'camp_in_a_branch_that_has_one': [[say(f'את הקייטנה ב{camp["branch"]} מנהל {camp["who"]}, וכל הפרטים אצלו\n📞 {camp["phone"]}')]],
        'camp_which_branch': [[say('באיזה סניף מדובר?')]],
        'camp_in_a_branch_that_has_none': [[say(f'ב{words["no_camp_place"]} אין כרגע קייטנה. אם תהיה, המדריך של הסניף יעדכן')]],
        'parents_watch_the_lesson': [[say('השיעור הוא הרגע של הילדים, בלי הורים — ככה קל להם להתרכז\nבהתחלה, אם הוא ממש צריך, המדריך מחליט')]],
        'ceremony_belt_cost_and_date': [
            [say('בסוף השנה יש טקס 🎉 העלות היא *₪60*, עבור חגורה, תעודה ומתנה קטנה\nאת התאריך וכל הפרטים, מתי ואיך, נעדכן בהמשך השנה')],
            [say('התאריך עוד לא נקבע 🙂 נודיע לכולם כשיהיה')],
        ],
        'holiday_board_of_the_branch': [[board(words['holiday_place']), say(lambda found: found['say'])]],
        'holiday_board_of_another_branch': [[board(words['holiday_other_place']), say(lambda found: found['say'])]],
        'holiday_which_branch': [[board(), say(lambda found: found['ask'])]],
        'holiday_branch_with_no_board': [[person('אין לוח חופשות לסניף'), board(words['holiday_none_place']), say(MISSING)]],
        'holiday_registered_customer': [[person('אין לוח חופשות לסניף'), board(), say(MISSING)]],
        # the evening of the same day: "הסר" is one line that confirms; a child taken out of a class is a cancellation
        'opt_out_one_line': [[say('הוסרתם מרשימת התפוצה, לא נשלח יותר הודעות 🙏')]],
        'opt_out_is_not_a_cancellation': [[person('מבקשת לבטל את החוג של הבן'), say('העברתי את בקשת הביטול למשרד, ויחזרו אליכם')]],
    }


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL=MODEL, REGISTRATION_FEE_ILS=120)
class TheConversationsOfThatDayTests(EvalTestCase):
    ANSWERS = tuple(scenario for scenario in scenarios.SCENARIOS if scenario.group == 'answers')

    def play(self, scenario_id, turns):
        claude = FakeClaude(*[step for turn in turns for step in turn])
        with patch(POST, side_effect=claude):
            run = runner.run_to_end(scenario_id, with_judge=False)
        self.assertEqual(claude.steps, [], scenario_id)
        return run

    def test_the_invented_catalogue_fills_every_word_of_that_day(self):
        built = world.build()
        self.assertEqual(
            [built['words'][key] for key in ('holiday_place', 'holiday_other_place', 'holiday_none_place', 'camp_place', 'no_camp_place', 'manager_place')],
            ['אם המושבות - הדגמה', 'יהוד', PLACE, 'אם המושבות', 'יהוד', PLACE],
        )
        holiday = built['facts']['holiday']
        self.assertEqual((holiday['main']['flow'], holiday['other']['flow'], holiday['none']['flow'], holiday['stage']['flow']), (MAIN, YAHUD, '', ''))
        self.assertEqual(built['facts']['manager']['name'], 'גלית בדויה')
        self.assertEqual((built['facts']['camp']['asked']['branch'], len(built['facts']['camp']['rows'])), ('אם המושבות', 3))
        self.assertEqual({scenario.id for scenario in self.ANSWERS}, set(good_answers(built)))
        self.assertEqual([runner._missing(scenario, built) for scenario in self.ANSWERS], [[]] * len(self.ANSWERS))
        # With nothing heard from ManyChat the conversations that need a board are skipped, and say why.
        settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {})
        skipped = runner.start('holiday_board_of_the_branch')
        self.assertEqual(skipped.status, 'skipped')
        self.assertIn('kogo_Holidays', skipped.detail)

    def test_a_bot_that_answers_well_passes_every_check_of_every_conversation_of_that_day(self):
        for scenario_id, turns in good_answers(world.build()).items():
            with self.subTest(scenario=scenario_id):
                self.assertEqual(len(turns), len(scenarios.BY_ID[scenario_id].turns))
                run = self.play(scenario_id, turns)
                failures = [(row['turn'], row['id'], row['explain']) for row in run.checks if row['status'] == 'failed']
                self.assertEqual((run.status, failures), ('passed', []), [turn['reply'] for turn in run.turns])

    def test_a_bot_that_answers_badly_fails_every_turn_that_expects_something(self):
        # This test is of the checks: the worst answer there is, held against them as it was written. Since 14.10.2026
        # an answer like it never gets this far — the money guard (shadow.py) stops its made-up ₪99 before it is kept,
        # and the customer is sent the office's line (test_money_guard) — so here the guard stands aside.
        self.enterContext(patch('apps.wahub.shadow.money_without_source', return_value=None))
        for scenario in self.ANSWERS:
            with self.subTest(scenario=scenario.id):
                loud = self.play(scenario.id, [[say(LOUD)] for _turn in scenario.turns])
                quiet = self.play(scenario.id, [[say(QUIET)] for _turn in scenario.turns])
                self.assertFalse(any('הבדיקה עצמה נכשלה' in row['explain'] for row in loud.checks + quiet.checks))
                self.assertEqual(loud.status, 'failed')
                for number, turn in enumerate(scenario.turns, 1):
                    own = {check.id for check in turn.expect}
                    caught = {row['id'] for run in (loud, quiet) for row in run.checks if row['turn'] == number and row['status'] == 'failed' and row['id'] in own}
                    self.assertTrue(caught, f'turn {number}: no check of its own catches a bad answer')

    def test_the_wrong_board_dates_from_its_own_head_and_a_promise_with_nothing_behind_it(self):
        words = world.build()['words']
        wrong = self.play('holiday_board_of_another_branch', [[board(words['holiday_place']), say(lambda found: found['say'])]])
        self.assertEqual(self.failed(wrong), {'sends_board:holiday.other.flow'})
        recited = self.play('holiday_board_of_the_branch', [[say('בחנוכה, 06.12 עד 11.12, הסטודיו פתוח כרגיל')]])
        self.assertLessEqual({'called:holiday_dates', 'sends_board:holiday.main.flow', 'no_dates'}, self.failed(recited))
        promised = self.play('holiday_branch_with_no_board', [[board(words['holiday_none_place']), say('שולחת לכם את לוח החופשות של הסניף')]])
        self.assertLessEqual({'board_really_sent', 'human:True'}, self.failed(promised))
        english_at_once = self.play('limit_english', [[find(), say('Sure! Which city are you looking in?')], [find(), say('באיזו עיר מדובר?')]])
        self.assertEqual(self.failed(english_at_once), {'hebrew_only', 'answers_in:hebrew', 'answers_in:latin'})

    @override_settings(ANTHROPIC_API_KEY='')
    def test_the_stub_sends_the_board_asks_which_branch_and_goes_to_the_office(self):
        sent = runner.run_to_end('holiday_board_of_the_branch')
        self.assertEqual((sent.status, sent.turns[0]['reply']), ('passed', 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים'))
        shown = runner.run_payload(sent)['conversation'][0]['tools'][0]
        self.assertIn('אחרי התשובה נשלחת האוטומציה kogo_Holidays', shown['summary'])
        self.assertEqual(runner.run_to_end('holiday_board_of_another_branch').status, 'passed')
        self.assertEqual(runner.run_to_end('holiday_which_branch').turns[0]['reply'], 'באיזה סניף מדובר?')
        for scenario_id in ('holiday_branch_with_no_board', 'holiday_registered_customer'):
            none = runner.run_to_end(scenario_id)
            self.assertEqual((none.status, none.turns[0]['request_human'], none.turns[0]['reply']), ('passed', True, MISSING), scenario_id)
        # A run of the self-checks sends nothing and writes into no conversation.
        self.deliver.assert_not_called()
        self.assertEqual((Contact.objects.count(), Message.objects.count(), ShadowReply.objects.count()), (0, 0, 0))
        json.dumps(runner.run_payload(sent), ensure_ascii=False)
