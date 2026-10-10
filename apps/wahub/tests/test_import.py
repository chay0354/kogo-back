"""The import of the old bot's texts: what it files, what it refuses, and that twice is once."""
from datetime import date

from apps.wahub import knowledge, knowledge_import, knowledge_seed
from apps.wahub.models import KnowledgeItem, QuickReply, Tag
from apps.wahub.tests.base import WahubTestCase

TODAY = date(2026, 10, 10)
PRICES = ('260', '275', '235', '335', '₪30', '₪120', '₪60', '₪100', '₪80', '₪130', '₪50', '₪180')


class ImportTests(WahubTestCase):
    def test_files_every_kind_and_twice_is_once(self):
        first = knowledge_import.run(today=TODAY)
        for kind in ('profile', 'style_rule', 'behavior_rule', 'phrasing', 'topic', 'fact', 'contact', 'link', 'alias', 'special_day', 'office_hours'):
            self.assertGreater(first['created'].get(kind, 0), 0, kind)
        self.assertEqual(first['created']['profile'], 1)
        self.assertEqual(first['created']['office_hours'], 1)
        self.assertGreaterEqual(first['created_total'], 90)
        total = KnowledgeItem.objects.count()
        self.assertEqual(total, first['created_total'])

        second = knowledge_import.run(today=TODAY)
        self.assertEqual((second['created_total'], second['skipped']), (0, first['created_total']))
        self.assertEqual(KnowledgeItem.objects.count(), total)
        self.assertEqual(Tag.objects.filter(name__in=['ברכת יום הולדת', 'הפעלת יום הולדת']).count(), 2)
        self.assertEqual(QuickReply.objects.get(title='ברכת יום הולדת').text.count('\n'), 2)
        self.assertNotIn('\\n', QuickReply.objects.get(title='ברכת יום הולדת').text)

    def test_no_price_or_discount_is_written_as_text(self):
        knowledge_import.run(today=TODAY)
        for item in KnowledgeItem.objects.filter(kind__in=['fact', 'phrasing', 'topic', 'behavior_rule']):
            for price in PRICES:
                self.assertNotIn(price, item.body, f'{item.kind} "{item.title}" carries a price: {price}')
        self.assertFalse(KnowledgeItem.objects.filter(title__icontains='הנחת אחים', kind='fact').exists())
        self.assertFalse(KnowledgeItem.objects.filter(body__icontains='פייטו').exists())
        self.assertFalse(KnowledgeItem.objects.filter(body__icontains='רישום מוקדם').exists())

    def test_the_owners_decisions_are_respected(self):
        knowledge_import.run(today=TODAY)
        hours = KnowledgeItem.objects.get(kind='office_hours')
        weekly = hours.data['weekly']
        self.assertEqual((weekly['sun']['from'], weekly['sun']['to'], weekly['thu']['to']), ('10:30', '18:00', '18:00'))
        self.assertFalse(weekly['fri']['open'])
        self.assertFalse(weekly['sat']['open'])
        self.assertIn('המשרד סגור כרגע', hours.data['default_closed_message'])
        self.assertEqual(hours.data['send_mode'], 'on_agent_request')

        cancel = KnowledgeItem.objects.get(kind='topic', title='ביטול חוג')
        self.assertIn('1.4', cancel.body)
        self.assertEqual(len(cancel.data['steps']), 3)
        self.assertTrue(KnowledgeItem.objects.filter(kind='phrasing', key='cancel_after_deadline', is_active=True).exists())

        group = KnowledgeItem.objects.get(kind='topic', title='קבוצת וואטסאפ של חוג')
        self.assertEqual(group.data['handoff_reason'], 'קבוצת וואטסאפ')
        self.assertFalse(KnowledgeItem.objects.filter(body__icontains='אין קבוצות').exists())

        self.assertEqual(KnowledgeItem.objects.get(kind='topic', title='ברכה אישית מקוגומלו').data['tag'], 'ברכת יום הולדת')
        self.assertEqual(KnowledgeItem.objects.get(kind='topic', title='הפעלת יום הולדת').data['tag'], 'הפעלת יום הולדת')
        gilad = KnowledgeItem.objects.get(kind='contact', title__startswith='גלעד')
        self.assertEqual(gilad.data['phone'], '050-722-3419')

        external = KnowledgeItem.objects.get(kind='behavior_rule', title__startswith='סניף חיצוני')
        self.assertIn('העירייה', external.body)
        self.assertTrue(KnowledgeItem.objects.filter(kind='fact', title='כתובת המשרד', body__icontains='רפאל איתן 5').exists())

        # Open decisions wait, inactive, with the decision in the note.
        for key in ('campaign_decline', 'registration_open'):
            item = KnowledgeItem.objects.get(kind='phrasing', key=key)
            self.assertFalse(item.is_active, key)
            self.assertIn('פתוחה', item.source_note)

    def test_special_days_past_ones_rest_and_kfar_ganim_has_its_own(self):
        knowledge_import.run(today=TODAY)
        days = KnowledgeItem.objects.filter(kind='special_day')
        self.assertEqual(days.count(), 28)
        rosh = days.get(title='ראש השנה', scope_level='business')
        self.assertFalse(rosh.is_active)
        self.assertEqual((rosh.data['date_from'], rosh.data['date_to']), ('2026-09-11', '2026-09-13'))
        pesach = days.get(title='פסח', scope_level='business')
        self.assertTrue(pesach.is_active)
        self.assertEqual((pesach.data['date_from'], pesach.data['state']), ('2027-04-21', 'closed'))
        shoah = days.get(title='ערב יום השואה ויום השואה')
        self.assertEqual((shoah.data['state'], shoah.data['hours_to']), ('hours', '18:15'))
        quiet = days.get(title='יום הזיכרון לחללי צה"ל')
        self.assertEqual((quiet.scope_level, quiet.scope_label, quiet.data['state'], quiet.data['hours_to']), ('branch', 'כפר גנים / מרכז זמיר', 'quiet', '13:00'))
        self.assertEqual(days.filter(scope_level='branch').count(), 16)
        self.assertEqual(days.get(title='חנוכה').data['message'], '🔹 *06.12* חנוכה - סטודיו פתוח כרגיל')

    def test_the_phrasings_are_word_for_word(self):
        docs = knowledge_import._docs_text(None)
        if not docs:
            self.skipTest('docs/bot-knowledge is not beside this checkout')
        result = knowledge_import.run(today=TODAY)
        self.assertGreater(result['verbatim_checked'], 20)
        self.assertEqual(result['verbatim_mismatch'], [])

    def test_a_dry_run_creates_nothing(self):
        result = knowledge_import.run(today=TODAY, dry_run=True)
        self.assertGreater(result['created_total'], 0)
        self.assertEqual(KnowledgeItem.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)

    def test_every_dropped_row_is_reported(self):
        self.assertGreaterEqual(len(knowledge_seed.DROPPED), 20)
        self.assertTrue(all(len(row) == 3 for row in knowledge_seed.DROPPED))


class ImportFromTheScreenTests(WahubTestCase):
    def test_the_button_files_everything_once_and_a_second_click_adds_nothing(self):
        response = self.post('knowledge/import/', {})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertGreaterEqual(response.data['created_total'], 90)
        self.assertEqual(response.data['total'], KnowledgeItem.objects.count())
        self.assertFalse(response.data['dry_run'])

        again = self.post('knowledge/import/', {})
        self.assertEqual((again.data['created_total'], again.data['skipped']), (0, response.data['created_total']))

    def test_a_dry_run_from_the_screen_creates_nothing(self):
        response = self.post('knowledge/import/', {'dry_run': True})
        self.assertTrue(response.data['dry_run'])
        self.assertGreaterEqual(response.data['created_total'], 90)
        self.assertEqual(KnowledgeItem.objects.count(), 0)
