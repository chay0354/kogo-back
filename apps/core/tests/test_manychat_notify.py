"""ManyChat registration WhatsApp: custom fields must land before the template."""
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.core.manychat_service import ManyChatError, ManyChatService


class CardLinkFlowTests(SimpleTestCase):
    """The card link goes by its own WhatsApp template — never the failed-charge one."""

    def test_the_setting_is_declared_so_the_environment_reaches_it(self):
        # It was read but never declared, so setting it in Vercel changed nothing
        # and every card link went out as free text.
        self.assertTrue(hasattr(settings, 'MANYCHAT_CARD_LINK_FLOW_NS'))

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='content_card_link')
    def test_the_configured_flow_is_used(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock()
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), 'content_card_link')
        svc.get_flows.assert_not_called()

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='')
    def test_without_one_an_automation_named_card_link_is_found(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[
            {'name': 'card-update', 'ns': 'content_update'},
            {'name': 'card-link', 'ns': 'content_link'},
        ])
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), 'content_link')

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='')
    def test_the_failed_charge_template_is_never_borrowed(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[{'name': 'card-update', 'ns': 'content_update'}])
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), '')


class SetCustomFieldsFallbackTests(SimpleTestCase):
    def test_retries_fields_one_by_one_when_batch_fails(self):
        svc = ManyChatService(api_key='x')
        calls = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {'status': 'success', 'data': []}
            calls.append(kwargs.get('json_body'))
            body = kwargs.get('json_body') or {}
            fields = body.get('fields') or []
            if len(fields) > 1:
                raise ManyChatError('unknown field kogo_location')
            if fields and fields[0].get('field_name') == 'kogo_location':
                raise ManyChatError('unknown field')
            return {'status': 'success'}

        svc._request = fake_request
        result = svc.set_custom_fields(11, {
            'kogo_branch_name': 'יהודה הלוי',
            'kogo_location': 'missing-in-manychat',
        })
        self.assertTrue(result.get('partial'))
        self.assertEqual(result.get('applied'), 1)
        self.assertEqual(len(calls), 3)

    def test_raises_when_every_field_fails(self):
        svc = ManyChatService(api_key='x')
        svc._request = MagicMock(side_effect=ManyChatError('boom'))
        with self.assertRaises(ManyChatError):
            svc.set_custom_fields(11, {'kogo_branch_name': 'יהודה הלוי'})

    def test_also_writes_timestamp_alias_fields(self):
        svc = ManyChatService(api_key='x')
        posted = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {
                    'status': 'success',
                    'data': [
                        {'id': 1, 'name': 'kogo_branch_name', 'type': 'text'},
                        {'id': 2, 'name': 'kogo_branch_name (2026-07-26 07:28:15)', 'type': 'text'},
                    ],
                }
            posted.append(kwargs.get('json_body'))
            return {'status': 'success'}

        svc._request = fake_request
        svc.set_custom_fields(11, {'kogo_branch_name': 'מינץ 24'})
        names = {row['field_name'] for row in posted[0]['fields']}
        self.assertIn('kogo_branch_name', names)
        self.assertIn('kogo_branch_name (2026-07-26 07:28:15)', names)

    def test_skips_fields_that_do_not_exist_in_manychat(self):
        svc = ManyChatService(api_key='x')
        posted = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {
                    'status': 'success',
                    'data': [{'id': 1, 'name': 'kogo_branch_name', 'type': 'text'}],
                }
            posted.append(kwargs.get('json_body'))
            return {'status': 'success'}

        svc._request = fake_request
        svc.set_custom_fields(11, {
            'kogo_branch_name': 'מינץ 24',
            'kogo_location': 'does-not-exist',
        })
        names = {row['field_name'] for row in posted[0]['fields']}
        self.assertEqual(names, {'kogo_branch_name'})


class NotifyRegistrationFieldOrderTests(SimpleTestCase):
    def _svc(self):
        svc = ManyChatService(api_key='x')
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 99})
        svc.get_subscriber = MagicMock(return_value={'whatsapp_phone': '972501234567'})
        svc.resolve_flow_ns = MagicMock(return_value='content123_flow')
        svc.send_flow = MagicMock(return_value={'status': 'success'})
        svc.send_whatsapp_text = MagicMock(return_value={'status': 'success'})
        return svc

    def _kwargs(self, **overrides):
        payload = {
            'phone': '0501234567',
            'parent_name': 'הורה בדיקה',
            'child_name': 'ילד בדיקה',
            'course_name': 'קפוארה',
            'day_name': 'ראשון',
            'start_time': '18:15',
            'end_time': '19:00',
            'branch_name': 'יהודה הלוי',
            'kind': ManyChatService.REGISTRATION_KIND_TRIAL,
            'trial_date': '06/09/2026',
            'location': 'רחוב יהודה הלוי 10',
        }
        payload.update(overrides)
        return payload

    @patch('apps.core.manychat_service.time.sleep')
    def test_writes_fields_waits_then_sends_flow(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(return_value={'status': 'success'})

        result = svc.notify_registration(**self._kwargs())

        self.assertEqual(result['method'], 'flow')
        fields = svc.set_custom_fields.call_args[0][1]
        self.assertEqual(fields['kogo_branch_name'], 'יהודה הלוי')
        self.assertEqual(fields['kogo_trial_date'], '06/09/2026')
        self.assertEqual(fields['kogo_lesson_time'], '18:15-19:00')
        self.assertEqual(fields['kogo_location'], 'רחוב יהודה הלוי 10')
        mock_sleep.assert_called_once()

    @patch('apps.core.manychat_service.time.sleep')
    def test_writes_extra_card_update_fields(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(return_value={'status': 'success'})

        result = svc.notify_registration(
            **self._kwargs(
                kind=ManyChatService.REGISTRATION_KIND_CARD_UPDATE,
                extra_fields={
                    'kogo_card_update_url': 'https://kogo-front.vercel.app/update-card/tok',
                    'kogo_card_update_token': 'tok',
                    'kogo_amount': '250',
                },
            )
        )

        self.assertEqual(result['method'], 'flow')
        fields = svc.set_custom_fields.call_args[0][1]
        self.assertEqual(fields['kogo_amount'], '250')
        self.assertEqual(fields['kogo_card_update_token'], 'tok')
        self.assertIn('update-card/tok', fields['kogo_card_update_url'])
        svc.send_flow.assert_called_once_with(99, 'content123_flow')
        svc.send_whatsapp_text.assert_not_called()

    @patch('apps.core.manychat_service.time.sleep')
    def test_skips_empty_template_when_fields_fail(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(side_effect=ManyChatError('set failed'))

        result = svc.notify_registration(**self._kwargs())

        self.assertTrue(result['sent'])
        self.assertEqual(result['method'], 'text')
        svc.send_flow.assert_not_called()
        svc.send_whatsapp_text.assert_called_once()
        mock_sleep.assert_not_called()
        text = svc.send_whatsapp_text.call_args[0][1]
        self.assertIn('יהודה הלוי', text)
        self.assertIn('18:15-19:00', text)
