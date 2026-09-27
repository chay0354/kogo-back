"""Alerts to the office: kept once, sent to the office's WhatsApp in sections, never breaking their caller."""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import OfficeAlert
from apps.core.office_alerts import ALERT_FIELDS, raise_office_alert

CONFIGURED = dict(MANYCHAT_KEY='mc-key', MANYCHAT_OFFICE_ALERT_FLOW_NS='content2026_office', OFFICE_ALERT_PHONES='0501111111,0502222222')


def _raise(key='k1', **overrides):
    fields = dict(
        kind='test', dedup_key=key, title='לא ידוע אם ההורה חויב',
        where='הרשמה לחוג באתר — החיוב', what='נשלח חיוב\nולא התקבלה תשובה', why='timeout',
        customer='הורה: דנה 0501234567', action='לבדוק בטרנזילה', link='https://crm.example.test/customers?child=1',
    )
    fields.update(overrides)
    raise_office_alert(**fields)


class OfficeAlertTest(TestCase):
    def test_without_the_template_it_is_kept_for_the_brief(self):
        with self.captureOnCommitCallbacks(execute=True):
            _raise()
        alert = OfficeAlert.objects.get()
        self.assertEqual(alert.status, OfficeAlert.STATUS_NOT_CONFIGURED)
        self.assertEqual(alert.what, 'נשלח חיוב ולא התקבלה תשובה', 'one line — a WhatsApp template value')

    def test_one_event_is_one_alert(self):
        with self.captureOnCommitCallbacks(execute=True):
            _raise()
            _raise()
        self.assertEqual(OfficeAlert.objects.count(), 1)

    @override_settings(**CONFIGURED)
    @patch('apps.core.manychat_service.FIELD_SETTLE_SECONDS', 0)
    def test_it_goes_to_every_office_phone_in_its_sections(self):
        with patch('apps.core.manychat_service.ManyChatService.lookup_or_create', side_effect=[
            {'subscriber_id': 11}, {'subscriber_id': 22},
        ]) as lookup, patch('apps.core.manychat_service.ManyChatService.set_custom_fields') as fields, \
                patch('apps.core.manychat_service.ManyChatService.send_flow') as flow:
            with self.captureOnCommitCallbacks(execute=True):
                _raise()
        self.assertEqual([c.args[0] for c in lookup.call_args_list], ['0501111111', '0502222222'])
        sent = fields.call_args_list[0].args[1]
        self.assertEqual(set(sent), set(ALERT_FIELDS.values()))
        self.assertTrue(all(sent.values()), 'every section is sent, or ManyChat keeps the last alert\'s')
        self.assertEqual(sent['kogo_alert_title'], 'לא ידוע אם ההורה חויב')
        self.assertEqual(sent['kogo_alert_where'], 'הרשמה לחוג באתר — החיוב')
        self.assertEqual([c.args for c in flow.call_args_list], [(11, 'content2026_office'), (22, 'content2026_office')])
        alert = OfficeAlert.objects.get()
        self.assertEqual(alert.status, OfficeAlert.STATUS_SENT)
        self.assertIsNotNone(alert.sent_at)

    @override_settings(**CONFIGURED)
    @patch('apps.core.manychat_service.FIELD_SETTLE_SECONDS', 0)
    def test_a_failed_send_is_recorded_and_never_breaks_the_caller(self):
        from apps.core.manychat_service import ManyChatError

        with patch('apps.core.manychat_service.ManyChatService.lookup_or_create', side_effect=ManyChatError('down')):
            with self.captureOnCommitCallbacks(execute=True):
                _raise()
        self.assertEqual(OfficeAlert.objects.get().status, OfficeAlert.STATUS_FAILED)

    def test_the_brief_lists_the_day_and_what_was_not_sent(self):
        from apps.core.daily_brief import check_office_alerts

        self.assertEqual(check_office_alerts(timezone.localdate()).severity, 'green')
        with self.captureOnCommitCallbacks(execute=True):
            _raise()
        item = check_office_alerts(timezone.localdate())
        self.assertEqual(item.severity, 'red')
        self.assertEqual(item.rows[0]['label'], 'לא ידוע אם ההורה חויב')
        self.assertIn('ווטסאפ למשרד לא הוגדר', item.rows[0]['detail'])


class MonthlyRunAlertTest(TestCase):
    def test_a_setup_problem_in_the_monthly_run_alerts_once_a_day_and_never_on_a_dry_run(self):
        from apps.customers.recurring_billing import process_due_recurring_charges
        from apps.customers.tests.test_charge_survives_receipt_failure import _due_standing_order

        recurring = _due_standing_order()
        recurring.tranzila_terminal = 'someone-elses'
        recurring.save(update_fields=['tranzila_terminal'])
        with self.captureOnCommitCallbacks(execute=True):
            process_due_recurring_charges(dry_run=True)
        self.assertFalse(OfficeAlert.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            process_due_recurring_charges()
            process_due_recurring_charges()
        alert = OfficeAlert.objects.get(kind='recurring_setup')
        self.assertIn('someone-elses', alert.why)
        self.assertIn('1 הוראות קבע', alert.what)


class OfficeAlertTestEndpointTest(TestCase):
    URL = '/api/v1/core/office-alerts/test/'

    def _client(self, role):
        from django.contrib.auth import get_user_model
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        from apps.core.models import UserProfile

        user = get_user_model().objects.create_user(username=f'{role}@t.co', email=f'{role}@t.co', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_a_manager_sends_a_test_and_sees_it(self):
        client = self._client('manager')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(client.post(self.URL).status_code, 200)
        state = client.get(self.URL).json()
        self.assertFalse(state['configured'])
        self.assertEqual(state['recent'][0]['title'], 'התראת בדיקה')

    def test_workers_cannot(self):
        self.assertEqual(self._client('worker').post(self.URL).status_code, 403)
