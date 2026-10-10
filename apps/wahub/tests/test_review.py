"""The review: the four sources of a proposal, the sweep's patterns, approve with history, reject — and that nothing changes without a click."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone

from apps.wahub import knowledge, knowledge_import, reviewer, shadow
from apps.wahub.models import KnowledgeHistory, KnowledgeItem, KnowledgeProposal, Message, ServiceNote
from apps.wahub.tests.base import PHONE, WahubTestCase

POST = 'apps.wahub.reviewer.requests.post'


class ReviewTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        knowledge_import.run()
        self.knowledge_before = KnowledgeItem.objects.count()
        patch('apps.wahub.sending._deliver', side_effect=AssertionError('the review tried to send')).start()
        patch('apps.wahub.handoff._in_manychat', side_effect=AssertionError('the review tried to touch ManyChat')).start()
        self.addCleanup(patch.stopall)

    def office_says(self, contact, text, minutes_ago=0):
        return Message.objects.create(
            contact=contact, direction='out', sender='office', sender_name='מיכל', text=text, status='sent', source='kogo',
            sent_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def bot_says(self, contact, text, minutes_ago=0):
        return Message.objects.create(
            contact=contact, direction='out', sender='bot', text=text, status='sent', source='manychat',
            sent_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def customer_says(self, contact, text, minutes_ago=0):
        return Message.objects.create(
            contact=contact, direction='in', sender='customer', text=text, status='received', source='manychat',
            sent_at=timezone.now() - timedelta(minutes=minutes_ago),
        )


@override_settings(ANTHROPIC_API_KEY='')
class HumanOverrideTests(ReviewTestCase):
    def test_the_office_answering_over_the_bot_becomes_a_proposal_once(self):
        self.incoming('יש הנחה אם נרשמים לשני חוגים?')
        self.bot_reply('אבדוק עם הצוות ואחזור אלייך.')
        contact = self.contact()
        Message.objects.filter(contact=contact).update(last_message_at=None) if False else None
        self.office_says(contact, 'על חוג שני יש ₪10 הנחה בחודש, מתעדכן לבד בהרשמה באתר')
        counts = reviewer.scan()
        self.assertEqual((counts['overrides'], counts['proposals']), (1, 1))
        proposal = KnowledgeProposal.objects.get()
        self.assertEqual((proposal.source, proposal.status, proposal.contact_id), ('human_override', 'pending', contact.id))
        self.assertEqual([row['who'] for row in proposal.evidence], ['customer', 'bot', 'office'])
        self.assertEqual(proposal.change['action'], 'create')
        self.assertEqual(proposal.change['kind'], 'phrasing')
        self.assertEqual(proposal.change['after']['body'], 'על חוג שני יש ₪10 הנחה בחודש, מתעדכן לבד בהרשמה באתר')
        self.assertIn('הבוט הישן ענה', proposal.explanation)
        self.assertIn('מיכל', proposal.explanation)
        # The same day, the same conversation: nothing twice.
        self.assertEqual(reviewer.scan()['proposals'], 0)
        self.assertEqual(KnowledgeProposal.objects.count(), 1)
        self.assertEqual(KnowledgeItem.objects.count(), self.knowledge_before)

    def test_the_office_answering_over_the_shadow_counts_too(self):
        self.incoming('הבת שלי בת 3 וחצי, מתאים לקפוארה?')
        contact = self.contact()
        shadow.propose(contact)
        self.office_says(contact, 'יש קבוצת גיל רך מגיל 3 בכפר סבא ובראש העין')
        reviewer.scan()
        proposal = KnowledgeProposal.objects.get(source='human_override')
        self.assertEqual(proposal.evidence[1]['who'], 'shadow')
        self.assertIsNotNone(proposal.shadow_id)


@override_settings(ANTHROPIC_API_KEY='')
class PatternTests(ReviewTestCase):
    def test_every_pattern_is_found_in_its_conversation(self):
        contact = self.make_contact()
        since = timezone.now() - timedelta(hours=24)
        rows = [
            self.customer_says(contact, 'יש חוג קפוארה בראש העין?', 300),
            self.bot_says(contact, '<invoke name="Course_Manager">{"city": "ראש העין"}</invoke>', 299),
            self.bot_says(contact, 'באיזו עיר אתם?', 298),
            self.customer_says(contact, 'ראש העין', 297),
            self.bot_says(contact, 'באיזו עיר אתם?', 296),
            self.customer_says(contact, 'כבר כתבתי, ראש העין', 295),
            self.bot_says(contact, 'רשמתי שתגיעו ביום חמישי. אבדוק ואחזור אלייך.', 200),
            self.customer_says(contact, 'לא עניתם לי', 100),
            self.bot_says(contact, 'אני מעבירה לניקול. המספר לא זמין כרגע.', 99),
            self.bot_says(contact, 'לא ניתן לשמוע הודעות קוליות', 98),
            self.customer_says(contact, 'יש חוג קפוארה בראש העין?', 50),
        ]
        found = {item['pattern']: item['evidence'] for item in reviewer.detect_patterns(rows, since)}
        self.assertEqual(set(found), {
            'internal_text', 'bot_twice', 'loop_question', 'will_check', 'voice_not_heard', 'not_answered',
            'customer_repeated', 'fake_registration', 'nicole', 'number_unavailable',
        })
        self.assertEqual([row.id for row in found['bot_twice']], [rows[1].id, rows[2].id])
        self.assertEqual([row.id for row in found['loop_question']], [rows[2].id, rows[4].id])
        self.assertEqual([row.id for row in found['customer_repeated']], [rows[0].id, rows[10].id])

    def test_the_sweep_proposes_fixes_and_marks_who_needs_a_person(self):
        contact = self.make_contact(name='רותם')
        self.customer_says(contact, 'יש חוג?', 200)
        self.bot_says(contact, 'אבדוק ואחזור אליך.', 199)
        from apps.wahub import state
        state.touch(contact.pk, last_message_at=timezone.now() - timedelta(minutes=199))
        counts = reviewer.scan()
        self.assertEqual(counts['patterns'], {'will_check': 1})
        self.assertEqual(counts['needs_human_marked'], 1)
        contact.refresh_from_db()
        self.assertTrue(contact.needs_human)
        self.assertEqual(contact.needs_human_reason, '"אבדוק ואחזור" בלי המשך')
        proposal = KnowledgeProposal.objects.get(source='reviewer')
        self.assertEqual(proposal.pattern, 'will_check')
        self.assertEqual(proposal.change['kind'], 'behavior_rule')
        self.assertEqual(proposal.change['after']['title'], 'לא מבטיחים "אבדוק ואחזור"')
        self.assertIn('אבדוק ואחזור אליך', proposal.change['after']['example_bad'])
        # A second conversation with the same failure adds no second open proposal.
        other = self.make_contact(phone='972505550199')
        self.customer_says(other, 'שאלה', 150)
        self.bot_says(other, 'אבדוק ואחזור.', 149)
        state.touch(other.pk, last_message_at=timezone.now())
        self.assertEqual(reviewer.scan()['proposals'], 0)
        self.assertEqual(KnowledgeItem.objects.count(), self.knowledge_before)

    def test_a_pattern_whose_rule_already_exists_adds_a_bad_example_instead(self):
        rule = knowledge.create_item({'kind': 'behavior_rule', 'title': 'אין ניקול במשרד', 'body': 'לא מפנים לניקול.'})
        contact = self.make_contact()
        self.customer_says(contact, 'רוצה לבטל', 20)
        self.bot_says(contact, 'מעבירה לניקול', 19)
        from apps.wahub import state
        state.touch(contact.pk, last_message_at=timezone.now())
        reviewer.scan()
        self.assertFalse(KnowledgeProposal.objects.filter(pattern='nicole').exists())     # the rule is already there, by title


@override_settings(ANTHROPIC_API_KEY='')
class ServiceNoteAndDecisionTests(ReviewTestCase):
    def test_a_short_note_asks_for_more_and_a_real_one_becomes_a_proposal(self):
        short = self.post('review/notes/', {'text': 'טעות'})
        self.assertEqual(short.status_code, 201)
        self.assertIsNone(short.data['proposal_id'])
        self.assertIn('לא הבנתי', short.data['detail'])
        self.assertEqual(ServiceNote.objects.count(), 1)

        self.incoming('אפשר להישאר עם הילד בשיעור?')
        self.bot_reply('בטח, אין בעיה!')
        contact = self.contact()
        message = Message.objects.filter(direction='in').get()
        response = self.post('review/notes/', {'text': 'הבוט אמר שאפשר להישאר בשיעור, וזה לא נכון — לא נשארים עם הילד', 'message_id': message.id})
        self.assertEqual(response.status_code, 201)
        proposal = KnowledgeProposal.objects.get(pk=response.data['proposal_id'])
        self.assertEqual((proposal.source, proposal.contact_id, proposal.message_id), ('service_note', contact.id, message.id))
        self.assertEqual([row['who'] for row in proposal.evidence], ['customer', 'bot', 'office'])
        self.assertEqual(ServiceNote.objects.get(pk=response.data['note_id']).proposal_id, proposal.id)
        listed = self.get('review/proposals/', status='pending').data
        self.assertEqual([row['id'] for row in listed], [proposal.id])
        self.assertEqual(listed[0]['source_label'], 'הערת שירות')
        self.assertEqual(listed[0]['status_label'], 'ממתין לאישור')
        self.assertEqual(self.get('review/proposals/', status='applied').data, [])
        self.assertEqual(len(self.get('review/notes/').data), 2)
        self.assertEqual(self.post('review/notes/', {'text': ''}).status_code, 400)
        self.assertEqual(self.post('review/notes/', {'text': 'הבוט טעה פה בגדול', 'contact_id': 999999}).status_code, 400)

    def test_approve_applies_the_change_with_history_and_only_once(self):
        response = self.post('review/notes/', {'text': 'כשמבקשים טלפון של המשרד — לתת את המספר ולא להגיד שהוא לא זמין'})
        proposal_id = response.data['proposal_id']
        before = KnowledgeItem.objects.count()
        approved = self.post(f'review/proposals/{proposal_id}/approve/', {'note': 'נכון'})
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data['proposal']['status'], 'applied')
        self.assertEqual(approved.data['proposal']['decided_by_name'], 'דנה מנהלת')
        self.assertEqual(approved.data['proposal']['decision_note'], 'נכון')
        item = KnowledgeItem.objects.get(pk=approved.data['item']['id'])
        self.assertEqual(KnowledgeItem.objects.count(), before + 1)
        self.assertEqual(item.kind, 'behavior_rule')
        self.assertIn('לתת את המספר', item.body)
        history = KnowledgeHistory.objects.get(item=item)
        self.assertEqual(history.note, f'לפי הצעה #{proposal_id}')
        self.assertEqual(history.changed_by, self.manager)
        self.assertEqual(KnowledgeProposal.objects.get(pk=proposal_id).applied_item_id, item.id)
        again = self.post(f'review/proposals/{proposal_id}/approve/')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.post(f'review/proposals/{proposal_id}/reject/', {'note': 'x'}).status_code, 409)
        self.assertEqual(KnowledgeItem.objects.count(), before + 1)

    def test_approve_of_an_update_makes_a_new_version_naming_the_conversation(self):
        item = knowledge.create_item({'kind': 'fact', 'title': 'חניה', 'body': 'יש חניה'})
        self.incoming('יש חניה?')
        contact = self.contact()
        proposal = KnowledgeProposal.objects.create(
            source='service_note', contact=contact, title='לדייק חניה', explanation='…',
            change={'action': 'update', 'item_id': item.id, 'kind': 'fact', 'before': {'body': 'יש חניה'}, 'after': {'body': 'יש חניה בקומה -1'}},
            evidence=[], dedup_key='x',
        )
        response = self.post(f'review/proposals/{proposal.id}/approve/')
        self.assertEqual(response.status_code, 200, response.data)
        item.refresh_from_db()
        self.assertEqual((item.body, item.version), ('יש חניה בקומה -1', 2))
        self.assertEqual(KnowledgeHistory.objects.get(item=item, version=2).note, f'לפי הצעה #{proposal.id} מתוך שיחה {contact.id}')

    def test_reject_and_summary(self):
        response = self.post('review/notes/', {'text': 'הבוט ענה באנגלית ללקוח, אסור'})
        proposal_id = response.data['proposal_id']
        rejected = self.post(f'review/proposals/{proposal_id}/reject/', {'note': 'כבר יש כלל כזה'})
        self.assertEqual((rejected.status_code, rejected.data['status'], rejected.data['decision_note']), (200, 'rejected', 'כבר יש כלל כזה'))
        summary = self.get('review/summary/').data
        self.assertEqual((summary['pending'], summary['applied_7d'], summary['rejected_7d'], summary['auto_mode']), (0, 0, 1, False))
        self.assertEqual(KnowledgeItem.objects.count(), self.knowledge_before)

    def test_a_broken_change_is_refused_and_the_proposal_stays_pending(self):
        proposal = KnowledgeProposal.objects.create(source='reviewer', title='x', change={'action': 'create', 'kind': 'link', 'after': {'title': 'x', 'url': 'no-https'}}, dedup_key='y')
        response = self.post(f'review/proposals/{proposal.id}/approve/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('https://', response.data['detail'])
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')


@override_settings(ANTHROPIC_API_KEY='test-key')
class FormulateTests(ReviewTestCase):
    def test_claude_words_the_proposal_when_there_is_a_key(self):
        answer = {
            'title': 'עובדה: לא נשארים בשיעור', 'explanation': 'הבוט אמר שאפשר; המשרד אומר שלא.', 'kind': 'fact', 'understood': True,
            'after': {'title': 'לא נשארים עם הילד', 'body': 'ההורים לא נשארים בשיעור', 'key': '', 'when_to_say': 'if_asked',
                      'example_good': '', 'example_bad': '', 'what_customer_writes': '', 'means': ''},
        }
        response = MagicMock(status_code=200)
        response.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}]}
        with patch(POST, return_value=response) as post:
            created = self.post('review/notes/', {'text': 'הבוט אמר שאפשר להישאר עם הילד בשיעור וזה לא נכון'})
        proposal = KnowledgeProposal.objects.get(pk=created.data['proposal_id'])
        self.assertEqual(proposal.title, 'עובדה: לא נשארים בשיעור')
        self.assertEqual(proposal.change['kind'], 'fact')
        self.assertEqual(proposal.change['after']['when_to_say'], 'if_asked')
        self.assertEqual(proposal.change['after']['body'], 'ההורים לא נשארים בשיעור')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['output_config']['format']['type'], 'json_schema')
        self.assertIn('הערת שירות', body['messages'][0]['content'])

    def test_when_claude_does_not_understand_the_rule_made_wording_stays(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps({
            'title': '', 'explanation': 'לא הבנתי, פרט', 'kind': 'fact', 'understood': False,
            'after': {'title': '', 'body': '', 'key': '', 'when_to_say': 'proactive', 'example_good': '', 'example_bad': '', 'what_customer_writes': '', 'means': ''},
        })}]}
        with patch(POST, return_value=response):
            created = self.post('review/notes/', {'text': 'משהו לא טוב קרה בשיחה הזאת'})
        proposal = KnowledgeProposal.objects.get(pk=created.data['proposal_id'])
        self.assertTrue(proposal.title.startswith('הערת שירות'))
        self.assertIn('לא הבנתי', proposal.explanation)
