"""
"מה הבוט לא ידע השבוע" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.3): which answers of the new bot are kept as
"did not know" and which are not, that no name and no phone is kept with them, how they are gathered under
topics — by Claude once a day, by their words when there is no key — the list of a week with its numbers,
and what the owner does to a topic: teaches it through "ללמד", or says it does not matter.

Claude is a mock throughout, and nothing here sends: `self.network` fails any test that reaches out.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.utils import timezone

from apps.core.models import UserProfile
from apps.customers.models import Child, Family, Parent
from apps.wahub import cron, knowledge, refine, settings_store, shadow, unknowns
from apps.wahub.models import BotUnknown, BotUnknownTopic, Contact, KnowledgeItem, KnowledgeProposal, RefineThread, ShadowReply
from apps.wahub.tests.base import WahubTestCase, client_for, make_user
from apps.wahub.tests.test_leads_v2 import ZONE, LeadsCase
from apps.wahub.tests.test_teach import POST, Claude, bot, change, taught, verdict

MISSING = 'את זה אני בודקת מול המשרד, ונחזור אליכם עם תשובה'
KEY = dict(ANTHROPIC_API_KEY='test-key', WAHUB_SHADOW_MODEL='claude-opus-5-5', WAHUB_AI_MODEL='claude-sonnet-5-5')
ASK = 'apps.wahub.refine._ask'


def said(text=MISSING, **fields) -> shadow.Draft:
    return shadow.Draft(text=text, reasoning='בדיקה', **fields)


class UnknownsCase(LeadsCase):
    """Monday 12.10.2026, noon. The week began on Sunday 11.10."""

    def setUp(self):
        super().setUp()
        self.phones = 0

    def customer(self, **fields) -> Contact:
        self.phones += 1
        return Contact.objects.create(phone=f'9725055588{self.phones:02d}', **fields)

    def asks(self, question, draft=None, contact=None) -> ShadowReply:
        """A customer writes `question`, and the new bot answers it with `draft` (in the shadow: nothing is sent)."""
        contact = contact or self.customer()
        self.incoming(question, phone=contact.phone)
        with patch.object(shadow, 'draft', return_value=draft or said(request_human=True, request_human_reason='שאלו על חניה, ואין אותה במערכת')):
            return shadow.propose(Contact.objects.get(pk=contact.pk))

    def case(self, question='יש חניה ליד הסניף?', *, topic=None, hint='', answer=MISSING, days_ago=0, contact=None) -> BotUnknown:
        return BotUnknown.objects.create(
            question=question, answer=answer, hint=hint, how='phrasing', topic=topic, contact=contact,
            created_at=self.clock() - timedelta(days=days_ago),
        )

    def topic(self, title='חניה בסניף', **fields) -> BotUnknownTopic:
        return BotUnknownTopic.objects.create(title=title, keywords=unknowns.keywords_of(title), **fields)

    def rows(self, **params) -> dict:
        response = self.get('bot/unknowns/', **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data


# --- when did the bot not know ---------------------------------------------------------------------------

class DetectTests(UnknownsCase):
    def test_the_bot_said_so_itself_together_with_a_handover_or_the_phrasing(self):
        self.assertEqual(
            unknowns.detect('בודקת ונחזור', unknown='חניה ליד הסניף', request_human=True, reason='שאלו על חניה'),
            ('model', 'חניה ליד הסניף'),
        )
        self.assertEqual(unknowns.detect(MISSING, unknown='מחיר תלבושת'), ('model', 'מחיר תלבושת'))
        # The field alone — a bot that only asks which branch is meant has not failed to know.
        self.assertIsNone(unknowns.detect('באיזה סניף מדובר?', unknown='לא ידוע איזה סניף'))

    def test_the_phrasing_as_the_knowledge_has_it_today(self):
        self.assertEqual(unknowns.detect(f'היי\n{MISSING} 🙂'), ('phrasing', ''))
        self.assertEqual(unknowns.detect('את זה אני בודקת מול המשרד ונחזור אליכם', reason='שאלו על חניה')[0], 'phrasing')
        # The owner rewrote the phrasing: its new opening words are what is looked for, and the old ones are not.
        rewritten = 'אין לי את הפרט הזה כרגע\nאפשר לבדוק במשרד {טלפון_משרד}'
        self.assertEqual(unknowns.detect('אין לי את הפרט הזה כרגע, מצטערת', phrasing=rewritten), ('phrasing', ''))
        self.assertIsNone(unknowns.detect(MISSING, phrasing=rewritten))
        # A tail every handover has is not the phrasing.
        self.assertIsNone(unknowns.detect('הפנייה הועברה לנציג, ונחזור אליכם עם תשובה'))

    def test_a_handover_because_the_information_is_missing(self):
        for reason in ('שאלו על הוויפי בסניף, ואין אותו במערכת', 'שאלו על חגים וחופשות, ואין לוח חופשות לשלוח לסניף',
                       'המחיר של התלבושת לא מופיע במערכת', 'חסר מידע על חניה'):
            with self.subTest(reason=reason):
                self.assertEqual(unknowns.detect('נציג יחזור אליכם', request_human=True, reason=reason), ('handoff', reason))

    def test_a_handover_for_another_reason_is_not_not_knowing(self):
        for reason in ('הלקוח ביקש נציג', 'תלונה על מדריך', 'לקוח כועס על חיוב כפול — לבדוק את החיוב', 'מבקש שיחת טלפון',
                       'בקשה חריגה: שיעור פרטי', 'נתקע בהרשמה באתר', ''):
            with self.subTest(reason=reason):
                self.assertIsNone(unknowns.detect('נציג מהמשרד יחזור אליכם', request_human=True, reason=reason))
        self.assertIsNone(unknowns.detect('יש קפוארה ביום שלישי ב-17:00'))


class NoNameNoPhoneTests(UnknownsCase):
    def test_phones_mail_and_long_numbers_are_taken_out_and_sums_stay(self):
        self.assertEqual(
            unknowns.scrub('תתקשרו ל-050-123 4567 או 0521234567, המייל dana@example.com. זה עולה ₪260?'),
            'תתקשרו ל-[טלפון] או [טלפון], המייל [דוא״ל]. זה עולה ₪260?',
        )
        self.assertEqual(unknowns.scrub('ת.ז 123456782 של הילד בן 5, כיתה ב, 17:00'), 'ת.ז [טלפון] של הילד בן 5, כיתה ב, 17:00')

    def test_the_names_the_system_knows_are_taken_out_also_behind_a_glued_letter(self):
        names = {'נועה', 'לוי', 'דנה'}
        self.assertEqual(
            unknowns.scrub('אני דנה לוי, רציתי לדעת אם יש חניה כשמביאים את נועה, ולנועה יש אח', names),
            'אני [שם] [שם], רציתי לדעת אם יש חניה כשמביאים את [שם], ול[שם] יש אח',
        )
        # A name inside another word is not a name.
        self.assertEqual(unknowns.scrub('הלוויה והדנהיל', names), 'הלוויה והדנהיל')

    def test_whose_names(self):
        family = Family.objects.create(name='לוי', phone='0505558801')
        Parent.objects.create(family=family, first_name='דנה', last_name='לוי', phone='0505558801', is_primary=True)
        Child.objects.create(family=family, first_name='נועם', last_name='לוי', birth_date='2019-01-01', gender='male')
        contact = self.customer(name='דנה של גיל 🌸', kogo_family_id=family.id, lead_ai={'child': {'name': 'יובל'}})
        names = unknowns.names_of(contact)
        self.assertLessEqual({'דנה', 'לוי', 'נועם', 'יובל'}, names)
        # An everyday word a question is made of is never treated as a name, whoever is called by it.
        self.assertFalse(names & {'של', 'גיל'})
        self.assertEqual(unknowns.names_of(None), set())


# --- what is kept ----------------------------------------------------------------------------------------

class NoteTests(UnknownsCase):
    def test_an_answer_that_did_not_know_is_kept_with_the_question_and_no_name(self):
        contact = self.customer(name='רותם אברהם')
        reply = self.asks('היי אני רותם, 050-1234567. יש חניה ליד הסניף?', contact=contact)
        case = BotUnknown.objects.get()
        self.assertEqual((case.contact_id, case.shadow_id, case.how), (contact.id, reply.id, 'phrasing'))
        self.assertEqual(case.question, 'היי אני [שם], [טלפון]. יש חניה ליד הסניף?')
        self.assertEqual((case.answer, case.hint, case.topic), (MISSING, 'שאלו על חניה, ואין אותה במערכת', None))

    def test_what_the_bot_marked_itself_is_what_names_the_case(self):
        self.asks('כמה עולה התלבושת?', said('נציג יחזור אליכם עם המחיר', unknown='מחיר תלבושת', request_human=True, request_human_reason='שאלו על מחיר תלבושת'))
        self.assertEqual(list(BotUnknown.objects.values_list('how', 'hint')), [('model', 'מחיר תלבושת')])

    def test_an_answer_that_knew_and_a_handover_for_another_reason_are_not_kept(self):
        self.asks('יש קפוארה?', said('יש קפוארה ביום שלישי'))
        self.asks('אני רוצה לדבר עם מישהו', said('נציג יחזור אליכם', request_human=True, request_human_reason='הלקוח ביקש נציג'))
        self.asks('באיזה סניף?', said('באיזה סניף מדובר?', unknown='לא ידוע איזה סניף'))
        self.assertEqual(ShadowReply.objects.count(), 3)
        self.assertFalse(BotUnknown.objects.exists())

    def test_a_demo_conversation_is_never_kept(self):
        self.asks('יש חניה?', contact=self.customer(is_demo=True, source='demo'))
        self.assertEqual(ShadowReply.objects.count(), 1)
        self.assertFalse(BotUnknown.objects.exists())

    def test_the_same_question_of_the_same_customer_is_one_a_day(self):
        contact = self.customer()
        self.asks('יש חניה?', contact=contact)
        self.clock.forward(minutes=30)
        self.asks('יש חניה?', contact=contact)
        self.asks('יש חניה?')                                       # somebody else: another case
        self.assertEqual(BotUnknown.objects.count(), 2)
        self.clock.forward(days=1)
        self.asks('יש חניה?', contact=contact)
        self.assertEqual(BotUnknown.objects.count(), 3)

    def test_a_fault_in_the_keeping_never_loses_the_answer(self):
        with patch.object(unknowns, 'note', side_effect=RuntimeError('boom')), self.assertLogs('apps.wahub.shadow', level='ERROR'):
            reply = self.asks('יש חניה?')
        self.assertIsNotNone(reply)
        self.assertEqual(ShadowReply.objects.count(), 1)

    @override_settings(**KEY)
    def test_claudes_own_mark_travels_from_its_answer_to_the_case(self):
        contact = self.customer()
        self.incoming('יש חניה ליד הסניף?', phone=contact.phone)
        answer = {'text': MISSING, 'reasoning': 'אין מידע על חניה', 'knowledge_ids': [], 'unknown': 'חניה ליד הסניף'}
        response = MagicMock(status_code=200)
        response.json.return_value = {
            'model': 'claude-opus-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(answer, ensure_ascii=False)}],
        }
        with patch('apps.wahub.shadow.requests.post', return_value=response) as post:
            shadow.propose(Contact.objects.get(pk=contact.pk))
        # The bot is asked for the field, and told when to fill it.
        body = post.call_args.kwargs['json']
        schema = body['output_config']['format']['schema']
        self.assertIn('unknown', schema['required'])
        self.assertIn('unknown', shadow.SYSTEM_TASK)
        self.assertEqual(list(BotUnknown.objects.values_list('how', 'hint', 'question')), [('model', 'חניה ליד הסניף', 'יש חניה ליד הסניף?')])


# --- the topics ------------------------------------------------------------------------------------------

class SortTests(UnknownsCase):
    def test_without_a_key_the_cases_are_gathered_by_their_words_at_once(self):
        first = self.case('יש חניה ליד הסניף?', hint='שאלו על חניה בסניף, ואין אותה במערכת')
        second = self.case('איפה אפשר לחנות? יש חניה בסניף?')
        third = self.case('מה הסיסמה של הוויפי?', hint='שאלו על הוויפי בסניף, ואין אותו במערכת')
        counts = unknowns.sort_new(now=self.clock())
        self.assertEqual(counts, {'unknowns_sorted': 3, 'unknown_topics_opened': 2})
        first.refresh_from_db(), second.refresh_from_db(), third.refresh_from_db()
        self.assertEqual(first.topic_id, second.topic_id)
        self.assertNotEqual(first.topic_id, third.topic_id)
        self.assertEqual((first.topic.title, third.topic.title), ('חניה בסניף', 'הוויפי בסניף'))
        self.assertEqual(settings_store.get(unknowns.SORTED_KEY)['by'], 'rules')
        # A later case about the same thing joins the topic that is there.
        fourth = self.case('החניה בתשלום?')
        unknowns.sort_new(now=self.clock())
        fourth.refresh_from_db()
        self.assertEqual((fourth.topic_id, BotUnknownTopic.objects.count()), (first.topic_id, 2))
        self.assertEqual(unknowns.sort_new(now=self.clock()), {'unknowns_sorted': 0, 'unknown_topics_opened': 0})

    @override_settings(**KEY)
    def test_with_a_key_claude_is_asked_once_a_day_with_the_quick_model(self):
        parking = self.topic('חניה בסניף')
        first = self.case('אפשר לחנות שם?')
        second = self.case('כמה עולה התלבושת?', hint='מחיר תלבושת')
        third = self.case('יש חולצה של הסטודיו? כמה היא?')
        answer = {'cases': [
            {'n': 1, 'topic_id': parking.id, 'new_title': ''},
            {'n': 2, 'topic_id': 0, 'new_title': 'מחיר תלבושת'},
            {'n': 3, 'topic_id': 0, 'new_title': 'מחיר תלבושת'},
        ]}
        with patch(ASK, return_value=(answer, 'claude-sonnet-5-5')) as ask:
            counts = unknowns.sort_new(now=self.clock())
        self.assertEqual(counts, {'unknowns_sorted': 3, 'unknown_topics_opened': 1})
        asked = ask.call_args.kwargs
        self.assertEqual((asked['model'], asked['effort']), ('claude-sonnet-5-5', 'low'))
        self.assertIn(f'{parking.id}: חניה בסניף', asked['user'])
        self.assertIn('1. הלקוח כתב: "אפשר לחנות שם?"', asked['user'])
        self.assertIn('מה חסר, לפי הבוט: מחיר תלבושת', asked['user'])
        first.refresh_from_db(), second.refresh_from_db(), third.refresh_from_db()
        self.assertEqual(first.topic_id, parking.id)
        self.assertEqual((second.topic.title, second.topic_id), ('מחיר תלבושת', third.topic_id))
        self.assertEqual(settings_store.get(unknowns.SORTED_KEY), {'on': '2026-10-12', 'at': self.clock().isoformat(), 'by': 'ai'})

        # What comes in later the same day waits for tomorrow — Claude is not asked twice a day.
        late = self.case('יש מזגן בסטודיו?')
        with patch(ASK) as ask:
            self.assertEqual(unknowns.sort_new(now=self.clock())['unknowns_sorted'], 0)
        ask.assert_not_called()
        self.clock.forward(days=1)
        with patch(ASK, return_value=({'cases': [{'n': 1, 'topic_id': 0, 'new_title': 'מיזוג בסטודיו'}]}, 'm')) as ask:
            unknowns.sort_new(now=self.clock())
        ask.assert_called_once()
        late.refresh_from_db()
        self.assertEqual(late.topic.title, 'מיזוג בסטודיו')

    @override_settings(**KEY)
    def test_an_answer_that_names_no_topic_leaves_the_case_for_tomorrow_and_a_title_is_cleaned(self):
        first = self.case('יש חניה?')
        second = self.case('מה עם החניה של דנה 0501234567?')
        answer = {'cases': [{'n': 1, 'topic_id': 999999, 'new_title': ''}, {'n': 2, 'topic_id': 0, 'new_title': ' חניה   0501234567. '}, {'n': 7, 'topic_id': 0, 'new_title': 'x'}]}
        with patch(ASK, return_value=(answer, 'm')):
            self.assertEqual(unknowns.sort_new(now=self.clock())['unknowns_sorted'], 1)
        first.refresh_from_db(), second.refresh_from_db()
        self.assertIsNone(first.topic)
        self.assertEqual(second.topic.title, 'חניה')

    @override_settings(**KEY)
    def test_when_claude_does_not_answer_the_words_do_and_it_is_asked_again_next_time(self):
        self.case('יש חניה ליד הסניף?')
        with patch(ASK, side_effect=refine.RefineUnavailable('Claude ענה 500')), self.assertLogs('apps.wahub.unknowns', level='WARNING'):
            counts = unknowns.sort_new(now=self.clock())
        self.assertEqual(counts, {'unknowns_sorted': 1, 'unknown_topics_opened': 1})
        self.assertEqual(settings_store.get(unknowns.SORTED_KEY)['by'], 'rules')
        self.case('מה הסיסמה של הוויפי?')
        with patch(ASK, return_value=({'cases': [{'n': 1, 'topic_id': 0, 'new_title': 'וויפי'}]}, 'm')) as ask:
            unknowns.sort_new(now=self.clock())
        ask.assert_called_once()

    @override_settings(**KEY)
    def test_claude_is_not_asked_with_no_time_left_in_the_call(self):
        import time

        self.case('יש חניה?')
        with patch(ASK) as ask:
            unknowns.sort_new(now=self.clock(), deadline=time.monotonic() + 2)
        ask.assert_not_called()
        self.assertEqual(BotUnknown.objects.filter(topic__isnull=True).count(), 1)

    def test_the_cron_files_them_and_says_how_many(self):
        self.case('יש חניה ליד הסניף?')
        counts = cron.tick()
        self.assertEqual((counts['unknowns_sorted'], counts['unknown_topics_opened']), (1, 1))


# --- the list --------------------------------------------------------------------------------------------

class ListTests(UnknownsCase):
    def setUp(self):
        super().setUp()
        self.parking = self.topic('חניה בסניף')
        self.clothes = self.topic('מחיר תלבושת')
        # This week (it began yesterday, Sunday): parking three times, clothes once. Last week: parking once, clothes twice.
        self.case('יש חניה ליד הסניף?', topic=self.parking, days_ago=1)
        self.case('איפה חונים?', topic=self.parking, answer='בודקת מול המשרד לגבי חניה')
        self.case('החניה בתשלום?', topic=self.parking, days_ago=0)
        self.case('כמה עולה חולצה?', topic=self.clothes, days_ago=1)
        self.case('יש חניה?', topic=self.parking, days_ago=5)
        self.case('כמה התלבושת?', topic=self.clothes, days_ago=3)
        self.case('מחיר חליפה?', topic=self.clothes, days_ago=6)
        self.case('יש מזגן?', days_ago=0)                                   # not filed under a topic yet

    def test_the_week_by_topic_the_most_asked_first(self):
        data = self.rows()
        self.assertEqual(data['week'], {'key': 'this', 'from': '2026-10-11', 'to': '2026-10-17', 'label': 'השבוע · 11.10–17.10'})
        self.assertEqual(data['counts'], {'topics': 2, 'questions': 5, 'unsorted': 1})
        parking, clothes = data['results']
        self.assertEqual(set(parking), {
            'id', 'title', 'status', 'status_label', 'count', 'count_before', 'trend', 'total', 'last_asked_at', 'examples', 'bot_said',
            'still_asked', 'taught', 'dismissed',
        })
        self.assertEqual((parking['id'], parking['title'], parking['status'], parking['status_label']), (self.parking.id, 'חניה בסניף', 'open', 'פתוח'))
        self.assertEqual((parking['count'], parking['count_before'], parking['trend'], parking['total']), (3, 1, 'up', 4))
        self.assertEqual((clothes['count'], clothes['count_before'], clothes['trend'], clothes['total']), (1, 2, 'down', 3))
        self.assertEqual([example['question'] for example in parking['examples']], ['החניה בתשלום?', 'איפה חונים?', 'יש חניה ליד הסניף?'])
        self.assertEqual(parking['bot_said'], 'את זה אני בודקת מול המשרד, ונחזור אליכם עם תשובה')
        self.assertEqual((parking['still_asked'], parking['taught'], parking['dismissed']), (False, None, None))
        # When the week has too few of its own, the examples go on with earlier ones.
        self.assertEqual([example['question'] for example in clothes['examples']], ['כמה עולה חולצה?', 'כמה התלבושת?', 'מחיר חליפה?'])
        self.assertEqual((data['unsorted']['count'], [example['question'] for example in data['unsorted']['examples']]), (1, ['יש מזגן?']))
        self.assertEqual(datetime.fromisoformat(data['unsorted']['examples'][0]['at']), self.clock())
        # No phone, no name and no id of a conversation anywhere in the answer.
        self.assertNotRegex(json.dumps(data, ensure_ascii=False), r'contact|shadow|9725|phone')

    def test_last_week(self):
        data = self.rows(week='last')
        self.assertEqual(data['week']['label'], 'שבוע שעבר · 4.10–10.10')
        clothes, parking = data['results']
        self.assertEqual((clothes['title'], clothes['count'], clothes['count_before'], clothes['trend']), ('מחיר תלבושת', 2, 0, 'new'))
        self.assertEqual((parking['count'], parking['trend']), (1, 'new'))
        self.assertEqual([example['question'] for example in clothes['examples']], ['כמה התלבושת?', 'מחיר חליפה?'])
        self.assertEqual(data['counts'], {'topics': 2, 'questions': 3, 'unsorted': 0})

    def test_a_week_with_nothing_and_what_is_refused(self):
        BotUnknown.objects.all().delete()
        data = self.rows()
        self.assertEqual((data['results'], data['counts'], data['unsorted']), ([], {'topics': 0, 'questions': 0, 'unsorted': 0}, {'count': 0, 'examples': []}))
        self.assertEqual(self.get('bot/unknowns/', week='month').status_code, 400)
        self.assertEqual(self.get('bot/unknowns/', status='x').status_code, 400)

    def test_the_small_number_beside_teach(self):
        self.assertEqual(self.get('review/summary/').data['unknowns'], {'topics': 2, 'questions': 5})

    def test_a_fixed_handful_of_queries_whatever_the_number_of_topics(self):
        with self.assertNumQueries(9):
            self.rows()
        for index in range(8):
            self.case(f'שאלה {index}', topic=self.topic(f'נושא {index}'))
        with self.assertNumQueries(9):
            self.rows()

    def test_managers_only(self):
        for role in (UserProfile.ROLE_WORKER, UserProfile.ROLE_PARTNER):
            other = client_for(make_user(f'{role}@unknowns.test', role=role))
            self.assertEqual(other.get('/api/v1/wahub/bot/unknowns/').status_code, 403)
            self.assertEqual(other.post(f'/api/v1/wahub/bot/unknowns/{self.parking.id}/dismiss/').status_code, 403)


# --- what the owner does to a topic ----------------------------------------------------------------------

class ActionsTests(UnknownsCase):
    def setUp(self):
        super().setUp()
        self.parking = self.topic('חניה בסניף')
        self.case('יש חניה ליד הסניף?', topic=self.parking)
        self.case('איפה חונים כשמביאים את [שם]?', topic=self.parking, days_ago=1)

    def test_not_relevant_takes_it_off_and_new_questions_do_not_bring_it_back(self):
        response = self.post(f'bot/unknowns/{self.parking.id}/dismiss/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['status'], response.data['status_label'], response.data['count']), ('dismissed', 'לא רלוונטי', 2))
        self.assertEqual(response.data['dismissed']['by_name'], 'דנה מנהלת')
        self.assertEqual(self.rows()['results'], [])
        self.case('החניה בתשלום?', topic=self.parking)
        self.assertEqual(self.rows()['results'], [])
        self.assertEqual([(row['title'], row['count']) for row in self.rows(status='dismissed')['results']], [('חניה בסניף', 3)])
        # ... and it can be put back.
        back = self.post(f'bot/unknowns/{self.parking.id}/restore/')
        self.assertEqual((back.data['status'], back.data['dismissed']), ('open', None))
        self.assertEqual([row['id'] for row in self.rows()['results']], [self.parking.id])
        self.assertEqual(self.post('bot/unknowns/999999/dismiss/').status_code, 404)

    def test_teaching_opens_a_teach_thread_with_the_context_ready(self):
        response = self.post('refine/', {'mode': 'teach', 'unknown_topic': self.parking.id})
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual((data['mode'], data['needs_answer'], data['question'], data['changes']), ('teach', True, 'מה התשובה הנכונה?', []))
        self.assertEqual(data['summary'], {
            'what_happened': 'לקוחות שאלו על חניה בסניף 2 פעמים השבוע, והבוט לא ידע לענות',
            'what_was_answered': MISSING, 'suspected_issue': 'המידע הזה חסר לבוט',
        })
        self.assertEqual(data['origin'], {
            'kind': 'unknown', 'label': 'מתוך מה שהבוט לא ידע: חניה בסניף', 'contact_id': None, 'contact_name': None,
            'link': '/wahub?tab=bot&sub=unknowns', 'topic_id': self.parking.id,
        })
        self.assertEqual([(quote['kind'], quote['who'], quote['text']) for quote in data['context']['quotes']], [
            ('unknown', 'customer', 'יש חניה ליד הסניף?'), ('unknown', 'customer', 'איפה חונים כשמביאים את [שם]?'),
        ])
        self.assertEqual((data['context']['contact_id'], data['context']['message_ids']), (None, []))
        # Nothing was changed, and the topic is still open: opening a thread teaches nothing.
        self.assertFalse(KnowledgeProposal.objects.exists())
        self.assertEqual(BotUnknownTopic.objects.get().status, 'open')
        # Until he writes in it, it is not something he taught.
        self.assertEqual(self.get('refine/', mode='teach').data['results'], [])

    @override_settings(**KEY)
    def test_his_answer_is_read_with_the_questions_and_approving_it_teaches_the_topic(self):
        thread = self.post('refine/', {'mode': 'teach', 'unknown_topic': self.parking.id}).data['thread']
        fact = change(action='create', kind='fact', title='חניה', body='יש חניה חינם ברחוב ליד כל הסניפים', headline='הבוט יידע לענות על חניה')
        asked = Claude(reading=taught(fact, understood='ליד כל הסניפים יש חניה חינם ברחוב', asks='יש חניה?'), answer=bot('יש חניה חינם ברחוב'), verdict=verdict())
        with patch(POST, side_effect=asked):
            said_ = self.post(f'refine/{thread}/say/', {'text': 'יש חניה חינם ברחוב ליד כל הסניפים'})
            self.assertEqual((said_.status_code, said_.data['needs_approval'], said_.data['origin']['kind']), (200, True, 'unknown'))
            # What Claude read: the customers' questions, and his answer as the answer to them.
            read = asked.asked['reading'][0]['messages'][0]['content']
            self.assertIn('שאלות שלקוחות שאלו והבוט לא ידע לענות עליהן', read)
            self.assertIn('לקוח שאל: "יש חניה ליד הסניף?"', read)
            self.assertIn('התשובה הנכונה לשאלות האלה', read)
            self.assertEqual(BotUnknownTopic.objects.get().status, 'open')          # not before his "מאשר"
            approved = self.post(f'refine/{thread}/approve/')
        self.assertEqual(approved.status_code, 200, approved.data)
        topic = BotUnknownTopic.objects.get()
        self.assertEqual((topic.status, topic.taught_thread_id, topic.taught_by), ('taught', thread, self.manager))
        self.assertTrue(KnowledgeItem.objects.filter(title='חניה', kind='fact', is_active=True).exists())
        # Off the list of the week; under "נלמדו" with who and when.
        self.assertEqual(self.rows()['results'], [])
        self.assertEqual(self.rows()['counts']['topics'], 0)
        (row,) = self.rows(status='taught')['results']
        self.assertEqual((row['status'], row['status_label'], row['still_asked'], row['taught']['thread'], row['taught']['by_name']),
                         ('taught', 'נלמד', False, thread, 'דנה מנהלת'))
        # It is in what he taught, saying where it came from.
        history = self.get('refine/', mode='teach').data['results']
        self.assertEqual([(line['thread'], line['origin']['kind'], line['origin']['label']) for line in history],
                         [(thread, 'unknown', 'מתוך מה שהבוט לא ידע: חניה בסניף')])

        # Asked again after it was taught: back on the list, saying so.
        self.clock.forward(hours=2)
        self.case('ומה עם חניה לנכים?', topic=topic)
        (row,) = self.rows()['results']
        self.assertEqual((row['status'], row['still_asked'], row['count']), ('taught', True, 3))

        # "בטל" on the thread takes the change back, and the topic is open again.
        with patch(POST, side_effect=asked):
            undone = self.post(f'refine/{thread}/undo/')
        self.assertEqual(undone.status_code, 200, undone.data)
        topic.refresh_from_db()
        self.assertEqual((topic.status, topic.taught_at, topic.taught_thread), ('open', None, None))

    def test_the_answer_may_come_with_the_request_and_without_a_key_it_waits_as_he_wrote_it(self):
        response = self.post('refine/', {'mode': 'teach', 'unknown_topic': self.parking.id, 'text': 'יש חניה חינם ברחוב'})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['text'], response.data['origin']['topic_id']), ('יש חניה חינם ברחוב', self.parking.id))
        thread = RefineThread.objects.get()
        self.assertEqual([turn.role for turn in thread.turns.all()][:2], ['system', 'operator'])
        self.assertEqual(self.get('refine/', mode='teach').data['results'][0]['origin']['kind'], 'unknown')

    def test_what_is_refused(self):
        self.assertEqual(self.post('refine/', {'mode': 'teach', 'unknown_topic': 999999}).status_code, 400)
        self.assertEqual(self.post('refine/', {'unknown_topic': self.parking.id}).status_code, 400)             # without the mode
        contact = self.customer()
        both = self.post('refine/', {'mode': 'teach', 'unknown_topic': self.parking.id, 'contact': contact.id, 'message_ids': [1]})
        self.assertEqual(both.status_code, 400)
        self.assertFalse(RefineThread.objects.exists())
