"""Whoever asked "הסר" gets no broadcast to municipality students either."""
from unittest.mock import patch

from django.db import DatabaseError

from apps.core import whatsapp_optout
from apps.core.models import WhatsAppOptOut
from apps.external_students.tests.test_external_students import STUDENTS_URL, ExternalStudentTestBase

SEND = 'apps.core.manychat_service.ManyChatService.send_automation_to_contact'


class ExternalBroadcastOptOutTests(ExternalStudentTestBase):
    def _send(self, ids, dry_run=True):
        return self.client.post(
            f'{STUDENTS_URL}broadcast/',
            {'student_ids': [str(i) for i in ids], 'automation_id': 'flow-ns', 'dry_run': dry_run}, format='json',
        )

    def test_whoever_asked_is_skipped_and_counted_apart(self):
        asked = self.student(first='נועם', phone='050-123-4567')
        other = self.student(first='שירה', phone='0507654321')
        whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_MESSAGE, text='הסר')
        with patch(SEND, return_value={'sent': True, 'method': 'flow'}) as send:
            res = self._send([asked.id, other.id], dry_run=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([call.kwargs['phone'] for call in send.call_args_list], ['0507654321'])
        self.assertEqual((res.data['sent'], res.data['skipped'], res.data['opted_out']), (1, 1, 1))
        by_id = {row['student_id']: row for row in res.data['results']}
        self.assertEqual((by_id[str(asked.id)]['status'], by_id[str(asked.id)]['reason']), ('skipped', 'opted_out'))
        self.assertEqual(res.data['phones'], ['972507654321'])

    def test_the_preview_says_it_too(self):
        asked = self.student(phone='0501234567')
        whatsapp_optout.add('0501234567', source=WhatsAppOptOut.SOURCE_OFFICE)
        res = self._send([asked.id])
        self.assertEqual((res.data['preview_count'], res.data['skipped'], res.data['opted_out']), (0, 1, 1))

    def test_when_the_list_cannot_be_read_nothing_leaves(self):
        row = self.student(phone='0501234567')
        with patch('apps.core.whatsapp_optout._rows', side_effect=DatabaseError('down')), patch(SEND) as send:
            res = self._send([row.id], dry_run=False)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data['code'], 'opt_out_list_unavailable')
        send.assert_not_called()
