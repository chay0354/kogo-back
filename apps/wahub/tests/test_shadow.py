"""The shadow bot: the context, the burst, the tools on invented Kogo data, the stub, Claude behind a mock — and that nothing is ever sent."""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import cron, knowledge_import, shadow
from apps.wahub.knowledge_seed import WEEKEND_MESSAGE
from apps.wahub.models import KnowledgeItem, KnowledgeProposal, Message, ShadowReply, TrialQuestion
from apps.wahub.tests.base import BASE, PHONE, WahubTestCase

POST = 'apps.wahub.shadow.requests.post'
SEND = 'apps.wahub.sending._deliver'
HANDOFF = 'apps.wahub.handoff._in_manychat'


def catalogue():
    """Invented Kogo data: one branch of ours with one class, and one external branch."""
    city = TestDataFactory.create_city('ראש העין')
    ours = TestDataFactory.create_branch(name='קרל וגרטי קורי 8', city=city, address='קרל וגרטי קורי 8, ראש העין', phone='03-5550000')
    kind = TestDataFactory.create_course_type('קפוארה')
    course = TestDataFactory.create_course(name='קפוארה צעירים', branch=ours, course_type=kind, price=Decimal('260.00'), min_age=5, max_age=7, capacity=12)
    instructor = TestDataFactory.create_instructor(first_name='משה', last_name='לוי', branch=ours)
    lesson = TestDataFactory.create_lesson(course=course, instructor=instructor, day_of_week=2, start_time=time(17, 0), end_time=time(17, 45))
    external = TestDataFactory.create_branch(
        name='רמת גן - גאולים', city=TestDataFactory.create_city('רמת גן'), is_external=True, external_link='https://ramat-gan.example/signup',
    )
    return ours, course, lesson, external


class ShadowTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        self.ours, self.course, self.lesson, self.external = catalogue()
        # The shadow may call neither of these, ever.
        self.deliver = patch(SEND, side_effect=AssertionError('the shadow tried to send')).start()
        self.addCleanup(patch.stopall)
        self.handoff = patch(HANDOFF, side_effect=AssertionError('the shadow tried to touch ManyChat')).start()

    def propose(self, phone=PHONE):
        contact = self.contact(phone)
        return shadow.propose(contact)


@override_settings(ANTHROPIC_API_KEY='')
class StubTests(ShadowTestCase):
    def test_a_greeting_gets_the_opening_phrasing_with_the_name(self):
        self.incoming('היי', name='רותם')
        reply = self.propose()
        self.assertEqual(reply.text, 'היי *רותם*, במה אוכל לעזור?')
        self.assertEqual(reply.model, 'stub')
        greeting = KnowledgeItem.objects.get(kind='phrasing', key='greeting')
        self.assertIn(greeting.id, reply.knowledge_used)
        self.assertIn('stub', reply.reasoning)
        contact = self.contact()
        self.assertFalse(contact.needs_shadow)
        self.assertIsNotNone(contact.last_shadow_at)
        self.assertEqual(reply.after_message_id, Message.objects.get().id)

    def test_a_class_question_is_answered_from_kogo_through_find_courses(self):
        self.incoming('יש חוג קפוארה לבן 6 בראש העין?')
        reply = self.propose()
        self.assertIn('שלישי', reply.text)
        self.assertIn('17:00', reply.text)
        self.assertIn('משה לוי', reply.text)
        self.assertNotIn('₪', reply.text)          # nobody asked the price
        self.assertEqual([tool['name'] for tool in reply.tools_used], ['find_courses'])
        self.assertIn('1 חוגים', reply.tools_used[0]['summary'])
        self.assertIn('find_courses', reply.reasoning)
        self.assertFalse(reply.text.endswith('.'))

    def test_a_price_question_names_the_price_from_the_course_card(self):
        self.incoming('כמה עולה חוג קפוארה בראש העין לבן 6?')
        reply = self.propose()
        self.assertIn('₪260', reply.text)
        self.assertNotIn('260 ₪', reply.text)

    def test_an_external_branch_is_sent_to_the_municipality_with_the_link(self):
        self.incoming('כמה עולה החוג ברמת גן? ויש שיעור ניסיון?')
        reply = self.propose()
        self.assertIn('העירייה', reply.text)
        self.assertIn('https://cogomelo.co.il/הרשמה-לחוגים-שיעור-ניסיון/', reply.text)
        self.assertNotIn('₪', reply.text)
        self.assertIn('חיצוניים: רמת גן - גאולים', reply.tools_used[0]['summary'])

    def test_a_city_with_no_branch(self):
        self.incoming('יש לכם חוג קפוארה בנתניה?')
        reply = self.propose()
        self.assertIn('אין לנו סניף בנתניה', reply.text)

    def test_a_burst_of_messages_gets_one_answer_after_twenty_quiet_seconds(self):
        self.incoming('היי')
        self.incoming('יש חוג קפוארה בראש העין לבן 6?')
        contact = self.contact()
        self.assertTrue(contact.needs_shadow)
        self.assertFalse(shadow.pending().filter(pk=contact.pk).exists())     # still within the window
        self.age(contact, last_inbound_at=1)
        self.assertTrue(shadow.pending().filter(pk=contact.pk).exists())
        reply = shadow.propose(contact)
        ids = list(Message.objects.filter(contact=contact).order_by('id').values_list('id', flat=True))
        self.assertEqual(reply.covers_message_ids, ids)
        self.assertEqual(reply.after_message_id, ids[-1])
        self.assertIn('2 הודעות רצופות', reply.reasoning)
        self.assertIn('שלישי', reply.text)
        self.assertEqual(ShadowReply.objects.count(), 1)

    def test_a_short_yes_is_read_against_the_last_message_the_system_sent(self):
        self.incoming('רוצים שיעור ניסיון בראש העין')
        contact = self.contact()
        Message.objects.create(
            contact=contact, direction='out', sender='office', sender_name='מיכל', text='תזכורת - שיעור ניסיון',
            message_type='template', status='sent', source='kogo',
        )
        shadow.propose(contact)
        self.incoming('כן')
        reply = shadow.propose(self.contact())
        self.assertEqual(reply.text, 'מעולה, רשמנו שאתם מגיעים! נתראה בשיעור 🥳')
        self.assertIn('תזכורת - שיעור ניסיון', reply.reasoning)

    def test_a_cancellation_question_gets_the_policy_and_a_request_gets_the_form(self):
        self.incoming('מה המדיניות ביטול אצלכם?')
        reply = self.propose()
        self.assertIn('הביטול נכנס לתוקף', reply.text)
        self.assertNotIn('https://studio-cancellation-form', reply.text)
        self.incoming('אני רוצה לבטל את החוג')
        reply = self.propose()
        self.assertIn('https://studio-cancellation-form.vercel.app/', reply.text)

    def test_asking_for_a_person_only_marks_and_sends_nothing(self):
        self.incoming('אפשר לדבר עם נציג בבקשה?')
        reply = self.propose()
        self.assertTrue(reply.request_human)
        self.assertIn('request_human', [tool['name'] for tool in reply.tools_used])
        self.assertTrue(reply.text)
        self.assertEqual(Message.objects.count(), 1)          # nothing was written to the conversation
        self.deliver.assert_not_called()
        self.handoff.assert_not_called()
        self.assertEqual(self.network.call_count, 0)

    def test_a_voice_message_gets_the_voice_phrasing(self):
        self.incoming('x')
        Message.objects.filter(contact=self.contact()).update(text='', message_type='voice')
        reply = self.propose()
        self.assertEqual(reply.text, 'לא ניתן לשמוע הודעות קוליות, בבקשה להשאיר הודעה כתובה')

    def test_nothing_to_answer_clears_the_mark(self):
        contact = self.make_contact(needs_shadow=True)
        self.assertIsNone(shadow.propose(contact))
        self.assertFalse(self.contact().needs_shadow)


@override_settings(ANTHROPIC_API_KEY='')
class ShadowApiTests(ShadowTestCase):
    def test_the_conversation_lists_proposals_beside_the_old_bots_answer(self):
        self.incoming('יש חוג קפוארה לבן 6 בראש העין?', name='רותם')
        self.bot_reply('באיזו עיר אתם?')
        contact = self.contact()
        shadow.propose(contact)
        rows = self.get(f'contacts/{contact.id}/shadow/').data
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['after_message_id'], Message.objects.filter(direction='in').get().id)
        self.assertEqual(row['old_bot_reply']['text'], 'באיזו עיר אתם?')
        self.assertIsNone(row['verdict'])
        self.assertIn('שלישי', row['text'])
        self.assertTrue(row['reasoning'])
        self.assertEqual(row['tools_used'][0]['name'], 'find_courses')
        self.assertTrue(all({'id', 'kind_label', 'title'} <= set(item) for item in row['knowledge_used']))
        self.assertEqual(row['model'], 'stub')

    def test_a_verdict_and_a_bad_one_with_a_note_becomes_a_proposal(self):
        self.incoming('כמה עולה החוג ברמת גן?')
        reply = self.propose()
        self.assertEqual(self.post(f'shadow/{reply.id}/verdict/', {'verdict': 'meh'}).status_code, 400)
        good = self.post(f'shadow/{reply.id}/verdict/', {'verdict': 'good'})
        self.assertEqual((good.status_code, good.data['verdict'], good.data['proposal_id']), (200, 'good', None))
        self.assertEqual(good.data['verdict_by_name'], 'דנה מנהלת')
        bad = self.post(f'shadow/{reply.id}/verdict/', {'verdict': 'bad', 'note': 'רמת גן זה סניף חיצוני, לא אומרים כלום על מחיר'})
        self.assertEqual(bad.status_code, 200)
        self.assertIsNotNone(bad.data['proposal_id'])
        proposal = KnowledgeProposal.objects.get(pk=bad.data['proposal_id'])
        self.assertEqual((proposal.source, proposal.status, proposal.shadow_id), ('bad_verdict', 'pending', reply.id))
        self.assertEqual([row['who'] for row in proposal.evidence], ['customer', 'shadow', 'office'])
        listed = self.get('shadow/bad/').data
        self.assertEqual([row['id'] for row in listed], [reply.id])
        self.assertEqual(listed[0]['contact_id'], reply.contact_id)
        self.assertEqual(listed[0]['verdict_note'], 'רמת גן זה סניף חיצוני, לא אומרים כלום על מחיר')
        self.assertEqual(KnowledgeItem.objects.filter(source_note='').count(), 0)   # the proposal changed no knowledge

    def test_try_a_question_with_a_pretend_clock_and_save_it(self):
        self.assertEqual(self.post('shadow/try/', {}).status_code, 400)
        # 16.10.2026 is a Friday: the office is closed, so a request for a person gets the weekend line.
        response = self.post('shadow/try/', {'question': 'אפשר לדבר עם נציג?', 'pretend_now': '2026-10-16T12:00:00', 'save': True})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['text'], WEEKEND_MESSAGE)
        self.assertTrue(response.data['request_human'])
        self.assertEqual(response.data['model'], 'stub')
        self.assertIsNotNone(response.data['trial_question_id'])
        self.assertEqual(TrialQuestion.objects.get().question, 'אפשר לדבר עם נציג?')
        self.assertEqual(ShadowReply.objects.count(), 0)
        response = self.post('shadow/try/', {'question': 'כן', 'last_outbound': 'תזכורת: האם אתם מגיעים לשיעור הניסיון מחר?'})
        self.assertEqual(response.data['text'], 'מעולה, רשמנו שאתם מגיעים! נתראה בשיעור 🥳')
        self.assertEqual(TrialQuestion.objects.count(), 1)
        self.assertEqual(self.post('shadow/try/', {'question': 'היי', 'pretend_now': 'not-a-date'}).status_code, 400)
        self.assertEqual(self.post('shadow/try/', {'question': 'היי', 'contact_id': 999999}).status_code, 400)

    def test_summary_and_status(self):
        self.incoming('היי')
        self.propose()
        summary = self.get('shadow/summary/').data
        self.assertEqual((summary['proposed_7d'], summary['awaiting_verdict'], summary['judged_good'], summary['judged_bad']), (1, 1, 0, 0))
        self.assertFalse(summary['shadow_configured'])
        self.assertEqual(summary['model'], 'stub')
        status = self.get('status/').data
        self.assertFalse(status['shadow_configured'])
        self.assertEqual(status['shadow_model'], 'stub')
        self.assertEqual(len(self.get('shadow/recent/').data), 1)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5', WAHUB_SHADOW_EFFORT='medium')
class ClaudeTests(ShadowTestCase):
    def _response(self, payload, status_code=200):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def test_the_tool_loop_the_request_shape_and_the_answer(self):
        self.incoming('יש חוג קפוארה לבן 6 בראש העין?', name='רותם')
        rule = KnowledgeItem.objects.get(kind='behavior_rule', title='אפס המצאות')
        first_turn = [
            {'type': 'thinking', 'thinking': '', 'signature': 'sig'},
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'find_courses', 'input': {'city': 'ראש העין', 'age': 6, 'course_type': 'קפוארה'}},
        ]
        answer = {'text': 'ב *ראש העין* יש קפוארה ביום שלישי 17:00 עם משה לוי.', 'reasoning': f'לפי הכלל [#{rule.id}] קראתי לכלי', 'knowledge_ids': [rule.id]}
        with patch(POST, side_effect=[
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'tool_use', 'content': first_turn}),
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}),
        ]) as post:
            reply = self.propose()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(reply.model, 'claude-opus-5-5')
        self.assertEqual(reply.text, 'ב *ראש העין* יש קפוארה ביום שלישי 17:00 עם משה לוי')   # the final period is dropped
        self.assertEqual(reply.reasoning, f'לפי הכלל [#{rule.id}] קראתי לכלי')
        self.assertIn(rule.id, reply.knowledge_used)
        self.assertEqual(reply.tools_used[0]['name'], 'find_courses')
        self.assertIn('1 חוגים', reply.tools_used[0]['summary'])

        first = post.call_args_list[0]
        body, headers = first.kwargs['json'], first.kwargs['headers']
        self.assertEqual(body['model'], 'claude-opus-5-5')
        self.assertNotIn('thinking', body)                              # Opus 5.5: adaptive by default; effort steers it
        self.assertEqual(body['output_config']['effort'], 'medium')
        self.assertEqual(body['output_config']['format']['type'], 'json_schema')
        self.assertEqual(body['tool_choice'], {'type': 'auto'})
        self.assertEqual([tool['name'] for tool in body['tools']], ['find_courses', 'branch_info', 'office_hours_now', 'customer_card', 'request_human'])
        self.assertEqual(body['fallbacks'], 'default')
        self.assertEqual(headers['anthropic-beta'], 'server-side-fallback-2026-07-01')
        self.assertEqual(headers['x-api-key'], 'test-key')
        self.assertIn('דנה', body['system'])
        self.assertIn('אפס המצאות', body['system'])
        self.assertIn('יש חוג קפוארה לבן 6 בראש העין?', body['messages'][0]['content'])

        second = post.call_args_list[1].kwargs['json']
        self.assertEqual(second['messages'][1], {'role': 'assistant', 'content': first_turn})     # thinking block passed back untouched
        result = second['messages'][2]['content'][0]
        self.assertEqual((result['type'], result['tool_use_id']), ('tool_result', 'toolu_1'))
        self.assertIn('קפוארה צעירים', result['content'])
        self.assertNotIn('is_error', result)

    def test_a_refusal_a_server_error_or_a_timeout_falls_back_to_the_stub(self):
        self.incoming('היי', name='רותם')
        with patch(POST, return_value=self._response({'error': {'message': 'boom'}}, status_code=500)):
            reply = self.propose()
        self.assertEqual(reply.model, 'stub')
        self.assertTrue(reply.reasoning.startswith('• Claude לא נתן תשובה (Claude ענה 500)'))
        self.assertEqual(reply.text, 'היי *רותם*, במה אוכל לעזור?')
        self.incoming('שלום')
        with patch(POST, return_value=self._response({'model': 'm', 'stop_reason': 'refusal', 'content': []})):
            reply = self.propose()
        self.assertEqual(reply.model, 'stub')
        self.assertIn('סירב', reply.reasoning)

    def test_request_human_by_claude_only_marks(self):
        self.incoming('יש לי תלונה, אני רוצה נציג')
        turn = [{'type': 'tool_use', 'id': 'toolu_9', 'name': 'request_human', 'input': {'reason': 'תלונה'}}]
        answer = {'text': 'מצטערת לשמוע, נציג מהמשרד יחזור אליך בהקדם', 'reasoning': 'תלונה — נציג', 'knowledge_ids': []}
        with patch(POST, side_effect=[
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'tool_use', 'content': turn}),
            self._response({'model': 'claude-opus-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}),
        ]):
            reply = self.propose()
        self.assertTrue(reply.request_human)
        self.assertEqual(reply.request_human_reason, 'תלונה')
        self.assertEqual(Message.objects.count(), 1)
        self.deliver.assert_not_called()
        self.handoff.assert_not_called()

    def test_status_says_the_shadow_is_configured(self):
        status = self.get('status/').data
        self.assertTrue(status['shadow_configured'])
        self.assertEqual(status['shadow_model'], 'claude-opus-5-5')


@override_settings(CRON_TOKEN='cron-secret', ANTHROPIC_API_KEY='')
class ShadowCronTests(ShadowTestCase):
    def test_the_tick_answers_after_the_summary_and_the_matching(self):
        self.incoming('יש חוג קפוארה לבן 6 בראש העין?')
        self.age(self.contact(), last_inbound_at=6, last_message_at=6)
        data = APIClient().get(f'{BASE}/cron/tick/', HTTP_X_CRON_TOKEN='cron-secret').data
        self.assertEqual((data['analyzed'], data['matched'], data['shadow_proposed'], data['shadow_stub'], data['pending_shadow']), (1, 1, 1, 1, 0))
        self.assertIn('review_proposals', data)
        reply = ShadowReply.objects.get()
        self.assertIn('שלישי', reply.text)

    def test_a_burst_waits_for_the_window(self):
        self.incoming('היי')
        counts = cron.tick()
        self.assertEqual((counts['shadow_proposed'], counts['pending_shadow']), (0, 0))   # not yet due, and not counted as pending
        self.assertTrue(self.contact().needs_shadow)
        self.age(self.contact(), last_inbound_at=1)
        counts = cron.tick()
        self.assertEqual(counts['shadow_proposed'], 1)

    def test_a_contact_that_fails_does_not_stop_the_rest(self):
        self.incoming('היי', phone='972505550101')
        self.incoming('שלום', phone='972505550102')
        for phone in ('972505550101', '972505550102'):
            self.age(self.contact(phone), last_inbound_at=2)
        real = shadow.propose

        def flaky(contact, **kwargs):
            if contact.phone.endswith('0101'):
                raise RuntimeError('boom')
            return real(contact, **kwargs)

        with patch('apps.wahub.cron.shadow.propose', side_effect=flaky):
            counts = cron.tick()
        self.assertEqual(counts['shadow_proposed'], 1)
        self.assertFalse(self.contact('972505550101').needs_shadow)   # not picked up again every five minutes
