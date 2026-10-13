"""
"דיוק" in a conversation works together with "ללמד את הבוט"
(docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.4): the same request as a "דיוק" on
picked messages or on a shadow reply, with `mode: "teach"`, opens a thread of
"ללמד" FROM that conversation. Claude is shown the excerpt beside his words;
from there on it is a "ללמד" thread in everything — nothing is applied by
itself, the example, "מאשר", the check, "בטל" — and it stands in the same list
of what he taught, saying where it came from and never a phone.

Without `mode` a "דיוק" is what it always was, and a "דיוק" on a proposal has
no other way. Claude is a mock throughout: nothing here reaches the network.
"""
from unittest.mock import patch

from django.test import override_settings

from apps.wahub import settings_store, shadow
from apps.wahub.models import Contact, KnowledgeItem, KnowledgeProposal, Message, RefineThread, RefineTurn, ShadowReply
from apps.wahub.tests.base import PHONE
from apps.wahub.tests.test_teach import (
    ASKED,
    CODE_NAMES,
    NEW_BODY,
    POST,
    THEN,
    TODAY,
    Claude,
    TeachTestCase,
    bot,
    claude,
    shown,
    taught,
    verdict,
)

WRONG = 'הוא שאל על מחיר והבוט ענה בלי לשאול איזה חוג'
SUMMARY = {
    'what_happened': 'הלקוח שאל כמה עולה חוג',
    'what_was_answered': 'הבוט הישן ענה מחיר בלי לשאול על איזה חוג',
    'suspected_issue': 'נראה שהיה צריך לשאול קודם על איזה חוג מדובר',
}


def summed() -> object:
    return claude(SUMMARY)


class FromChatCase(TeachTestCase):
    """A conversation with an invented customer: a question about the price, and the old bot's answer."""

    def setUp(self):
        super().setUp()
        self.incoming('כמה עולה חוג?', name='רותם')
        self.asked = Message.objects.latest('id')
        self.bot_reply('החוג עולה 250 שקלים')
        self.answered = Message.objects.latest('id')
        self.who = self.contact()
        Contact.objects.filter(pk=self.who.pk).update(name='רותם אלון')
        self.who.refresh_from_db()

    def open(self, **body):
        return self.post('refine/', {'mode': 'teach', 'contact': self.who.id, **body})

    def opened(self, **body):
        """A thread from the conversation whose reading is done: one waiting change."""
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule()))) as post:
            response = self.open(**{'message_ids': [self.asked.id, self.answered.id], 'text': WRONG, **body})
        self.assertEqual(response.status_code, 201, response.data)
        return response.data, post


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class SameFlowTests(FromChatCase):
    def test_it_is_a_teach_thread_and_nothing_is_applied_with_the_standing_approval_on(self):
        self.assertTrue(settings_store.review_auto_apply())
        before = self.untouched()[:2]
        data, _post = self.opened()
        self.assertEqual((data['mode'], data['status'], data['state'], data['state_label']), ('teach', 'open', 'waiting', 'מחכה לאישור שלך'))
        self.assertEqual((data['needs_approval'], data['needs_answer'], data['text']), (True, False, WRONG))
        self.assertEqual(data['preview'], {'question': ASKED, 'before': None, 'after': None, 'pending': True})
        self.assertEqual([(card['status'], card['applied']) for card in data['changes']], [('pending', False)])
        # Nothing went on the knowledge: the same items, the same history.
        self.assertEqual(self.untouched()[:2], before)
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.auto_applied, proposal.source), ('pending', False, 'refine'))
        # The change is tied to the conversation it came from, as every "דיוק" is: the review shows "לשיחה".
        self.assertEqual((proposal.contact_id, proposal.message_id), (self.who.id, self.asked.id))
        thread = RefineThread.objects.get()
        self.assertEqual((thread.contact_id, thread.message_ids, thread.shadow_ids), (self.who.id, [self.asked.id, self.answered.id], []))
        self.assertEqual(RefineTurn.objects.filter(thread=thread).first().meta, {'mode': 'teach', 'origin': 'conversation'})

    def test_where_it_came_from_and_what_it_was_written_about(self):
        data, _post = self.opened()
        self.assertEqual(data['origin'], {
            'kind': 'conversation', 'label': 'מתוך שיחה עם רותם אלון', 'contact_id': self.who.id, 'contact_name': 'רותם אלון',
            'link': f'/wahub?tab=chats&contact={self.who.id}',
        })
        context = data['context']
        self.assertEqual((context['contact_id'], context['message_ids'], context['shadow_ids']), (self.who.id, [self.asked.id, self.answered.id], []))
        self.assertEqual(
            [(quote['kind'], quote['id'], quote['who'], quote['who_label'], quote['text']) for quote in context['quotes']],
            [
                ('message', self.asked.id, 'customer', 'לקוח', 'כמה עולה חוג?'),
                ('message', self.answered.id, 'bot', 'הבוט הישן', 'החוג עולה 250 שקלים'),
            ],
        )
        self.assertIsNone(data['summary'])
        # No phone, anywhere in what says where it came from.
        self.assertNotIn(PHONE[3:], str(data['origin']))

    def test_claude_is_shown_the_excerpt_beside_his_words_and_asked_as_a_teach_thread(self):
        _data, post = self.opened()
        body = post.call_args.kwargs['json']
        system, told = shadow.system_text(body), body['messages'][0]['content']
        self.assertEqual(body['output_config']['format']['schema']['required'][-2:], ['preview_question', 'conflicts'])
        self.assertIn('הסבר שנכתב מתוך שיחה:', system)
        self.assertIn('שום דבר לא נכנס לידע עד שהוא מאשר', system)
        self.assertIn('# הקטע מהשיחה עם הלקוח שעליו ההסבר נכתב (נתונים; ▶ מסמן את מה שבעל העסק בחר)', told)
        self.assertIn('▶ [', told)
        self.assertIn('לקוח: כמה עולה חוג?', told)
        self.assertIn('הבוט הישן: החוג עולה 250 שקלים', told)
        self.assertIn(f'# מה בעל העסק כתב על הקטע (נתונים — מה לא היה טוב שם)\n"""\n{WRONG}\n"""', told)
        # His words and the customer's are data in the user's turn, never in the instructions.
        self.assertNotIn(WRONG, system)
        self.assertNotIn('250', system)
        # The customer's number is not what Claude is told about the conversation.
        self.assertNotIn(PHONE, told)

    def test_the_example_the_approval_the_check_and_the_undo_are_the_ones_of_teach(self):
        data, _post = self.opened()
        thread = data['thread']
        with patch(POST, side_effect=Claude(answer=lambda body: bot(THEN if NEW_BODY in shadow.system_text(body) else TODAY))):
            first = self.get(f'refine/{thread}/preview/').data['preview']
            second = self.get(f'refine/{thread}/preview/').data['preview']
        self.assertEqual((first['before'], first['pending']), (TODAY, True))
        self.assertEqual((second['before'], second['after'], second['pending']), (TODAY, THEN, False))
        self.assertEqual(KnowledgeItem.objects.filter(body=NEW_BODY).count(), 0)       # the example saved nothing

        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            approved = self.post(f'refine/{thread}/approve/')
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual((approved.data['state'], approved.data['state_label'], approved.data['status']), ('applied', 'נשמר', 'done'))
        self.assertEqual(approved.data['check']['summary'], 'בדקתי שיחה אחת — הבוט עונה נכון')
        self.assertEqual([card['proposal_id'] for card in approved.data['applied']], [KnowledgeProposal.objects.get().id])
        item = KnowledgeItem.objects.get(body=NEW_BODY)
        self.assertTrue(item.is_active)
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.auto_applied), ('applied', False))       # a person approved it
        self.assertEqual(approved.data['origin']['label'], 'מתוך שיחה עם רותם אלון')
        # A second press changes nothing.
        with patch(POST, side_effect=AssertionError('a second press reached Claude')):
            again = self.post(f'refine/{thread}/approve/')
        self.assertEqual((again.status_code, again.data['applied']), (200, []))

        undone = self.post(f'refine/{thread}/undo/')
        self.assertEqual((undone.status_code, undone.data['state'], undone.data['state_label']), (200, 'undone', 'בוטל'))
        self.assertFalse(KnowledgeItem.objects.get(pk=item.pk).is_active)

    def test_not_exactly_replaces_what_waited(self):
        data, _post = self.opened()
        better = self.new_rule(title='מחיר: שואלים איזה חוג ואיזה סניף', headline='מעכשיו הבוט ישאל על איזה חוג ואיזה סניף')
        with patch(POST, side_effect=Claude(reading=taught(better, earlier='replace'))) as post:
            said = self.post(f'refine/{data["thread"]}/say/', {'text': 'לא בדיוק, גם לשאול באיזה סניף'})
        self.assertEqual(said.status_code, 200, said.data)
        self.assertEqual([proposal.status for proposal in KnowledgeProposal.objects.order_by('id')], ['undone', 'pending'])
        self.assertEqual((said.data['mode'], said.data['needs_approval'], len(said.data['changes'])), ('teach', True, 1))
        # The excerpt is shown again with the chat so far: the reading never loses the conversation.
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('לקוח: כמה עולה חוג?', told)
        self.assertIn('# מה בעל העסק כתב עכשיו (נתונים — על זה עונים)', told)
        self.assertEqual(RefineTurn.objects.filter(role='operator').last().meta, {'mode': 'teach'})

    def test_no_code_name_reaches_him_here_either(self):
        data, _post = self.opened()
        listed = self.get('refine/', mode='teach').data
        for answer in (data, listed):
            for path, text in shown(answer):
                if path.endswith(('.kind', '.who', '.link')):
                    continue                    # codes the screen decides by, and an address
                for word in CODE_NAMES:
                    self.assertNotIn(word, text, f'{path}: {text}')


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class SystemSpeaksFirstTests(FromChatCase):
    def test_no_text_opens_with_the_summary_in_the_shape_of_teach(self):
        with patch(POST, return_value=summed()):
            response = self.open(message_ids=[self.asked.id, self.answered.id])
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual((data['mode'], data['state'], data['state_label']), ('teach', 'waiting', 'מחכה לתשובה שלך'))
        self.assertEqual((data['needs_answer'], data['needs_approval'], data['changes'], data['preview']), (True, False, [], None))
        self.assertEqual((data['summary'], data['question']), (SUMMARY, 'מה לא היה טוב כאן?'))
        self.assertTrue(data['reply'].endswith('מה לא היה טוב כאן?'))
        self.assertEqual(data['origin']['label'], 'מתוך שיחה עם רותם אלון')
        self.assertEqual((data['text'], data['understood']), ('', ''))
        self.assertEqual(KnowledgeProposal.objects.count(), 0)
        # He has not said a word yet: it is not something he taught.
        self.assertEqual(self.get('refine/', mode='teach').data['count'], 0)
        # ... and it is no thread of the old "דיוק" list either.
        self.assertEqual(self.get('refine/', contact=self.who.id).data, [])

    def test_his_answer_to_the_summary_is_read_as_a_teach_thread(self):
        with patch(POST, return_value=summed()):
            thread = self.open(message_ids=[self.answered.id]).data['thread']
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule()))) as post:
            said = self.post(f'refine/{thread}/say/', {'text': 'כן, בדיוק'})
        self.assertEqual(said.status_code, 200, said.data)
        self.assertEqual((said.data['mode'], said.data['needs_approval'], said.data['summary'], said.data['text']), ('teach', True, None, 'כן, בדיוק'))
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')
        body = post.call_args.kwargs['json']
        told = body['messages'][0]['content']
        self.assertEqual(body['output_config']['format']['schema']['required'][-2:], ['preview_question', 'conflicts'])
        self.assertIn('# הסיכום שבו המערכת פתחה, והשאלה ששאלה (נתונים; הסיכום הוא השערה של המערכת, לא עובדה)', told)
        self.assertIn(SUMMARY['suspected_issue'], told)
        self.assertIn('# מה בעל העסק ענה על הסיכום (נתונים — התיאור שלו של מה שלא היה טוב)\n"""\nכן, בדיוק\n"""', told)
        self.assertIn('הסבר שנפתח בסיכום:', shadow.system_text(body))
        self.assertEqual([row['thread'] for row in self.get('refine/', mode='teach').data['results']], [thread])


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class ShadowReplyTests(FromChatCase):
    def test_opened_on_a_shadow_reply(self):
        reply = ShadowReply.objects.create(contact=self.who, after_message=self.asked, text='החוג עולה לפי הסניף')
        data, post = self.opened(message_ids=None, shadow_ids=[reply.id], text='גם הבוט החדש לא שואל איזה חוג')
        self.assertEqual(data['origin'], {
            'kind': 'shadow', 'label': 'על תשובה של הבוט בצל, בשיחה עם רותם אלון', 'contact_id': self.who.id,
            'contact_name': 'רותם אלון', 'link': f'/wahub?tab=chats&contact={self.who.id}',
        })
        # The shadow reply brings the customer's message it answers, and stands right after it.
        self.assertEqual((data['context']['message_ids'], data['context']['shadow_ids']), ([self.asked.id], [reply.id]))
        self.assertEqual(
            [(quote['kind'], quote['who'], quote['who_label'], quote['text']) for quote in data['context']['quotes']],
            [('message', 'customer', 'לקוח', 'כמה עולה חוג?'), ('shadow', 'shadow', 'הבוט החדש (בצל)', 'החוג עולה לפי הסניף')],
        )
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('▶    ↳ מה הבוט החדש היה עונה כאן (בצל, לא נשלח): החוג עולה לפי הסניף', told)
        self.assertEqual((data['mode'], data['needs_approval']), ('teach', True))

    def test_messages_and_a_shadow_reply_together_are_a_conversation(self):
        reply = ShadowReply.objects.create(contact=self.who, after_message=self.asked, text='החוג עולה לפי הסניף')
        data, _post = self.opened(message_ids=[self.answered.id], shadow_ids=[reply.id])
        self.assertEqual(data['origin']['kind'], 'conversation')
        self.assertEqual(data['context']['message_ids'], [self.asked.id, self.answered.id])


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class NeverAPhoneTests(FromChatCase):
    def test_a_contact_with_no_name_is_a_conversation_and_nothing_more(self):
        for name in ('', '   ', PHONE, '050-555-0101', '+972 50 555 0101'):
            with self.subTest(name=name):
                Contact.objects.filter(pk=self.who.pk).update(name=name)
                data, _post = self.opened()
                self.assertEqual((data['origin']['label'], data['origin']['contact_name']), ('מתוך שיחה', ''))
                row = self.get('refine/', mode='teach').data['results'][0]
                self.assertEqual(row['origin']['label'], 'מתוך שיחה')
                for digits in (PHONE, PHONE[3:], '555-0101'):
                    self.assertNotIn(digits, str(data['origin']) + str(row))

    def test_a_shadow_reply_of_a_contact_with_no_name(self):
        Contact.objects.filter(pk=self.who.pk).update(name='')
        reply = ShadowReply.objects.create(contact=self.who, after_message=self.asked, text='החוג עולה לפי הסניף')
        data, _post = self.opened(message_ids=None, shadow_ids=[reply.id])
        self.assertEqual(data['origin']['label'], 'על תשובה של הבוט בצל')

    def test_a_conversation_that_was_deleted_since(self):
        data, _post = self.opened()
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            self.post(f'refine/{data["thread"]}/approve/')
        self.who.delete()
        again = self.get(f'refine/{data["thread"]}/').data
        self.assertEqual(again['origin'], {
            'kind': 'conversation', 'label': 'מתוך שיחה שנמחקה', 'contact_id': None, 'contact_name': '', 'link': None,
        })
        # What was taught stays taught, and stays in the list.
        self.assertEqual((again['state'], again['context']['quotes']), ('applied', []))
        self.assertEqual([row['origin']['label'] for row in self.get('refine/', mode='teach').data['results']], ['מתוך שיחה שנמחקה'])
        self.assertTrue(KnowledgeItem.objects.get(body=NEW_BODY).is_active)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class OneHistoryTests(FromChatCase):
    def test_what_was_taught_from_a_conversation_is_in_the_list_of_teach(self):
        from_chat, _post = self.opened()
        free = self.taught()['thread']
        # An old "דיוק" on the same conversation is not something he taught here.
        with patch(POST, side_effect=Claude(reading=claude({
            'understood': 'x', 'question': 'מה?', 'confidence': 'low', 'earlier_changes': 'keep', 'changes': [],
        }))):
            old = self.post('refine/', {'contact': self.who.id, 'message_ids': [self.asked.id], 'text': 'לא טוב'}).data
        self.assertNotIn('mode', old)

        with patch(POST, side_effect=AssertionError('a list reached Claude')):
            listed = self.get('refine/', mode='teach').data
            self.assertEqual([row['thread'] for row in listed['results']], [free, from_chat['thread']])
            self.assertEqual(
                [(row['origin']['kind'], row['origin']['label']) for row in listed['results']],
                [('free', ''), ('conversation', 'מתוך שיחה עם רותם אלון')],
            )
            self.assertEqual(listed['results'][1]['state'], 'waiting')
            # Of one conversation.
            mine = self.get('refine/', mode='teach', contact=self.who.id).data
            self.assertEqual([row['thread'] for row in mine['results']], [from_chat['thread']])
            self.assertEqual(self.get('refine/', mode='teach', contact=999999).data['count'], 0)
            self.assertEqual(self.get('refine/', mode='teach', contact='רותם').status_code, 400)
            # The old list of the conversation shows the old thread only, in the old shape.
            legacy = self.get('refine/', contact=self.who.id).data
            self.assertEqual([row['thread'] for row in legacy], [old['thread']])

    def test_the_list_does_not_grow_queries_with_threads(self):
        self.opened()
        with self.assertNumQueries(6):      # the user, the profile, the count, the threads with their contacts, the turns, the proposals
            self.get('refine/', mode='teach')
        for _index in range(4):
            self.opened()
        with self.assertNumQueries(6):
            self.assertEqual(self.get('refine/', mode='teach').data['count'], 5)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class DoorTests(FromChatCase):
    def test_what_is_refused_at_the_door(self):
        self.incoming('שלום', phone='972505550177')
        foreign = Message.objects.latest('id')
        proposal = KnowledgeProposal.objects.create(source='reviewer', title='הצעה', change={'action': 'create', 'kind': 'fact', 'after': {'body': 'x'}})
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            for body, word in (
                ({'mode': 'learn', 'contact': self.who.id, 'message_ids': [self.asked.id], 'text': WRONG}, 'mode לא תקין.'),
                ({'mode': 'teach', 'proposal': proposal.id, 'text': WRONG}, 'דיוק על הצעה נפתח בלי mode.'),
                ({'mode': 'teach', 'contact': self.who.id, 'text': WRONG}, 'נדרשת לפחות הודעה אחת מהשיחה'),
                ({'mode': 'teach', 'contact': self.who.id, 'message_ids': [foreign.id], 'text': WRONG}, 'לא שייכת לשיחה הזאת'),
                ({'mode': 'teach', 'contact': self.who.id, 'shadow_ids': [999999], 'text': WRONG}, 'לא שייכת לשיחה הזאת'),
                ({'mode': 'teach', 'contact': 999999, 'message_ids': [self.asked.id], 'text': WRONG}, 'איש הקשר לא נמצא'),
            ):
                with self.subTest(body=str(body)[:60]):
                    response = self.post('refine/', body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(word, response.data['detail'])
        self.assertEqual((RefineThread.objects.count(), KnowledgeProposal.objects.count()), (0, 1))

    def test_mode_teach_and_a_text_alone_is_teach_as_it_was(self):
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule()))):
            data = self.post('refine/', {'mode': 'teach', 'text': 'כששואלים על מחיר, קודם לשאול איזה חוג'}).data
        self.assertEqual((data['mode'], data['origin']['kind'], data['context']), ('teach', 'free', None))

    def test_without_mode_a_fix_is_what_it_always_was(self):
        with patch(POST, side_effect=Claude(reading=claude({
            'understood': 'קודם שואלים איזה חוג', 'question': '', 'confidence': 'high', 'earlier_changes': 'keep', 'changes': [self.new_rule()],
        }))):
            fix = self.post('refine/', {'contact': self.who.id, 'message_ids': [self.asked.id], 'text': WRONG}).data
        # The old shape, applied at once under the standing approval — and not in the list of what was taught.
        self.assertNotIn('mode', fix)
        self.assertNotIn('origin', fix)
        self.assertEqual((fix['status'], fix['changes'][0]['applied'], fix['changes'][0]['action']), ('done', True, 'create'))
        self.assertTrue(KnowledgeItem.objects.get(body=NEW_BODY).is_active)
        self.assertEqual(self.get('refine/', mode='teach').data['count'], 0)
        self.assertEqual(self.post(f'refine/{fix["thread"]}/approve/').status_code, 400)

    def test_a_fix_on_a_proposal_is_what_it_always_was(self):
        proposal = KnowledgeProposal.objects.create(
            source='reviewer', title='הצעה', contact=self.who, message=self.asked,
            change={'action': 'create', 'kind': 'behavior_rule', 'item_id': None, 'before': None, 'after': {'kind': 'behavior_rule', 'title': 'מחיר', 'body': 'x'}},
        )
        settings_store.set_review_auto_apply(False, self.manager)
        with patch(POST, side_effect=Claude(reading=claude({
            'understood': 'קודם שואלים איזה חוג', 'question': '', 'confidence': 'high', 'earlier_changes': 'replace', 'changes': [self.new_rule()],
        }))):
            fix = self.post('refine/', {'proposal': proposal.id, 'text': 'לנסח אחרת'})
        self.assertEqual(fix.status_code, 201, fix.data)
        self.assertNotIn('mode', fix.data)
        self.assertEqual(fix.data['proposal_id'], proposal.id)
        self.assertEqual(self.get('refine/', mode='teach').data['count'], 0)


class NobodyReadTests(FromChatCase):
    """No key at all: the summary is two quotes, and his words wait as he wrote them."""

    def test_without_a_key_the_summary_is_quoted_and_his_words_are_kept(self):
        with patch(POST, side_effect=AssertionError('nobody has a key to reach Claude with')):
            opened = self.open(message_ids=[self.asked.id, self.answered.id])
            self.assertEqual(opened.status_code, 201, opened.data)
            self.assertEqual((opened.data['mode'], opened.data['needs_answer'], opened.data['question']), ('teach', True, 'מה לא היה טוב כאן?'))
            self.assertIn('כמה עולה חוג?', opened.data['summary']['what_happened'])
            said = self.post(f'refine/{opened.data["thread"]}/say/', {'text': 'צריך לשאול קודם איזה חוג'})
        self.assertEqual(said.status_code, 200, said.data)
        self.assertEqual((said.data['mode'], said.data['needs_approval'], said.data['preview'], said.data['understood']), ('teach', True, None, ''))
        self.assertTrue(said.data['changes'][0]['technical']['kept_as_written'])
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')
        self.assertEqual(said.data['origin']['label'], 'מתוך שיחה עם רותם אלון')
