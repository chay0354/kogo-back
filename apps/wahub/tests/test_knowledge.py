"""The bot's knowledge: items with history and restore, what comes from Kogo, the office hours."""
from datetime import date, datetime
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.wahub import knowledge
from apps.wahub.models import KnowledgeHistory, KnowledgeItem
from apps.wahub.tests.base import WahubTestCase, client_for, make_user

ISRAEL = timezone.get_default_timezone()


def israel(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute), ISRAEL)


class KnowledgeItemApiTests(WahubTestCase):
    def test_create_edit_history_and_restore(self):
        response = self.post('knowledge/', {'kind': 'fact', 'title': 'אין ילדים מתחת לגיל 3', 'body': 'מבחינת ביטוח', 'when_to_say': 'if_asked'})
        self.assertEqual(response.status_code, 201, response.data)
        item = response.data
        self.assertEqual((item['kind_label'], item['when_to_say_label'], item['version']), ('עובדה', 'רק אם שואלים', 1))
        self.assertEqual(item['scope'], {'level': 'business', 'id': None, 'label': 'כל העסק'})
        self.assertEqual(item['updated_by_name'], 'דנה מנהלת')

        response = self.client.patch(f'/api/v1/wahub/knowledge/{item["id"]}/', {'body': 'מבחינת ביטוח, חד משמעית', 'note': 'דיוק'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['version'], 2)

        history = self.get(f'knowledge/{item["id"]}/history/').data
        self.assertEqual([row['version'] for row in history], [2, 1])
        self.assertEqual(history[0]['before']['body'], 'מבחינת ביטוח')
        self.assertEqual(history[0]['after']['body'], 'מבחינת ביטוח, חד משמעית')
        self.assertEqual((history[0]['note'], history[0]['changed_by_name']), ('דיוק', 'דנה מנהלת'))
        self.assertIsNone(history[1]['before'])

        response = self.post(f'knowledge/{item["id"]}/restore/', {'version': 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['body'], response.data['version']), ('מבחינת ביטוח', 3))
        self.assertEqual(KnowledgeHistory.objects.filter(item_id=item['id']).count(), 3)
        self.assertEqual(KnowledgeHistory.objects.get(item_id=item['id'], version=3).note, 'שחזור לגרסה 1')

    def test_an_unchanged_save_makes_no_version(self):
        item = knowledge.create_item({'kind': 'fact', 'title': 'עונה', 'body': 'מ-01.09 עד 28.07'})
        self.client.patch(f'/api/v1/wahub/knowledge/{item.id}/', {'body': 'מ-01.09 עד 28.07'}, format='json')
        item.refresh_from_db()
        self.assertEqual((item.version, KnowledgeHistory.objects.filter(item=item).count()), (1, 1))

    def test_delete_is_soft_and_the_list_filters(self):
        item = knowledge.create_item({'kind': 'fact', 'title': 'ישן', 'body': 'טקסט'})
        response = self.client.delete(f'/api/v1/wahub/knowledge/{item.id}/')
        self.assertEqual(response.status_code, 204)
        item.refresh_from_db()
        self.assertFalse(item.is_active)
        self.assertEqual(item.version, 2)
        self.assertEqual([row['id'] for row in self.get('knowledge/', active='1').data], [])
        self.assertEqual([row['id'] for row in self.get('knowledge/', active='0').data], [item.id])
        self.assertEqual(len(self.get('knowledge/', kind='fact', search='ישן').data), 1)

    def test_kind_specific_fields_are_flattened_and_checked(self):
        link = self.post('knowledge/', {'kind': 'link', 'title': 'האתר', 'key': 'website', 'url': 'cogomelo.co.il'})
        self.assertEqual(link.status_code, 400)
        self.assertIn('https://', link.data['detail'])
        link = self.post('knowledge/', {'kind': 'link', 'title': 'האתר', 'key': 'website', 'url': 'https://cogomelo.co.il/', 'when': 'כששואלים'})
        self.assertEqual(link.status_code, 201, link.data)
        self.assertEqual((link.data['url'], link.data['when']), ('https://cogomelo.co.il/', 'כששואלים'))

        self.assertEqual(self.post('knowledge/', {'kind': 'phrasing', 'title': 'פתיחה', 'body': 'היי'}).status_code, 400)
        first = self.post('knowledge/', {'kind': 'phrasing', 'title': 'פתיחה', 'key': 'greeting', 'body': 'היי {שם}'})
        self.assertEqual(first.status_code, 201)
        twice = self.post('knowledge/', {'kind': 'phrasing', 'title': 'פתיחה 2', 'key': 'greeting', 'body': 'שלום'})
        self.assertEqual(twice.status_code, 400)
        self.assertIn('greeting', twice.data['detail'])

        self.assertEqual(self.post('knowledge/', {'kind': 'special_day', 'title': 'פסח', 'state': 'closed'}).status_code, 400)
        day = self.post('knowledge/', {'kind': 'special_day', 'title': 'פסח', 'date_from': '2027-04-21', 'date_to': '2027-04-28', 'state': 'closed', 'message': 'סגור'})
        self.assertEqual(day.status_code, 201, day.data)
        self.assertEqual((day.data['state_label'], day.data['date_to']), ('סגור', '2027-04-28'))

        alias = self.post('knowledge/', {'kind': 'alias', 'title': 'זמיר', 'what_customer_writes': 'מרכז זמיר', 'means': 'כפר גנים'})
        self.assertEqual(alias.status_code, 201)
        self.assertEqual(alias.data['means'], 'כפר גנים')

    def test_one_profile_and_one_office_hours_row(self):
        self.assertEqual(self.post('knowledge/', {'kind': 'profile', 'title': 'דנה', 'body': 'נציגה'}).status_code, 201)
        second = self.post('knowledge/', {'kind': 'profile', 'title': 'דני', 'body': 'נציג'})
        self.assertEqual(second.status_code, 400)
        self.assertIn('כבר', second.data['detail'])
        bad_hours = self.post('knowledge/', {'kind': 'office_hours', 'title': 'שעות', 'weekly': {'sun': {'open': True, 'from': '10', 'to': '18:00'}}})
        self.assertEqual(bad_hours.status_code, 400)

    def test_a_scoped_item_takes_its_label_from_kogo(self):
        branch = TestDataFactory.create_branch(name='דמרי סנטר', city=TestDataFactory.create_city('כפר סבא'))
        response = self.post('knowledge/', {'kind': 'fact', 'title': 'חניה', 'body': 'בקומה -1', 'scope_level': 'branch', 'scope_id': str(branch.id)})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['scope'], {'level': 'branch', 'id': str(branch.id), 'label': 'דמרי סנטר'})

    def test_only_a_manager(self):
        worker = client_for(make_user('worker@wahub.test', role='worker'))
        self.assertEqual(worker.get('/api/v1/wahub/knowledge/').status_code, 403)
        self.assertEqual(worker.get('/api/v1/wahub/knowledge/from-kogo/').status_code, 403)
        self.assertEqual(worker.post('/api/v1/wahub/shadow/try/', {'question': 'היי'}, format='json').status_code, 403)
        self.assertEqual(worker.get('/api/v1/wahub/review/proposals/').status_code, 403)


@override_settings(REGISTRATION_FEE_ILS=120)
class FromKogoTests(WahubTestCase):
    def test_what_the_bot_reads_and_what_is_still_empty(self):
        city = TestDataFactory.create_city('ראש העין')
        ours = TestDataFactory.create_branch(name='פסגות אפק', city=city, address='', phone='')
        TestDataFactory.create_branch(name='רמת גן - גאולים', city=TestDataFactory.create_city('רמת גן'), is_external=True, external_link='')
        kind = TestDataFactory.create_course_type('קפוארה')
        TestDataFactory.create_course(name='קפוארה צעירים', branch=ours, course_type=kind, price=Decimal('0'), trial_lesson_is_paid=True)

        data = self.get('knowledge/from-kogo/').data
        by_name = {row['name']: row for row in data['branches']}
        self.assertEqual(by_name['פסגות אפק']['missing'], ['address', 'phone', 'directions'])
        self.assertEqual(by_name['רמת גן - גאולים']['missing'], ['external_link'])
        self.assertTrue(by_name['רמת גן - גאולים']['is_external'])
        self.assertEqual(data['course_types'][0]['missing'], ['trial_bring_note'])
        course = data['pricing_summary']['courses'][0]
        self.assertEqual(course['missing'], ['price', 'trial_lesson_price'])
        self.assertEqual(data['pricing_summary']['courses_without_price'], 1)
        self.assertEqual(data['registration_fee'], 120)
        self.assertIn('blocked_dates', data)
        self.assertIn('discounts', data)


class OfficeHoursTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        weekday = {'open': True, 'from': '10:30', 'to': '18:00', 'message': ''}
        weekend = {'open': False, 'from': None, 'to': None, 'message': 'נחזור ביום ראשון'}
        self.hours = knowledge.create_item({
            'kind': 'office_hours', 'title': 'שעות',
            'weekly': {'sun': weekday, 'mon': weekday, 'tue': weekday, 'wed': weekday, 'thu': weekday, 'fri': weekend, 'sat': weekend},
            'default_closed_message': 'המשרד סגור כרגע ✨', 'send_mode': 'on_agent_request',
        })

    def test_open_and_closed_by_israels_clock(self):
        # 11.10.2026 is a Sunday.
        now = knowledge.office_hours_now(israel(2026, 10, 11, 11))
        self.assertTrue(now['open'])
        self.assertEqual((now['today']['day'], now['today']['from'], now['today']['to']), ('sun', '10:30', '18:00'))
        self.assertIsNone(now['message_if_closed'])
        late = knowledge.office_hours_now(israel(2026, 10, 11, 18, 30))
        self.assertFalse(late['open'])
        self.assertEqual(late['message_if_closed'], 'המשרד סגור כרגע ✨')
        friday = knowledge.office_hours_now(israel(2026, 10, 16, 11))
        self.assertFalse(friday['open'])
        self.assertEqual(friday['message_if_closed'], 'נחזור ביום ראשון')
        self.assertEqual(friday['send_mode_label'], 'רק כשמבקשים נציג')

    def test_a_special_day_wins_over_the_weekly_line(self):
        knowledge.create_item({'kind': 'special_day', 'title': 'יום כיפור', 'date_from': '2026-10-12', 'state': 'closed', 'message': 'גמר חתימה טובה, סגור היום'})
        knowledge.create_item({'kind': 'special_day', 'title': 'ערב חג', 'date_from': '2026-10-13', 'state': 'hours', 'hours_from': '10:30', 'hours_to': '13:00'})
        closed = knowledge.office_hours_now(israel(2026, 10, 12, 11))
        self.assertFalse(closed['open'])
        self.assertEqual(closed['message_if_closed'], 'גמר חתימה טובה, סגור היום')
        self.assertEqual(closed['special']['title'], 'יום כיפור')
        short = knowledge.office_hours_now(israel(2026, 10, 13, 12))
        self.assertTrue(short['open'])
        self.assertFalse(knowledge.office_hours_now(israel(2026, 10, 13, 14))['open'])
        self.assertEqual(short['today']['to'], '13:00')

    def test_the_endpoint_and_an_unconfigured_office(self):
        response = self.get('knowledge/office-hours/now/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('open', response.data)
        knowledge.soft_delete(self.hours)
        self.assertFalse(knowledge.office_hours_now()['configured'])

    def test_relevant_items_follow_the_scope_and_the_dates(self):
        branch = TestDataFactory.create_branch(name='מינץ')
        other = TestDataFactory.create_branch(name='דמרי')
        everybody = knowledge.create_item({'kind': 'fact', 'title': 'כללי', 'body': 'לכולם'})
        mine = knowledge.create_item({'kind': 'fact', 'title': 'מינץ', 'body': 'רק שם', 'scope_level': 'branch', 'scope_id': str(branch.id)})
        expired = knowledge.create_item({'kind': 'fact', 'title': 'פג', 'body': 'עבר', 'valid_until': '2026-01-01'})
        today = date(2026, 10, 11)
        ids = {item.id for item in knowledge.relevant_items(today=today, branch_id=branch.id)}
        self.assertIn(everybody.id, ids)
        self.assertIn(mine.id, ids)
        self.assertNotIn(expired.id, ids)
        self.assertNotIn(mine.id, {item.id for item in knowledge.relevant_items(today=today, branch_id=other.id)})
        # With no branch known yet, a branch-scoped fact is still told (the bot reads its label).
        self.assertIn(mine.id, {item.id for item in knowledge.relevant_items(today=today)})
