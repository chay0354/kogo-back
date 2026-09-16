"""
A failed broadcast has to say what went wrong.

Two rows once came back reading only "Validation error", which is ManyChat's
headline and never the reason. The office could not tell a number WhatsApp will
not accept from an automation that had been deleted, and neither could we — the
detail ManyChat does send sat in the payload and was thrown away on the way to
the screen. The dialog even offers a "copy the failures" button, which copied
nothing worth reading.
"""
from apps.core.manychat_service import ManyChatError, manychat_error_detail
from django.test import SimpleTestCase


class ManyChatErrorDetailTests(SimpleTestCase):
    def test_the_rejected_field_reaches_the_message(self):
        exc = ManyChatError('Validation error', status_code=400, payload={
            'status': 'error',
            'message': 'Validation error',
            'details': {'messages': {'last_name': ['The last name field is required.']}},
        })
        text = manychat_error_detail(exc)
        self.assertIn('Validation error', text)
        self.assertIn('last_name', text)
        self.assertIn('required', text)

    def test_a_flat_detail_string_is_kept(self):
        exc = ManyChatError('Validation error', payload={'details': 'flow_ns not found'})
        self.assertIn('flow_ns not found', manychat_error_detail(exc))

    def test_several_rejected_fields_are_all_named(self):
        exc = ManyChatError('Validation error', payload={'details': {'messages': {
            'whatsapp_phone': ['invalid'],
            'first_name': ['required'],
        }}})
        text = manychat_error_detail(exc)
        self.assertIn('whatsapp_phone', text)
        self.assertIn('first_name', text)

    def test_it_never_returns_an_empty_string(self):
        """A row with no message at all still has to render as something."""
        self.assertTrue(manychat_error_detail(ManyChatError('')))

    def test_the_headline_is_not_repeated_when_it_is_all_there_is(self):
        exc = ManyChatError('Validation error', payload={'message': 'Validation error'})
        self.assertEqual(manychat_error_detail(exc), 'Validation error')


class NotifyRegistrationReportsTheDetailTests(SimpleTestCase):
    """
    The 'kind' automations take a different road out.

    They *return* the failure rather than raising it, so the broadcast's own
    ``except ManyChatError`` never sees them — fixing only the raise path would
    have left half the broadcasts saying "Validation error" and nothing else.
    """

    def _service(self):
        from apps.core.manychat_service import ManyChatService

        svc = ManyChatService(api_key='test-key')
        return svc

    def test_a_contact_failure_names_the_field_and_the_stage(self):
        from unittest.mock import patch

        from apps.core.manychat_service import ManyChatError

        exc = ManyChatError('Validation error', payload={
            'details': {'messages': {'whatsapp_phone': ['is not a valid whatsapp id']}},
        })
        svc = self._service()
        with patch.object(svc, 'lookup_or_create', side_effect=exc):
            out = svc.notify_registration(
                phone='0522659322', parent_name='דור סער', child_name='דור',
                course_name='קפוארה', day_name='רביעי', start_time='17:30',
                end_time='18:15', branch_name='דמרי',
            )
        self.assertFalse(out['sent'])
        self.assertEqual(out['reason'], 'lookup_failed')
        self.assertIn('איש הקשר', out['error'])
        self.assertIn('whatsapp_phone', out['error'])
