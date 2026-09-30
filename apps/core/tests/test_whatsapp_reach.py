"""
Every current customer reachable on WhatsApp, and the office told when one is not.

30.9.2026: after the ManyChat import fixed the parents a broadcast could not
find, the owner asked for two things. That every customer — and every new one
— gets what he sends. And that when one does not, he hears about it. Until then
a failed send was a line in the server log, and a message that went out as
free text was marked sent though WhatsApp delivers it only inside the 24-hour
window.

These pin:
- ManyChatService.reach: finds or creates, sends nothing, writes nothing to a
  contact it finds, and names what it cannot fix.
- The morning check over every recipient: slices, resumes, lists, alerts once.
- An alert for every WhatsApp that did not go out — one per phone a day, the
  office's phone spared a flood — and for free text.
"""
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.core import daily_brief
from apps.core.daily_brief import RED, YELLOW, GREEN, check_manychat_health, check_whatsapp_reachability
from apps.core.daily_brief_views import merge_into_today
from apps.core.manychat_service import (
    ManyChatContactUnfindable,
    ManyChatError,
    ManyChatService,
)
from apps.core.models import Branch, ManyChatContact, OfficeAlert
from apps.core.office_alerts import HELD_NOTE
from apps.core.whatsapp_alerts import (
    KIND_FAILED,
    KIND_FAILED_MANY,
    KIND_FREE_TEXT,
    KIND_UNREACHABLE,
    MAX_DELIVERED_PER_DAY,
    alert_send_failure,
)
from apps.customers.models import Child, Family, Parent

TODAY = date(2026, 9, 30)
ALREADY_EXISTS = ManyChatError(
    'Validation error', status_code=400,
    payload={'details': {'messages': {'wa_id': {'message': ['This WhatsApp ID already exists: 972545757056']}}}},
)
NOT_ON_WHATSAPP = ManyChatError(
    'Validation error', status_code=400,
    payload={'details': {'messages': {'wa_id': {'message': ['972545757056 is not a valid WhatsApp ID']}}}},
)


class NoNetworkMixin:
    """Nothing here may reach ManyChat."""

    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.manychat_service.requests.request', side_effect=AssertionError('network call'))
        patcher.start()
        self.addCleanup(patcher.stop)


def make_family(branch, phone, first='הורה', last='x', status='active', extra=None):
    family = Family.objects.create(name=last, phone='', branch=branch)
    Parent.objects.create(family=family, first_name=first, last_name=last, phone=phone, is_primary=True)
    if extra:
        Parent.objects.create(family=family, first_name='נוסף', last_name=last, phone=extra, is_primary=False)
    child = Child.objects.create(
        family=family, first_name='ילד', last_name=last, birth_date=date(2016, 1, 1), gender='male', status=status,
    )
    return family, child


class ReachTests(NoNetworkMixin, TestCase):
    def service(self):
        svc = ManyChatService(api_key='test-key')
        # A check must not become a bulk edit of the contacts it finds.
        svc._ensure_phone_indexed = MagicMock(side_effect=AssertionError('must not write to a found contact'))
        svc.find_by_custom_phone_field = MagicMock(return_value=[])
        svc._find_by_whatsapp_phone = MagicMock(return_value=[])
        svc.find_by_phone = MagicMock(return_value=[])
        svc.find_by_name_for_phone = MagicMock(return_value=[])
        return svc

    def test_a_remembered_contact_needs_no_search(self):
        ManyChatContact.objects.create(phone='972545757056', subscriber_id=11, source=ManyChatContact.SOURCE_FOUND)
        svc = self.service()
        svc.get_subscriber = MagicMock(return_value={'id': 11, 'whatsapp_phone': '972545757056'})
        self.assertEqual(svc.reach('0545757056'), {'state': 'remembered', 'subscriber_id': 11})
        svc.find_by_custom_phone_field.assert_not_called()

    def test_found_through_the_phone_field_first_and_remembered(self):
        svc = self.service()
        svc.find_by_custom_phone_field.return_value = [{'id': 22, 'whatsapp_phone': '972545757056'}]
        svc._find_by_whatsapp_phone.side_effect = AssertionError('the field answers first')
        self.assertEqual(svc.reach('0545757056', 'נעמה', 'שלמה'), {'state': 'found', 'subscriber_id': 22})
        row = ManyChatContact.objects.get(phone='972545757056')
        self.assertEqual((row.subscriber_id, row.source), (22, ManyChatContact.SOURCE_FOUND))

    def test_the_name_is_the_last_search(self):
        svc = self.service()
        svc.find_by_name_for_phone.return_value = [{'id': 23, 'whatsapp_phone': '972545757056'}]
        self.assertEqual(svc.reach('0545757056', 'נעמה', 'שלמה')['state'], 'found')
        svc.find_by_name_for_phone.assert_called_once_with('0545757056', 'נעמה שלמה')

    def test_a_number_manychat_does_not_have_is_created_with_the_parent_s_name(self):
        svc = self.service()
        svc.create_whatsapp_subscriber = MagicMock(return_value={'id': 33})
        self.assertEqual(svc.reach('0545757056', 'נעמה', 'שלמה'), {'state': 'created', 'subscriber_id': 33})
        svc.create_whatsapp_subscriber.assert_called_once_with('0545757056', 'נעמה', 'שלמה')
        self.assertEqual(ManyChatContact.objects.get(phone='972545757056').source, ManyChatContact.SOURCE_CREATED)

    def test_exists_but_cannot_be_found_is_named_and_not_remembered(self):
        svc = self.service()
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ALREADY_EXISTS)
        self.assertEqual(svc.reach('0545757056')['state'], 'unfindable')
        self.assertFalse(ManyChatContact.objects.exists())

    def test_a_number_not_on_whatsapp_is_named(self):
        svc = self.service()
        svc.create_whatsapp_subscriber = MagicMock(side_effect=NOT_ON_WHATSAPP)
        self.assertEqual(svc.reach('0545757056')['state'], 'not_on_whatsapp')

    def test_could_not_check_is_not_unreachable(self):
        svc = self.service()
        svc.create_whatsapp_subscriber = MagicMock(side_effect=ManyChatError('Too many requests', status_code=429))
        with self.assertRaises(ManyChatError):
            svc.reach('0545757056')


@override_settings(MANYCHAT_KEY='test-key')
class FailureAlertTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.office_alerts.deliver_office_alert')
        self.deliver = patcher.start()
        self.addCleanup(patcher.stop)

    def fail(self, phone, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            alert_send_failure(phone=phone, where='תפוצה מדף הלקוחות', **kwargs)

    def test_a_failed_send_tells_the_office_who_what_why_and_what_to_do(self):
        self.fail('0545757056', parent_name='נעמה שלמה', child_name='אור שלמה',
                  reason='contact_unfindable', error='איש הקשר: ...')
        alert = OfficeAlert.objects.get(kind=KIND_FAILED)
        self.assertEqual(alert.title, 'וואטסאפ לא יצא ל-נעמה שלמה')
        self.assertEqual(alert.where, 'תפוצה מדף הלקוחות')
        self.assertIn('972545757056', alert.customer)
        self.assertIn('אור שלמה', alert.customer)
        self.assertIn('חסר לו השדה', alert.why)
        self.assertIn('לייבא אותו ב-ManyChat', alert.action)
        self.deliver.assert_called_once()

    def test_one_alert_per_phone_a_day(self):
        self.fail('0545757056', reason='lookup_failed')
        self.fail('054-575-7056', reason='send_flow_failed')
        self.assertEqual(OfficeAlert.objects.filter(kind=KIND_FAILED).count(), 1)

    def test_a_flood_reaches_the_office_phone_as_one_message(self):
        for n in range(MAX_DELIVERED_PER_DAY + 3):
            self.fail(f'05450000{n:02d}', reason='send_flow_failed')
        failed = OfficeAlert.objects.filter(kind=KIND_FAILED)
        self.assertEqual(failed.count(), MAX_DELIVERED_PER_DAY + 3)
        self.assertEqual(failed.filter(error=HELD_NOTE).count(), 3)
        self.assertEqual(OfficeAlert.objects.filter(kind=KIND_FAILED_MANY).count(), 1)
        # The first few, then the one saying more failed — nothing else.
        self.assertEqual(self.deliver.call_count, MAX_DELIVERED_PER_DAY + 1)

    @override_settings(MANYCHAT_KEY='')
    def test_no_manychat_at_all_is_not_one_alert_per_parent(self):
        self.fail('0545757056', reason='lookup_failed')
        self.assertFalse(OfficeAlert.objects.exists())

    def test_an_alert_that_cannot_be_written_never_breaks_the_send(self):
        with patch('apps.core.office_alerts.raise_office_alert', side_effect=RuntimeError('db down')):
            alert_send_failure(phone='0545757056', where='x')  # no exception


@override_settings(MANYCHAT_KEY='test-key')
class NotifyRegistrationAlertTests(NoNetworkMixin, TestCase):
    ARGS = dict(
        phone='0545757056', parent_name='נעמה שלמה', child_name='אור', course_name='קפוארה',
        day_name='רביעי', start_time='17:00', end_time='17:45', branch_name='תל אביב',
    )

    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.office_alerts.deliver_office_alert')
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        svc = ManyChatService(api_key='test-key')
        svc._ensure_phone_indexed = MagicMock()
        return svc

    def test_a_parent_manychat_will_not_find_raises_an_alert_naming_the_message(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(side_effect=ManyChatContactUnfindable('x'))
        with self.captureOnCommitCallbacks(execute=True):
            out = svc.notify_registration(kind='trial', **self.ARGS)
        self.assertEqual(out['reason'], 'contact_unfindable')
        alert = OfficeAlert.objects.get(kind=KIND_FAILED)
        self.assertEqual(alert.where, 'הודעת "רישום לשיעור ניסיון" ללקוח')
        self.assertEqual(alert.details['reason'], 'contact_unfindable')

    def test_a_refused_automation_raises_an_alert(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 5})
        svc.get_subscriber = MagicMock(return_value={'whatsapp_phone': '972545757056'})
        svc._set_custom_fields_with_retry = MagicMock(return_value=True)
        svc.resolve_flow_for = MagicMock(return_value='content123')
        svc.send_flow = MagicMock(side_effect=ManyChatError('Flow not found'))
        with patch('apps.core.manychat_service.FIELD_SETTLE_SECONDS', 0), self.captureOnCommitCallbacks(execute=True):
            out = svc.notify_registration(kind='payment_failed', **self.ARGS)
        self.assertEqual(out['reason'], 'send_flow_failed')
        self.assertEqual(OfficeAlert.objects.get(kind=KIND_FAILED).details['reason'], 'send_flow_failed')

    def test_a_message_sent_as_free_text_is_reported_once_a_day(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 5})
        svc.get_subscriber = MagicMock(return_value={})
        svc._set_custom_fields_with_retry = MagicMock(return_value=True)
        svc.resolve_flow_for = MagicMock(return_value='')
        svc.send_whatsapp_text = MagicMock(return_value={})
        with self.captureOnCommitCallbacks(execute=True):
            first = svc.notify_registration(kind='subscription', **self.ARGS)
            svc.notify_registration(kind='subscription', **self.ARGS)
        self.assertEqual((first['sent'], first['method']), (True, 'text'))
        alert = OfficeAlert.objects.get(kind=KIND_FREE_TEXT)
        self.assertIn('הרשמה למנוי', alert.title)
        self.assertIn('24 השעות', alert.why)
        self.assertIn('MANYCHAT_REGISTRATION_FLOW_NS', alert.action)

    def test_a_message_that_went_out_raises_nothing(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 5})
        svc.get_subscriber = MagicMock(return_value={})
        svc._set_custom_fields_with_retry = MagicMock(return_value=True)
        svc.resolve_flow_for = MagicMock(return_value='content123')
        svc.send_flow = MagicMock(return_value={})
        with patch('apps.core.manychat_service.FIELD_SETTLE_SECONDS', 0), self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(svc.notify_registration(kind='trial', **self.ARGS)['sent'])
        self.assertFalse(OfficeAlert.objects.exists())

    def test_a_test_send_does_not_alert(self):
        svc = self.service()
        svc.lookup_or_create = MagicMock(side_effect=ManyChatContactUnfindable('x'))
        with self.captureOnCommitCallbacks(execute=True):
            svc.notify_registration(kind='trial', alert_office=False, **self.ARGS)
        self.assertFalse(OfficeAlert.objects.exists())


@override_settings(MANYCHAT_KEY='test-key')
class BroadcastAlertTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.office_alerts.deliver_office_alert')
        patcher.start()
        self.addCleanup(patcher.stop)
        _, self.child = make_family(Branch.objects.create(name='Main'), '0545757056', 'נעמה', 'שלמה')

    def broadcast(self, svc):
        from apps.customers.broadcast import broadcast_to_children

        with self.captureOnCommitCallbacks(execute=True):
            return broadcast_to_children(
                [self.child], automation_type='flow', automation_id='content1', dry_run=False, service=svc,
            )

    def test_a_raised_failure_of_an_automation_sent_by_name_alerts(self):
        svc = ManyChatService(api_key='test-key')
        svc.send_automation_to_contact = MagicMock(side_effect=ManyChatContactUnfindable('x'))
        result = self.broadcast(svc)
        self.assertEqual(result['failed'], 1)
        alert = OfficeAlert.objects.get(kind=KIND_FAILED)
        self.assertEqual(alert.where, 'תפוצה מדף הלקוחות')
        self.assertIn('נעמה שלמה', alert.customer)

    def test_a_returned_failure_alerts_too(self):
        svc = ManyChatService(api_key='test-key')
        svc.send_automation_to_contact = MagicMock(return_value={'sent': False, 'reason': 'no_subscriber_id'})
        self.broadcast(svc)
        self.assertEqual(OfficeAlert.objects.get(kind=KIND_FAILED).details['reason'], 'no_subscriber_id')

    def test_a_preview_alerts_nothing(self):
        from apps.customers.broadcast import broadcast_to_children

        svc = ManyChatService(api_key='test-key')
        svc.send_automation_to_contact = MagicMock(side_effect=AssertionError('a preview sends nothing'))
        with self.captureOnCommitCallbacks(execute=True):
            broadcast_to_children([self.child], automation_type='flow', automation_id='content1', service=svc)
        self.assertFalse(OfficeAlert.objects.exists())


@override_settings(MANYCHAT_KEY='test-key')
class ReachabilityCheckTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch('apps.core.office_alerts.deliver_office_alert')
        patcher.start()
        self.addCleanup(patcher.stop)
        branch = Branch.objects.create(name='Main')
        _, self.known = make_family(branch, '0541000001', 'ידוע')
        _, self.lost = make_family(branch, '0541000002', 'אבוד', extra='0521000009')
        _, self.new = make_family(branch, '0541000003', 'חדש')
        make_family(branch, '0541000004', 'עזב', status='inactive')
        ManyChatContact.objects.create(phone='972541000001', subscriber_id=1, source=ManyChatContact.SOURCE_FOUND)

    def run_check(self, states):
        calls = []

        def reach(svc, phone, first_name='', last_name=''):
            calls.append(phone)
            state = states[phone]
            if isinstance(state, Exception):
                raise state
            return {'state': state, 'subscriber_id': None}

        with patch.object(ManyChatService, 'reach', autospec=True, side_effect=reach), \
                self.captureOnCommitCallbacks(execute=True):
            item = check_whatsapp_reachability(TODAY)
        return item, calls

    def test_every_current_phone_is_made_sure_of_and_what_cannot_be_fixed_is_listed(self):
        item, calls = self.run_check({
            '972541000002': 'unfindable', '972521000009': 'not_on_whatsapp', '972541000003': 'created',
        })
        # The remembered one costs nothing; a former student is not checked.
        self.assertEqual(sorted(calls), ['972521000009', '972541000002', '972541000003'])
        self.assertEqual(item.severity, RED)
        self.assertEqual(item.count, 2)
        self.assertFalse(item.continues)
        self.assertIn('4 טלפונים', item.summary)
        self.assertIn('2 מהם יקבלו', item.summary)
        self.assertIn('1 נוספו עכשיו', item.summary)
        labels = {row['label']: row for row in item.rows}
        self.assertIn('אבוד x · ילד x', labels)
        self.assertEqual(labels['אבוד x · ילד x']['href'], f'/customers?child={self.lost.id}')
        self.assertIn('הורה נוסף', labels['נוסף x · ילד x']['detail'])
        self.assertIn('לא רשום בוואטסאפ', labels['נוסף x · ילד x']['detail'])

    def test_the_office_gets_one_alert_a_day_for_them(self):
        states = {'972541000002': 'unfindable', '972521000009': 'created', '972541000003': 'created'}
        self.run_check(states)
        self.run_check(states)
        alert = OfficeAlert.objects.get(kind=KIND_UNREACHABLE)
        self.assertEqual(alert.title, '1 לקוחות לא יקבלו הודעות וואטסאפ')
        self.assertIn('אבוד x', alert.customer)

    def test_all_reachable_is_green_and_alerts_nothing(self):
        item, _ = self.run_check({'972541000002': 'found', '972521000009': 'found', '972541000003': 'created'})
        self.assertEqual(item.severity, GREEN)
        self.assertEqual(item.count, 0)
        self.assertFalse(OfficeAlert.objects.exists())

    def test_could_not_check_is_yellow_and_alerts_nothing(self):
        item, _ = self.run_check({
            '972541000002': ManyChatError('Too many requests'), '972521000009': 'found', '972541000003': 'found',
        })
        self.assertEqual(item.severity, YELLOW)
        self.assertIn('ManyChat לא ענה', item.rows[0]['detail'])
        self.assertFalse(OfficeAlert.objects.exists())

    def test_a_long_first_pass_goes_on_from_where_the_last_slice_stopped(self):
        states = {'972541000002': 'unfindable', '972521000009': 'found', '972541000003': 'created'}
        ticks = iter([0, 0, 0, 100, 100, 100, 100])  # the budget runs out after the first phone
        with patch.object(daily_brief, 'REACH_SLICE_SECONDS', 50), \
                patch('time.monotonic', side_effect=lambda: next(ticks, 1000)):
            first, calls = self.run_check(states)
        self.assertTrue(first.continues)
        self.assertEqual(first.severity, YELLOW)
        self.assertEqual(calls, ['972521000009'])
        # The extra parent's phone (checked) and a remembered one (free) before the budget ran out.
        self.assertIn('2 מתוך 4', first.summary)

        with patch.object(daily_brief, '_israel_today', return_value=TODAY):
            merge_into_today({**first.as_dict()})
        with patch('apps.core.daily_brief._progress_so_far', return_value=first.progress):
            second, calls = self.run_check(states)
        self.assertFalse(second.continues)
        self.assertNotIn('972521000009', calls)
        self.assertEqual(second.count, 1)
        self.assertIn('4 טלפונים', second.summary)

    @override_settings(MANYCHAT_KEY='')
    def test_no_manychat_is_red(self):
        item = check_whatsapp_reachability(TODAY)
        self.assertEqual(item.severity, RED)


@override_settings(MANYCHAT_KEY='test-key')
class HealthFreeTextTests(NoNetworkMixin, TestCase):
    def test_a_message_type_without_an_automation_is_red(self):
        def resolve(entry):
            return '' if entry['flow_setting'] == 'MANYCHAT_TRIAL_FLOW_NS' else 'content1'

        with patch.object(ManyChatService, 'get_flows', return_value=[{'ns': 'content1'}]), \
                patch.object(ManyChatService, 'resolve_flow_for', side_effect=resolve):
            item = check_manychat_health(TODAY)
        self.assertEqual(item.severity, RED)
        self.assertEqual(item.count, 1)
        self.assertEqual(item.rows[0]['label'], 'רישום לשיעור ניסיון')
        self.assertIn('טקסט חופשי', item.rows[0]['detail'])

    def test_every_type_with_an_automation_is_green(self):
        with patch.object(ManyChatService, 'get_flows', return_value=[{'ns': 'content1'}]), \
                patch.object(ManyChatService, 'resolve_flow_for', return_value='content1'):
            self.assertEqual(check_manychat_health(TODAY).severity, GREEN)


@override_settings(MANYCHAT_KEY='test-key')
class HeldAlertInBriefTests(TestCase):
    def test_a_held_alert_says_why_it_was_not_sent(self):
        OfficeAlert.objects.create(
            kind=KIND_FAILED, dedup_key='k', title='וואטסאפ לא יצא ל-x', where='w', what='w', error=HELD_NOTE,
        )
        row = daily_brief.check_office_alerts(TODAY).rows[0]
        self.assertIn(HELD_NOTE, row['detail'])
        self.assertNotIn('ממתינה לשליחה', row['detail'])
