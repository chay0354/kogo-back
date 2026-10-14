"""
"ללמד את הבוט" (docs/WAHUB-CONTRACT-TEACH-AND-TESTS.md, א): `POST refine/` with a
text and nothing else opens a thread on the owner's words alone. In it nothing
is applied by itself — not with the standing approval on, not when the model is
sure; a question comes in place of a guess; the example is made with the change
on the knowledge in memory and saves nothing; "מאשר" applies and then asks the
bot again; "לא בדיוק" replaces what waited; "בטל" takes it back; without a key
the words wait as they were written and the answer says so plainly; no code
name reaches the screen outside `technical`; managers only.
Claude is a mock throughout: nothing here reaches the network.
"""
import json
import sys
import types
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.wahub import knowledge, refine, reviewer, settings_store, shadow
from apps.wahub.models import (
    Contact,
    KnowledgeHistory,
    KnowledgeItem,
    KnowledgeProposal,
    Message,
    RefineThread,
    RefineTurn,
    ShadowReply,
    TrialQuestion,
)
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user

# `requests.post` itself: the reading of the text, the bot's own answers and the check all reach Claude through it.
POST = 'apps.wahub.refine.requests.post'
EMPTY = {
    'action': 'create', 'item_id': 0, 'kind': 'behavior_rule', 'key': '', 'title': '', 'body': '', 'when_to_say': '',
    'example_good': '', 'example_bad': '', 'what_customer_writes': '', 'means': '', 'headline': '',
}
TEXT = 'כששואלים על מחיר, קודם לשאול על איזה חוג ורק אז לענות'
ASKED = 'כמה עולה חוג?'
TODAY = 'החוג עולה לפי הסניף, אפשר לראות בקישור'
THEN = 'בשמחה! על איזה חוג מדובר?'
NEW_BODY = 'כששואלים על מחיר בלי לומר איזה חוג — קודם שואלים על איזה חוג מדובר, ורק אז עונים.'
OLD_BODY = 'כששואלים על מחיר עונים מיד, לפי מה שחוזר מהמערכת'
# What may not reach the owner's eyes outside a `technical`: code names, and the names of what is behind the screen.
CODE_NAMES = (
    'behavior_rule', 'style_rule', 'phrasing', 'when_to_say', 'item_id', 'example_good', 'proactive', 'refine_stub',
    'Claude', 'Anthropic', 'prompt', 'stub', '#', 'create', 'update', 'deactivate',
)
# The words this file's own sentences never use with him (docs/WAHUB-CONTRACT-TEACH-AND-TESTS.md, א).
JARGON = ('רשומה', 'כלל התנהגות', 'ביקורת', 'מפתח')
# The fields of an answer that are codes for the screen's own logic, or a moment — not text it shows.
CODES = ('technical', 'mode', 'status', 'state', 'role', 'at', 'created_at')


def change(**fields) -> dict:
    """One change as the schema makes Claude write it: every field there, the unused ones empty."""
    return {**EMPTY, **fields}


def claude(answer: dict, stop='end_turn') -> MagicMock:
    """An answer of Claude's, as the Messages API returns it."""
    response = MagicMock(status_code=200)
    response.json.return_value = {
        'model': 'claude-opus-5-5', 'stop_reason': stop,
        'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
    }
    return response


def taught(*changes, understood='כששואלים על מחיר, הבוט ישאל קודם על איזה חוג מדובר ורק אז יענה', question='', confidence='high',
           earlier='keep', asks=ASKED, conflicts=()) -> MagicMock:
    """Claude's reading of what the owner wrote in "ללמד את הבוט"."""
    return claude({
        'understood': understood, 'question': question, 'confidence': confidence, 'earlier_changes': earlier,
        'changes': list(changes), 'preview_question': asks, 'conflicts': list(conflicts),
    })


def bot(text=THEN) -> MagicMock:
    """The new bot's own answer to a typed question (shadow.draft)."""
    return claude({'text': text, 'reasoning': 'לפי הכללים', 'knowledge_ids': []})


def verdict(follows=True, line='הבוט שאל קודם על איזה חוג מדובר') -> MagicMock:
    return claude({'follows': follows, 'line': line})


class Claude:
    """
    One stand-in for `requests.post`. Claude is asked three kinds of question
    here, told apart by the request itself: the reading of the owner's text, an
    answer of the bot's (the only request that carries tools), and the check's
    verdict. A kind nobody prepared an answer for fails the test.
    """

    def __init__(self, *, reading=None, answer=None, verdict=None):
        self.answers = {'reading': reading, 'answer': answer, 'verdict': verdict}
        self.asked = {'reading': [], 'answer': [], 'verdict': []}

    def __call__(self, url, **request):
        body = request['json']
        schema = body['output_config']['format']['schema']['properties']
        kind = 'answer' if 'tools' in body else 'verdict' if 'follows' in schema else 'reading'
        self.asked[kind].append(body)
        prepared = self.answers[kind]
        if prepared is None:
            raise AssertionError(f'Claude was asked for a {kind} nobody expected')
        if isinstance(prepared, Exception):
            raise prepared
        return prepared(body) if not isinstance(prepared, MagicMock) else prepared

    def count(self) -> tuple:
        return tuple(len(self.asked[kind]) for kind in ('reading', 'answer', 'verdict'))


def shown(payload, path='') -> list:
    """Every text of an answer a screen may put before the owner: all of it but its codes and what sits under a `technical`."""
    if isinstance(payload, dict):
        return [text for name, value in payload.items() if name not in CODES for text in shown(value, f'{path}.{name}')]
    if isinstance(payload, (list, tuple)):
        return [text for value in payload for text in shown(value, path)]
    return [(path, payload)] if isinstance(payload, str) else []


class TeachTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        # Nothing here may send, or touch ManyChat, ever.
        patch('apps.wahub.sending._deliver', side_effect=AssertionError('"ללמד את הבוט" tried to send')).start()
        patch('apps.wahub.handoff._in_manychat', side_effect=AssertionError('"ללמד את הבוט" tried to touch ManyChat')).start()
        self.addCleanup(patch.stopall)
        self.price = knowledge.create_item({'kind': 'behavior_rule', 'title': 'מחיר', 'body': OLD_BODY})
        self.parking = knowledge.create_item({'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה'})
        # An invented person and an invented number: nothing of a real customer, or of the real office, is in these tests.
        self.person = knowledge.create_item({'kind': 'contact', 'title': 'נציגת המשרד', 'name': 'נציגה', 'role': 'משרד', 'phone': '0500000000'})

    def new_rule(self, **fields):
        return change(**{'action': 'create', 'kind': 'behavior_rule', 'title': 'מחיר: קודם שואלים איזה חוג', 'body': NEW_BODY,
                         'headline': 'מעכשיו הבוט ישאל על איזה חוג מדובר לפני שיענה על מחיר', **fields})

    def edited_rule(self, **fields):
        return change(**{'action': 'update', 'item_id': self.price.id, 'kind': 'behavior_rule', 'body': NEW_BODY, **fields})

    def teach(self, text=TEXT, **extra):
        return self.post('refine/', {'text': text, **extra})

    def taught(self, *changes, **said):
        """A thread whose reading is done: one waiting change (a new rule unless others are given)."""
        with patch(POST, side_effect=Claude(reading=taught(*(changes or [self.new_rule()]), **said))):
            response = self.teach()
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def untouched(self):
        """Everything "nothing was saved" is held against."""
        return (
            list(KnowledgeItem.objects.order_by('id').values_list('id', 'title', 'body', 'version', 'is_active')),
            KnowledgeHistory.objects.count(), ShadowReply.objects.count(), Message.objects.count(), TrialQuestion.objects.count(),
            Contact.objects.count(), list(KnowledgeProposal.objects.order_by('id').values_list('id', 'status', 'applied_item_id')),
        )


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class NothingByItselfTests(TeachTestCase):
    def test_a_text_alone_opens_a_teach_thread_and_nothing_is_applied_with_the_standing_approval_on(self):
        self.assertTrue(settings_store.review_auto_apply())
        before = self.untouched()
        asked = Claude(reading=taught(self.new_rule(), confidence='high', conflicts=[{'item_id': self.price.id, 'why': 'היום כתוב שם שעונים מחיר מיד'}]))
        with patch(POST, side_effect=asked):
            response = self.teach()
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual((data['mode'], data['status'], data['state'], data['state_label']), ('teach', 'open', 'waiting', 'מחכה לאישור שלך'))
        self.assertEqual(data['understood'], 'כששואלים על מחיר, הבוט ישאל קודם על איזה חוג מדובר ורק אז יענה')
        self.assertEqual((data['question'], data['needs_approval'], data['needs_answer'], data['text']), (None, True, False, TEXT))
        self.assertEqual(data['reply'], f'הבנתי כך: {data["understood"]}\nשום דבר עוד לא השתנה — זה מחכה לאישור שלך')

        # Sure, and the owner's standing approval is on — and still nothing went on the knowledge.
        self.assertEqual(self.untouched()[:6], before[:6])
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.pattern, proposal.status, proposal.auto_applied), ('refine', 'teach', 'pending', False))
        self.assertEqual((proposal.contact_id, proposal.message_id, proposal.applied_item_id, proposal.decided_at), (None, None, None, None))
        self.assertEqual(proposal.title, 'ללמד: להוסיף כלל התנהגות — מחיר: קודם שואלים איזה חוג')
        self.assertEqual(proposal.evidence, [{'message_id': None, 'who': 'office', 'text': f'ללמד: {TEXT}'}])
        self.assertIn(f'דנה מנהלת הסביר/ה לבוט: "{TEXT}".', proposal.explanation)
        self.assertEqual(reviewer.auto_applied_today(), 0)

        card = data['changes'][0]
        self.assertEqual(set(card), {'proposal_id', 'plain', 'status', 'status_label', 'applied', 'moved', 'can_undo', 'technical'})
        self.assertEqual((card['proposal_id'], card['status'], card['status_label'], card['applied']), (proposal.id, 'pending', 'מחכה לאישור שלך', False))
        self.assertFalse(card['moved'])
        self.assertEqual(card['plain'], {
            'headline': 'מעכשיו הבוט ישאל על איזה חוג מדובר לפני שיענה על מחיר', 'rule': NEW_BODY, 'why': f'כך הסברת: "{TEXT}"',
            'is_new': True, 'before': None, 'after': NEW_BODY, 'example_good': '', 'example_bad': '',
        })
        self.assertEqual((card['technical']['action'], card['technical']['kind'], card['technical']['kind_label']), ('create', 'behavior_rule', 'כלל התנהגות'))
        self.assertEqual(card['technical']['after']['body'], NEW_BODY)

        # The example is asked for apart (preview/): here only its question, and that it is still to come.
        self.assertEqual(data['preview'], {'question': ASKED, 'before': None, 'after': None, 'pending': True})
        self.assertEqual(data['conflicts'], [{'item_id': self.price.id, 'title': 'מחיר', 'why': 'היום כתוב שם שעונים מחיר מיד'}])
        self.assertEqual((data['check'], data['notes'], data['can_undo']), (None, [], True))
        self.assertEqual(asked.count(), (1, 0, 0))

        thread = RefineThread.objects.get()
        self.assertEqual((thread.contact_id, thread.proposal_id, thread.message_ids, thread.created_by), (None, None, [], self.manager))
        self.assertEqual(
            [(turn['role'], turn['technical']['outcome'], len(turn['changes'])) for turn in data['turns']], [('operator', None, 0), ('system', 'waiting', 1)],
        )
        # Every sentence of it is the owner's language: no code name, and none of the words of the screens behind this one.
        for path, text in shown(data):
            for word in CODE_NAMES + JARGON:
                self.assertNotIn(word, text, f'{path}: {text}')
        self.assertEqual(self.get(f'refine/{data["thread"]}/').data, data)
        # It waits in the review like any proposal; the list of "דיוק" threads does not mix it in.
        self.assertEqual([row['id'] for row in self.get('review/proposals/', status='pending').data], [proposal.id])
        self.assertEqual(self.get('refine/').data, [])

    def test_what_claude_is_shown(self):
        asked = Claude(reading=taught(self.new_rule()))
        with patch(POST, side_effect=asked) as post:
            self.teach()
        body, headers = asked.asked['reading'][0], post.call_args.kwargs['headers']
        self.assertEqual((body['model'], body['fallbacks'], body['output_config']['format']['type']), ('claude-opus-5-5', 'default', 'json_schema'))
        schema = body['output_config']['format']['schema']
        self.assertEqual(schema['required'][-2:], ['preview_question', 'conflicts'])
        self.assertEqual(schema['properties']['conflicts']['items']['required'], ['item_id', 'why'])
        self.assertEqual((headers['x-api-key'], headers['anthropic-version']), ('test-key', '2023-06-01'))
        self.assertEqual(post.call_args.kwargs['timeout'], (5, refine.TIMEOUT_SECONDS))

        system, told = shadow.system_text(body), body['messages'][0]['content']
        # His words are data in the user's turn; the rules and the whole knowledge are the system's.
        self.assertIn(f'# מה בעל העסק רוצה להסביר לבוט (נתונים)\n"""\n{TEXT}\n"""', told)
        self.assertNotIn(TEXT, system)
        self.assertTrue(system.startswith(refine.TEACH_SYSTEM))
        self.assertIn('שום דבר לא נכנס לידע עד שהוא מאשר', system)
        self.assertIn('לא מנחשים', system)
        # The one list of what is never written to the knowledge, word for word as a "דיוק" is told it.
        forbidden = refine._paragraph(refine.SYSTEM, 'מה אסור לכתוב בידע, בשום ניסוח:')
        self.assertIn('מחיר של חוג, הנחה, דמי רישום', forbidden)
        self.assertIn(forbidden, system)
        self.assertIn(f'[#{self.price.id}] behavior_rule · מחיר\n   {OLD_BODY}', system)
        self.assertIn(f'[#{self.person.id}] נציגת המשרד', system)
        # The prompt of a "דיוק" is not this one, and stays as it was.
        self.assertNotIn('בעל העסק כותב לך בחופשיות', refine.SYSTEM)
        self.assertNotIn('preview_question', refine.ANSWER_SCHEMA['properties'])

    def test_a_question_comes_in_place_of_a_guess(self):
        unsure = taught(self.new_rule(), question='על איזה חוגים זה חל — על כולם, או רק כשיש כמה באותו סניף?', confidence='low')
        with patch(POST, side_effect=Claude(reading=unsure)):
            data = self.teach('שיהיה יותר ברור עם המחירים').data
        self.assertEqual(data['question'], 'על איזה חוגים זה חל — על כולם, או רק כשיש כמה באותו סניף?')
        self.assertEqual((data['changes'], data['preview'], data['conflicts']), ([], None, []))
        self.assertEqual((data['needs_answer'], data['needs_approval'], data['status']), (True, False, 'open'))
        self.assertEqual((data['state'], data['state_label']), ('waiting', 'מחכה לתשובה שלך'))
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (0, 3))
        self.assertEqual(self.post(f'refine/{data["thread"]}/approve/').status_code, 409)

        # His answer goes on in the same thread, and Claude is shown what was said so far.
        asked = Claude(reading=taught(self.new_rule()))
        with patch(POST, side_effect=asked):
            answered = self.post(f'refine/{data["thread"]}/say/', {'text': 'על כולם'}).data
        told = asked.asked['reading'][0]['messages'][0]['content']
        self.assertIn('בעל העסק: """שיהיה יותר ברור עם המחירים"""', told)
        self.assertIn('המערכת (אתה): """', told)
        self.assertIn('# מה בעל העסק כתב עכשיו (נתונים — על זה עונים)\n"""\nעל כולם\n"""', told)
        self.assertEqual((answered['question'], answered['needs_approval'], len(answered['changes'])), (None, True, 1))
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')
        self.assertEqual([turn.meta.get('mode') for turn in RefineTurn.objects.order_by('id')], ['teach'] * 4)

    def test_a_doubt_is_said_and_the_switch_changes_nothing_here(self):
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule(), confidence='low'))):
            unsure = self.teach().data
        self.assertEqual((unsure['notes'], unsure['needs_approval'], unsure['question']), ([refine.TEACH_UNSURE], True, None))
        self.assertTrue(unsure['reply'].endswith(f'{refine.TEACH_WAITS}\n{refine.TEACH_UNSURE}'))
        # With the standing approval off the answer is the same one: the switch is not what holds a change here.
        settings_store.set_review_auto_apply(False, self.manager)
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule(title='מחיר: עוד כלל')))):
            off = self.teach().data
        self.assertEqual((off['needs_approval'], off['notes'], off['state']), (True, [], 'waiting'))
        self.assertNotIn('אוטומטי', off['reply'])
        self.assertEqual(list(KnowledgeProposal.objects.values_list('status', flat=True)), ['pending', 'pending'])
        self.assertEqual(KnowledgeItem.objects.count(), 3)

    def test_nothing_understood_and_nothing_asked_is_a_question_too(self):
        with patch(POST, side_effect=Claude(reading=taught(understood='', asks=''))):
            data = self.teach('אממ').data
        self.assertEqual((data['question'], data['needs_answer'], data['changes'], data['understood']), (refine.TEACH_UNCLEAR, True, [], ''))

    def test_what_is_refused_at_the_door(self):
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            for body, word in (({'text': ''}, 'מה תרצה להסביר לבוט?'), ({}, 'מה תרצה להסביר לבוט?'), ({'text': 'א' * 4001}, 'ארוך מדי')):
                with self.subTest(body=str(body)[:30]):
                    response = self.post('refine/', body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(word, response.data['detail'])
            self.assertEqual(self.get('refine/', mode='other').status_code, 400)
            self.assertEqual(self.post('refine/999999/approve/').status_code, 404)
            self.assertEqual(self.get('refine/999999/check/').status_code, 404)
            self.assertEqual(self.get('refine/999999/preview/').status_code, 404)
        self.assertEqual((RefineThread.objects.count(), KnowledgeProposal.objects.count()), (0, 0))
        thread = self.taught()['thread']
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            blank = self.post(f'refine/{thread}/say/', {'text': '  '})
        self.assertEqual((blank.status_code, blank.data['detail']), (400, 'נדרש טקסט: מה תרצה להסביר לבוט?'))


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class ExampleTests(TeachTestCase):
    def answers(self):
        """The bot answers by what it was shown: the new wording in its knowledge, or the old."""
        return Claude(answer=lambda body: bot(THEN if NEW_BODY in shadow.system_text(body) else TODAY), verdict=verdict())

    def test_the_example_is_made_a_part_a_call_with_the_change_in_memory_and_saves_nothing(self):
        data = self.taught(self.edited_rule())
        thread = data['thread']
        self.assertEqual(data['changes'][0]['plain']['before'], OLD_BODY)
        self.assertEqual((data['changes'][0]['plain']['after'], data['changes'][0]['plain']['is_new']), (NEW_BODY, False))
        self.assertEqual(data['changes'][0]['plain']['headline'], 'מעכשיו הבוט יפעל אחרת בנושא "מחיר"')
        before = self.untouched()
        turns = RefineTurn.objects.count()

        asked = self.answers()
        with patch(POST, side_effect=asked):
            first = self.get(f'refine/{thread}/preview/')
            self.assertEqual(first.status_code, 200, first.data)
            self.assertEqual(first.data, {'thread': thread, 'preview': {'question': ASKED, 'before': TODAY, 'after': None, 'pending': True}})
            second = self.get(f'refine/{thread}/preview/').data
            # The answer after the change is judged before he approves (the owner, 14.10.2026: no "done" that does not work).
            self.assertEqual(second['preview'], {'question': ASKED, 'before': TODAY, 'after': THEN, 'pending': False, 'note': refine.EXAMPLE_FOLLOWS})
            # Made once: asked for again, it is read from where it was kept.
            self.assertEqual(self.get(f'refine/{thread}/preview/').data, second)
        self.assertEqual(asked.count(), (0, 2, 1))
        judged = asked.asked['verdict'][0]['messages'][0]['content']
        self.assertIn(THEN, judged)
        self.assertIn(TODAY, judged)

        today, then = asked.asked['answer']
        self.assertIn(f'[#{self.price.id}] מחיר: {OLD_BODY}', shadow.system_text(today))
        self.assertNotIn(NEW_BODY, shadow.system_text(today))
        self.assertIn(f'[#{self.price.id}] מחיר: {NEW_BODY}', shadow.system_text(then))
        self.assertNotIn(OLD_BODY, shadow.system_text(then))
        # The question of a stranger: no conversation, no customer card.
        for body in (today, then):
            self.assertIn(f'- {ASKED}', body['messages'][0]['content'])
            self.assertIn('הכותב: לא ידוע', body['messages'][0]['content'])

        # Nothing was saved by it: the item, its history, the proposal — and no reply, message or trial question was made.
        self.assertEqual(self.untouched(), before)
        self.assertEqual(RefineTurn.objects.count(), turns)
        self.price.refresh_from_db()
        self.assertEqual((self.price.body, self.price.version), (OLD_BODY, 1))
        self.assertEqual(self.get(f'refine/{thread}/').data['preview'], second['preview'])

    def test_a_new_rule_and_a_rule_switched_off_are_in_the_example_and_not_in_the_database(self):
        off = change(action='deactivate', item_id=self.parking.id, kind='fact')
        nickname = change(action='create', kind='alias', what_customer_writes='קפו', means='קפוארה')
        thread = self.taught(self.new_rule(), off, nickname)['thread']
        waiting = list(KnowledgeProposal.objects.order_by('id'))
        items = knowledge.relevant_items(today=refine.state.now_israel_date())
        with_changes = refine._items_with(items, waiting)
        made = [item for item in with_changes if item.title == 'מחיר: קודם שואלים איזה חוג']
        self.assertEqual((len(made), made[0].body, made[0].kind, made[0]._state.adding), (1, NEW_BODY, 'behavior_rule', True))
        self.assertFalse(KnowledgeItem.objects.filter(pk=made[0].id).exists())
        self.assertNotIn(self.parking.id, [item.id for item in with_changes])
        self.assertEqual([item.data['means'] for item in with_changes if item.kind == 'alias'], ['קפוארה'])
        # The list it was made from, and the rows behind it, are as they were.
        self.assertEqual([item.id for item in items], [self.price.id, self.person.id, self.parking.id])
        self.assertEqual((KnowledgeItem.objects.count(), KnowledgeItem.objects.get(pk=self.parking.id).is_active), (3, True))

        asked = Claude(answer=lambda body: bot('אחרי' if NEW_BODY in shadow.system_text(body) and 'יש חניה' not in shadow.system_text(body) else 'היום'),
                       verdict=verdict())
        with patch(POST, side_effect=asked):
            self.get(f'refine/{thread}/preview/')
            example = self.get(f'refine/{thread}/preview/').data['preview']
        self.assertEqual((example['before'], example['after'], example['pending']), ('היום', 'אחרי', False))
        self.assertIn('"קפו" → קפוארה', shadow.system_text(asked.asked['answer'][1]))
        self.assertEqual(KnowledgeItem.objects.count(), 3)

    def test_an_example_that_still_does_not_do_what_was_taught_is_said_before_the_approval(self):
        thread = self.taught(self.edited_rule())['thread']
        asked = Claude(answer=lambda body: bot(TODAY), verdict=verdict(False, 'הבוט עדיין ענה מחיר בלי לשאול על איזה חוג'))
        with patch(POST, side_effect=asked), self.assertLogs('apps.wahub.refine', level='WARNING') as logged:
            self.get(f'refine/{thread}/preview/')
            example = self.get(f'refine/{thread}/preview/').data['preview']
        self.assertEqual(example['note'], refine.EXAMPLE_FAILS.format(line='הבוט עדיין ענה מחיר בלי לשאול על איזה חוג'))
        self.assertIn('עדיף לא לאשר', example['note'])
        self.assertIn('still does not answer as taught', logged.output[-1])
        # Nothing was applied by it, and it is judged once.
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')
        with patch(POST, side_effect=asked):
            self.assertEqual(self.get(f'refine/{thread}/preview/').data['preview'], example)
        self.assertEqual(asked.count(), (0, 2, 1))

    def test_saved_is_not_said_to_work_before_the_bot_was_asked_again(self):
        self.assertNotIn('כבר עונה', refine.TEACH_APPROVED)
        self.assertIn('לוודא שהוא באמת עונה לפיו', refine.TEACH_APPROVED)

    def test_an_example_that_cannot_be_made_is_given_up_and_never_asked_for_for_ever(self):
        thread = self.taught()['thread']
        down = Claude(answer=requests.Timeout('slow'))
        with patch(POST, side_effect=down):
            states = [self.get(f'refine/{thread}/preview/').data['preview'] for _ in range(5)]
        self.assertEqual([state['pending'] for state in states], [True, True, True, False, False])
        self.assertEqual(states[-1], {'question': ASKED, 'before': None, 'after': None, 'pending': False, 'note': 'לא הצלחתי להכין דוגמה כרגע'})
        self.assertEqual(down.count(), (0, 4, 0))                 # two tries a part, and then no more

    def test_no_example_without_a_question_or_for_a_thread_that_is_not_a_teach_one(self):
        thread = self.taught(asks='')
        self.assertIsNone(thread['preview'])
        with patch(POST, side_effect=AssertionError('no question, no call')):
            self.assertEqual(self.get(f'refine/{thread["thread"]}/preview/').data, {'thread': thread['thread'], 'preview': None})

    def test_approved_before_the_example_was_made_only_the_answer_of_now_is_shown(self):
        thread = self.taught()['thread']
        asked = Claude(answer=bot(THEN), verdict=verdict())
        with patch(POST, side_effect=asked):
            self.post(f'refine/{thread}/approve/')
            example = self.get(f'refine/{thread}/preview/').data['preview']
        self.assertEqual((example['before'], example['after'], example['pending']), (None, THEN, False))
        # Nothing failed, and it is not said as a failure. The check had just asked the bot this very question: it is not asked again.
        self.assertEqual(example['note'], refine.EXAMPLE_AFTER_ONLY)
        self.assertEqual(asked.count(), (0, 1, 1))
        # With no check to read the answer from (it could not run), the bot is asked — the knowledge already holds the change.
        other = self.taught()['thread']
        with patch.object(refine, 'CHECK_SECONDS', 5), patch(POST, side_effect=AssertionError('no time, no call')):
            self.post(f'refine/{other}/approve/')
        asked = Claude(answer=bot(THEN))
        with patch(POST, side_effect=asked):
            example = self.get(f'refine/{other}/preview/').data['preview']
        self.assertEqual((example['after'], example['note'], asked.count()), (THEN, refine.EXAMPLE_AFTER_ONLY, (0, 1, 0)))


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class ApproveTests(TeachTestCase):
    def test_approve_applies_with_history_and_the_bot_is_asked_again(self):
        thread = self.taught()['thread']
        proposal = KnowledgeProposal.objects.get()
        asked = Claude(answer=bot(THEN), verdict=verdict())
        with patch(POST, side_effect=asked):
            response = self.post(f'refine/{thread}/approve/')
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data['state'], data['state_label'], data['status']), ('applied', 'נשמר', 'done'))
        self.assertEqual((data['needs_approval'], data['can_undo'], data['reply']), (False, True, refine.TEACH_APPROVED))
        self.assertEqual([(card['proposal_id'], card['applied'], card['status_label']) for card in data['applied']], [(proposal.id, True, 'נשמר')])
        self.assertEqual(data['changes'], data['applied'])

        item = KnowledgeItem.objects.get(title='מחיר: קודם שואלים איזה חוג')
        self.assertEqual((item.kind, item.body, item.is_active, item.version, item.updated_by), ('behavior_rule', NEW_BODY, True, 1, self.manager))
        history = KnowledgeHistory.objects.get(item=item)
        self.assertEqual((history.note, history.before, history.changed_by), (f'ללמד את הבוט: {TEXT}', None, self.manager))
        proposal.refresh_from_db()
        self.assertEqual((proposal.status, proposal.auto_applied, proposal.applied_item_id, proposal.applied_version), ('applied', False, item.id, 1))
        self.assertEqual((proposal.decided_by, proposal.decision_note), (self.manager, 'אושר ב"ללמד את הבוט"'))
        # His own click: it is not one of the day's automatic changes, and "עודכן לבד" does not list it.
        self.assertEqual(reviewer.auto_applied_today(), 0)
        self.assertEqual(self.get('review/proposals/', status='applied', auto='1').data, [])

        # The check: the question of the example, asked again for real — the knowledge now holds the change.
        check = data['check']
        self.assertEqual((check['ran'], check['passed'], check['pending']), (1, 1, False))
        self.assertEqual(check['lines'], [f'שאלתי "{ASKED}" — הבוט שאל קודם על איזה חוג מדובר ✓'])
        self.assertEqual(check['summary'], 'בדקתי שיחה אחת — הבוט עונה נכון')
        self.assertEqual(check['details'], [{'question': ASKED, 'answer': THEN, 'passed': True, 'line': 'הבוט שאל קודם על איזה חוג מדובר'}])
        self.assertEqual(asked.count(), (0, 1, 1))
        self.assertIn(f'[#{item.id}] מחיר: קודם שואלים איזה חוג: {NEW_BODY}', shadow.system_text(asked.asked['answer'][0]))
        judged = asked.asked['verdict'][0]
        self.assertEqual(judged['output_config']['effort'], 'low')
        told = judged['messages'][0]['content']
        self.assertIn('כששואלים על מחיר, הבוט ישאל קודם על איזה חוג מדובר ורק אז יענה', told)
        self.assertIn(f'- מעכשיו הבוט ישאל על איזה חוג מדובר לפני שיענה על מחיר: {NEW_BODY}', told)
        self.assertIn(f'# שאלה שלקוח עשוי לכתוב (נתונים)\n"""\n{ASKED}\n"""', told)
        self.assertIn(f'# מה הבוט עונה עליה עכשיו (נתונים — את זה שופטים)\n"""\n{THEN}\n"""', told)
        self.assertNotIn(THEN, shadow.system_text(judged))

        # Pressed twice: nothing more is applied, nobody is asked again, and the answer is the same.
        with patch(POST, side_effect=AssertionError('a second press reached Claude')):
            again = self.post(f'refine/{thread}/approve/')
            self.assertEqual((again.status_code, again.data['applied'], again.data['check']), (200, [], check))
            self.assertEqual(self.get(f'refine/{thread}/check/').data, {'thread': thread, 'check': check})
            self.assertEqual(self.get(f'refine/{thread}/').data['check'], check)
        self.assertEqual((KnowledgeItem.objects.count(), KnowledgeHistory.objects.filter(item=item).count()), (4, 1))
        self.assertEqual(RefineTurn.objects.filter(meta__outcome='approved').count(), 1)

    def test_a_check_that_fails_is_said_and_takes_nothing_back_and_undo_is_his(self):
        thread = self.taught(self.edited_rule())['thread']
        failing = Claude(answer=bot(TODAY), verdict=verdict(False, 'הבוט עדיין ענה על המחיר בלי לשאול על איזה חוג'))
        with patch(POST, side_effect=failing):
            data = self.post(f'refine/{thread}/approve/').data
        check = data['check']
        self.assertEqual((check['ran'], check['passed'], check['pending']), (1, 0, False))
        self.assertEqual(check['lines'], [f'שאלתי "{ASKED}" — הבוט עדיין ענה על המחיר בלי לשאול על איזה חוג ✗'])
        self.assertEqual(check['summary'], f'השיחה שבדקתי לא עברה: שאלתי "{ASKED}" — הבוט עדיין ענה על המחיר בלי לשאול על איזה חוג')
        self.price.refresh_from_db()
        self.assertEqual((self.price.body, self.price.version, data['state'], data['can_undo']), (NEW_BODY, 2, 'applied', True))

        undone = self.post(f'refine/{thread}/undo/')
        self.assertEqual(undone.status_code, 200, undone.data)
        self.price.refresh_from_db()
        self.assertEqual((self.price.body, self.price.version, self.price.is_active), (OLD_BODY, 3, True))
        self.assertEqual((undone.data['state'], undone.data['state_label'], undone.data['reply']), ('undone', 'בוטל', refine.TEACH_UNDONE))
        self.assertEqual((undone.data['check'], undone.data['needs_approval'], undone.data['can_undo']), (None, False, False))
        self.assertEqual([card['status_label'] for card in undone.data['changes']], ['בוטל'])
        self.assertEqual(KnowledgeProposal.objects.get().status, 'undone')
        # Nothing waits any more: a press on "מאשר" now is refused, and a second "בטל" too.
        self.assertEqual(self.post(f'refine/{thread}/approve/').status_code, 409)
        self.assertEqual(self.post(f'refine/{thread}/undo/').status_code, 409)
        with patch(POST, side_effect=AssertionError('nothing is in force, nothing to check')):
            self.assertEqual(self.get(f'refine/{thread}/check/').data['check']['summary'], check['summary'])

    def test_a_check_that_did_not_fit_stays_pending_and_the_next_call_carries_on(self):
        thread = self.taught()['thread']
        # Too little time to start an answer of the bot's: the change is approved all the same, and the check waits.
        with patch.object(refine, 'CHECK_SECONDS', 5), patch(POST, side_effect=AssertionError('no time, no call')):
            data = self.post(f'refine/{thread}/approve/').data
        self.assertEqual((data['state'], data['check']['pending'], data['check']['ran'], data['check']['summary']), ('applied', True, 0, 'הבדיקה עוד רצה'))
        self.assertTrue(KnowledgeItem.objects.get(title='מחיר: קודם שואלים איזה חוג').is_active)
        # The answer came, and the time for the verdict did not: the answer is kept, and only the verdict is asked next.
        asked = Claude(answer=bot(THEN), verdict=verdict())
        with patch(POST, side_effect=asked):
            with patch.object(refine, 'VERDICT_NEEDS_SECONDS', 10 ** 6):
                half = self.get(f'refine/{thread}/check/').data['check']
            self.assertEqual((half['pending'], half['ran'], asked.count()), (True, 0, (0, 1, 0)))
            done = self.get(f'refine/{thread}/check/').data['check']
        self.assertEqual((done['pending'], done['ran'], done['passed'], asked.count()), (False, 1, 1, (0, 1, 1)))

    def test_taken_back_while_the_check_was_still_to_run_there_is_nothing_left_to_check(self):
        thread = self.taught()['thread']
        with patch.object(refine, 'CHECK_SECONDS', 5), patch(POST, side_effect=AssertionError('no time, no call')):
            self.assertTrue(self.post(f'refine/{thread}/approve/').data['check']['pending'])
        self.post(f'refine/{thread}/undo/')
        with patch(POST, side_effect=AssertionError('nothing is in force, nobody is asked')):
            check = self.get(f'refine/{thread}/check/').data['check']
            self.assertEqual((check['pending'], check['ran'], check['summary']), (False, 0, 'השינוי כבר לא בתוקף, אז אין מה לבדוק'))
            self.assertEqual(self.get(f'refine/{thread}/check/').data['check'], check)

    def test_a_check_nobody_could_run_says_so_and_is_not_tried_for_ever(self):
        thread = self.taught()['thread']
        down = Claude(answer=requests.ConnectionError('down'))
        with patch(POST, side_effect=down):
            first = self.post(f'refine/{thread}/approve/').data['check']
            second = self.get(f'refine/{thread}/check/').data['check']
            third = self.get(f'refine/{thread}/check/').data['check']
        self.assertEqual([state['pending'] for state in (first, second, third)], [True, False, False])
        self.assertEqual((second['ran'], second['lines'], second['summary']), (0, [], 'לא הצלחתי להריץ את הבדיקה כרגע'))
        self.assertEqual(down.count(), (0, 2, 0))
        # And with no question for the example there is nothing to ask, and nobody is asked.
        other = self.taught(asks='')['thread']
        with patch(POST, side_effect=AssertionError('no question, no call')):
            check = self.post(f'refine/{other}/approve/').data['check']
        self.assertEqual((check['ran'], check['pending'], check['summary']), (0, False, 'אין לי שיחת בדיקה שנוגעת לשינוי הזה'))

    def test_the_bots_own_test_conversations_are_run_when_this_deployment_has_them(self):
        thread = self.taught()['thread']
        suite = types.ModuleType('apps.wahub.evals')
        suite.related_checks = MagicMock(return_value=[
            {'name': 'הורה שואל כמה עולה חוג בלי לומר איזה', 'passed': True, 'line': 'הבוט שאל קודם על איזה חוג'},
            {'name': 'הורה שואל על מחיר של קפוארה', 'passed': True, 'line': 'הבוט ענה מהמערכת'},
            {'name': 'סבתא שואלת על מחיר', 'passed': False, 'line': 'הבוט ענה מחיר בלי לשאול'},
            {'name': 'אחת יותר מדי', 'passed': True, 'line': 'לא נספרת'},
        ])
        with patch.dict(sys.modules, {'apps.wahub.evals': suite}), patch(POST, side_effect=AssertionError('the suite ran; nobody else is asked')):
            check = self.post(f'refine/{thread}/approve/').data['check']
        self.assertEqual((check['ran'], check['passed'], check['pending']), (3, 2, False))
        self.assertEqual(check['lines'][0], 'הורה שואל כמה עולה חוג בלי לומר איזה — הבוט שאל קודם על איזה חוג ✓')
        self.assertEqual(check['summary'], 'אחת מתוך שלוש לא עברה: סבתא שואלת על מחיר — הבוט ענה מחיר בלי לשאול')
        told, asked = suite.related_checks.call_args.args[0], suite.related_checks.call_args.kwargs
        self.assertIn(NEW_BODY, told)
        self.assertEqual((asked['limit'], isinstance(asked['deadline'], float), asked['kept']), (3, True, None))
        self.assertTrue(check['technical']['from_suite'])

        # A suite that breaks does not break the approval: the example's question is asked in its place.
        other = self.taught()['thread']
        suite.related_checks = MagicMock(side_effect=RuntimeError('boom'))
        with patch.dict(sys.modules, {'apps.wahub.evals': suite}), patch(POST, side_effect=Claude(answer=bot(THEN), verdict=verdict())):
            check = self.post(f'refine/{other}/approve/').data['check']
        self.assertEqual((check['ran'], check['passed']), (1, 1))

    def test_a_test_conversation_the_suite_did_not_finish_is_carried_on_by_the_next_call(self):
        thread = self.taught()['thread']
        suite = types.ModuleType('apps.wahub.evals')
        suite.related_checks = MagicMock(side_effect=[
            [{'name': 'הורה שואל על מחיר', 'passed': True, 'line': 'הבוט שאל על איזה חוג'}, {'name': 'סבתא שואלת על מחיר', 'done': False, 'run': 7}],
            [{'name': 'הורה שואל על מחיר', 'passed': True, 'line': 'הבוט שאל על איזה חוג'}, {'name': 'סבתא שואלת על מחיר', 'passed': True, 'line': 'הבוט שאל גם אותה', 'run': 7}],
        ])
        with patch.dict(sys.modules, {'apps.wahub.evals': suite}), patch(POST, side_effect=AssertionError('the suite runs its own conversations')):
            first = self.post(f'refine/{thread}/approve/').data['check']
            self.assertEqual((first['ran'], first['passed'], first['pending'], first['summary']), (1, 1, True, 'הבדיקה עוד רצה'))
            done = self.get(f'refine/{thread}/check/').data['check']
            self.assertEqual((done['ran'], done['passed'], done['pending'], done['summary']), (2, 2, False, 'בדקתי 2 שיחות — הבוט עונה נכון'))
            self.assertEqual(self.get(f'refine/{thread}/check/').data['check'], done)
        self.assertEqual(suite.related_checks.call_count, 2)
        # What it left unfinished is handed back to it as it gave it — its own run number with it.
        handed = suite.related_checks.call_args.kwargs['kept']
        self.assertEqual([(row['name'], row['done'], row.get('run')) for row in handed], [('הורה שואל על מחיר', True, None), ('סבתא שואלת על מחיר', False, 7)])

        # A suite that stops answering in the middle does not leave the check pending for ever.
        other = self.taught()['thread']
        suite.related_checks = MagicMock(side_effect=[[{'name': 'הורה שואל על מחיר', 'done': False}], RuntimeError('boom')])
        with patch.dict(sys.modules, {'apps.wahub.evals': suite}), patch(POST, side_effect=AssertionError('nobody else is asked')):
            self.assertTrue(self.post(f'refine/{other}/approve/').data['check']['pending'])
            given_up = self.get(f'refine/{other}/check/').data['check']
        self.assertEqual((given_up['pending'], given_up['ran'], given_up['summary']), (False, 0, 'לא הצלחתי להריץ את הבדיקה כרגע'))

    def test_an_approval_that_cannot_be_saved_changes_nothing(self):
        phrase = change(action='create', kind='phrasing', key='price_first', title='קודם איזה חוג', body='על איזה חוג מדובר?')
        thread = self.taught(self.new_rule(), phrase)['thread']
        # Somebody made a phrasing by that key in the meantime: the second change is refused, so the first is not kept either.
        knowledge.create_item({'kind': 'phrasing', 'key': 'price_first', 'title': 'אחר', 'body': 'משהו אחר'})
        with patch(POST, side_effect=AssertionError('nothing was applied, nothing to check')):
            response = self.post(f'refine/{thread}/approve/')
        self.assertEqual((response.status_code, response.data['detail']), (400, refine.TEACH_NOT_SAVED))
        self.assertIn('price_first', response.data['technical'])
        self.assertFalse(KnowledgeItem.objects.filter(title='מחיר: קודם שואלים איזה חוג').exists())
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'pending'})
        self.assertFalse(RefineTurn.objects.filter(meta__outcome='approved').exists())

    def test_an_approval_that_comes_after_the_item_was_edited_does_not_write_over_the_edit(self):
        data = self.taught(self.edited_rule(), change(action='deactivate', item_id=self.parking.id, kind='fact'))
        thread = data['thread']
        self.assertEqual(([card['moved'] for card in data['changes']], data['needs_approval']), ([False, False], True))
        # The thread waited; meanwhile somebody edited the rule on the knowledge screen.
        knowledge.update_item(self.price, {'body': 'עונים מחיר רק אחרי שיודעים את גיל הילד'}, self.manager, note='עודכן מהמסך')
        late = self.get(f'refine/{thread}/').data
        self.assertEqual(([card['moved'] for card in late['changes']], late['needs_approval'], late['notes']), ([True, False], False, [refine.TEACH_MOVED]))
        with patch(POST, side_effect=AssertionError('nothing was applied, nothing to check')):
            response = self.post(f'refine/{thread}/approve/')
        self.assertEqual((response.status_code, response.data['detail']), (409, refine.TEACH_MOVED))
        self.price.refresh_from_db()
        self.parking.refresh_from_db()
        self.assertEqual((self.price.body, self.price.version, self.parking.is_active), ('עונים מחיר רק אחרי שיודעים את גיל הילד', 2, True))
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'pending'})
        # Explained again, the change is proposed against what the item says now — and that one can be approved.
        again = self.edited_rule(body='קודם שואלים על איזה חוג ועל גיל הילד, ורק אז עונים מחיר.')
        with patch(POST, side_effect=Claude(reading=taught(again, earlier='replace'))):
            fresh = self.post(f'refine/{thread}/say/', {'text': 'גם גיל וגם איזה חוג'}).data
        self.assertEqual(([card['moved'] for card in fresh['changes']], fresh['needs_approval'], fresh['notes']), ([False], True, []))
        self.assertEqual(fresh['changes'][0]['plain']['before'], 'עונים מחיר רק אחרי שיודעים את גיל הילד')
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            self.assertEqual(self.post(f'refine/{thread}/approve/').status_code, 200)
        self.price.refresh_from_db()
        self.assertEqual(self.price.body, 'קודם שואלים על איזה חוג ועל גיל הילד, ורק אז עונים מחיר.')
        # An item switched off in between is "moved" too: a change to it would be written on something nobody reads.
        other = self.taught(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה חינם'))
        knowledge.soft_delete(self.parking, self.manager)
        self.assertEqual(self.post(f'refine/{other["thread"]}/approve/').status_code, 409)
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.body, self.parking.is_active), ('יש חניה', False))

    def test_the_suite_is_not_asked_for_ever_for_a_conversation_it_never_finishes(self):
        thread = self.taught()['thread']
        suite = types.ModuleType('apps.wahub.evals')
        suite.related_checks = MagicMock(return_value=[{'name': 'הורה שואל על מחיר', 'done': False, 'run': 7}])
        with patch.dict(sys.modules, {'apps.wahub.evals': suite}), patch.object(refine, 'MAX_SUITE_CALLS', 3), \
                patch(POST, side_effect=AssertionError('the suite runs its own conversations')):
            states = [self.post(f'refine/{thread}/approve/').data['check']] + [self.get(f'refine/{thread}/check/').data['check'] for _ in range(4)]
        self.assertEqual([state['pending'] for state in states], [True, True, True, False, False])
        self.assertEqual((states[-1]['ran'], states[-1]['summary'], suite.related_checks.call_count), (0, 'לא הצלחתי להריץ את הבדיקה כרגע', 3))

    def test_approve_is_for_a_teach_thread_only(self):
        self.incoming('אפשר להישאר עם הילד בשיעור?')
        self.bot_reply('בטח, אין בעיה!')
        who = self.contact()
        settings_store.set_review_auto_apply(False, self.manager)
        with patch(POST, side_effect=Claude(reading=claude({
            'understood': 'ההורים מחכים בחוץ', 'question': '', 'confidence': 'high', 'earlier_changes': 'keep', 'changes': [self.new_rule()],
        }))):
            fix = self.post('refine/', {'contact': who.id, 'message_ids': [Message.objects.last().id], 'text': 'זה לא נכון'}).data
        self.assertNotIn('mode', fix)
        self.assertEqual(fix['changes'][0]['action'], 'create')
        response = self.post(f'refine/{fix["thread"]}/approve/')
        self.assertEqual((response.status_code, response.data['detail']), (400, refine.TEACH_ONLY))
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')
        self.assertEqual(self.get(f'refine/{fix["thread"]}/check/').data['check'], None)
        self.assertEqual(self.get(f'refine/{fix["thread"]}/preview/').data['preview'], None)
        # A "דיוק" with the standing approval on is still applied at once, as it always was.
        settings_store.set_review_auto_apply(True, self.manager)
        with patch(POST, side_effect=Claude(reading=claude({
            'understood': 'ההורים מחכים בחוץ', 'question': '', 'confidence': 'high', 'earlier_changes': 'keep',
            'changes': [self.new_rule(title='הורים מחכים בחוץ')],
        }))):
            at_once = self.post('refine/', {'contact': who.id, 'message_ids': [Message.objects.last().id], 'text': 'זה לא נכון'}).data
        self.assertEqual((at_once['status'], at_once['changes'][0]['applied']), ('done', True))
        self.assertEqual([row['thread'] for row in self.get('refine/').data], [at_once['thread'], fix['thread']])


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class SayAndUndoTests(TeachTestCase):
    def test_not_exactly_takes_down_what_waited_and_the_right_one_waits_in_its_place(self):
        first = self.taught()
        wrong = KnowledgeProposal.objects.get()
        right = self.new_rule(title='מחיר: רק כשיש כמה חוגים', body='כשבסניף יש כמה חוגים ושואלים על מחיר — קודם שואלים על איזה חוג.', headline='')
        asked = Claude(reading=taught(right, earlier='replace', understood='רק כשיש כמה חוגים בסניף שואלים קודם על איזה חוג'))
        with patch(POST, side_effect=asked):
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'לא בדיוק, רק כשיש כמה חוגים באותו סניף'}).data
        wrong.refresh_from_db()
        self.assertEqual((wrong.status, wrong.decision_note, wrong.applied_item_id), ('undone', 'הוחלף בהמשך ההסבר', None))
        self.assertEqual([card['plain']['after'] for card in second['changes']], ['כשבסניף יש כמה חוגים ושואלים על מחיר — קודם שואלים על איזה חוג.'])
        self.assertEqual(second['changes'][0]['plain']['headline'], 'מעכשיו הבוט יפעל כך: מחיר: רק כשיש כמה חוגים')
        self.assertEqual(second['changes'][0]['plain']['why'], 'כך הסברת: "לא בדיוק, רק כשיש כמה חוגים באותו סניף"')
        self.assertIn('הורדתי את מה שהצעתי קודם', second['reply'])
        self.assertEqual((second['understood'], second['needs_approval'], KnowledgeItem.objects.count()), ('רק כשיש כמה חוגים בסניף שואלים קודם על איזה חוג', True, 3))
        self.assertEqual([card['status'] for card in second['turns'][1]['changes']], ['undone'])
        # Claude was told what the thread had proposed, and that it still waited.
        told = asked.asked['reading'][0]['messages'][0]['content']
        self.assertIn('# מה כבר הוצע או אושר בשיחה הזאת\n- [ממתין, לא הוחל] create · behavior_rule · רשומה חדשה', told)
        # "מאשר" applies what stands — the right one, and only it.
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            approved = self.post(f'refine/{first["thread"]}/approve/').data
        self.assertEqual(len(approved['applied']), 1)
        self.assertEqual(list(KnowledgeItem.objects.filter(kind='behavior_rule').order_by('id').values_list('title', flat=True)), ['מחיר', 'מחיר: רק כשיש כמה חוגים'])

    def test_something_added_waits_beside_what_waits_and_one_press_applies_both(self):
        first = self.taught()
        more = change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה חינם ליד הסניף')
        with patch(POST, side_effect=Claude(reading=taught(more, earlier='keep', understood='וגם: החניה ליד הסניף חינם'))):
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'ותוסיף שהחניה חינם'}).data
        self.assertEqual([(card['plain']['is_new'], card['status']) for card in second['changes']], [(True, 'pending'), (False, 'pending')])
        self.assertEqual((second['changes'][1]['plain']['before'], second['changes'][1]['plain']['after']), ('יש חניה', 'יש חניה חינם ליד הסניף'))
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            approved = self.post(f'refine/{first["thread"]}/approve/').data
        self.assertEqual(len(approved['applied']), 2)
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.body, KnowledgeItem.objects.filter(title='מחיר: קודם שואלים איזה חוג', is_active=True).count()), ('יש חניה חינם ליד הסניף', 1))
        # Each item's history quotes the words its own change came from.
        self.assertEqual(KnowledgeHistory.objects.get(item=self.parking, version=2).note, 'ללמד את הבוט: ותוסיף שהחניה חינם')
        self.assertEqual(KnowledgeHistory.objects.get(item__title='מחיר: קודם שואלים איזה חוג').note, f'ללמד את הבוט: {TEXT}')

        # Once approved, "לא בדיוק" moves nothing in the knowledge by itself: the correction waits for its own press.
        item = KnowledgeItem.objects.get(title='מחיר: קודם שואלים איזה חוג')
        sharper = change(action='update', item_id=item.id, kind='behavior_rule', body='קודם שואלים על איזה חוג ובאיזה סניף.')
        with patch(POST, side_effect=Claude(reading=taught(sharper, earlier='replace', confidence='high'))):
            third = self.post(f'refine/{first["thread"]}/say/', {'text': 'לא בדיוק, גם באיזה סניף'}).data
        item.refresh_from_db()
        self.parking.refresh_from_db()
        self.assertEqual((item.body, item.is_active, self.parking.body), (NEW_BODY, True, 'יש חניה חינם ליד הסניף'))
        self.assertEqual(list(KnowledgeProposal.objects.order_by('id').values_list('status', flat=True)), ['applied', 'applied', 'pending'])
        self.assertEqual((third['state'], third['needs_approval'], third['check']), ('waiting', True, None))
        self.assertNotIn('הורדתי', third['reply'])
        # "בטל" walks all of it back, the newest first: what waited is withdrawn, what was approved is undone.
        undone = self.post(f'refine/{first["thread"]}/undo/').data
        item.refresh_from_db()
        self.parking.refresh_from_db()
        self.assertEqual((item.is_active, self.parking.body, undone['state']), (False, 'יש חניה', 'undone'))
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'undone'})

    def test_words_that_change_nothing_leave_what_waits_waiting_and_say_so(self):
        first = self.taught()
        # "מאשר" typed in the field is not a press on the button: nothing is approved by it, and the answer must not read as if it were.
        with patch(POST, side_effect=Claude(reading=taught(understood='אתה מאשר את מה שהצעתי', asks=''))):
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'כן, מאשר'}).data
        self.assertEqual((second['status'], second['state'], second['needs_approval']), ('open', 'waiting', True))
        self.assertEqual(second['reply'], f'הבנתי כך: אתה מאשר את מה שהצעתי\n{refine.TEACH_STILL_WAITS}')
        self.assertNotIn('לא שיניתי דבר', second['reply'])
        # The card still tells of the change that waits, not of the "כן" written since.
        self.assertEqual((second['understood'], second['changes'], second['preview']), (first['understood'], first['changes'], first['preview']))
        self.assertEqual((KnowledgeProposal.objects.get().status, KnowledgeItem.objects.count()), ('pending', 3))
        self.assertEqual(self.get('refine/', mode='teach').data['results'][0]['understood'], first['understood'])

    def test_undo_that_is_refused_says_so_plainly_and_keeps_the_reason_for_the_details(self):
        thread = self.taught()['thread']
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            self.post(f'refine/{thread}/approve/')
        reason = 'אי אפשר לבטל: כבר קיים נוסח פעיל עם המפתח "price_first".'
        with patch('apps.wahub.refine.reviewer.undo', side_effect=reviewer.ReviewError(reason)):
            response = self.post(f'refine/{thread}/undo/')
        self.assertEqual((response.status_code, response.data['detail'], response.data['technical']), (400, refine.TEACH_NOT_UNDONE, reason))
        self.assertEqual((KnowledgeProposal.objects.get().status, self.get(f'refine/{thread}/').data['state']), ('applied', 'applied'))

    def test_undo_of_what_only_waited_withdraws_it(self):
        data = self.taught()
        undone = self.post(f'refine/{data["thread"]}/undo/')
        self.assertEqual((undone.status_code, undone.data['reply'], undone.data['state']), (200, refine.TEACH_WITHDRAWN, 'undone'))
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.decision_note, proposal.applied_item_id), ('undone', 'בוטל ב"ללמד את הבוט"', None))
        # An example nobody made is not shown, and the screen is not sent to ask for one.
        self.assertEqual((KnowledgeItem.objects.count(), undone.data['needs_approval'], undone.data['preview']), (3, False, None))
        with patch(POST, side_effect=AssertionError('nothing stands, nobody is asked')):
            gone = self.get(f'refine/{data["thread"]}/preview/').data['preview']
        self.assertEqual(gone, {'question': ASKED, 'before': None, 'after': None, 'pending': False})
        response = self.post(f'refine/{data["thread"]}/approve/')
        self.assertEqual((response.status_code, response.data['detail']), (409, refine.TEACH_NOTHING_WAITS))
        self.assertEqual(KnowledgeItem.objects.count(), 3)


class NobodyReadTests(TeachTestCase):
    @override_settings(ANTHROPIC_API_KEY='')
    def test_without_a_key_his_words_wait_as_written_and_the_answer_says_so_plainly(self):
        with patch(POST, side_effect=AssertionError('no key, no call')):
            response = self.teach()
            self.assertEqual(response.status_code, 201, response.data)
            data = response.data
            self.assertEqual((data['mode'], data['status'], data['state'], data['understood'], data['question']), ('teach', 'open', 'waiting', '', None))
            self.assertEqual((data['preview'], data['conflicts'], data['needs_approval'], data['check']), (None, [], True, None))
            self.assertEqual(data['reply'], f'{refine.TEACH_NO_KEY}. {refine.TEACH_KEPT}')
            for word in ('Claude', 'Anthropic', 'מפתח', 'stub', 'כלל התנהגות', 'ביקורת'):
                self.assertNotIn(word, data['reply'])
            # Why nobody read it is there for whoever unfolds the details, and only there.
            self.assertEqual((data['technical']['model'], data['technical']['reason'], data['technical']['outcome']), ('stub', 'אין מפתח Anthropic', 'stub'))
            card = data['changes'][0]
            self.assertEqual((card['plain']['headline'], card['plain']['after'], card['plain']['is_new']), ('מעכשיו הבוט יפעל לפי מה שכתבת, מילה במילה', TEXT, True))
            self.assertTrue(card['technical']['kept_as_written'])
            proposal = KnowledgeProposal.objects.get()
            self.assertEqual((proposal.source, proposal.status, proposal.pattern, proposal.change['after']['body']), ('refine', 'pending', 'refine_stub', TEXT))
            self.assertEqual(KnowledgeItem.objects.count(), 3)
            self.assertEqual(self.get(f'refine/{data["thread"]}/preview/').data['preview'], None)

            # He may approve his own words as they are; there is nobody to check them with, and that is said.
            approved = self.post(f'refine/{data["thread"]}/approve/')
            self.assertEqual(approved.status_code, 200, approved.data)
            self.assertEqual((approved.data['state'], approved.data['check']['ran'], approved.data['check']['pending']), ('applied', 0, False))
            self.assertEqual(approved.data['check']['summary'], 'אין לי שיחת בדיקה שנוגעת לשינוי הזה')
        item = KnowledgeItem.objects.get(body=TEXT)
        self.assertEqual((item.kind, item.is_active, KnowledgeHistory.objects.get(item=item).note), ('behavior_rule', True, f'ללמד את הבוט: {TEXT}'))
        for path, text in shown(data) + shown(approved.data):
            for word in CODE_NAMES + JARGON:
                self.assertNotIn(word, text, f'{path}: {text}')

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_when_claude_does_not_answer_the_words_are_kept_and_the_next_reading_takes_their_place(self):
        with patch(POST, side_effect=Claude(reading=requests.Timeout('slow'))):
            data = self.teach().data
        self.assertEqual(data['reply'], f'{refine.TEACH_NO_ANSWER}. {refine.TEACH_KEPT}')
        self.assertEqual((data['technical']['reason'], data['changes'][0]['status']), ('Claude לא ענה (Timeout)', 'pending'))
        kept = KnowledgeProposal.objects.get()
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule()))):
            read = self.post(f'refine/{data["thread"]}/say/', {'text': 'כלומר: קודם איזה חוג'}).data
        kept.refresh_from_db()
        self.assertEqual((kept.status, [card['plain']['after'] for card in read['changes']]), ('undone', [NEW_BODY]))
        self.assertNotIn('הורדתי', read['reply'])
        # And a failure once the thread has a reading of its own leaves that reading where it is.
        with patch(POST, side_effect=Claude(reading=requests.ConnectionError('down'))):
            later = self.post(f'refine/{data["thread"]}/say/', {'text': 'ועוד משהו'}).data
        self.assertEqual(later['reply'], f'{refine.TEACH_NO_ANSWER}. {refine.TEACH_STAYS}')
        self.assertEqual(([card['status'] for card in later['changes']], later['needs_approval'], later['understood']), (['pending'], True, read['understood']))


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class PlainWordsTests(TeachTestCase):
    def test_a_price_is_never_written_whoever_asks(self):
        priced = self.new_rule(body='חוג קפוארה עולה ₪260 לחודש')
        with patch(POST, side_effect=Claude(reading=taught(priced, understood='הבוט יגיד שהחוג עולה 260'))):
            data = self.teach('תכתוב שקפוארה עולה 260 שקל').data
        self.assertEqual((data['changes'], data['needs_approval'], data['state'], data['state_label']), ([], False, 'nothing', 'לא שונה דבר'))
        # What Claude said it understood describes a change that was refused: the refusal is the answer.
        self.assertEqual((data['understood'], data['notes'], data['preview']), ('', [refine.NOTE_MONEY], None))
        self.assertEqual(data['reply'], f'{refine.NOTE_MONEY}\nלא שיניתי דבר')
        self.assertEqual((KnowledgeProposal.objects.count(), data['status']), (0, 'done'))

    def test_a_price_in_an_example_is_a_price_in_the_knowledge(self):
        # An example is shown to the bot as an answer to give: a sum there is refused like a sum in the rule itself.
        priced = self.new_rule(example_good='החוג עולה ₪250 לחודש')
        with patch(POST, side_effect=Claude(reading=taught(priced, self.edited_rule()))):
            data = self.teach().data
        self.assertEqual(([card['plain']['is_new'] for card in data['changes']], data['notes']), ([False], [refine.NOTE_MONEY]))
        self.assertEqual(KnowledgeProposal.objects.count(), 1)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_his_own_words_with_a_price_in_them_are_not_written_by_a_press_either(self):
        data = self.teach('חוג קפוארה עולה ₪260 לחודש').data
        self.assertEqual(data['changes'][0]['status'], 'pending')
        response = self.post(f'refine/{data["thread"]}/approve/')
        self.assertEqual((response.status_code, response.data['detail']), (400, refine.NOTE_MONEY))
        self.assertEqual((KnowledgeItem.objects.count(), KnowledgeProposal.objects.get().status), (3, 'pending'))

    def test_what_is_edited_on_its_own_screen_is_not_edited_from_here(self):
        moved = change(action='update', item_id=self.person.id, kind='contact', body='הנציגה כבר לא במשרד')
        gone = change(action='update', item_id=999999, kind='fact', body='משהו')
        with patch(POST, side_effect=Claude(reading=taught(moved, gone, understood='הנציגה כבר לא במשרד'))):
            data = self.teach('הנציגה כבר לא עובדת במשרד').data
        self.assertEqual((data['changes'], data['notes']), ([], [refine.NOTE_SCREEN, refine.TEACH_NOTE_GONE]))
        self.assertEqual(KnowledgeProposal.objects.count(), 0)

    def test_conflicts_name_only_items_that_are_there_and_in_their_own_title(self):
        knowledge.soft_delete(self.parking)
        phrase = knowledge.create_item({'kind': 'phrasing', 'key': 'price_now', 'body': 'המחיר מופיע בקישור ההרשמה, אפשר לראות שם את כל הפרטים'})
        named = [
            {'item_id': self.price.id, 'why': 'ב-[#%d] (behavior_rule) כתוב שעונים מחיר מיד.' % self.price.id},
            {'item_id': self.price.id, 'why': 'פעם שנייה'},
            {'item_id': self.parking.id, 'why': 'כבר לא בשימוש'},
            {'item_id': 999999, 'why': 'לא קיים'},
            {'item_id': str(phrase.id), 'why': 'שם נאמר שהמחיר בקישור'},
            {'item_id': self.person.id, 'why': ''},
        ]
        with patch(POST, side_effect=Claude(reading=taught(self.new_rule(), conflicts=named))):
            data = self.teach().data
        self.assertEqual(data['conflicts'], [
            {'item_id': self.price.id, 'title': 'מחיר', 'why': 'ב (כלל התנהגות) כתוב שעונים מחיר מיד'},      # scrubbed, not reworded
            {'item_id': phrase.id, 'title': 'המחיר מופיע בקישור ההרשמה, אפשר לראות שם את כל הפרטים', 'why': 'שם נאמר שהמחיר בקישור'},
        ])

    def test_no_code_name_reaches_the_owner_outside_technical(self):
        nickname = change(action='create', kind='alias', what_customer_writes='קפו', means='קפוארה')
        edit = self.edited_rule(when_to_say='if_asked', example_good='על איזה חוג מדובר?', headline='לא משפט של "מעכשיו"')
        off = change(action='deactivate', item_id=self.parking.id, kind='fact')
        leaky = taught(
            edit, nickname, off, understood=f'עדכנתי את [#{self.price.id}] (behavior_rule) ואת when_to_say שלו.',
            asks=f'כמה עולה חוג? #{self.price.id}', conflicts=[{'item_id': self.price.id, 'why': f'ה-behavior_rule #{self.price.id} אומר אחרת'}],
        )
        with patch(POST, side_effect=Claude(reading=leaky)):
            data = self.teach().data
        self.assertEqual(data['understood'], 'עדכנתי את (כלל התנהגות) ואת מתי אומרים שלו')
        self.assertEqual(data['preview']['question'], 'כמה עולה חוג?')
        update, alias, switched = (card['plain'] for card in data['changes'])
        self.assertEqual(update['headline'], 'מעכשיו הבוט יפעל אחרת בנושא "מחיר"')
        self.assertEqual(update['before'], f'{OLD_BODY}\nמתי אומרים: מיוזמתו כשרלוונטי')
        self.assertEqual(update['after'], f'{NEW_BODY}\nמתי אומרים: רק אם שואלים\nדוגמה לתשובה טובה: על איזה חוג מדובר?')
        self.assertEqual((alias['headline'], alias['after']), ('מעכשיו הבוט מבין שכשלקוח כותב "קפו" — הכוונה לקפוארה', 'כשלקוח כותב "קפו" — הכוונה לקפוארה'))
        self.assertEqual((switched['headline'], switched['before'], switched['after']), ('מעכשיו הבוט יפסיק לפעול לפי "חניה"', 'יש חניה', None))
        self.assertEqual(switched['rule'], 'הבוט יפסיק לפעול לפי זה: יש חניה')
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict(line=f'הבוט נהג לפי behavior_rule [#{self.price.id}].'))):
            self.get(f'refine/{data["thread"]}/preview/')
            self.get(f'refine/{data["thread"]}/preview/')
            approved = self.post(f'refine/{data["thread"]}/approve/').data
        self.assertEqual(approved['check']['lines'], [f'שאלתי "{ASKED}" — הבוט נהג לפי כלל התנהגות ✓'])
        self.assertEqual(data['conflicts'][0]['why'], 'ה-כלל התנהגות אומר אחרת')
        answers = [data, approved, self.get('refine/', mode='teach').data, self.post(f'refine/{data["thread"]}/undo/').data]
        for answer in answers:
            texts = shown(answer)
            self.assertTrue(texts)
            for path, text in texts:
                for word in CODE_NAMES:
                    self.assertNotIn(word, text, f'{path}: {text}')
        # The code names are where the folded details read them.
        self.assertEqual([card['technical']['kind'] for card in data['changes']], ['behavior_rule', 'alias', 'fact'])
        self.assertEqual(data['changes'][0]['technical']['when_label'], 'רק אם שואלים')

    def test_plain_words(self):
        for raw, plain in (
            ('הבוט ישאל קודם  על איזה\nחוג.', 'הבוט ישאל קודם על איזה חוג'),
            ('לפי [#12] ו-#7', 'לפי ו'),
            ('ה-behavior_rule וה-when_to_say', 'ה-כלל התנהגות וה-מתי אומרים'),
            ('שיניתי ל-if_asked במקום proactive', 'שיניתי ל-רק אם שואלים במקום מיוזמתו'),
            ('זה fact ולא phrasing', 'זה עובדה ולא נוסח'),
            # A code name that is part of an address, or of a longer word, is left alone.
            ('ראו https://kogomelo.co.il/contact/link', 'ראו https://kogomelo.co.il/contact/link'),
            ('factory', 'factory'),
        ):
            self.assertEqual(refine._plain_words(raw), plain)
        self.assertEqual(refine._plain_words('א' * 50, 10), 'א' * 10)


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class ListTests(TeachTestCase):
    def test_what_you_taught_so_far_newest_first_a_page_at_a_time(self):
        applied = self.taught()['thread']
        with patch(POST, side_effect=Claude(answer=bot(), verdict=verdict())):
            self.post(f'refine/{applied}/approve/')
        undone = self.taught(self.edited_rule(), understood='קודם שואלים איזה חוג')['thread']
        self.post(f'refine/{undone}/undo/')
        waiting = self.taught(self.edited_rule(), change(action='deactivate', item_id=self.parking.id, kind='fact'))['thread']
        with patch(POST, side_effect=Claude(reading=taught(question='למה הכוונה?', confidence='low', understood=''))):
            asked = self.teach('משהו לא ברור עם ההרשמה').data['thread']
        with patch(POST, side_effect=Claude(reading=taught(understood='את המחיר מתקנים בכרטיס החוג'))):
            nothing = self.teach('תעלה את המחיר').data['thread']
        # A "דיוק" on a conversation is not something he taught here.
        self.incoming('שלום')
        with patch(POST, side_effect=Claude(reading=claude({'understood': 'x', 'question': 'מה?', 'confidence': 'low', 'earlier_changes': 'keep', 'changes': []}))):
            self.post('refine/', {'contact': self.contact().id, 'message_ids': [Message.objects.last().id], 'text': 'לא טוב'})

        with patch(POST, side_effect=AssertionError('a list reached Claude')):
            first = self.get('refine/', mode='teach', page_size=3)
            self.assertEqual(first.status_code, 200, first.data)
            self.assertEqual((first.data['count'], first.data['previous'], bool(first.data['next'])), (5, None, True))
            self.assertEqual([row['thread'] for row in first.data['results']], [nothing, asked, waiting])
            second = self.get('refine/', mode='teach', page_size=3, page=2).data
            self.assertEqual(([row['thread'] for row in second['results']], second['next']), ([undone, applied], None))
            missing = self.get('refine/', mode='teach', page=9)
            self.assertEqual((missing.status_code, missing.data['detail']), (404, 'אין עמוד כזה.'))
        rows = {row['thread']: row for row in first.data['results'] + second['results']}
        self.assertEqual(set(rows[applied]), {
            'thread', 'at', 'by', 'origin', 'understood', 'text', 'state', 'state_label', 'needs_answer', 'changes_count',
        })
        # Written from his head: nowhere to say it came from.
        self.assertEqual(rows[applied]['origin'], {'kind': 'free', 'label': '', 'contact_id': None, 'contact_name': None, 'link': None})
        self.assertEqual(
            {name: (row['state'], row['state_label'], row['changes_count'], row['needs_answer']) for name, row in (
                ('applied', rows[applied]), ('undone', rows[undone]), ('waiting', rows[waiting]), ('asked', rows[asked]), ('nothing', rows[nothing]),
            )},
            {
                'applied': ('applied', 'נשמר', 1, False), 'undone': ('undone', 'בוטל', 1, False), 'waiting': ('waiting', 'מחכה לאישור שלך', 2, False),
                'asked': ('waiting', 'מחכה לתשובה שלך', 0, True), 'nothing': ('nothing', 'לא שונה דבר', 0, False),
            },
        )
        self.assertEqual((rows[applied]['by'], rows[applied]['text'], rows[applied]['at'][:4]), ('דנה מנהלת', TEXT, str(refine.state.now_israel_date().year)))
        self.assertEqual(rows[undone]['understood'], 'קודם שואלים איזה חוג')
        # Nobody understood it yet: the line shows his own words, never nothing.
        self.assertEqual(rows[asked]['understood'], 'משהו לא ברור עם ההרשמה')
        # A proposal of his rejected from the review screen reads as taken back here.
        reviewer.reject(KnowledgeProposal.objects.filter(status='pending').first(), self.manager, 'לא')
        reviewer.reject(KnowledgeProposal.objects.filter(status='pending').first(), self.manager, 'לא')
        self.assertEqual(self.get(f'refine/{waiting}/').data['state'], 'undone')


@override_settings(ANTHROPIC_API_KEY='test-key')
class PermissionTests(TeachTestCase):
    def test_managers_only(self):
        thread = self.taught()['thread']
        before = self.untouched()
        calls = [
            ('post', 'refine/', {'text': 'משהו ללמד את הבוט'}),
            ('get', 'refine/?mode=teach', None),
            ('get', f'refine/{thread}/', None),
            ('get', f'refine/{thread}/preview/', None),
            ('get', f'refine/{thread}/check/', None),
            ('post', f'refine/{thread}/say/', {'text': 'לא בדיוק'}),
            ('post', f'refine/{thread}/approve/', {}),
            ('post', f'refine/{thread}/undo/', {}),
        ]
        partner = client_for(make_user('partner@wahub.test', UserProfile.ROLE_PARTNER))
        instructor = client_for(make_user('worker@wahub.test', UserProfile.ROLE_WORKER))
        with patch(POST, side_effect=AssertionError('a refused caller reached Claude')):
            for name, client, allowed in (('partner', partner, (403,)), ('instructor', instructor, (403,)), ('stranger', APIClient(), (401, 403))):
                for method, path, data in calls:
                    with self.subTest(who=name, call=f'{method} {path}'):
                        response = getattr(client, method)(f'{BASE}/{path}', data, format='json') if data is not None \
                            else getattr(client, method)(f'{BASE}/{path}')
                        self.assertIn(response.status_code, allowed)
        self.assertEqual(self.untouched(), before)
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.count(), KnowledgeProposal.objects.get().status), (1, 2, 'pending'))
