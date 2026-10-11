"""
"דיוק" and the automatic approval (docs/WAHUB-CONTRACT-REFINE.md): a clear fix is
applied at once with its history and its approved proposal; a question, a doubt,
the switch turned off or a full day leave it waiting; "say" corrects, "undo"
gives the knowledge back; a price is never written; no key keeps the text as a
waiting rule; managers only — and the sweep's own proposals under the same
standing approval. And the "smart summary" (docs/WAHUB-CONTRACT-POLISH-1.md, ב):
with no text the system speaks first — a summary and a question, nothing changed
and nothing proposed — and her answer goes on as the first text of any thread.
Claude is a mock throughout: nothing here reaches the network.
"""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.wahub import knowledge, refine, reviewer, settings_store
from apps.wahub.models import (
    Contact,
    KnowledgeHistory,
    KnowledgeItem,
    KnowledgeProposal,
    Message,
    RefineThread,
    RefineTurn,
    ShadowReply,
    WahubSetting,
)
from apps.wahub.tests.base import BASE, PHONE, WahubTestCase, client_for, make_user

# `requests.post` itself: refine.py and reviewer.py both reach Claude through it.
POST = 'apps.wahub.refine.requests.post'
EMPTY = {
    'action': 'create', 'item_id': 0, 'kind': 'behavior_rule', 'key': '', 'title': '', 'body': '', 'when_to_say': '',
    'example_good': '', 'example_bad': '', 'what_customer_writes': '', 'means': '', 'headline': '',
}


def change(**fields) -> dict:
    """One change as the schema makes Claude write it: every field there, the unused ones empty."""
    return {**EMPTY, **fields}


def said(*changes, understood='הבוט ענה שאפשר להישאר בשיעור; מעכשיו יגיד שההורים מחכים בחוץ', question='', confidence='high',
         earlier='keep') -> MagicMock:
    """Claude's answer to a "דיוק", as the Messages API returns it."""
    answer = {'understood': understood, 'question': question, 'confidence': confidence, 'earlier_changes': earlier, 'changes': list(changes)}
    response = MagicMock(status_code=200)
    response.json.return_value = {
        'model': 'claude-opus-5-5', 'stop_reason': 'end_turn',
        'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
    }
    return response


def summed(happened='הלקוח שאל אם אפשר להישאר עם הילד בשיעור', answered='הבוט הישן ענה שאפשר, בלי בעיה',
           suspected='נראה שהתשובה לא נכונה: לפי מה שידוע לבוט, ההורים מחכים מחוץ לשיעור', stop='end_turn', **extra) -> MagicMock:
    """Claude's summary of what she picked (the system speaks first), as the Messages API returns it."""
    answer = {'what_happened': happened, 'what_was_answered': answered, 'suspected_issue': suspected, **extra}
    response = MagicMock(status_code=200)
    response.json.return_value = {
        'model': 'claude-opus-5-5', 'stop_reason': stop,
        'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
    }
    return response


def worded(*, kind='fact', title='לא נשארים עם הילד', body='ההורים לא נשארים בשיעור; מחכים בחוץ', understood=True, confidence='high') -> MagicMock:
    """Claude's wording of a proposal of the sweep (reviewer.formulate)."""
    answer = {
        'title': f'עובדה: {title}', 'explanation': 'הבוט ענה אחרת; הנציגה תיקנה.', 'kind': kind, 'understood': understood, 'confidence': confidence,
        'after': {'title': title, 'body': body, 'key': '', 'when_to_say': 'if_asked', 'example_good': '', 'example_bad': '',
                  'what_customer_writes': '', 'means': ''},
    }
    response = MagicMock(status_code=200)
    response.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}
    return response


class RefineTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        # Nothing here may send, or touch ManyChat, ever.
        patch('apps.wahub.sending._deliver', side_effect=AssertionError('a "דיוק" tried to send')).start()
        patch('apps.wahub.handoff._in_manychat', side_effect=AssertionError('a "דיוק" tried to touch ManyChat')).start()
        self.addCleanup(patch.stopall)
        self.parking = knowledge.create_item({'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה'})
        self.rule = knowledge.create_item({'kind': 'behavior_rule', 'title': 'הורים בשיעור', 'body': 'אפשר להישאר עם הילד בשיעור'})
        self.person = knowledge.create_item({'kind': 'contact', 'title': 'שלי', 'name': 'שלי', 'role': 'משרד', 'phone': '0509424755'})
        self.incoming('אפשר להישאר עם הילד בשיעור?')
        self.bot_reply('בטח, אין בעיה!')
        self.who = self.contact()
        self.asked, self.answered = Message.objects.filter(contact=self.who).order_by('id')

    def line(self, contact, text, *, direction='out', sender='bot', minutes_ago=0, **extra):
        return Message.objects.create(
            contact=contact, direction=direction, sender=sender, text=text, source='manychat',
            status='received' if direction == 'in' else 'sent', sent_at=timezone.now() - timedelta(minutes=minutes_ago), **extra,
        )

    def refine(self, text='הבוט אמר שאפשר להישאר בשיעור וזה לא נכון, ההורים מחכים בחוץ', message_ids=None, **extra):
        return self.post('refine/', {'contact': self.who.id, 'message_ids': message_ids or [self.answered.id], 'text': text, **extra})

    def begin(self, message_ids=None, **extra):
        """ "המשך" on the picked messages with nothing written: the system is asked to speak first."""
        return self.post('refine/', {'contact': self.who.id, 'message_ids': message_ids or [self.asked.id, self.answered.id], 'text': '', **extra})

    def new_rule(self, **fields):
        return change(**{'action': 'create', 'kind': 'behavior_rule', 'title': 'הורים לא נשארים בשיעור',
                         'body': 'ההורים לא נשארים בשיעור. מחכים בחוץ עד סוף השיעור.', **fields})


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class AppliedAtOnceTests(RefineTestCase):
    def test_a_clear_fix_is_applied_at_once_with_history_and_an_approved_proposal(self):
        messages_before = Message.objects.count()
        with patch(POST, return_value=said(self.new_rule())) as post:
            response = self.refine()
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(set(data) >= {'thread', 'status', 'reply', 'needs_answer', 'changes', 'turns'}, True)
        self.assertEqual((data['status'], data['needs_answer']), ('done', False))
        self.assertIn('הבנתי: הבוט ענה שאפשר להישאר בשיעור', data['reply'])
        self.assertIn('שיניתי: הוספתי כלל התנהגות "הורים לא נשארים בשיעור"', data['reply'])

        item = KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור')
        self.assertEqual((item.kind, item.is_active, item.version, item.updated_by), ('behavior_rule', True, 1, self.manager))
        history = KnowledgeHistory.objects.get(item=item)
        self.assertEqual(history.note, 'דיוק: הבוט אמר שאפשר להישאר בשיעור וזה לא נכון, ההורים מחכים בחוץ')
        self.assertEqual((history.before, history.changed_by), (None, self.manager))

        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.status, proposal.auto_applied), ('refine', 'applied', True))
        self.assertEqual((proposal.decision_note, proposal.applied_item_id, proposal.applied_version), ('אושר אוטומטית', item.id, 1))
        self.assertEqual((proposal.contact_id, proposal.message_id, proposal.decided_by), (self.who.id, self.answered.id, self.manager))
        self.assertEqual([row['who'] for row in proposal.evidence], ['bot', 'office'])
        self.assertIn('דיוק: הבוט אמר שאפשר להישאר', proposal.evidence[-1]['text'])

        card = data['changes'][0]
        self.assertEqual((card['proposal_id'], card['action'], card['item_id'], card['kind_label']), (proposal.id, 'create', item.id, 'כלל התנהגות'))
        self.assertEqual((card['title'], card['before'], card['applied'], card['status']), ('הורים לא נשארים בשיעור', None, True, 'applied'))
        self.assertEqual(card['after']['body'], 'ההורים לא נשארים בשיעור. מחכים בחוץ עד סוף השיעור.')
        self.assertEqual([(turn['role'], bool(turn['changes'])) for turn in data['turns']], [('operator', False), ('system', True)])
        self.assertEqual(data['turns'][0]['by_name'], 'דנה מנהלת')

        # The thread is there to come back to; the review shows the change as one that was made by itself.
        self.assertEqual(self.get(f'refine/{data["thread"]}/').data['changes'], data['changes'])
        self.assertEqual([row['thread'] for row in self.get('refine/', contact=self.who.id).data], [data['thread']])
        self.assertEqual(self.get('refine/', contact=999999).data, [])
        listed = self.get('review/proposals/', status='applied', auto='1').data
        self.assertEqual([(row['id'], row['source_label'], row['auto_applied'], row['can_undo']) for row in listed], [(proposal.id, 'דיוק', True, True)])
        self.assertEqual(self.get('review/summary/').data['auto_applied_7d'], 1)

        # A change to the knowledge and nothing else: no message, nothing on the contact.
        self.assertEqual(Message.objects.count(), messages_before)
        self.who.refresh_from_db()
        self.assertEqual((self.who.followup_status, self.who.handled_by, self.who.needs_human), ('', 'bot', False))
        self.assertEqual(post.call_count, 1)

    def test_what_claude_is_shown(self):
        other = self.make_contact(phone='972505550177')
        rows = [self.line(other, f'הודעה {number:02d}', direction='in' if number % 2 else 'out', sender='customer' if number % 2 else 'bot',
                          minutes_ago=60 - number) for number in range(1, 31)]
        ShadowReply.objects.create(contact=other, after_message=rows[14], text='תשובת הצל של הבוט החדש', knowledge_used=[self.parking.id])
        with patch(POST, return_value=said(self.new_rule())) as post:
            response = self.post('refine/', {'contact': other.id, 'message_ids': [rows[14].id], 'text': 'כאן הבוט לא ענה על מה שנשאל'})
        self.assertEqual(response.status_code, 201, response.data)
        body, headers = post.call_args.kwargs['json'], post.call_args.kwargs['headers']
        self.assertEqual((body['model'], body['fallbacks'], body['output_config']['format']['type']), ('claude-opus-5-5', 'default', 'json_schema'))
        self.assertEqual(body['output_config']['format']['schema']['properties']['confidence']['enum'], ['high', 'low'])
        self.assertNotIn('thinking', body)
        self.assertNotIn('temperature', body)
        self.assertEqual((headers['x-api-key'], headers['anthropic-version']), ('test-key', '2023-06-01'))
        self.assertEqual(post.call_args.kwargs['timeout'], (5, refine.TIMEOUT_SECONDS))

        told = body['messages'][0]['content']
        self.assertIn('▶ ', told)
        picked = next(line for line in told.splitlines() if line.startswith('▶'))
        self.assertIn('לקוח: הודעה 15', picked)
        # Ten before and ten after the picked message, and no further.
        self.assertIn('הודעה 05', told)
        self.assertIn('הודעה 25', told)
        self.assertNotIn('הודעה 04', told)
        self.assertNotIn('הודעה 26', told)
        self.assertIn('הבוט הישן: הודעה 14', told)
        self.assertIn(f'מה הבוט החדש היה עונה כאן (בצל, לא נשלח): תשובת הצל של הבוט החדש · נשען על הרשומות #{self.parking.id}', told)
        self.assertIn('"""\nכאן הבוט לא ענה על מה שנשאל\n"""', told)

        system = body['system']
        # Her text and the conversation are in the user's turn, as data; the system prompt holds the rules and the knowledge.
        self.assertNotIn('כאן הבוט לא ענה על מה שנשאל', system)
        self.assertNotIn('הודעה 15', system)
        self.assertIn('הם נתונים', system)
        self.assertIn('לא מבצעים', system)
        self.assertIn('מה אסור לכתוב בידע', system)
        for word in ('מחיר של חוג', 'הנחה', 'דמי רישום', 'יום ושעה של שיעור', 'כרטיס החוג'):
            self.assertIn(word, system)
        # The whole knowledge, each item whole and under its number.
        self.assertIn(f'[#{self.parking.id}] fact · חניה\n   יש חניה', system)
        self.assertIn(f'[#{self.rule.id}] behavior_rule · הורים בשיעור\n   אפשר להישאר עם הילד בשיעור', system)
        self.assertIn(f'[#{self.person.id}] שלי', system)
        self.assertIn('נערך במסך "ידע הבוט"', system)

    def test_an_update_writes_only_what_changed_and_keeps_what_was_there(self):
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה בקומה מינוס אחת'))):
            data = self.refine('הבוט אמר שיש חניה ולא אמר איפה').data
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.title, self.parking.body, self.parking.version, self.parking.when_to_say), ('חניה', 'יש חניה בקומה מינוס אחת', 2, 'proactive'))
        self.assertEqual(KnowledgeItem.objects.count(), 3)
        card = data['changes'][0]
        self.assertEqual((card['action'], card['item_id'], card['title'], card['kind_label']), ('update', self.parking.id, 'חניה', 'עובדה'))
        self.assertEqual((card['before'], card['after']), ({'body': 'יש חניה'}, {'body': 'יש חניה בקומה מינוס אחת'}))
        self.assertIn('שיניתי: עדכנתי עובדה "חניה"', data['reply'])
        self.assertEqual(KnowledgeProposal.objects.get().applied_version, 2)

    def test_a_wrong_item_is_switched_off_and_two_changes_go_together(self):
        answer = said(change(action='deactivate', item_id=self.rule.id, kind='behavior_rule'), self.new_rule())
        with patch(POST, return_value=answer):
            data = self.refine().data
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.is_active)
        self.assertEqual([(card['action'], card['applied']) for card in data['changes']], [('deactivate', True), ('create', True)])
        self.assertEqual((data['changes'][0]['before']['is_active'], data['changes'][0]['after']), (True, {'is_active': False}))
        self.assertIn('כיביתי כלל התנהגות "הורים בשיעור"; הוספתי כלל התנהגות "הורים לא נשארים בשיעור"', data['reply'])
        self.assertEqual(KnowledgeProposal.objects.filter(status='applied', auto_applied=True).count(), 2)

    def test_a_change_that_cannot_be_saved_leaves_everything_waiting(self):
        # Two changes; the second is refused by the knowledge — so the first is not left half done.
        bad = change(action='update', item_id=self.parking.id, kind='fact', when_to_say='if_asked')
        with patch(POST, return_value=said(self.new_rule(), bad)), \
                patch('apps.wahub.knowledge.update_item', side_effect=knowledge.KnowledgeError('שדה לא תקין')):
            data = self.refine().data
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'pending'})
        self.assertEqual((data['status'], [card['applied'] for card in data['changes']]), ('open', [False, False]))
        self.assertIn('לא הצלחתי לבצע: אי אפשר להחיל את ההצעה: שדה לא תקין', data['reply'])
        self.assertNotIn('שיניתי', data['reply'])

    def test_what_a_fix_may_not_touch_is_left_alone(self):
        profile = knowledge.create_item({'kind': 'profile', 'title': 'דנה', 'body': 'נציגת שירות'})
        answer = said(
            change(action='update', item_id=self.person.id, kind='fact', body='הטלפון של שלי הוא 050-0000000'),
            change(action='create', kind='link', title='קישור', body='https://example.com'),
            change(action='deactivate', item_id=profile.id, kind='profile'),
            change(action='update', item_id=999999, kind='fact', body='רשומה שאינה'),
            change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה'),       # says what is already there
            {'action': 'delete_everything', 'item_id': self.parking.id},
        )
        with patch(POST, return_value=answer):
            data = self.refine('תעדכן את הטלפון של שלי').data
        self.assertEqual((data['changes'], KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), ([], 0, 4))
        self.assertIn(refine.NOTE_SCREEN, data['reply'])
        self.assertIn(refine.NOTE_GONE, data['reply'])
        self.assertIn('לא שיניתי דבר בידע', data['reply'])
        self.assertNotIn('הבנתי:', data['reply'])          # the model's own summary described changes that were refused
        self.person.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual((self.person.version, profile.is_active, self.parking.version), (1, True, 1))


@override_settings(ANTHROPIC_API_KEY='test-key')
class WaitingTests(RefineTestCase):
    def test_a_question_applies_nothing_and_its_answer_does(self):
        asks = said(understood='הבוט ענה שאפשר להישאר בשיעור', question='מה כן נכון — ההורים מחכים בחוץ, או שאפשר להישאר בשיעור הראשון?', confidence='low')
        with patch(POST, return_value=asks):
            first = self.refine('זה לא נכון מה שהבוט ענה כאן').data
        self.assertEqual((first['status'], first['needs_answer'], first['changes']), ('open', True, []))
        self.assertIn('מה כן נכון', first['reply'])
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (0, 3))

        with patch(POST, return_value=said(self.new_rule())) as post:
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'מחכים בחוץ, תמיד'})
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual((second.data['status'], second.data['needs_answer']), ('done', False))
        self.assertTrue(second.data['changes'][0]['applied'])
        self.assertTrue(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור', is_active=True).exists())
        self.assertEqual(KnowledgeHistory.objects.get(item__title='הורים לא נשארים בשיעור').note, 'דיוק: מחכים בחוץ, תמיד')
        self.assertEqual([turn['role'] for turn in second.data['turns']], ['operator', 'system', 'operator', 'system'])
        # The second call carries the chat so far, and that nothing was changed yet.
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('המפעילה: """זה לא נכון מה שהבוט ענה כאן"""', told)
        self.assertIn('מה כן נכון', told)
        self.assertIn('(עדיין לא שונה ולא הוצע דבר)', told)
        self.assertIn('"""\nמחכים בחוץ, תמיד\n"""', told)

    def test_a_question_with_a_tentative_change_keeps_it_waiting(self):
        with patch(POST, return_value=said(self.new_rule(), question='גם בשיעור ניסיון?', confidence='low')):
            data = self.refine().data
        self.assertEqual((data['needs_answer'], data['status'], data['changes'][0]['applied'], data['changes'][0]['status']), (True, 'open', False, 'pending'))
        self.assertIn('ההצעה שלי: להוסיף כלל התנהגות', data['reply'])
        self.assertIn('עדיין לא שיניתי דבר', data['reply'])
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())
        # Her answer brings the final version in place of the tentative one.
        with patch(POST, return_value=said(self.new_rule(example_bad='בטח, אין בעיה!'), earlier='replace')):
            after = self.post(f'refine/{data["thread"]}/say/', {'text': 'כן, גם בניסיון'}).data
        self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['applied', 'undone'])
        self.assertEqual(KnowledgeProposal.objects.get(status='undone').decision_note, refine.NOTE_SUPERSEDED)
        self.assertEqual(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').count(), 1)
        self.assertIn('הורדתי את ההצעה הקודמת של השיחה הזאת', after['reply'])
        self.assertEqual(after['turns'][1]['changes'][0]['status'], 'undone')

    def test_a_doubt_leaves_the_proposal_waiting_for_a_click(self):
        with patch(POST, return_value=said(self.new_rule(), confidence='low')):
            data = self.refine().data
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.auto_applied, proposal.source), ('pending', False, 'refine'))
        self.assertEqual((data['needs_answer'], data['status'], data['changes'][0]['applied']), (True, 'open', False))
        self.assertIn('ההצעה ממתינה לאישור בביקורת', data['reply'])
        self.assertNotIn('שיניתי:', data['reply'])
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())
        # The owner's click still works on it, as on any proposal — and the thread shows it.
        approved = self.post(f'review/proposals/{proposal.id}/approve/')
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual((approved.data['proposal']['status'], approved.data['proposal']['auto_applied']), ('applied', False))
        self.assertTrue(self.get(f'refine/{data["thread"]}/').data['changes'][0]['applied'])

    def test_an_answer_that_does_not_say_how_sure_it_is_is_not_sure(self):
        response = said(self.new_rule())
        answer = json.loads(response.json.return_value['content'][1]['text'])
        del answer['confidence']
        response.json.return_value['content'][1]['text'] = json.dumps(answer, ensure_ascii=False)
        with patch(POST, return_value=response):
            self.refine()
        self.assertEqual(KnowledgeProposal.objects.get().status, 'pending')

    def test_with_the_switch_off_everything_waits(self):
        self.assertEqual(self.get('settings/review/').data['auto_apply'], True)          # on until somebody turns it off
        self.assertEqual(self.client.patch(f'{BASE}/settings/review/', {'auto_apply': 'no'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(f'{BASE}/settings/review/', {}, format='json').status_code, 400)
        off = self.client.patch(f'{BASE}/settings/review/', {'auto_apply': False}, format='json')
        self.assertEqual((off.status_code, off.data['auto_apply'], off.data['updated_by_name'], off.data['daily_cap']), (200, False, 'דנה מנהלת', 20))
        self.assertEqual(WahubSetting.objects.get(key='review_auto_apply').value, False)
        summary = self.get('review/summary/').data
        self.assertEqual((summary['auto_apply'], summary['auto_mode']), (False, False))

        with patch(POST, return_value=said(self.new_rule())):
            data = self.refine().data
        self.assertEqual((data['status'], data['needs_answer'], data['auto_apply']), ('done', False, False))
        self.assertEqual((KnowledgeProposal.objects.get().status, data['changes'][0]['applied']), ('pending', False))
        self.assertIn('האישור האוטומטי כבוי', data['reply'])
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())

        on = self.client.patch(f'{BASE}/settings/review/', {'auto_apply': True}, format='json')
        self.assertEqual((on.data['auto_apply'], self.get('review/summary/').data['auto_apply']), (True, True))

    def test_a_waiting_switch_off_is_applied_by_a_click_and_taken_back(self):
        settings_store.set_review_auto_apply(False)
        with patch(POST, return_value=said(change(action='deactivate', item_id=self.rule.id, kind='behavior_rule'))):
            data = self.refine().data
        proposal_id = data['changes'][0]['proposal_id']
        self.rule.refresh_from_db()
        self.assertTrue(self.rule.is_active)
        approved = self.post(f'review/proposals/{proposal_id}/approve/')
        self.assertEqual((approved.status_code, approved.data['item']['is_active'], approved.data['proposal']['can_undo']), (200, False, True))
        self.assertEqual(KnowledgeHistory.objects.get(item=self.rule, version=2).note, f'לפי הצעה #{proposal_id} מתוך שיחה {self.who.id}')
        self.assertEqual(self.post(f'review/proposals/{proposal_id}/undo/').data['item']['is_active'], True)

    def test_the_day_has_room_for_so_many_changes_and_no_more(self):
        def another(number):
            return said(change(action='create', kind='fact', title=f'עובדה {number}', body=f'גוף {number}'))

        with patch('apps.wahub.reviewer.AUTO_DAILY_CAP', 2):
            with patch(POST, return_value=another(1)):
                first = self.refine().data
            with patch(POST, return_value=another(2)):
                self.refine()
            self.assertEqual(self.get('settings/review/').data['auto_applied_today'], 2)
            with patch(POST, return_value=another(3)):
                third = self.refine().data
            self.assertEqual((third['changes'][0]['applied'], third['status']), (False, 'done'))
            self.assertIn('כבר נעשו היום 2 שינויים אוטומטיים', third['reply'])
            self.assertFalse(KnowledgeItem.objects.filter(title='עובדה 3').exists())
            self.assertEqual(KnowledgeProposal.objects.filter(status='pending').count(), 1)
            # A change that was taken back gives its place back.
            self.assertEqual(self.post(f'refine/{first["thread"]}/undo/').status_code, 200)
            with patch(POST, return_value=another(4)):
                fourth = self.refine().data
            self.assertTrue(fourth['changes'][0]['applied'])
            # Yesterday's changes do not count against today.
            KnowledgeProposal.objects.filter(status='applied').update(decided_at=timezone.now() - timedelta(days=1, hours=1))
            self.assertEqual(reviewer.auto_room(), 2)


@override_settings(ANTHROPIC_API_KEY='test-key')
class SayAndUndoTests(RefineTestCase):
    def test_say_corrects_takes_the_first_change_back_and_applies_the_right_one(self):
        with patch(POST, return_value=said(self.new_rule())):
            first = self.refine().data
        wrong = KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור')
        right = change(action='create', kind='behavior_rule', title='הורים בשיעור ניסיון', body='בשיעור ניסיון הורה יכול להישאר; בשיעור רגיל מחכים בחוץ.')
        with patch(POST, return_value=said(right, earlier='replace', understood='בשיעור ניסיון אפשר להישאר, בשיעור רגיל לא')) as post:
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'לא, התכוונתי שרק בשיעור ניסיון אפשר להישאר'}).data
        wrong.refresh_from_db()
        self.assertFalse(wrong.is_active)
        self.assertEqual(wrong.version, 2)                                        # switched off with a version, not erased
        self.assertEqual(KnowledgeHistory.objects.get(item=wrong, version=2).note, f'ביטול הצעה #{first["changes"][0]["proposal_id"]}')
        fixed = KnowledgeItem.objects.get(title='הורים בשיעור ניסיון')
        self.assertTrue(fixed.is_active)
        self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['applied', 'undone'])
        self.assertIn('ביטלתי את השינוי הקודם של השיחה הזאת', second['reply'])
        self.assertIn('שיניתי: הוספתי כלל התנהגות "הורים בשיעור ניסיון"', second['reply'])
        self.assertEqual((second['status'], second['changes'][0]['item_id'], second['changes'][0]['applied']), ('done', fixed.id, True))
        self.assertEqual([card['status'] for card in second['turns'][1]['changes']], ['undone'])
        # Claude was told what the thread had already put on the knowledge.
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn(f'[הוחל על הידע] create · behavior_rule · רשומה #{wrong.id}', told)

    def test_say_may_add_to_what_was_done_and_sharpen_the_item_the_thread_made(self):
        with patch(POST, return_value=said(self.new_rule())):
            first = self.refine().data
        item = KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור')
        sharper = change(action='update', item_id=item.id, kind='behavior_rule', body='ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.')
        with patch(POST, return_value=said(sharper, earlier='keep')):
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'ותוסיף שבניסיון אפשר להישאר'}).data
        item.refresh_from_db()
        self.assertEqual((item.body, item.version, item.is_active), ('ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.', 2, True))
        self.assertEqual(list(KnowledgeProposal.objects.order_by('id').values_list('status', flat=True)), ['applied', 'applied'])
        self.assertNotIn('ביטלתי', second['reply'])
        # "replace" that goes on editing the same item keeps it and edits it — no orphan is left behind.
        again = change(action='update', item_id=item.id, kind='behavior_rule', body='ההורים מחכים בחוץ, גם בשיעור ניסיון.')
        with patch(POST, return_value=said(again, earlier='replace')):
            third = self.post(f'refine/{first["thread"]}/say/', {'text': 'בעצם לא, גם בניסיון מחכים בחוץ'}).data
        item.refresh_from_db()
        self.assertEqual((item.body, item.is_active), ('ההורים מחכים בחוץ, גם בשיעור ניסיון.', True))
        self.assertEqual(KnowledgeItem.objects.filter(kind='behavior_rule').count(), 2)
        self.assertEqual(KnowledgeProposal.objects.filter(status='applied').count(), 3)
        # Undo of the thread walks all of it back, the newest first: the item it made is off, nothing else moved.
        undone = self.post(f'refine/{first["thread"]}/undo/')
        self.assertEqual(undone.status_code, 200, undone.data)
        item.refresh_from_db()
        self.assertEqual((item.is_active, item.body), (False, 'ההורים לא נשארים בשיעור. מחכים בחוץ עד סוף השיעור.'))
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'undone'})
        self.assertEqual(third['status'], 'done')

    def test_say_cancel_only_takes_back(self):
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='אין חניה'))):
            first = self.refine('אין שם חניה').data
        with patch(POST, return_value=said(earlier='replace', understood='טעיתי, יש חניה — מחזיר כמו שהיה')):
            second = self.post(f'refine/{first["thread"]}/say/', {'text': 'סליחה טעיתי, תבטל'}).data
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.body, self.parking.version), ('יש חניה', 3))
        self.assertEqual(KnowledgeProposal.objects.get().status, 'undone')
        self.assertIn('ביטלתי את השינוי הקודם של השיחה הזאת', second['reply'])
        self.assertEqual((second['status'], second['needs_answer']), ('done', False))
        # "Switch off what you made" is the same thing: the making is taken back, no second change is written.
        with patch(POST, return_value=said(self.new_rule())):
            third = self.refine().data
        made = third['changes'][0]['item_id']
        with patch(POST, return_value=said(change(action='deactivate', item_id=made, kind='behavior_rule'))):
            self.post(f'refine/{third["thread"]}/say/', {'text': 'תוריד את הכלל הזה'})
        self.assertFalse(KnowledgeItem.objects.get(pk=made).is_active)
        self.assertEqual(KnowledgeProposal.objects.filter(status='undone').count(), 2)
        self.assertEqual(KnowledgeProposal.objects.count(), 2)

    def test_a_question_or_a_doubt_never_takes_back_what_is_applied(self):
        with patch(POST, return_value=said(self.new_rule())):
            first = self.refine().data
        item = KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור')
        # "replace" beside a question: the model is not sure what she meant, so the knowledge stays as it is.
        with patch(POST, return_value=said(question='מה בדיוק לא נכון בכלל?', confidence='low', earlier='replace')):
            asked = self.post(f'refine/{first["thread"]}/say/', {'text': 'לא בדיוק'}).data
        item.refresh_from_db()
        self.assertEqual((item.is_active, asked['needs_answer'], KnowledgeProposal.objects.get().status), (True, True, 'applied'))
        self.assertNotIn('ביטלתי', asked['reply'])
        # "replace" with a guess it is not sure of: the guess waits, and the applied change still stands.
        guess = change(action='create', kind='fact', title='שיעור ניסיון', body='בשיעור ניסיון אפשר להישאר')
        with patch(POST, return_value=said(guess, confidence='low', earlier='replace')):
            unsure = self.post(f'refine/{first["thread"]}/say/', {'text': 'אולי רק בניסיון'}).data
        item.refresh_from_db()
        self.assertTrue(item.is_active)
        self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['applied', 'pending'])
        self.assertNotIn('ביטלתי', unsure['reply'])
        # Once she is clear, the guess that waited goes and the first change is taken back for the right one.
        with patch(POST, return_value=said(guess, earlier='replace')):
            clear = self.post(f'refine/{first["thread"]}/say/', {'text': 'כן, רק בשיעור ניסיון אפשר להישאר'}).data
        item.refresh_from_db()
        self.assertFalse(item.is_active)
        self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['applied', 'undone', 'undone'])
        self.assertIn('ביטלתי את השינוי הקודם של השיחה הזאת', clear['reply'])
        self.assertTrue(KnowledgeItem.objects.get(title='שיעור ניסיון').is_active)

    def test_undo_gives_the_knowledge_back_and_marks_the_proposal(self):
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה בקומה מינוס אחת', when_to_say='if_asked'))):
            data = self.refine('לא אמר איפה החניה').data
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.body, self.parking.when_to_say), ('יש חניה בקומה מינוס אחת', 'if_asked'))
        undone = self.post(f'refine/{data["thread"]}/undo/')
        self.assertEqual(undone.status_code, 200, undone.data)
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.body, self.parking.when_to_say, self.parking.version, self.parking.is_active), ('יש חניה', 'proactive', 3, True))
        self.assertEqual(KnowledgeHistory.objects.filter(item=self.parking).count(), 3)          # every step is still in the history
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.undone_by, proposal.auto_applied), ('undone', self.manager, True))
        self.assertIsNotNone(proposal.undone_at)
        self.assertEqual((undone.data['status'], undone.data['changes'][0]['applied'], undone.data['changes'][0]['status_label']), ('open', False, 'בוטל'))
        self.assertIn('הידע חזר למה שהיה לפניה', undone.data['reply'])
        self.assertEqual(self.get('review/summary/').data['auto_applied_7d'], 0)
        # Nothing is left to take back.
        self.assertEqual(self.post(f'refine/{data["thread"]}/undo/').status_code, 409)
        self.assertEqual(self.post('refine/999999/undo/').status_code, 404)

    def test_undo_takes_back_only_what_the_change_wrote(self):
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה בקומה מינוס אחת'))):
            data = self.refine('לא אמר איפה החניה').data
        # Meanwhile a person renamed the item on the knowledge screen.
        knowledge.update_item(self.parking, {'title': 'חניה בסניף'}, self.manager)
        self.assertEqual(self.post(f'refine/{data["thread"]}/undo/').status_code, 200)
        self.parking.refresh_from_db()
        self.assertEqual((self.parking.title, self.parking.body), ('חניה בסניף', 'יש חניה'))
        # And a field somebody rewrote since is left as that person wrote it.
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה בתשלום'))):
            again = self.refine('החניה בתשלום').data
        knowledge.update_item(self.parking, {'body': 'יש חניה, שעה ראשונה בלי תשלום'}, self.manager)
        self.assertEqual(self.post(f'refine/{again["thread"]}/undo/').status_code, 200)
        self.parking.refresh_from_db()
        self.assertEqual(self.parking.body, 'יש חניה, שעה ראשונה בלי תשלום')
        self.assertEqual(KnowledgeProposal.objects.filter(status='undone').count(), 2)

    def test_a_switched_off_item_comes_back_on_undo_and_a_waiting_proposal_is_only_withdrawn(self):
        with patch(POST, return_value=said(change(action='deactivate', item_id=self.rule.id, kind='behavior_rule'))):
            data = self.refine().data
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.is_active)
        self.post(f'refine/{data["thread"]}/undo/')
        self.rule.refresh_from_db()
        self.assertEqual((self.rule.is_active, self.rule.version), (True, 3))

        settings_store.set_review_auto_apply(False)
        with patch(POST, return_value=said(self.new_rule())):
            waiting = self.refine().data
        undone = self.post(f'refine/{waiting["thread"]}/undo/')
        self.assertEqual(undone.status_code, 200)
        self.assertIn('בידע לא שונה דבר', undone.data['reply'])
        self.assertEqual(KnowledgeProposal.objects.order_by('-id').first().status, 'undone')
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())

    def test_a_change_taken_back_from_the_review_shows_in_the_thread(self):
        with patch(POST, return_value=said(self.new_rule())):
            data = self.refine().data
        proposal_id = data['changes'][0]['proposal_id']
        undone = self.post(f'review/proposals/{proposal_id}/undo/')
        self.assertEqual(undone.status_code, 200, undone.data)
        self.assertEqual((undone.data['proposal']['status'], undone.data['proposal']['status_label'], undone.data['proposal']['can_undo']), ('undone', 'בוטל', False))
        self.assertEqual((undone.data['proposal']['undone_by_name'], undone.data['item']['is_active']), ('דנה מנהלת', False))
        self.assertEqual(self.post(f'review/proposals/{proposal_id}/undo/').status_code, 409)
        self.assertEqual(self.get(f'refine/{data["thread"]}/').data['changes'][0]['applied'], False)
        self.assertEqual(self.get('review/proposals/', status='undone').data[0]['id'], proposal_id)


@override_settings(ANTHROPIC_API_KEY='test-key')
class PriceTests(RefineTestCase):
    def test_asked_to_write_a_price_claude_explains_and_nothing_changes(self):
        explains = said(understood='מחיר לא נכתב בידע של הבוט: מתקנים אותו בכרטיס החוג ב-Kogo, והבוט קורא אותו משם')
        with patch(POST, return_value=explains):
            data = self.refine('הבוט אמר 260, תכתוב לו שהמחיר של קפוארה הוא 250 שקל').data
        self.assertEqual((data['changes'], data['needs_answer'], data['status']), ([], False, 'done'))
        self.assertIn('מתקנים אותו בכרטיס החוג ב-Kogo', data['reply'])
        self.assertIn('לא שיניתי דבר בידע', data['reply'])
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (0, 3))
        self.assertEqual(KnowledgeHistory.objects.count(), 3)

    def test_a_price_claude_wrote_all_the_same_never_reaches_the_knowledge(self):
        for body in ('חוג קפוארה עולה ₪250 לחודש', 'המחיר הוא 250 ש"ח', 'דמי הרישום 120 שקלים', 'הנחת אחים: 10₪'):
            with self.subTest(body=body):
                with patch(POST, return_value=said(change(action='create', kind='fact', title='מחיר', body=body))):
                    data = self.refine('תכתוב לו את המחיר').data
                self.assertEqual(data['changes'], [])
                self.assertIn('מתקנים את המחיר בכרטיס החוג ב-Kogo', data['reply'])
                self.assertNotIn('שיניתי:', data['reply'])
                self.assertNotIn('הבנתי:', data['reply'])
        with patch(POST, return_value=said(change(action='update', item_id=self.parking.id, kind='fact', body='יש חניה, 20 ש"ח לשעה'))):
            self.refine('תוסיף כמה עולה החניה')
        with patch(POST, return_value=said(change(action='create', kind='alias', what_customer_writes='כמה', means='המחיר הוא ₪260'))):
            self.refine('כינוי')
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (0, 3))
        self.parking.refresh_from_db()
        self.assertEqual(self.parking.body, 'יש חניה')

    def test_a_rule_about_how_to_show_a_price_is_not_a_price(self):
        rule = change(action='create', kind='style_rule', title='₪ לפני המספר', body='סימן השקל תמיד לפני המספר, ומחיר אומרים רק כששואלים.',
                      example_good='₪255', example_bad='255₪')
        with patch(POST, return_value=said(rule, self.new_rule())):
            data = self.refine('הבוט כתב את השקל אחרי המספר').data
        self.assertEqual([card['applied'] for card in data['changes']], [True, True])
        # One of two changes names a sum: it alone is left out, and the reply says why.
        with patch(POST, return_value=said(change(action='create', kind='fact', title='מחיר', body='₪260'),
                                           change(action='create', kind='fact', title='מים', body='להביא בקבוק מים'))):
            mixed = self.refine('שני דברים').data
        self.assertEqual([card['title'] for card in mixed['changes']], ['מים'])
        self.assertIn('שיניתי: הוספתי עובדה "מים"', mixed['reply'])
        self.assertIn('מתקנים את המחיר בכרטיס החוג ב-Kogo', mixed['reply'])
        self.assertFalse(KnowledgeItem.objects.filter(title='מחיר').exists())

    def test_how_a_sum_of_money_is_recognised(self):
        self.assertEqual(reviewer.money_in('המחיר ₪260, ובחודש הבא 280 ש"ח או 300 שקלים'), {'260', '280', '300'})
        self.assertEqual(reviewer.money_in('₪ לפני המספר; הילד בן 6; חוג ב-17:00'), set())
        # The ways a sum is really typed: with no quotation mark at all, with one, with two single ones, in English.
        self.assertEqual(
            reviewer.money_in("250 שח, או 260 ש'ח, או 270 ש׳׳ח, או 280 NIS, או 290שח"), {'250', '260', '270', '280', '290'},
        )
        # A number beside a word that only begins like it is not a sum.
        self.assertEqual(reviewer.money_in('3 שחקנים בקבוצה; 12 שחיינים; כיתה 4 שחר'), set())
        update = {'action': 'update', 'before': {'body': 'משלמים ₪50 מקדמה'}, 'after': {'body': 'משלמים ₪50 מקדמה בהרשמה'}}
        self.assertFalse(reviewer.names_money(update))                    # the sum was already there; the change did not bring it
        self.assertTrue(reviewer.names_money({**update, 'after': {'body': 'משלמים ₪60 מקדמה'}}))


class NobodyInterpretedTests(RefineTestCase):
    @override_settings(ANTHROPIC_API_KEY='')
    def test_without_a_key_the_text_waits_as_a_rule_and_the_reply_says_so(self):
        with patch(POST, side_effect=AssertionError('no key, no call')) as post:
            response = self.refine('הבוט לא צריך להבטיח שיחזרו אליו היום')
            data = response.data
            self.assertEqual(response.status_code, 201, data)
            proposal = KnowledgeProposal.objects.get()
            self.assertEqual((proposal.source, proposal.status, proposal.pattern), ('refine', 'pending', 'refine_stub'))
            self.assertEqual((proposal.change['action'], proposal.change['kind']), ('create', 'behavior_rule'))
            self.assertEqual(proposal.change['after']['body'], 'הבוט לא צריך להבטיח שיחזרו אליו היום')
            self.assertEqual((data['status'], data['needs_answer'], data['changes'][0]['applied']), ('open', False, False))
            self.assertIn('אין חיבור ל-Claude (חסר מפתח Anthropic)', data['reply'])
            self.assertIn('ממתינה לאישור בביקורת', data['reply'])
            self.assertEqual(KnowledgeItem.objects.count(), 3)
            # One more line from her: one proposal with everything she wrote, not two.
            more = self.post(f'refine/{data["thread"]}/say/', {'text': 'ושיגיד שחוזרים ביום העסקים הבא'}).data
        post.assert_not_called()
        self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['pending', 'undone'])
        waiting = KnowledgeProposal.objects.get(status='pending')
        self.assertEqual(waiting.change['after']['body'], 'הבוט לא צריך להבטיח שיחזרו אליו היום\nושיגיד שחוזרים ביום העסקים הבא')
        self.assertEqual(more['changes'][0]['proposal_id'], waiting.id)
        self.assertEqual(self.get('review/proposals/', status='pending').data[0]['source_label'], 'דיוק')

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_when_claude_does_not_answer_nothing_is_changed_and_the_text_is_kept(self):
        refused = MagicMock(status_code=200)
        refused.json.return_value = {'stop_reason': 'refusal', 'content': []}
        broken = MagicMock(status_code=200)
        broken.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'not json'}]}
        for answer, reason in (
            ({'side_effect': requests.Timeout('slow')}, 'Claude לא ענה (Timeout)'),
            ({'return_value': MagicMock(status_code=529)}, 'Claude ענה 529'),
            ({'return_value': refused}, 'Claude סירב לענות'),
            ({'return_value': broken}, 'תשובה לא קריאה מ-Claude'),
        ):
            with self.subTest(reason=reason):
                with patch(POST, **answer):
                    data = self.refine().data
                self.assertIn(f'לא הצלחתי לפענח כרגע ({reason})', data['reply'])
                self.assertEqual((data['status'], data['changes'][0]['status']), ('open', 'pending'))
        self.assertEqual(KnowledgeItem.objects.count(), 3)
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', 'pattern')), {('pending', 'refine_stub')})
        # Once Claude answers, the kept note gives way to what was understood.
        with patch(POST, return_value=said(self.new_rule())):
            fixed = self.post(f'refine/{data["thread"]}/say/', {'text': 'ההורים מחכים בחוץ'}).data
        self.assertTrue(fixed['changes'][0]['applied'])
        self.assertEqual(KnowledgeProposal.objects.get(pk=data['changes'][0]['proposal_id']).status, 'undone')
        self.assertNotIn('הורדתי את ההצעה', fixed['reply'])
        # And a failure after the thread has changes of its own leaves them as they are.
        with patch(POST, side_effect=requests.ConnectionError('down')):
            later = self.post(f'refine/{data["thread"]}/say/', {'text': 'ועוד משהו'}).data
        self.assertIn('נשאר כמו שהוא', later['reply'])
        self.assertTrue(later['changes'][0]['applied'])
        self.assertEqual(KnowledgeProposal.objects.get(pk=fixed['changes'][0]['proposal_id']).status, 'applied')


@override_settings(ANTHROPIC_API_KEY='test-key')
class DoorTests(RefineTestCase):
    def test_what_is_refused_at_the_door(self):
        other = self.make_contact(phone='972505550188')
        stranger = self.line(other, 'הודעה של מישהו אחר', direction='in', sender='customer')
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            for body, word in (
                # No text at all is the summary's door (SummaryFirstTests); a text that is not one is still refused.
                ({'contact': self.who.id, 'message_ids': [self.answered.id], 'text': 0}, 'נדרש טקסט'),
                ({'contact': self.who.id, 'message_ids': [self.answered.id], 'text': 'א' * 4001}, 'ארוך מדי'),
                ({'contact': self.who.id, 'message_ids': [], 'text': 'משהו לא טוב'}, 'לפחות הודעה אחת'),
                ({'contact': self.who.id, 'message_ids': 'x', 'text': 'משהו לא טוב'}, 'רשימה של מספרים'),
                ({'contact': self.who.id, 'message_ids': [stranger.id], 'text': 'משהו לא טוב'}, 'לא שייכת לשיחה'),
                ({'contact': 999999, 'message_ids': [self.answered.id], 'text': 'משהו לא טוב'}, 'איש הקשר לא נמצא'),
                ({'message_ids': [self.answered.id], 'text': 'משהו לא טוב'}, 'איש הקשר לא נמצא'),
                ({'contact': self.who.id, 'message_ids': [self.answered.id], 'shadow_ids': [999999], 'text': 'משהו'}, 'תשובת הצל'),
            ):
                with self.subTest(word=word):
                    response = self.post('refine/', body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(word, response.data['detail'])
            self.assertEqual(self.get('refine/', contact='x').status_code, 400)
            self.assertEqual(self.get('refine/999999/').status_code, 404)
            self.assertEqual(self.post('refine/999999/say/', {'text': 'x'}).status_code, 404)
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.count(), KnowledgeProposal.objects.count()), (0, 0, 0))
        with patch(POST, return_value=said(self.new_rule())):
            data = self.refine().data
            self.assertEqual(self.post(f'refine/{data["thread"]}/say/', {'text': '  '}).status_code, 400)
        self.assertEqual(RefineTurn.objects.count(), 2)

    def test_a_shadow_reply_can_be_what_she_points_at(self):
        reply = ShadowReply.objects.create(contact=self.who, after_message=self.asked, text='כן, אפשר להישאר')
        with patch(POST, return_value=said(self.new_rule())) as post:
            data = self.post('refine/', {'contact': self.who.id, 'shadow_ids': [reply.id], 'text': 'גם הבוט החדש טועה כאן'}).data
        self.assertEqual((data['message_ids'], data['shadow_ids']), ([self.asked.id], [reply.id]))
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('▶    ↳ מה הבוט החדש היה עונה כאן (בצל, לא נשלח): כן, אפשר להישאר', told)

    def test_managers_only(self):
        with patch(POST, return_value=said(self.new_rule())):
            thread = self.refine().data
        proposal_id = thread['changes'][0]['proposal_id']
        calls = [
            ('get', 'refine/', None),
            ('get', f'refine/{thread["thread"]}/', None),
            ('post', 'refine/', {'contact': self.who.id, 'message_ids': [self.answered.id], 'text': 'משהו לא טוב כאן'}),
            ('post', f'refine/{thread["thread"]}/say/', {'text': 'ועוד משהו'}),
            ('post', f'refine/{thread["thread"]}/undo/', {}),
            ('get', 'settings/review/', None),
            ('patch', 'settings/review/', {'auto_apply': False}),
            ('post', f'review/proposals/{proposal_id}/undo/', {}),
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
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.count()), (1, 2))
        self.assertEqual(KnowledgeProposal.objects.get().status, 'applied')
        self.assertTrue(settings_store.review_auto_apply())
        self.assertTrue(KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור').is_active)


# --- the system speaks first: the summary (docs/WAHUB-CONTRACT-POLISH-1.md, ב) ----------------------------

QUOTED = {
    'what_happened': 'הלקוח כתב: "אפשר להישאר עם הילד בשיעור?"',
    'what_was_answered': 'הבוט הישן: "בטח, אין בעיה!"',
    'suspected_issue': None,
}


@override_settings(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5')
class SummaryFirstTests(RefineTestCase):
    def nothing_touched(self, messages_before):
        """The summary is words on the screen: no proposal, nothing in the knowledge, no message, nothing on the contact."""
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count(), KnowledgeHistory.objects.count()), (0, 3, 3))
        self.assertEqual(Message.objects.count(), messages_before)
        self.who.refresh_from_db()
        self.assertEqual((self.who.followup_status, self.who.handled_by, self.who.needs_human), ('', 'bot', False))

    def test_no_text_opens_the_thread_with_a_summary_and_changes_nothing(self):
        self.assertTrue(settings_store.review_auto_apply())                       # the standing approval is on, and still
        messages_before = Message.objects.count()
        with patch(POST, return_value=summed()) as post:
            response = self.begin()
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(data['summary'], {
            'what_happened': 'הלקוח שאל אם אפשר להישאר עם הילד בשיעור',
            'what_was_answered': 'הבוט הישן ענה שאפשר, בלי בעיה',
            'suspected_issue': 'נראה שהתשובה לא נכונה: לפי מה שידוע לבוט, ההורים מחכים מחוץ לשיעור',
        })
        self.assertEqual((data['status'], data['needs_answer'], data['changes']), ('open', True, []))
        self.assertEqual(data['reply'], (
            'מה קרה: הלקוח שאל אם אפשר להישאר עם הילד בשיעור\n'
            'מה נענה: הבוט הישן ענה שאפשר, בלי בעיה\n'
            'מה נראה לא טוב: נראה שהתשובה לא נכונה: לפי מה שידוע לבוט, ההורים מחכים מחוץ לשיעור\n'
            'מה לא היה טוב כאן?'
        ))
        # The system's turn is the first of the thread, and the only one.
        turn, = data['turns']
        self.assertEqual((turn['role'], turn['outcome'], turn['needs_answer'], turn['changes'], turn['by_name']), ('system', 'summary', True, [], None))
        self.assertEqual((turn['summary'], turn['text']), (data['summary'], data['reply']))
        self.assertEqual((data['message_ids'], data['contact_id'], data['created_by_name']), ([self.asked.id, self.answered.id], self.who.id, 'דנה מנהלת'))

        # Kept in the turn's own JSON: no column was added for it.
        kept = RefineTurn.objects.get()
        self.assertEqual((kept.role, kept.needs_answer, kept.changes, kept.created_by), ('system', True, [], None))
        self.assertEqual((kept.meta['outcome'], kept.meta['model'], kept.meta['summary']), ('summary', 'claude-opus-5-5', data['summary']))
        self.assertEqual(RefineThread.objects.get().status, 'open')

        # Nothing was applied and nothing was proposed — and so nothing of it counts in the day.
        self.nothing_touched(messages_before)
        self.assertEqual((reviewer.auto_applied_today(), self.get('settings/review/').data['auto_applied_today']), (0, 0))
        self.assertEqual(self.get('review/proposals/').data, [])
        self.assertEqual(post.call_count, 1)

        # The thread is there to come back to, with its summary.
        again = self.get(f'refine/{data["thread"]}/').data
        self.assertEqual((again['summary'], again['turns'][0]['summary'], again['reply']), (data['summary'], data['summary'], data['reply']))
        self.assertEqual([row['summary'] for row in self.get('refine/', contact=self.who.id).data], [data['summary']])
        # And there is nothing in it to take back.
        self.assertEqual(self.post(f'refine/{data["thread"]}/undo/').status_code, 409)

    def test_a_text_that_is_missing_null_or_blank_is_no_text(self):
        for name, body in (('missing', {}), ('null', {'text': None}), ('blank', {'text': '  \n '})):
            with self.subTest(name=name):
                with patch(POST, return_value=summed()):
                    response = self.post('refine/', {'contact': self.who.id, 'message_ids': [self.answered.id], **body})
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual((response.data['turns'][0]['outcome'], response.data['needs_answer']), ('summary', True))
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.filter(role='system').count(), KnowledgeProposal.objects.count()), (3, 3, 0))

    def test_what_claude_is_shown_for_the_summary(self):
        other = self.make_contact(phone='972505550177')
        rows = [self.line(other, f'הודעה {number:02d}', direction='in' if number % 2 else 'out', sender='customer' if number % 2 else 'bot',
                          minutes_ago=60 - number) for number in range(1, 31)]
        ShadowReply.objects.create(contact=other, after_message=rows[14], text='תשובת הצל של הבוט החדש', knowledge_used=[self.parking.id])
        with patch(POST, return_value=summed()) as post:
            response = self.post('refine/', {'contact': other.id, 'message_ids': [rows[14].id, rows[15].id], 'text': ''})
        self.assertEqual(response.status_code, 201, response.data)
        body, headers = post.call_args.kwargs['json'], post.call_args.kwargs['headers']
        # The same call as the "דיוק" itself, with the summary's own schema — and less thought, for she is waiting.
        self.assertEqual((body['model'], body['fallbacks'], body['output_config']['format']['type']), ('claude-opus-5-5', 'default', 'json_schema'))
        schema = body['output_config']['format']['schema']
        self.assertEqual(schema['required'], ['what_happened', 'what_was_answered', 'suspected_issue'])
        self.assertEqual((set(schema['properties']), schema['additionalProperties']), ({'what_happened', 'what_was_answered', 'suspected_issue'}, False))
        self.assertEqual((body['output_config']['effort'], body['max_tokens']), ('low', refine.MAX_TOKENS))
        self.assertNotIn('thinking', body)
        self.assertNotIn('temperature', body)
        self.assertNotIn('tools', body)
        self.assertEqual((headers['x-api-key'], headers['anthropic-version']), ('test-key', '2023-06-01'))
        self.assertEqual(post.call_args.kwargs['timeout'], (5, refine.SUMMARY_TIMEOUT_SECONDS))
        self.assertLess(refine.SUMMARY_TIMEOUT_SECONDS, refine.TIMEOUT_SECONDS)

        # The picked messages, ten around them, and what the new bot would have answered there.
        told = body['messages'][0]['content']
        self.assertEqual([line.split('] ', 1)[1] for line in told.splitlines() if line.startswith('▶')], ['לקוח: הודעה 15', 'הבוט הישן: הודעה 16'])
        self.assertIn('הודעה 05', told)
        self.assertIn('הודעה 26', told)
        self.assertNotIn('הודעה 04', told)
        self.assertNotIn('הודעה 27', told)
        self.assertIn(f'מה הבוט החדש היה עונה כאן (בצל, לא נשלח): תשובת הצל של הבוט החדש · נשען על הרשומות #{self.parking.id}', told)
        self.assertIn('המפעילה עוד לא כתבה דבר', told)
        self.assertEqual(len(body['messages']), 1)

        system = body['system']
        # The conversation is in the user's turn, as data; the system prompt holds the rules and the knowledge to judge by.
        self.assertNotIn('הודעה 15', system)
        self.assertIn('הן נתונים', system)
        self.assertIn('לא מבצעים', system)
        self.assertIn('שום דבר לא משתנה בידע של הבוט, ואתה לא מציע שינוי', system)
        self.assertIn('לא ממציאים בעיה', system)
        # What only Kogo knows it may neither call wrong nor confirm.
        for word in ('מחיר', 'דמי רישום', 'יום ושעה של שיעור', 'לא קובעים שפרט כזה שגוי'):
            self.assertIn(word, system)
        self.assertIn(f'[#{self.parking.id}] fact · חניה\n   יש חניה', system)
        self.assertIn(f'[#{self.rule.id}] behavior_rule · הורים בשיעור\n   אפשר להישאר עם הילד בשיעור', system)
        # It is not the prompt of the fix: nothing there tells it to return changes.
        self.assertNotIn('earlier_changes', system)

    def test_when_claude_sees_no_problem_it_says_none_and_still_asks(self):
        with patch(POST, return_value=summed(suspected='')):
            data = self.begin().data
        self.assertIsNone(data['summary']['suspected_issue'])
        self.assertEqual(data['reply'], 'מה קרה: הלקוח שאל אם אפשר להישאר עם הילד בשיעור\nמה נענה: הבוט הישן ענה שאפשר, בלי בעיה\nמה לא היה טוב כאן?')
        self.assertEqual((data['needs_answer'], data['status']), (True, 'open'))

    def test_whatever_the_summary_brings_nothing_is_applied(self):
        messages_before = Message.objects.count()
        # An answer that carries changes, sure of itself, beside the summary: a summary has no way to change anything.
        loud = summed(
            happened='  הלקוח   שאל\nאם אפשר להישאר.  ', answered='ב' * 900, suspected='נראה שצריך לשנות את הידע.',
            understood='לשנות', question='', confidence='high', earlier_changes='replace',
            changes=[self.new_rule(), change(action='deactivate', item_id=self.rule.id, kind='behavior_rule')],
        )
        with patch(POST, return_value=loud):
            data = self.begin().data
        self.nothing_touched(messages_before)
        self.rule.refresh_from_db()
        self.assertTrue(self.rule.is_active)
        self.assertEqual((data['changes'], data['turns'][0]['outcome']), ([], 'summary'))
        # Its words are tidied for the bubble: one line each, no closing dot, not longer than a few lines.
        self.assertEqual((data['summary']['what_happened'], data['summary']['suspected_issue']), ('הלקוח שאל אם אפשר להישאר', 'נראה שצריך לשנות את הידע'))
        self.assertEqual(data['summary']['what_was_answered'], 'ב' * refine.MAX_SUMMARY_CHARS)
        self.assertEqual(set(data['summary']), {'what_happened', 'what_was_answered', 'suspected_issue'})
        # A part that is not text is not shown as one; an answer left empty falls back on the quote.
        with patch(POST, return_value=summed(answered='', suspected=['a', 'b'])):
            odd = self.begin().data
        self.assertEqual((odd['summary']['what_was_answered'], odd['summary']['suspected_issue']), (QUOTED['what_was_answered'], None))
        self.nothing_touched(messages_before)

    def test_a_summary_is_not_counted_in_the_day_and_a_full_day_does_not_stop_it(self):
        with patch('apps.wahub.reviewer.AUTO_DAILY_CAP', 1):
            # Summaries take no room in the day...
            for _ in range(3):
                with patch(POST, return_value=summed()):
                    self.assertEqual(self.begin().status_code, 201)
            self.assertEqual((reviewer.auto_applied_today(), reviewer.auto_room()), (0, 1))
            with patch(POST, return_value=said(change(action='create', kind='fact', title='עובדה 1', body='גוף 1'))):
                self.assertTrue(self.refine().data['changes'][0]['applied'])
            self.assertEqual(reviewer.auto_room(), 0)
            # ...and a day that is full does not keep the system from summarising.
            with patch(POST, return_value=summed()):
                opened = self.begin()
            self.assertEqual((opened.status_code, opened.data['summary']['what_happened']), (201, 'הלקוח שאל אם אפשר להישאר עם הילד בשיעור'))
            self.assertEqual(reviewer.auto_applied_today(), 1)
            # What her answer brings is a change like any other, and the day's rule holds for it.
            with patch(POST, return_value=said(self.new_rule())):
                after = self.post(f'refine/{opened.data["thread"]}/say/', {'text': 'כן, בדיוק'}).data
            self.assertIn('כבר נעשו היום 1 שינויים אוטומטיים', after['reply'])
            self.assertEqual((after['changes'][0]['applied'], after['changes'][0]['status']), (False, 'pending'))

    def test_her_answer_is_read_as_the_first_text_and_the_change_is_applied(self):
        with patch(POST, return_value=summed()):
            opened = self.begin().data
        with patch(POST, return_value=said(self.new_rule())) as post:
            response = self.post(f'refine/{opened["thread"]}/say/', {'text': 'כן, בדיוק'})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data['status'], data['needs_answer']), ('done', False))
        self.assertIn('שיניתי: הוספתי כלל התנהגות "הורים לא נשארים בשיעור"', data['reply'])
        item = KnowledgeItem.objects.get(title='הורים לא נשארים בשיעור')
        self.assertEqual((item.is_active, item.updated_by), (True, self.manager))
        self.assertEqual(KnowledgeHistory.objects.get(item=item).note, 'דיוק: כן, בדיוק')
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.status, proposal.auto_applied, proposal.applied_item_id), ('refine', 'applied', True, item.id))
        self.assertEqual((proposal.contact_id, proposal.message_id), (self.who.id, self.asked.id))
        self.assertEqual((data['changes'][0]['proposal_id'], data['changes'][0]['applied']), (proposal.id, True))
        # The summary stays on the turn that made it; the answer of the thread is now its last turn.
        self.assertEqual([(turn['role'], turn['outcome']) for turn in data['turns']], [('system', 'summary'), ('operator', None), ('system', 'applied')])
        self.assertEqual((data['summary'], data['turns'][0]['summary'], data['turns'][2]['summary']), (None, opened['summary'], None))
        self.assertEqual(data['turns'][1]['by_name'], 'דנה מנהלת')

        # Claude read her answer as the description of the problem, with the summary beside it as data — as a first text, not as a correction.
        body = post.call_args.kwargs['json']
        told = body['messages'][0]['content']
        self.assertIn('# הסיכום שבו המערכת פתחה את הדיוק, והשאלה ששאלה (נתונים; הסיכום הוא השערה של המערכת, לא עובדה)', told)
        self.assertIn('מה נראה לא טוב: נראה שהתשובה לא נכונה: לפי מה שידוע לבוט, ההורים מחכים מחוץ לשיעור\nמה לא היה טוב כאן?\n"""', told)
        self.assertIn('# מה המפעילה ענתה על הסיכום (נתונים — התיאור שלה של מה שלא היה טוב)\n"""\nכן, בדיוק\n"""', told)
        self.assertIn('▶ ', told)
        self.assertNotIn('מהלך הדיוק עד עכשיו', told)
        self.assertNotIn('מה כבר שונה או הוצע בידע', told)
        self.assertNotIn('על זה עונים', told)
        self.assertEqual((body['output_config']['effort'], post.call_args.kwargs['timeout']), (refine.EFFORT, (5, refine.TIMEOUT_SECONDS)))
        self.assertIn('דיוק שנפתח בסיכום', body['system'])
        self.assertIn('הסיכום הוא השערה של המערכת ולא עובדה', body['system'])
        self.assertNotIn('כן, בדיוק', body['system'])

        # From here the chat is any chat: a correction is told the whole of it, and "undo" gives the knowledge back.
        right = change(action='update', item_id=item.id, kind='behavior_rule', body='ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.')
        with patch(POST, return_value=said(right, earlier='keep')) as post:
            more = self.post(f'refine/{opened["thread"]}/say/', {'text': 'ותוסיף שבניסיון אפשר להישאר'}).data
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('# מהלך הדיוק עד עכשיו (נתונים, מהישן לחדש)\nהמערכת (אתה): """מה קרה: הלקוח שאל', told)
        self.assertIn('המפעילה: """כן, בדיוק"""', told)
        self.assertIn(f'[הוחל על הידע] create · behavior_rule · רשומה #{item.id}', told)
        self.assertIn('# מה המפעילה כתבה עכשיו (נתונים — על זה עונים)\n"""\nותוסיף שבניסיון אפשר להישאר\n"""', told)
        item.refresh_from_db()
        self.assertEqual((item.body, more['status']), ('ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.', 'done'))
        self.assertEqual(self.post(f'refine/{opened["thread"]}/undo/').status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.is_active)
        self.assertEqual(set(KnowledgeProposal.objects.values_list('status', flat=True)), {'undone'})

    def test_her_answer_follows_the_usual_rules(self):
        # A question of Claude's: nothing is applied, and the thread waits for her again.
        with patch(POST, return_value=summed(suspected='')):
            opened = self.begin().data
        asks = said(understood='לא ברור מה לא היה טוב', question='מה הבוט היה צריך לענות?', confidence='low')
        with patch(POST, return_value=asks):
            asked = self.post(f'refine/{opened["thread"]}/say/', {'text': 'כן'}).data
        self.assertEqual((asked['needs_answer'], asked['status'], asked['changes'], asked['summary']), (True, 'open', [], None))
        self.assertIn('מה הבוט היה צריך לענות?', asked['reply'])
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (0, 3))
        # A doubt: the proposal waits for a click.
        with patch(POST, return_value=said(self.new_rule(), confidence='low')):
            unsure = self.post(f'refine/{opened["thread"]}/say/', {'text': 'שההורים מחכים בחוץ'}).data
        self.assertEqual((unsure['changes'][0]['applied'], KnowledgeProposal.objects.get().status), (False, 'pending'))
        # The switch off: her answer to a summary waits like any fix.
        settings_store.set_review_auto_apply(False)
        with patch(POST, return_value=summed()):
            second = self.begin().data
        with patch(POST, return_value=said(self.new_rule())):
            off = self.post(f'refine/{second["thread"]}/say/', {'text': 'כן, בדיוק'}).data
        self.assertIn('האישור האוטומטי כבוי', off['reply'])
        self.assertEqual((off['changes'][0]['applied'], off['auto_apply']), (False, False))
        self.assertFalse(KnowledgeItem.objects.filter(title='הורים לא נשארים בשיעור').exists())
        # And a price is not written after a summary either.
        settings_store.set_review_auto_apply(True)
        with patch(POST, return_value=summed()):
            third = self.begin().data
        with patch(POST, return_value=said(change(action='create', kind='fact', title='מחיר', body='השיעור עולה ₪60'))):
            priced = self.post(f'refine/{third["thread"]}/say/', {'text': 'שיכתוב שזה עולה 60 שקל'}).data
        self.assertEqual(priced['changes'], [])
        self.assertIn('מתקנים את המחיר בכרטיס החוג ב-Kogo', priced['reply'])
        self.assertFalse(KnowledgeItem.objects.filter(title='מחיר').exists())

    def test_when_claude_does_not_answer_the_summary_is_made_by_rules(self):
        messages_before = Message.objects.count()
        refused = MagicMock(status_code=200)
        refused.json.return_value = {'stop_reason': 'refusal', 'content': []}
        broken = MagicMock(status_code=200)
        broken.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'not json'}]}
        for answer, reason in (
            ({'side_effect': requests.Timeout('slow')}, 'Claude לא ענה (Timeout)'),
            ({'side_effect': requests.ConnectionError('down')}, 'Claude לא ענה (ConnectionError)'),
            ({'return_value': MagicMock(status_code=529)}, 'Claude ענה 529'),
            ({'return_value': refused}, 'Claude סירב לענות'),
            ({'return_value': broken}, 'תשובה לא קריאה מ-Claude'),
            ({'return_value': summed(stop='max_tokens')}, 'התשובה נקטעה'),
            ({'return_value': summed(happened='  ')}, 'Claude החזיר סיכום ריק'),
        ):
            with self.subTest(reason=reason):
                with patch(POST, **answer):
                    response = self.begin()
                self.assertEqual(response.status_code, 201, response.data)
                data = response.data
                self.assertEqual(data['summary'], QUOTED)
                self.assertEqual(data['reply'], 'מה קרה: הלקוח כתב: "אפשר להישאר עם הילד בשיעור?"\nמה נענה: הבוט הישן: "בטח, אין בעיה!"\nמה לא היה טוב כאן?')
                self.assertEqual((data['needs_answer'], data['status'], data['changes']), (True, 'open', []))
                meta = RefineTurn.objects.get(thread_id=data['thread']).meta
                self.assertEqual((meta['outcome'], meta['model'], meta['reason']), ('summary', 'stub', reason))
        self.nothing_touched(messages_before)
        # Once Claude answers, her answer is read like any first text.
        with patch(POST, return_value=said(self.new_rule())):
            fixed = self.post(f'refine/{data["thread"]}/say/', {'text': 'ההורים מחכים בחוץ, הבוט לא היה צריך לומר שאפשר'}).data
        self.assertTrue(fixed['changes'][0]['applied'])
        self.assertEqual(KnowledgeProposal.objects.get().status, 'applied')

    def test_when_claude_is_gone_for_her_answer_what_the_summary_suspected_is_kept_beside_it(self):
        with patch(POST, return_value=summed()):
            opened = self.begin().data
        with patch(POST, side_effect=requests.Timeout('slow')):
            data = self.post(f'refine/{opened["thread"]}/say/', {'text': 'כן, בדיוק'}).data
        self.assertIn('לא הצלחתי לפענח כרגע (Claude לא ענה (Timeout))', data['reply'])
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.status, proposal.pattern, proposal.change['kind']), ('pending', 'refine_stub', 'behavior_rule'))
        # "כן, בדיוק" alone would say nothing to whoever reads the proposal: the suspicion is there, marked as the system's.
        self.assertEqual(proposal.change['after']['body'], '(מהסיכום שפתח את הדיוק: נראה שהתשובה לא נכונה: לפי מה שידוע לבוט, ההורים מחכים מחוץ לשיעור)\nכן, בדיוק')
        self.assertEqual((data['changes'][0]['applied'], KnowledgeItem.objects.count()), (False, 3))

    @override_settings(ANTHROPIC_API_KEY='')
    def test_without_a_key_the_summary_is_two_short_quotes(self):
        messages_before = Message.objects.count()
        with patch(POST, side_effect=AssertionError('no key, no call')) as post:
            response = self.begin()
            data = response.data
            self.assertEqual(response.status_code, 201, data)
            self.assertEqual(data['summary'], QUOTED)
            self.assertEqual(data['reply'], 'מה קרה: הלקוח כתב: "אפשר להישאר עם הילד בשיעור?"\nמה נענה: הבוט הישן: "בטח, אין בעיה!"\nמה לא היה טוב כאן?')
            self.assertEqual((data['needs_answer'], data['status'], data['changes'], data['turns'][0]['outcome']), (True, 'open', [], 'summary'))
            meta = RefineTurn.objects.get().meta
            self.assertEqual((meta['model'], meta['reason']), ('stub', 'אין מפתח Anthropic'))
            self.nothing_touched(messages_before)
            # Her answer, still with no key: kept as a waiting rule, as the first text of any thread is.
            more = self.post(f'refine/{data["thread"]}/say/', {'text': 'הבוט לא צריך לומר שאפשר להישאר, ההורים מחכים בחוץ'}).data
        post.assert_not_called()
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.status, proposal.pattern), ('refine', 'pending', 'refine_stub'))
        self.assertEqual(proposal.change['after']['body'], 'הבוט לא צריך לומר שאפשר להישאר, ההורים מחכים בחוץ')
        self.assertIn('אין חיבור ל-Claude (חסר מפתח Anthropic)', more['reply'])
        self.assertEqual((more['summary'], more['turns'][0]['summary'], KnowledgeItem.objects.count()), (None, QUOTED, 3))

    @override_settings(ANTHROPIC_API_KEY='')
    def test_which_two_messages_the_rules_quote(self):
        office = self.line(self.who, 'לא נשארים בשיעור, ההורים מחכים בחוץ', sender='office', sender_name='מיכל')
        voice = self.line(self.who, '', direction='in', sender='customer', message_type='voice', transcript='אפשר לבוא לשיעור ניסיון?')
        self.line(self.who, 'מילה ' * 100)                                        # longer than a quote may be
        failed = self.line(self.who, 'נשמח לראות אתכם', sender='office')
        Message.objects.filter(pk=failed.pk).update(status='failed')
        last = self.line(self.who, 'תודה', direction='in', sender='customer')
        shadow_reply = ShadowReply.objects.create(contact=self.who, after_message=self.asked, text='כן,  אפשר\nלהישאר')
        other = self.make_contact(phone='972505550188')
        template = self.line(other, 'תזכורת: שיעור ניסיון מחר', sender='system', message_type='template')
        asked = QUOTED['what_happened']
        cases = (
            # Only the answer was picked: the customer's message it came after.
            ('answer only', {'message_ids': [self.answered.id]}, 'לפני כן הלקוח כתב: "אפשר להישאר עם הילד בשיעור?"', 'הבוט הישן: "בטח, אין בעיה!"'),
            # Only the customer's message: what was sent after it.
            ('question only', {'message_ids': [self.asked.id]}, asked, 'אחר כך — הבוט הישן: "בטח, אין בעיה!"'),
            # Several answers picked: the last of them, and the customer's last message before it.
            ('office over the bot', {'message_ids': [self.asked.id, self.answered.id, office.id]}, asked, 'נציג מהמשרד (מיכל): "לא נשארים בשיעור, ההורים מחכים בחוץ"'),
            ('a voice message', {'message_ids': [voice.id]}, 'הלקוח כתב: "[🎤 הודעה קולית] אפשר לבוא לשיעור ניסיון?"', 'אחר כך — הבוט הישן: "' + ('מילה ' * 32).strip() + '…"'),
            ('an answer that was not sent', {'message_ids': [voice.id, failed.id]}, 'הלקוח כתב: "[🎤 הודעה קולית] אפשר לבוא לשיעור ניסיון?"', 'נציג מהמשרד (לא נשלחה): "נשמח לראות אתכם"'),
            ('nothing was answered', {'message_ids': [last.id]}, 'הלקוח כתב: "תודה"', 'לא נשלחה תשובה בקטע שנבחר'),
            # "לתקן" on a shadow reply: that is the answer, and the message it answers is the question.
            ('a shadow reply', {'shadow_ids': [shadow_reply.id]}, asked, 'הבוט החדש היה עונה (בצל, לא נשלח): "כן, אפשר להישאר"'),
            ('no customer at all', {'contact': other.id, 'message_ids': [template.id]}, 'לא נבחרה הודעה של הלקוח', 'המערכת (תבנית שנשלחה ללקוח): "תזכורת: שיעור ניסיון מחר"'),
        )
        for name, picked, happened, answered in cases:
            with self.subTest(name=name):
                response = self.post('refine/', {'contact': self.who.id, 'text': '', **picked})
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['summary'], {'what_happened': happened, 'what_was_answered': answered, 'suspected_issue': None})
        self.assertEqual(KnowledgeProposal.objects.count(), 0)

    def test_what_is_refused_at_the_summarys_door(self):
        other = self.make_contact(phone='972505550188')
        stranger = self.line(other, 'הודעה של מישהו אחר', direction='in', sender='customer')
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            for body, word in (
                ({'contact': self.who.id, 'message_ids': [], 'text': ''}, 'לפחות הודעה אחת'),
                ({'contact': self.who.id, 'text': ''}, 'לפחות הודעה אחת'),
                ({'contact': self.who.id, 'message_ids': 'x', 'text': ''}, 'רשימה של מספרים'),
                ({'contact': self.who.id, 'message_ids': [stranger.id], 'text': ''}, 'לא שייכת לשיחה'),
                ({'contact': self.who.id, 'message_ids': [self.answered.id], 'shadow_ids': [999999], 'text': ''}, 'תשובת הצל'),
                ({'contact': 999999, 'message_ids': [self.answered.id], 'text': ''}, 'איש הקשר לא נמצא'),
                ({'message_ids': [self.answered.id]}, 'איש הקשר לא נמצא'),
                ({'proposal': 999999, 'text': ''}, 'ההצעה לא נמצאה'),
            ):
                with self.subTest(word=word, body=body):
                    response = self.post('refine/', body)
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(word, response.data['detail'])
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.count()), (0, 0))
        # In the chat itself an empty line is still nothing to answer: the summary asked, and waits for words.
        with patch(POST, return_value=summed()):
            opened = self.begin().data
        with patch(POST, side_effect=AssertionError('an empty answer reached Claude')):
            for text in ('', '   ', None):
                self.assertEqual(self.post(f'refine/{opened["thread"]}/say/', {'text': text}).status_code, 400)
        self.assertEqual(RefineTurn.objects.count(), 1)

    def test_managers_only(self):
        with patch(POST, return_value=summed()):
            opened = self.begin().data
        waiting = KnowledgeProposal.objects.create(
            source='service_note', contact=self.who, message=self.asked, title='להוסיף עובדה', dedup_key='x',
            change={'action': 'create', 'item_id': None, 'kind': 'fact', 'before': None, 'after': {'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה'}},
        )
        calls = [
            ('post', 'refine/', {'contact': self.who.id, 'message_ids': [self.answered.id], 'text': ''}),
            ('post', 'refine/', {'contact': self.who.id, 'message_ids': [self.answered.id]}),
            ('post', 'refine/', {'proposal': waiting.id, 'text': ''}),
            ('post', f'refine/{opened["thread"]}/say/', {'text': 'כן, בדיוק'}),
            ('get', f'refine/{opened["thread"]}/', None),
            ('get', 'refine/', None),
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
                        self.assertNotIn('summary', response.data or {})
        self.assertEqual((RefineThread.objects.count(), RefineTurn.objects.count()), (1, 1))
        waiting.refresh_from_db()
        self.assertEqual((waiting.status, KnowledgeItem.objects.count()), ('pending', 3))


# --- the sweep under the standing approval ---------------------------------------------------------------

class SweepTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        patch('apps.wahub.sending._deliver', side_effect=AssertionError('the review tried to send')).start()
        patch('apps.wahub.handoff._in_manychat', side_effect=AssertionError('the review tried to touch ManyChat')).start()
        self.addCleanup(patch.stopall)

    def line(self, contact, text, *, direction='out', sender='bot', minutes_ago=0, **extra):
        return Message.objects.create(
            contact=contact, direction=direction, sender=sender, text=text, source='manychat',
            status='received' if direction == 'in' else 'sent', sent_at=timezone.now() - timedelta(minutes=minutes_ago), **extra,
        )

    def overridden(self, phone=PHONE, minutes_ago=30, answer='לא נשארים עם הילד בשיעור, ההורים מחכים בחוץ', recent=True) -> Contact:
        """A conversation in which the office answered over the old bot."""
        contact = self.make_contact(phone=phone)
        self.line(contact, 'אפשר להישאר עם הילד בשיעור?', direction='in', sender='customer', minutes_ago=minutes_ago + 2)
        self.line(contact, 'בטח, אין בעיה!', minutes_ago=minutes_ago + 1)
        self.line(contact, answer, sender='office', sender_name='מיכל', minutes_ago=minutes_ago)
        if recent:
            Contact.objects.filter(pk=contact.pk).update(last_message_at=timezone.now() - timedelta(minutes=minutes_ago))
        return contact


@override_settings(ANTHROPIC_API_KEY='test-key')
class SweepAutoTests(SweepTestCase):
    def test_what_the_office_answered_is_applied_by_itself_when_claude_is_sure(self):
        contact = self.overridden()
        with patch(POST, return_value=worded()) as post:
            counts = reviewer.scan()
        self.assertEqual((counts['overrides'], counts['proposals'], counts['auto_applied']), (1, 1, 1))
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.status, proposal.auto_applied, proposal.decision_note), ('human_override', 'applied', True, 'אושר אוטומטית'))
        self.assertIsNone(proposal.decided_by)
        item = proposal.applied_item
        self.assertEqual((item.kind, item.title, item.body, item.when_to_say, item.is_active), ('fact', 'לא נשארים עם הילד', 'ההורים לא נשארים בשיעור; מחכים בחוץ', 'if_asked', True))
        history = KnowledgeHistory.objects.get(item=item)
        self.assertEqual((history.note, history.changed_by), (f'אושר אוטומטית · הצעה #{proposal.id} מתוך שיחה {contact.id}', None))
        self.assertIn('confidence', post.call_args.kwargs['json']['output_config']['format']['schema']['required'])
        self.assertIn('היום: ', post.call_args.kwargs['json']['messages'][0]['content'])

        summary = self.get('review/summary/').data
        self.assertEqual((summary['pending'], summary['applied_7d'], summary['auto_applied_7d'], summary['auto_apply']), (0, 1, 1, True))
        self.assertEqual([row['id'] for row in self.get('review/proposals/', status='applied', auto='1').data], [proposal.id])
        self.assertEqual(self.get('review/proposals/', status='applied', auto='0').data, [])

        # "בטל" on the list of what was updated by itself.
        undone = self.post(f'review/proposals/{proposal.id}/undo/')
        self.assertEqual((undone.status_code, undone.data['proposal']['status'], undone.data['item']['is_active']), (200, 'undone', False))
        self.assertEqual(KnowledgeHistory.objects.filter(item=item).order_by('version').last().note, f'ביטול הצעה #{proposal.id}')
        self.assertEqual(self.get('review/summary/').data['auto_applied_7d'], 0)
        self.assertEqual(self.post(f'review/proposals/{proposal.id}/undo/').status_code, 409)
        # The same answer never makes a second proposal — not on another day, not in the sweep over the old conversations.
        KnowledgeProposal.objects.update(dedup_key='made-on-another-day')
        with patch(POST, return_value=worded()):
            self.assertEqual(reviewer.scan()['proposals'], 0)
        self.assertEqual(KnowledgeProposal.objects.count(), 1)

    def test_what_keeps_a_proposal_of_the_sweep_waiting(self):
        cases = (
            ('claude is not sure', {'return_value': worded(confidence='low')}, {}),
            ('claude did not understand', {'return_value': worded(understood=False)}, {}),
            ('claude did not answer', {'return_value': MagicMock(status_code=500)}, {}),
            ('the wording names a price', {'return_value': worded(body='על חוג שני יש ₪10 הנחה בחודש')}, {}),
            ('the switch is off', {'return_value': worded()}, {'auto': False}),
        )
        for number, (name, answer, extra) in enumerate(cases):
            with self.subTest(name=name):
                settings_store.set_review_auto_apply(extra.get('auto', True))
                self.overridden(phone=f'97250555{number:04d}')
                with patch(POST, **answer):
                    reviewer.scan(history=False)
                proposal = KnowledgeProposal.objects.order_by('-id').first()
                self.assertEqual((proposal.status, proposal.auto_applied, proposal.applied_item_id), ('pending', False, None))
        self.assertEqual((KnowledgeProposal.objects.count(), KnowledgeItem.objects.count()), (5, 0))

    def test_claude_is_not_asked_again_for_what_already_has_its_proposal(self):
        contact = self.overridden()
        with patch(POST, return_value=worded(confidence='low')) as post:
            reviewer.scan()
            self.assertEqual(post.call_count, 1)
            # A second answer of the office in the same conversation, the same day: one proposal a day,
            # and the sweep that runs every five minutes does not pay for a wording nobody will keep.
            self.line(contact, 'ועוד שאלה: יש חניה?', direction='in', sender='customer', minutes_ago=5)
            self.line(contact, 'אבדוק', minutes_ago=4)
            self.line(contact, 'יש חניה בבניין', sender='office', sender_name='מיכל', minutes_ago=3)
            for _ in range(3):
                self.assertEqual(reviewer.scan()['proposals'], 0)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(KnowledgeProposal.objects.count(), 1)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_a_rule_made_wording_always_waits(self):
        self.overridden()
        with patch(POST, side_effect=AssertionError('no key, no call')):
            counts = reviewer.scan()
        self.assertEqual((counts['proposals'], counts['auto_applied']), (1, 0))
        self.assertEqual((KnowledgeProposal.objects.get().status, KnowledgeItem.objects.count()), ('pending', 0))

    def test_a_pattern_of_the_sweep_waits_even_when_claude_is_sure(self):
        contact = self.make_contact()
        self.line(contact, 'רוצה לבטל', direction='in', sender='customer', minutes_ago=20)
        self.line(contact, 'מעבירה לניקול', minutes_ago=19)
        Contact.objects.filter(pk=contact.pk).update(last_message_at=timezone.now())
        with patch(POST, return_value=worded(kind='behavior_rule', title='אין ניקול', body='לא מפנים לניקול')):
            reviewer.scan()
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.pattern, proposal.status, proposal.auto_applied), ('reviewer', 'nicole', 'pending', False))
        self.assertEqual(reviewer.why_not_automatic(proposal), 'הצעה של הסורק לפי דפוס ממתינה תמיד לאישור')

    def test_the_day_has_room_for_so_many_automatic_changes(self):
        for number in range(3):
            self.overridden(phone=f'97250555{number:04d}')
        with patch('apps.wahub.reviewer.AUTO_DAILY_CAP', 2), patch(POST, return_value=worded()):
            counts = reviewer.scan(history=False)
            self.assertEqual((counts['proposals'], counts['auto_applied']), (3, 2))
            self.assertEqual(sorted(KnowledgeProposal.objects.values_list('status', flat=True)), ['applied', 'applied', 'pending'])
            self.assertEqual(reviewer.why_not_automatic(KnowledgeProposal.objects.get(status='pending')), 'כבר נעשו היום 2 שינויים אוטומטיים')
        self.assertEqual(self.get('settings/review/').data['auto_applied_today'], 2)

    def test_a_service_note_and_a_thumbs_down_are_applied_too(self):
        self.incoming('אפשר להישאר עם הילד בשיעור?')
        self.bot_reply('בטח, אין בעיה!')
        contact = self.contact()
        with patch(POST, return_value=worded()):
            noted = self.post('review/notes/', {'text': 'הבוט אמר שאפשר להישאר בשיעור וזה לא נכון', 'contact_id': contact.id})
        proposal = KnowledgeProposal.objects.get(pk=noted.data['proposal_id'])
        self.assertEqual((proposal.source, proposal.status, proposal.auto_applied, proposal.decided_by), ('service_note', 'applied', True, self.manager))
        shadow_reply = ShadowReply.objects.create(contact=contact, after_message=Message.objects.filter(direction='in').get(), text='כן אפשר')
        with patch(POST, return_value=worded(title='לא נשארים בשיעור ניסיון', body='גם בשיעור ניסיון מחכים בחוץ')):
            marked = self.post(f'shadow/{shadow_reply.id}/verdict/', {'verdict': 'bad', 'note': 'גם בשיעור ניסיון לא נשארים בפנים'})
        verdict = KnowledgeProposal.objects.get(pk=marked.data['proposal_id'])
        self.assertEqual((verdict.source, verdict.status, verdict.auto_applied), ('bad_verdict', 'applied', True))
        self.assertEqual(KnowledgeItem.objects.filter(is_active=True).count(), 2)

    def test_a_change_approved_by_a_click_can_be_taken_back_as_well(self):
        item = knowledge.create_item({'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה'})
        proposal = KnowledgeProposal.objects.create(
            source='service_note', title='לדייק חניה',
            change={'action': 'update', 'item_id': item.id, 'kind': 'fact', 'before': {'body': 'יש חניה'}, 'after': {'body': 'יש חניה בקומה -1'}},
        )
        approved = self.post(f'review/proposals/{proposal.id}/approve/').data['proposal']
        self.assertEqual((approved['auto_applied'], approved['can_undo']), (False, True))
        self.assertEqual(self.post(f'review/proposals/{proposal.id}/undo/').status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.body, item.version), ('יש חניה', 3))
        # A waiting proposal has nothing to take back; one applied before versions were kept says where to look.
        waiting = KnowledgeProposal.objects.create(source='reviewer', title='x', change={'action': 'create', 'kind': 'fact', 'after': {'title': 'x', 'body': 'y'}})
        self.assertEqual(self.post(f'review/proposals/{waiting.id}/undo/').status_code, 400)
        old = KnowledgeProposal.objects.create(source='reviewer', title='ישן', status='applied', applied_item=item, change={'action': 'update', 'item_id': item.id, 'after': {}})
        refused = self.post(f'review/proposals/{old.id}/undo/')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('לשחזר גרסה מההיסטוריה', refused.data['detail'])
        self.assertFalse(self.get(f'review/proposals/{old.id}/').data['can_undo'])


@override_settings(ANTHROPIC_API_KEY='')
class HistorySweepTests(SweepTestCase):
    def old_conversations(self, count=3):
        # Last week's conversations: no message in the last day, so the daily sweep does not see them.
        return [self.overridden(phone=f'97250555{number:04d}', minutes_ago=60 * 24 * 7 + number, recent=False) for number in range(count)]

    def test_the_old_conversations_are_read_once_a_few_a_call(self):
        contacts = self.old_conversations()
        with patch('apps.wahub.reviewer.HISTORY_BATCH', 2):
            first = reviewer.scan()
            self.assertEqual((first['contacts_scanned'], first['proposals']), (0, 0))               # the day's sweep saw nothing
            self.assertEqual(first['history'], {'contacts_scanned': 2, 'proposals': 2, 'auto_applied': 0, 'done': False, 'paused': False})
            self.assertEqual(reviewer.history_state()['contact_id'], contacts[1].id)
            second = reviewer.scan()['history']
            self.assertEqual((second['contacts_scanned'], second['proposals'], second['done']), (1, 1, True))
            third = reviewer.scan()['history']
            self.assertEqual((third['contacts_scanned'], third['done']), (0, True))
        self.assertEqual(KnowledgeProposal.objects.filter(source='human_override', status='pending').count(), 3)
        state_now = self.get('settings/review/').data['history_scan']
        self.assertTrue(state_now['done'])
        self.assertIsNotNone(state_now['finished_at'])
        # Done is done: a conversation that turns up later belongs to the daily sweep.
        self.overridden(phone='972505559999', minutes_ago=60 * 24 * 9, recent=False)
        self.assertEqual(reviewer.scan()['history']['contacts_scanned'], 0)
        self.assertEqual(KnowledgeProposal.objects.count(), 3)

    def test_an_old_conversation_teaches_and_marks_nobody(self):
        contact = self.make_contact()
        week = 60 * 24 * 7
        self.line(contact, 'יש חוג?', direction='in', sender='customer', minutes_ago=week + 50)
        self.line(contact, 'אבדוק ואחזור אליך.', minutes_ago=week + 49)
        self.line(contact, 'לא עניתם לי', direction='in', sender='customer', minutes_ago=week + 10)
        self.line(contact, 'אבדוק ואחזור.', minutes_ago=week + 9)
        self.line(contact, 'התשובה הראשונה של המשרד', sender='office', sender_name='מיכל', minutes_ago=week + 8)
        self.line(contact, 'ועוד שאלה', direction='in', sender='customer', minutes_ago=week + 5)
        self.line(contact, 'אבדוק עם הצוות', minutes_ago=week + 4)
        self.line(contact, 'התשובה האחרונה של המשרד', sender='office', sender_name='מיכל', minutes_ago=week + 3)
        counts = reviewer.scan()['history']
        self.assertEqual((counts['contacts_scanned'], counts['done']), (1, True))
        override = KnowledgeProposal.objects.get(source='human_override')
        self.assertEqual(override.change['after']['body'], 'התשובה האחרונה של המשרד')          # the office's latest word
        self.assertTrue(KnowledgeProposal.objects.filter(source='reviewer', status='pending').exists())
        contact.refresh_from_db()
        self.assertFalse(contact.needs_human)                                                  # last week's talk is not today's work

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_a_full_day_pauses_the_sweep_until_tomorrow(self):
        contacts = self.old_conversations()
        with patch('apps.wahub.reviewer.AUTO_DAILY_CAP', 1), patch(POST, return_value=worded()):
            first = reviewer.scan()['history']
            self.assertEqual((first['contacts_scanned'], first['auto_applied'], first['paused'], first['done']), (1, 1, True, False))
            again = reviewer.scan()['history']
            self.assertEqual((again['contacts_scanned'], again['paused']), (0, True))
            self.assertEqual(reviewer.history_state()['contact_id'], contacts[0].id)
            # Tomorrow (today's change is now yesterday's) it carries on from where it stopped.
            KnowledgeProposal.objects.filter(status='applied').update(decided_at=timezone.now() - timedelta(days=1, hours=1))
            tomorrow = reviewer.scan()['history']
            self.assertEqual((tomorrow['contacts_scanned'], tomorrow['auto_applied'], tomorrow['paused']), (1, 1, True))
        self.assertEqual(KnowledgeProposal.objects.filter(auto_applied=True, status='applied').count(), 2)
        self.assertEqual(reviewer.history_state()['contact_id'], contacts[1].id)

    def test_a_conversation_that_cannot_be_read_does_not_hold_the_sweep(self):
        contacts = self.old_conversations(2)
        real = reviewer.review_contact

        def flaky(contact, **kwargs):
            if contact.id == contacts[0].id:
                raise RuntimeError('boom')
            return real(contact, **kwargs)

        with patch('apps.wahub.reviewer.review_contact', side_effect=flaky):
            counts = reviewer.scan_history()
        self.assertEqual((counts['contacts_scanned'], counts['proposals'], counts['done']), (2, 1, True))


# --- section ג of the contract: the proposal's card in plain words, and "דיוק" on a proposal ------------------

@override_settings(ANTHROPIC_API_KEY='')
class PlainCardTests(SweepTestCase):
    """What the owner reads on a proposal's card: "מעכשיו הבוט…", the rule in words, Hebrew labels — no code names."""

    def proposal(self, change, **fields):
        return KnowledgeProposal.objects.create(source='service_note', title='כותרת ישנה', explanation='הבוט ענה אחרת; המשרד תיקן.', change=change, **fields)

    def plain(self, proposal):
        return self.get(f'review/proposals/{proposal.id}/').data['plain']

    def test_a_pattern_of_the_sweep_reads_as_a_sentence(self):
        contact = self.make_contact()
        self.line(contact, 'באיזו עיר אתם?', minutes_ago=30)
        self.line(contact, 'ראש העין', direction='in', sender='customer', minutes_ago=29)
        self.line(contact, 'באיזו עיר אתם?', minutes_ago=28)
        Contact.objects.filter(pk=contact.pk).update(last_message_at=timezone.now())
        reviewer.scan()
        proposal = KnowledgeProposal.objects.get(pattern='loop_question')
        card = self.plain(proposal)
        self.assertEqual(card['headline'], 'מעכשיו הבוט לא ישאל שוב שאלה שהלקוח כבר ענה עליה')
        self.assertEqual((card['kind_label'], card['when_label'], card['is_new']), ('כלל התנהגות', 'מיוזמתו כשרלוונטי', True))
        self.assertEqual(card['rule'], 'שאלה שהלקוח כבר ענה עליה (עיר, גיל, סניף) לא נשאלת שוב. הפרט נעול וממשיכים לשלב הבא.')
        self.assertEqual((card['why'], card['example_bad'], card['example_good']), (proposal.explanation, 'באיזו עיר אתם?', ''))
        self.assertEqual(set(card), {'headline', 'rule', 'why', 'example_bad', 'example_good', 'kind_label', 'when_label', 'is_new'})
        # Not one code name in what he reads; they stay in `change`, for the folded "technical details".
        for code in ('behavior_rule', 'loop_question', 'proactive', 'when_to_say', 'create'):
            self.assertNotIn(code, json.dumps(card, ensure_ascii=False))
        self.assertEqual(proposal.change['kind'], 'behavior_rule')
        # Reworded since (a "דיוק" on it, say): the sentence follows what it says now, not the pattern it came from.
        proposal.change = {**proposal.change, 'after': {**proposal.change['after'], 'title': 'לא חוזרים על שאלת העיר'}}
        proposal.save(update_fields=['change'])
        self.assertEqual(self.plain(proposal)['headline'], 'מעכשיו הבוט יפעל לפי הכלל: לא חוזרים על שאלת העיר')

    def test_every_kind_of_change_has_its_sentence(self):
        fact = knowledge.create_item({'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה', 'when_to_say': 'if_asked', 'example_good': 'יש חניה בבניין'})
        rule = knowledge.create_item({'kind': 'behavior_rule', 'title': 'הורים בשיעור', 'body': 'אפשר להישאר עם הילד'})
        new_fact = self.plain(self.proposal({'action': 'create', 'kind': 'fact', 'after': {'kind': 'fact', 'title': 'לא נשארים בשיעור', 'body': 'ההורים מחכים בחוץ', 'when_to_say': 'internal'}}))
        self.assertEqual((new_fact['headline'], new_fact['rule'], new_fact['is_new']), ('מעכשיו הבוט יודע: לא נשארים בשיעור', 'ההורים מחכים בחוץ', True))
        self.assertEqual((new_fact['kind_label'], new_fact['when_label']), ('עובדה', 'הנחיה פנימית, לא נאמר ללקוח'))

        phrasing = self.plain(self.proposal({'action': 'create', 'kind': 'phrasing', 'after': {'kind': 'phrasing', 'key': 'office_12', 'title': '', 'body': 'נתראה בשיעור'}}))
        self.assertEqual((phrasing['headline'], phrasing['kind_label']), ('מעכשיו הבוט ישתמש בנוסח: office_12', 'נוסח'))

        alias = self.plain(self.proposal({'action': 'create', 'kind': 'alias', 'after': {'kind': 'alias', 'title': 'גאולים', 'what_customer_writes': 'גאולים', 'means': 'סניף רמת גן'}}))
        self.assertEqual(alias['headline'], 'מעכשיו הבוט מבין שכשלקוח כותב "גאולים" הכוונה לסניף רמת גן')
        self.assertEqual(alias['rule'], 'כשלקוח כותב "גאולים" — הכוונה לסניף רמת גן')

        update = self.plain(self.proposal({'action': 'update', 'item_id': fact.id, 'kind': 'fact', 'before': {'body': 'יש חניה'}, 'after': {'body': 'יש חניה בקומה מינוס אחת'}}))
        self.assertEqual(update['headline'], 'מעכשיו הבוט יפעל לפי גרסה מעודכנת של העובדה "חניה"')
        self.assertEqual((update['rule'], update['is_new'], update['kind_label']), ('יש חניה בקומה מינוס אחת', False, 'עובדה'))
        # What the change does not repeat is read from the item it points at.
        self.assertEqual((update['when_label'], update['example_good']), ('רק אם שואלים', 'יש חניה בבניין'))

        example = self.plain(self.proposal({'action': 'update', 'item_id': rule.id, 'kind': 'behavior_rule', 'after': {'example_bad': 'בטח, אין בעיה!'}}))
        self.assertEqual(example['headline'], 'מעכשיו לכלל ההתנהגות "הורים בשיעור" מצורפת דוגמה מהשיחה')
        self.assertEqual((example['rule'], example['example_bad']), ('דוגמה למה לא לכתוב: בטח, אין בעיה!', 'בטח, אין בעיה!'))

        renamed = self.plain(self.proposal({'action': 'update', 'item_id': fact.id, 'kind': 'fact', 'after': {'title': 'חניה בסניף', 'when_to_say': 'proactive'}}))
        self.assertEqual(renamed['rule'], 'הכותרת: חניה בסניף\nמתי לומר: מיוזמתו כשרלוונטי')

        off = self.plain(self.proposal({'action': 'deactivate', 'item_id': rule.id, 'kind': 'behavior_rule', 'before': {'is_active': True}, 'after': {'is_active': False}}))
        self.assertEqual(off['headline'], 'מעכשיו הבוט לא ישתמש יותר בכלל ההתנהגות "הורים בשיעור"')
        self.assertEqual((off['rule'], off['is_new']), ('הבוט יפסיק להשתמש בכלל ההתנהגות: אפשר להישאר עם הילד', False))

        # An item that is gone, a change with nothing in it: still a card, never an error.
        self.assertTrue(self.plain(self.proposal({'action': 'update', 'item_id': 999999, 'after': {'body': 'x'}}))['headline'].startswith('מעכשיו הבוט'))
        self.assertTrue(self.plain(self.proposal({}))['headline'].startswith('מעכשיו הבוט'))
        # The list carries the same card for every row.
        listed = self.get('review/proposals/').data
        self.assertEqual(len(listed), 9)
        self.assertTrue(all(row['plain']['headline'].startswith('מעכשיו') for row in listed))
        self.assertEqual(next(row for row in listed if row['change'].get('action') == 'deactivate')['plain']['kind_label'], 'כלל התנהגות')

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_the_sentence_claude_wrote_is_the_one_shown(self):
        self.overridden()
        response = worded(confidence='low')
        answer = json.loads(response.json.return_value['content'][0]['text'])
        answer['headline'] = 'מעכשיו הבוט יגיד להורים שמחכים מחוץ לשיעור.'
        response.json.return_value['content'][0]['text'] = json.dumps(answer, ensure_ascii=False)
        with patch(POST, return_value=response) as post:
            reviewer.scan(history=False)
        self.assertIn('headline', post.call_args.kwargs['json']['output_config']['format']['schema']['required'])
        self.assertEqual(self.plain(KnowledgeProposal.objects.get())['headline'], 'מעכשיו הבוט יגיד להורים שמחכים מחוץ לשיעור')
        # A sentence that does not open the way the card does is not used.
        KnowledgeProposal.objects.update(change={**KnowledgeProposal.objects.get().change, 'headline': 'עדכון של עובדה'})
        self.assertEqual(self.plain(KnowledgeProposal.objects.get())['headline'], 'מעכשיו הבוט יודע: לא נשארים עם הילד')


@override_settings(ANTHROPIC_API_KEY='test-key')
class RefineAProposalTests(RefineTestCase):
    """She pressed "דיוק" on a proposal's card and wrote what to change in it."""

    def setUp(self):
        super().setUp()
        # A proposal as the sweep leaves it: the office answered over the bot; a rule-made wording, waiting.
        self.waiting = KnowledgeProposal.objects.create(
            source='human_override', contact=self.who, message=self.asked, title='להוסיף נוסח: לא נשארים בשיעור',
            explanation='הלקוח שאל אם אפשר להישאר; הבוט הישן ענה שכן; מיכל ענתה מעליו.',
            change={'action': 'create', 'item_id': None, 'kind': 'phrasing', 'before': None,
                    'after': {'kind': 'phrasing', 'title': 'מהמשרד: לא נשארים', 'body': 'לא נשארים', 'key': 'office_77', 'when_to_say': 'proactive'}},
            evidence=[{'message_id': self.asked.id, 'who': 'customer', 'text': 'אפשר להישאר עם הילד בשיעור?'},
                      {'message_id': self.answered.id, 'who': 'bot', 'text': 'בטח, אין בעיה!'},
                      {'message_id': None, 'who': 'office', 'text': 'לא נשארים'}],
            dedup_key='x',
        )

    def press(self, text='זה צריך להיות כלל ולא נוסח, ולהסביר שמחכים בחוץ', proposal=None):
        return self.post('refine/', {'proposal': (proposal or self.waiting).id, 'text': text})

    def corrected(self, **fields):
        return said(self.new_rule(headline='מעכשיו הבוט יגיד להורים שמחכים מחוץ לשיעור', **fields), earlier='replace')

    def test_a_waiting_proposal_is_rewritten_in_place_and_applied(self):
        with patch(POST, return_value=self.corrected()) as post:
            response = self.press()
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(KnowledgeProposal.objects.count(), 1)                    # the same proposal, not another one beside it
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.auto_applied, self.waiting.source), ('applied', True, 'human_override'))
        self.assertEqual((self.waiting.change['kind'], self.waiting.change['after']['body']), ('behavior_rule', 'ההורים לא נשארים בשיעור. מחכים בחוץ עד סוף השיעור.'))
        self.assertEqual(self.waiting.title, 'להוסיף כלל התנהגות — הורים לא נשארים בשיעור')
        self.assertIn('מיכל ענתה מעליו.\nדיוק (דנה מנהלת): "זה צריך להיות כלל ולא נוסח', self.waiting.explanation)
        self.assertEqual(self.waiting.evidence[-1], {'message_id': None, 'who': 'office', 'text': 'דיוק: זה צריך להיות כלל ולא נוסח, ולהסביר שמחכים בחוץ'})
        item = self.waiting.applied_item
        self.assertEqual((item.kind, item.title, item.is_active), ('behavior_rule', 'הורים לא נשארים בשיעור', True))
        self.assertEqual(KnowledgeHistory.objects.get(item=item).note, 'דיוק: זה צריך להיות כלל ולא נוסח, ולהסביר שמחכים בחוץ')
        self.assertFalse(KnowledgeItem.objects.filter(kind='phrasing').exists())

        self.assertEqual((data['proposal_id'], data['contact_id'], data['message_ids']), (self.waiting.id, self.who.id, [self.asked.id, self.answered.id]))
        card = data['changes'][0]
        self.assertEqual((card['proposal_id'], card['applied'], card['item_id']), (self.waiting.id, True, item.id))
        self.assertEqual(card['plain']['headline'], 'מעכשיו הבוט יגיד להורים שמחכים מחוץ לשיעור')
        self.assertEqual((card['plain']['kind_label'], card['plain']['is_new']), ('כלל התנהגות', True))
        self.assertIn('שיניתי: הוספתי כלל התנהגות "הורים לא נשארים בשיעור"', data['reply'])
        self.assertEqual(RefineTurn.objects.get(role='system').meta['proposal_before']['change']['kind'], 'phrasing')
        self.assertEqual([row['thread'] for row in self.get('refine/', proposal=self.waiting.id).data], [data['thread']])

        # Claude was shown the proposal itself, its evidence, and the conversation around its messages.
        body = post.call_args.kwargs['json']
        told = body['messages'][0]['content']
        self.assertIn('# ההצעה שהמפעילה לחצה עליה "דיוק" (נתונים)', told)
        self.assertIn('מקור: נציג ענה מעל הבוט · מצב: ממתין לאישור', told)
        self.assertIn('כותרת: להוסיף נוסח: לא נשארים בשיעור', told)
        self.assertIn('  המשרד: לא נשארים', told)
        self.assertIn('▶ ', told)
        self.assertIn('[ממתין, לא הוחל] create · phrasing · רשומה חדשה · זו ההצעה שהמפעילה לחצה עליה "דיוק"', told)
        self.assertIn('"""\nזה צריך להיות כלל ולא נוסח, ולהסביר שמחכים בחוץ\n"""', told)
        self.assertIn('דיוק על הצעה קיימת', body['system'])

        # "בטל" in the chat takes it back like any change of a thread.
        self.assertEqual(self.post(f'refine/{data["thread"]}/undo/').status_code, 200)
        item.refresh_from_db()
        self.waiting.refresh_from_db()
        self.assertEqual((item.is_active, self.waiting.status), (False, 'undone'))

    def test_with_the_switch_off_or_a_doubt_it_is_rewritten_and_goes_on_waiting(self):
        settings_store.set_review_auto_apply(False)
        with patch(POST, return_value=self.corrected()):
            data = self.press().data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.change['kind'], KnowledgeProposal.objects.count()), ('pending', 'behavior_rule', 1))
        self.assertEqual((data['changes'][0]['proposal_id'], data['changes'][0]['applied'], data['status']), (self.waiting.id, False, 'done'))
        self.assertIn('עדכנתי את ההצעה', data['reply'])
        self.assertIn('האישור האוטומטי כבוי', data['reply'])
        self.assertEqual(KnowledgeItem.objects.count(), 3)
        # One more word from her rewrites the same proposal again.
        settings_store.set_review_auto_apply(True)
        again = said(self.new_rule(body='ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.'), earlier='replace', confidence='low')
        with patch(POST, return_value=again):
            more = self.post(f'refine/{data["thread"]}/say/', {'text': 'ובשיעור ניסיון אפשר להישאר'}).data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.change['after']['body']), ('pending', 'ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.'))
        self.assertEqual((more['needs_answer'], KnowledgeProposal.objects.count()), (True, 1))
        # And the owner's click applies what the proposal says now.
        approved = self.post(f'review/proposals/{self.waiting.id}/approve/')
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data['item']['body'], 'ההורים מחכים בחוץ. בשיעור ניסיון אפשר להישאר.')

    def test_a_question_leaves_the_proposal_as_it_was(self):
        before = dict(self.waiting.change)
        with patch(POST, return_value=said(self.new_rule(), question='גם בשיעור ניסיון?', confidence='low', earlier='replace')):
            data = self.press('לא מדויק').data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.change, KnowledgeProposal.objects.count()), ('pending', before, 1))
        self.assertEqual((data['needs_answer'], data['changes'], data['status']), (True, [], 'open'))
        self.assertIn('גם בשיעור ניסיון?', data['reply'])

    def test_not_needed_at_all_rejects_it_and_a_price_never_does(self):
        # The model wrote a price in spite of the rules: the change is dropped, and the proposal is NOT thrown away for it.
        priced = said(change(action='create', kind='fact', title='מחיר', body='השיעור עולה ₪60'), earlier='replace')
        with patch(POST, return_value=priced):
            data = self.press('תכתוב שם שזה עולה 60 שקל').data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.change['kind'], data['changes']), ('pending', 'phrasing', []))
        self.assertIn('מתקנים את המחיר בכרטיס החוג ב-Kogo', data['reply'])

        with patch(POST, return_value=said(earlier='replace', understood='ההצעה לא נחוצה: כבר יש כלל כזה')):
            gone = self.press('לא צריך את זה, כבר יש כלל כזה').data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.decided_by, KnowledgeItem.objects.count()), ('rejected', self.manager, 3))
        self.assertEqual(self.waiting.decision_note, 'נדחה בדיוק: לא צריך את זה, כבר יש כלל כזה')
        self.assertIn('דחיתי את ההצעה', gone['reply'])
        self.assertEqual((gone['status'], gone['needs_answer']), ('done', False))
        # A proposal that was decided against has nothing left to refine.
        refused = self.press('ועוד משהו')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('"נדחה"', refused.data['detail'])
        self.assertEqual(self.post('refine/', {'proposal': 999999, 'text': 'משהו'}).status_code, 400)
        self.assertEqual(self.post('refine/', {'proposal': self.waiting.id, 'text': ''}).status_code, 400)

    def test_an_applied_proposal_stays_and_its_item_is_what_gets_edited(self):
        approved = self.post(f'review/proposals/{self.waiting.id}/approve/').data
        item = KnowledgeItem.objects.get(pk=approved['item']['id'])
        self.assertEqual((item.kind, item.body, item.version), ('phrasing', 'לא נשארים', 1))
        sharper = said(change(action='update', item_id=item.id, kind='phrasing', body='לא נשארים בשיעור, מחכים בחוץ 🙂'), earlier='keep')
        with patch(POST, return_value=sharper) as post:
            data = self.press('הנוסח קצר מדי, שיסביר שמחכים בחוץ').data
        item.refresh_from_db()
        self.assertEqual((item.body, item.version, item.is_active), ('לא נשארים בשיעור, מחכים בחוץ 🙂', 2, True))
        self.assertEqual(KnowledgeHistory.objects.get(item=item, version=2).note, 'דיוק: הנוסח קצר מדי, שיסביר שמחכים בחוץ')
        self.waiting.refresh_from_db()
        self.assertEqual(self.waiting.status, 'applied')                          # the first proposal stands
        fix = KnowledgeProposal.objects.exclude(pk=self.waiting.id).get()
        self.assertEqual((fix.source, fix.status, fix.auto_applied, fix.change['action']), ('refine', 'applied', True, 'update'))
        self.assertEqual((data['changes'][0]['proposal_id'], data['changes'][0]['plain']['is_new']), (fix.id, False))
        self.assertIn(f'[הוחל על הידע] create · phrasing · רשומה #{item.id} · זו ההצעה שהמפעילה לחצה עליה "דיוק"', post.call_args.kwargs['json']['messages'][0]['content'])
        # Undo of the thread gives back what the proposal itself had made — and leaves the proposal applied.
        self.post(f'refine/{data["thread"]}/undo/')
        item.refresh_from_db()
        self.waiting.refresh_from_db()
        self.assertEqual((item.body, item.is_active, self.waiting.status), ('לא נשארים', True, 'applied'))
        # "Take it out altogether" on an applied proposal undoes it.
        with patch(POST, return_value=said(earlier='replace', understood='מבטל את הנוסח')):
            out = self.press('זה לא נכון בכלל, תוריד את זה').data
        item.refresh_from_db()
        self.waiting.refresh_from_db()
        self.assertEqual((item.is_active, self.waiting.status), (False, 'undone'))
        self.assertIn('ביטלתי את השינוי הקודם של השיחה הזאת', out['reply'])

    def test_without_claude_the_proposal_is_left_as_it_is(self):
        with patch(POST, side_effect=requests.Timeout('slow')):
            data = self.press().data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.change['kind'], KnowledgeProposal.objects.count()), ('pending', 'phrasing', 1))
        self.assertIn('ההצעה נשארה כמו שהיא', data['reply'])
        self.assertEqual((data['changes'], data['status']), ([], 'open'))

    def test_with_no_text_the_proposal_is_summarised_and_left_as_it_is(self):
        before = (self.waiting.title, self.waiting.explanation, dict(self.waiting.change), list(self.waiting.evidence))
        about = summed(suspected='נראה שההצעה כפולה: כבר יש בידע כלל על הורים בשיעור')
        with patch(POST, return_value=about) as post:
            response = self.press('')
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(data['summary']['suspected_issue'], 'נראה שההצעה כפולה: כבר יש בידע כלל על הורים בשיעור')
        self.assertEqual(data['reply'].splitlines()[-1], 'מה לשנות בהצעה?')
        self.assertEqual((data['proposal_id'], data['needs_answer'], data['status'], data['changes']), (self.waiting.id, True, 'open', []))
        self.assertEqual([(turn['role'], turn['outcome']) for turn in data['turns']], [('system', 'summary')])
        # The proposal she pressed on is exactly as it was — with the standing approval on — and nothing stands beside it.
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.title, self.waiting.explanation, self.waiting.change, self.waiting.evidence), before)
        self.assertEqual((self.waiting.status, self.waiting.auto_applied, KnowledgeProposal.objects.count()), ('pending', False, 1))
        self.assertEqual((KnowledgeItem.objects.count(), KnowledgeHistory.objects.count(), reviewer.auto_applied_today()), (3, 3, 0))

        # Claude was shown the proposal, its evidence, the change it proposes, and the conversation around its messages.
        body = post.call_args.kwargs['json']
        told = body['messages'][0]['content']
        self.assertIn('# ההצעה שהמפעילה לחצה עליה "דיוק" (נתונים)', told)
        self.assertIn('כותרת: להוסיף נוסח: לא נשארים בשיעור', told)
        self.assertIn('  המשרד: לא נשארים', told)
        self.assertIn('# השינוי שההצעה מציעה בידע (נתונים)\n- [ממתין, לא הוחל] create · phrasing · רשומה חדשה', told)
        self.assertIn('▶ ', told)
        self.assertIn('סיכום של הצעה קיימת', body['system'])
        self.assertEqual(body['output_config']['format']['schema']['required'], ['what_happened', 'what_was_answered', 'suspected_issue'])

        # Her answer is what to change in it: the same proposal is rewritten in place and applied, as with a first text.
        with patch(POST, return_value=self.corrected()) as post:
            after = self.post(f'refine/{data["thread"]}/say/', {'text': 'זה צריך להיות כלל ולא נוסח'}).data
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, self.waiting.auto_applied, self.waiting.change['kind'], KnowledgeProposal.objects.count()), ('applied', True, 'behavior_rule', 1))
        self.assertEqual((after['changes'][0]['proposal_id'], after['changes'][0]['applied'], after['summary']), (self.waiting.id, True, None))
        told = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('# מה המפעילה ענתה על הסיכום (נתונים — מה לשנות בהצעה)\n"""\nזה צריך להיות כלל ולא נוסח\n"""', told)
        self.assertIn('מה לשנות בהצעה?\n"""', told)
        self.assertIn('[ממתין, לא הוחל] create · phrasing · רשומה חדשה · זו ההצעה שהמפעילה לחצה עליה "דיוק"', told)
        self.assertNotIn('מהלך הדיוק עד עכשיו', told)

    def test_with_no_text_and_no_claude_the_summary_quotes_the_evidence(self):
        with patch(POST, side_effect=requests.Timeout('slow')):
            data = self.press('').data
        self.assertEqual(data['summary'], QUOTED)                                 # the bot's answer, the first after the customer's message
        self.assertEqual(data['reply'], 'מה קרה: הלקוח כתב: "אפשר להישאר עם הילד בשיעור?"\nמה נענה: הבוט הישן: "בטח, אין בעיה!"\nמה לשנות בהצעה?')
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, KnowledgeProposal.objects.count(), data['changes']), ('pending', 1, []))
        # A line an earlier "דיוק" left in the evidence is not an answer to the customer; a proposal with no
        # conversation behind it says so; and an applied one is summarised as well.
        bare = KnowledgeProposal.objects.create(
            source='service_note', title='להוסיף כלל', dedup_key='y', evidence=[{'message_id': None, 'who': 'office', 'text': 'דיוק: לא מדויק'}],
            change={'action': 'create', 'item_id': None, 'kind': 'behavior_rule', 'before': None, 'after': {'kind': 'behavior_rule', 'title': 'כלל', 'body': 'גוף'}},
        )
        with override_settings(ANTHROPIC_API_KEY=''), patch(POST, side_effect=AssertionError('no key, no call')):
            empty = self.press('', proposal=bare).data
            self.post(f'review/proposals/{bare.id}/approve/')
            applied = self.press('', proposal=bare)
        self.assertEqual(empty['summary'], {'what_happened': 'אין בהצעה ציטוט של הלקוח', 'what_was_answered': 'אין בהצעה ציטוט של תשובה', 'suspected_issue': None})
        self.assertEqual((empty['contact_id'], empty['message_ids']), (None, []))
        self.assertEqual((applied.status_code, applied.data['needs_answer']), (201, True))
        bare.refresh_from_db()
        self.assertEqual(bare.status, 'applied')
        # One that was decided against has nothing to summarise.
        self.post(f'review/proposals/{self.waiting.id}/reject/')
        with patch(POST, side_effect=AssertionError('a refused request reached Claude')):
            refused = self.press('')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('"נדחה"', refused.data['detail'])

    def test_managers_only(self):
        partner = client_for(make_user('partner@wahub.test', UserProfile.ROLE_PARTNER))
        with patch(POST, side_effect=AssertionError('a refused caller reached Claude')):
            self.assertEqual(partner.post(f'{BASE}/refine/', {'proposal': self.waiting.id, 'text': ''}, format='json').status_code, 403)
            self.assertEqual(partner.post(f'{BASE}/refine/', {'proposal': self.waiting.id, 'text': 'משהו לשנות'}, format='json').status_code, 403)
            self.assertIn(APIClient().post(f'{BASE}/refine/', {'proposal': self.waiting.id, 'text': 'משהו לשנות'}, format='json').status_code, (401, 403))
        self.waiting.refresh_from_db()
        self.assertEqual((self.waiting.status, RefineThread.objects.count()), ('pending', 0))
