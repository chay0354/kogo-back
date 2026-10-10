"""The automatic summary: the keyword rules, Claude behind a mock, and what neither may touch."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.test import override_settings
from django.utils import timezone

from apps.core.models import IntegrationCredential
from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import state
from apps.wahub.analysis import Line, Place, analyze_rules, resolve_branch
from apps.wahub.models import Contact, ContactEvent, Message
from apps.wahub.tests.base import WahubTestCase

POST = 'apps.wahub.analysis.requests.post'

PLACES = [
    Place(id='b-rosh', name='קרל וגרטי קורי 8', city='ראש העין'),
    Place(id='b-ks', name='קניון דמרי סנטר', city='כפר סבא'),
    Place(id='b-em', name='אם המושבות, רפאל איתן 5', city='פתח תקווה'),
    Place(id='b-mintz', name='מרכז העיר - מינץ 24', city='פתח תקווה'),
    Place(id='b-shoham', name='ביה״ס ניצנים - צורן 6', city='שוהם'),
]


def said(*texts, who='customer', when=None):
    return [Line(who=who, text=text, sent_at=when or timezone.now()) for text in texts]


def rules(*texts, places=PLACES, **kwargs):
    return analyze_rules(said(*texts, **kwargs), places)


class TopicRulesTests(WahubTestCase):
    def test_a_trial_lesson(self):
        for text in ('אפשר לקבוע שיעור ניסיון?', 'רוצים לבוא לשיעור נסיון', 'אפשר להתנסות קודם?'):
            self.assertEqual(rules(text).topic, 'trial', text)

    def test_registration(self):
        for text in ('איך נרשמים? אני רוצה להירשם', 'רוצה לרשום את הבת שלי', 'אשמח להצטרף לחוג'):
            self.assertEqual(rules(text).topic, 'registration', text)

    def test_a_trial_wins_over_registration_and_information(self):
        self.assertEqual(rules('כמה עולה החוג?', 'רוצה להירשם', 'אבל קודם שיעור ניסיון').topic, 'trial')
        self.assertEqual(rules('כמה עולה החוג?', 'רוצה להירשם').topic, 'registration')

    def test_asking_for_details_about_a_class(self):
        self.assertEqual(rules('אפשר פרטים על חוג קפוארה?').topic, 'info')
        self.assertEqual(rules('היי, אפשר לקבל מידע נוסף על זה?').topic, 'info')     # the ad's own button
        self.assertEqual(rules('יש לכם חוג מחול לגיל 6?').topic, 'info')
        self.assertEqual(rules('שלום', 'מה המחיר?', 'של חוג היפ הופ').topic, 'info')

    def test_a_price_question_with_no_class_anywhere_is_not_a_lead(self):
        self.assertEqual(rules('מה המחיר?').topic, 'other')
        self.assertEqual(rules('תודה רבה').topic, 'other')

    def test_renting_a_studio_or_a_birthday_is_not_a_class_lead(self):
        rental = rules('אפשר פרטים על השכרת סטודיו לשיעור פרטי?')
        self.assertEqual(rental.topic, 'other')
        self.assertEqual(rental.interest, 'none')
        self.assertIn('השכרת סטודיו', rental.summary)
        self.assertEqual(rules('מה המחיר ליום הולדת לגיל 7?').topic, 'other')
        # A rental question that also names a class is a class lead.
        self.assertEqual(rules('יש לכם חוג קפוארה? וגם מה המחיר להשכרה').topic, 'info')

    def test_only_the_parents_words_decide_the_topic(self):
        lines = said('שלום') + said('רוצים לקבוע שיעור ניסיון? אפשר להירשם כאן', who='bot')
        self.assertEqual(analyze_rules(lines, PLACES).topic, 'other')

    def test_nothing_written_by_the_parent_knows_nothing(self):
        known = analyze_rules(said('שלום, כאן קוגומלו', who='bot'), PLACES)
        self.assertEqual((known.topic, known.summary, known.interest), ('', '', ''))


class BranchRulesTests(WahubTestCase):
    def test_a_city_with_one_branch_is_that_branch(self):
        known = rules('יש חוג קפוארה בראש העין?')
        self.assertEqual((known.branch_id, known.branch_name, known.city), ('b-rosh', 'קרל וגרטי קורי 8', 'ראש העין'))
        known = rules('אנחנו מכפ"ס, יש חוג?')
        self.assertEqual(known.branch_id, 'b-ks')

    def test_nicknames_of_a_branch(self):
        self.assertEqual(rules('החוג בדמרי?').branch_id, 'b-ks')
        self.assertEqual(rules('יש חוג באם המושבות?').branch_id, 'b-em')
        self.assertEqual(rules('החוג במינץ').branch_id, 'b-mintz')
        self.assertEqual(rules('בבית ספר ניצנים').branch_id, 'b-shoham')

    def test_a_city_with_several_branches_stays_a_city(self):
        known = rules('יש חוג קפוארה בפתח תקווה?')
        self.assertIsNone(known.branch_id)
        self.assertEqual(known.city, 'פתח תקווה')
        self.assertEqual(known.branch_name, '')

    def test_a_named_branch_beats_its_city(self):
        known = rules('אנחנו מפתח תקווה, ליד אם המושבות')
        self.assertEqual(known.branch_id, 'b-em')

    def test_the_words_pick_between_two_branches_of_one_city(self):
        places = PLACES + [Place(id='b-psagot', name='פסגות אפק', city='ראש העין')]
        self.assertEqual(rules('החוג בפסגות אפק?', places=places).branch_id, 'b-psagot')
        self.assertEqual(rules('ברחוב קרל וגרטי', places=places).branch_id, 'b-rosh')
        unsure = rules('יש חוג בראש העין?', places=places)
        self.assertIsNone(unsure.branch_id)
        self.assertEqual((unsure.city, unsure.branch_name), ('ראש העין', 'ראש העין'))

    def test_a_branch_the_business_does_not_have_stays_as_text(self):
        known = rules('יש חוג ברמת גן?')
        self.assertIsNone(known.branch_id)
        self.assertEqual((known.city, known.branch_name), ('רמת גן', 'רמת גן'))

    def test_yehud_is_not_yehuda(self):
        self.assertEqual(rules('אנחנו מיהוד').city, 'יהוד')
        self.assertEqual(rules('גרים באור יהודה').city, 'אור יהודה')

    def test_a_city_with_no_branch_is_flagged(self):
        known = rules('יש לכם חוג בנתניה?')
        self.assertEqual(known.city, 'נתניה')
        self.assertIn('no_branch_nearby', known.flags)
        self.assertIsNone(known.branch_id)

    def test_the_parents_words_come_before_the_bots(self):
        lines = said('יש חוג קפוארה בשוהם?') + said('יש לנו סניף בכפר סבא', who='bot')
        self.assertEqual(analyze_rules(lines, PLACES).branch_id, 'b-shoham')
        lines = said('יש חוג קפוארה?') + said('כן, בכפר סבא בקניון דמרי', who='bot')
        self.assertEqual(analyze_rules(lines, PLACES).branch_id, 'b-ks')

    def test_a_list_of_every_city_and_the_office_address_do_not_count(self):
        listing = 'יש לנו סניפים בראש העין, כפר סבא, שוהם ופתח תקווה'
        self.assertIsNone(analyze_rules(said('יש חוג?') + said(listing, who='bot'), PLACES).branch_id)
        office = 'המשרד שלנו נמצא בראש העין'
        self.assertIsNone(analyze_rules(said('יש חוג?') + said(office, who='bot'), PLACES).branch_id)

    def test_a_template_the_system_sent_never_counts(self):
        template = Line(who='office', text='תזכורת: שיעור ניסיון בכפר סבא', sent_at=timezone.now(), is_template=True)
        self.assertIsNone(analyze_rules(said('יש חוג?') + [template], PLACES).branch_id)

    def test_resolving_a_name_claude_returned(self):
        self.assertEqual(resolve_branch('קניון דמרי סנטר', PLACES).id, 'b-ks')
        self.assertEqual(resolve_branch('כפר סבא', PLACES).id, 'b-ks')
        self.assertIsNone(resolve_branch('פתח תקווה', PLACES))
        self.assertIsNone(resolve_branch('סניף שלא קיים', PLACES))
        self.assertIsNone(resolve_branch('', PLACES))


class OtherRulesTests(WahubTestCase):
    def test_the_class(self):
        self.assertEqual(rules('חוג קפואירה לילדים').course_type, 'קפוארה')
        self.assertEqual(rules('חוג היפ-הופ').course_type, 'היפ הופ')
        self.assertEqual(rules('חוג אקרובטיקה').course_type, 'אקרובטיקה')
        self.assertEqual(rules('שיעור ריקוד').course_type, 'מחול')
        self.assertEqual(rules('שלום').course_type, '')

    def test_the_age(self):
        self.assertEqual(rules('הבן שלי בן 5').child_age, '5')
        self.assertEqual(rules('היא בת 7 וחצי').child_age, '7.5')
        self.assertEqual(rules('חוג לגיל 4?').child_age, '4')
        self.assertEqual(rules('הוא בכיתה ב').child_age, 'כיתה ב')
        self.assertEqual(rules('שלום').child_age, '')

    def test_interest(self):
        self.assertEqual(rules('רוצה להירשם לחוג קפוארה').interest, 'hot')
        self.assertEqual(rules('אפשר שיעור ניסיון?').interest, 'hot')
        self.assertEqual(rules('אפשר פרטים על חוג קפוארה?').interest, 'warm')
        self.assertEqual(rules('אפשר פרטים על חוג קפוארה?', 'תודה, לא מעוניינת').interest, 'cold')
        self.assertEqual(rules('תודה').interest, 'none')

    def test_flags(self):
        self.assertIn('price', rules('חוג קפוארה - זה יקר לנו').flags)
        self.assertIn('class_full', rules('אמרו לי שאין מקום בחוג').flags)
        self.assertIn('lives_far', rules('זה קצת רחוק לנו').flags)
        self.assertIn('says_registered', rules('כבר נרשמנו אתמול').flags)
        self.assertIn('child_too_young', rules('אמרו שהוא קטן מדי לחוג').flags)
        self.assertIn('complaint', rules('אני מאוכזבת מהשירות').flags)
        self.assertEqual(rules('אפשר פרטים על חוג קפוארה?').flags, [])

    def test_a_day_to_come_back_is_counted_from_the_day_it_was_written(self):
        written = timezone.now() - timedelta(days=10)
        day = timezone.localtime(written).date()
        self.assertEqual(rules('אחזור אליכם בעוד שבועיים', when=written).callback_on, day + timedelta(days=14))
        self.assertEqual(rules('נדבר שבוע הבא', when=written).callback_on, day + timedelta(days=7))
        self.assertEqual(rules('אעדכן אתכם מחר', when=written).callback_on, day + timedelta(days=1))
        self.assertEqual(rules('אחשוב על זה ואחזור בעוד חודש', when=written).callback_on, day + timedelta(days=30))
        self.assertIsNone(rules('השיעור מחר?', when=written).callback_on)
        self.assertIsNone(rules('אחזור אליכם', when=written).callback_on)

    def test_the_summary_is_short_hebrew(self):
        known = rules('אפשר לקבוע שיעור ניסיון בקפוארה בראש העין? הוא בן 5')
        self.assertEqual(known.summary, 'מתעניינים בשיעור ניסיון בקפוארה, קרל וגרטי קורי 8. גיל הילד: 5.')
        self.assertEqual(rules('רוצה להירשם לחוג מחול').summary, 'רוצים להירשם לחוג מחול.')
        self.assertEqual(rules('אפשר פרטים על חוג קפוארה?').summary, 'ביקשו פרטים על קפוארה.')
        self.assertLessEqual(len(known.summary.split('. ')), 2)


def claude(card, stop_reason='end_turn', status=200, thinking=True):
    """A stand-in for the Messages API's answer."""
    content = ([{'type': 'thinking', 'thinking': '', 'signature': 'x'}] if thinking else []) + [
        {'type': 'text', 'text': card if isinstance(card, str) else json.dumps(card, ensure_ascii=False)},
    ]
    response = MagicMock(status_code=status)
    response.json.return_value = {
        'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-sonnet-5-5',
        'content': content, 'stop_reason': stop_reason, 'usage': {'input_tokens': 900, 'output_tokens': 120},
    }
    return response


CARD = {
    'topic': 'trial', 'course_type': 'קפוארה', 'city': 'ראש העין', 'branch_name': 'קרל וגרטי קורי 8',
    'child_age': '5', 'interest': 'warm', 'callback_on': '2026-10-24', 'flags': ['price'],
    'summary': 'שאלה על שיעור ניסיון בקפוארה לבן 5. אמרה שתחזור בעוד שבועיים.',
}


@override_settings(ANTHROPIC_API_KEY='test-anthropic-key', WAHUB_AI_MODEL='claude-sonnet-5-5')
class ClaudeTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        city = TestDataFactory.create_city('ראש העין')
        self.branch = TestDataFactory.create_branch(name='קרל וגרטי קורי 8', city=city)
        TestDataFactory.create_branch(name='סניף סגור', city=city, is_active=False)
        self.incoming('היי, אפשר שיעור ניסיון בקפוארה? הוא בן 5', name='רותם')
        self.bot_reply('בשמחה! באיזה סניף?')
        self.incoming('ראש העין. אבל זה יקר לי, אחזור בעוד שבועיים')

    def analyze(self):
        return self.post(f'contacts/{self.contact().id}/analyze/')

    def test_the_card_is_kept_in_the_known_fields(self):
        with patch(POST, return_value=claude(CARD)) as post:
            response = self.analyze()
        self.assertEqual(response.status_code, 200)
        post.assert_called_once()
        known = response.data['known']
        self.assertEqual(known['topic'], 'trial')
        self.assertEqual(known['topic_label'], 'שיעור ניסיון')
        self.assertEqual(known['course_type'], 'קפוארה')
        self.assertEqual(known['city'], 'ראש העין')
        self.assertEqual(known['branch_id'], str(self.branch.id))
        self.assertEqual(known['branch_name'], 'קרל וגרטי קורי 8')
        self.assertEqual(known['child_age'], '5')
        self.assertEqual((known['interest'], known['interest_label']), ('warm', 'מתעניין'))
        self.assertEqual(known['callback_on'], '2026-10-24')
        self.assertEqual((known['flags'], known['flag_labels']), (['price'], ['המחיר עצר אותו']))
        self.assertEqual(known['summary'], CARD['summary'])
        self.assertEqual(known['analysis_source'], 'ai')
        self.assertIsNotNone(known['analyzed_at'])
        self.assertFalse(self.contact().needs_analysis)
        self.assertEqual(ContactEvent.objects.filter(kind='analyzed').count(), 1)

    def test_the_request_is_the_messages_api_as_it_should_be_called(self):
        with patch(POST, return_value=claude(CARD)) as post:
            self.analyze()
        (url,), kwargs = post.call_args
        self.assertEqual(url, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(kwargs['headers']['x-api-key'], 'test-anthropic-key')
        self.assertEqual(kwargs['headers']['anthropic-version'], '2023-06-01')
        self.assertLessEqual(max(kwargs['timeout']), 12)

        body = kwargs['json']
        self.assertEqual(body['model'], 'claude-sonnet-5-5')
        # Structured output, not a forced tool call (current models refuse one);
        # no thinking budget, no sampling parameters, no prefilled answer.
        self.assertEqual(body['output_config']['format']['type'], 'json_schema')
        schema = body['output_config']['format']['schema']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), set(schema['properties']))
        self.assertEqual(set(schema['properties']), set(CARD))
        self.assertEqual(body['output_config']['effort'], 'low')
        for absent in ('tool_choice', 'tools', 'thinking', 'temperature', 'top_p', 'top_k'):
            self.assertNotIn(absent, body)
        self.assertEqual([message['role'] for message in body['messages']], ['user'])

        prompt = body['messages'][0]['content']
        self.assertIn(f'היום: {state.now_israel_date().isoformat()}', prompt)
        self.assertIn('קרל וגרטי קורי 8 — ראש העין', prompt)
        self.assertNotIn('סניף סגור', prompt)
        self.assertIn('לקוח: היי, אפשר שיעור ניסיון בקפוארה? הוא בן 5', prompt)
        self.assertIn('בוט: בשמחה! באיזה סניף?', prompt)
        today = timezone.localtime(timezone.now()).strftime('%Y-%m-%d')
        self.assertIn(f'[{today} ', prompt)

    def test_at_most_forty_messages_are_sent_the_newest_ones(self):
        contact = self.contact()
        Message.objects.bulk_create([
            Message(contact=contact, direction='in', sender='customer', text=f'הודעה מספר {index}')
            for index in range(60)
        ])
        with patch(POST, return_value=claude(CARD)) as post:
            self.analyze()
        prompt = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertEqual(prompt.count('לקוח: הודעה מספר'), 40)
        self.assertIn('הודעה מספר 59', prompt)
        self.assertNotIn('הודעה מספר 19\n', prompt)

    def test_a_failed_send_is_not_part_of_the_conversation(self):
        Message.objects.create(
            contact=self.contact(), direction='out', sender='office', text='הודעה שלא נשלחה', status='failed',
        )
        with patch(POST, return_value=claude(CARD)) as post:
            self.analyze()
        self.assertNotIn('הודעה שלא נשלחה', post.call_args.kwargs['json']['messages'][0]['content'])

    def test_values_outside_the_contract_are_dropped(self):
        card = dict(CARD, topic='sales', interest='very', flags=['price', 'vip', 'price'], callback_on='בעוד שבועיים',
                    branch_name='סניף שלא קיים', summary='  שורה  \n ארוכה ' + 'א' * 500)
        with patch(POST, return_value=claude(card)):
            known = self.analyze().data['known']
        self.assertEqual((known['topic'], known['interest']), ('', ''))
        self.assertEqual(known['flags'], ['price'])
        self.assertIsNone(known['callback_on'])
        self.assertIsNone(known['branch_id'])
        self.assertEqual(known['branch_name'], 'סניף שלא קיים')      # no match: the words only
        self.assertLessEqual(len(known['summary']), 300)
        self.assertNotIn('\n', known['summary'])

    def test_any_failure_falls_back_to_the_rules_without_raising(self):
        failures = {
            'a timeout': dict(side_effect=requests.Timeout('slow')),
            'no connection': dict(side_effect=requests.ConnectionError('down')),
            'an error status': dict(return_value=claude(CARD, status=529)),
            'a refusal': dict(return_value=claude('', stop_reason='refusal')),
            'an answer cut short': dict(return_value=claude('{"topic": "tri', stop_reason='max_tokens')),
            'not json': dict(return_value=claude('מצטער, לא הצלחתי')),
            'json that is not a card': dict(return_value=claude('[1, 2]')),
            'no text at all': dict(return_value=MagicMock(status_code=200, json=MagicMock(return_value={'content': []}))),
        }
        for name, behaviour in failures.items():
            with self.subTest(failure=name):
                Contact.objects.update(known_summary='', analysis_source='', needs_analysis=True)
                with patch(POST, **behaviour):
                    response = self.analyze()
                self.assertEqual(response.status_code, 200)
                known = response.data['known']
                self.assertEqual(known['analysis_source'], 'rules')
                self.assertEqual(known['topic'], 'trial')
                self.assertEqual(known['branch_id'], str(self.branch.id))
                self.assertIn('price', known['flags'])
                self.assertFalse(self.contact().needs_analysis)

    @override_settings(ANTHROPIC_API_KEY='')
    def test_with_no_key_claude_is_never_called(self):
        with patch(POST) as post:
            known = self.analyze().data['known']
        post.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(known['analysis_source'], 'rules')
        self.assertFalse(self.get('status/').data['ai_configured'])

    @override_settings(ANTHROPIC_API_KEY='')
    def test_a_key_stored_by_a_manager_is_used(self):
        IntegrationCredential.objects.create(key='ANTHROPIC_API_KEY', value='stored-key')
        self.assertTrue(self.get('status/').data['ai_configured'])
        with patch(POST, return_value=claude(CARD)) as post:
            self.assertEqual(self.analyze().data['known']['analysis_source'], 'ai')
        self.assertEqual(post.call_args.kwargs['headers']['x-api-key'], 'stored-key')

    @override_settings(WAHUB_AI_MODEL='claude-haiku-5-5', WAHUB_AI_TIMEOUT_SECONDS=4)
    def test_the_model_and_the_timeout_are_settings(self):
        with patch(POST, return_value=claude(CARD)) as post:
            self.analyze()
        self.assertEqual(post.call_args.kwargs['json']['model'], 'claude-haiku-5-5')
        self.assertEqual(max(post.call_args.kwargs['timeout']), 4)

    def test_the_summary_never_touches_a_follow_up_mark(self):
        today = state.now_israel_date()
        Contact.objects.update(
            followup_status='later', followup_due=today + timedelta(days=3), followup_note='לחזור אחרי החגים',
            followup_by=self.manager, followup_at=timezone.now(),
        )
        fields = ('followup_status', 'followup_due', 'followup_note', 'followup_by', 'followup_at')
        marks = Contact.objects.values(*fields).get()
        with patch(POST, return_value=claude(CARD)):
            self.analyze()
        self.assertEqual(Contact.objects.values(*fields).get(), marks)
        with patch(POST, side_effect=requests.Timeout('slow')):
            self.analyze()
        self.assertEqual(Contact.objects.values(*fields).get(), marks)
        # What the customer said is in "known" — and that is what makes the contact due.
        self.assertEqual(str(self.contact().known_callback_on - today), '14 days, 0:00:00')

    def test_the_same_result_again_writes_no_second_journal_line(self):
        with patch(POST, return_value=claude(CARD)):
            self.analyze()
            Contact.objects.update(touched_at=timezone.now() - timedelta(minutes=5))
            touched = self.contact().touched_at
            self.analyze()
        self.assertEqual(ContactEvent.objects.filter(kind='analyzed').count(), 1)
        self.assertEqual(self.contact().touched_at, touched)
