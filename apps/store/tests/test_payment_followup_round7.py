"""
Stage 3, seventh round (1.10.2026), part 1: the lookup of one transaction by
its number. It serves flows that live today (payment links, the business
charge), so it must be no stricter than before for them: a row that is there
is found whatever the envelope reads; "unknown" only when there are no rows
AND the answer is an error (or the network failed); "no such transaction"
only on a good answer.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.core.tranzila_service import TranzilaService
from apps.store.tests.test_payment_followup import SETTINGS, paid_row


@override_settings(**SETTINGS)
class FindTransactionTest(SimpleTestCase):
    def find(self, answer):
        with patch.object(TranzilaService, '_make_api_request', return_value=answer):
            return TranzilaService.iframe().find_transaction('123456')

    def test_a_row_of_this_number_is_found(self):
        self.assertEqual(self.find({'error_code': 0, 'message': 'Success', 'transactions': [paid_row()]}),
                         {'success': True, 'transaction': paid_row()})

    def test_a_row_of_this_number_is_found_whatever_the_error_code_reads(self):
        for envelope in ({'error_code': 20002}, {'error_code': None}, {}):
            found = self.find({**envelope, 'transactions': [paid_row()]})
            self.assertTrue(found['success'], envelope)
            self.assertEqual(found['transaction']['index'], '123456')

    def test_an_error_with_an_empty_or_missing_list_is_unknown(self):
        for answer in ({'error_code': 20002, 'message': 'Invalid application key', 'transactions': []},
                       {'error_code': 20002, 'message': 'Invalid application key'}):
            found = self.find(answer)
            self.assertFalse(found['success'], answer)
            self.assertEqual(found['error'], 'Invalid application key')

    def test_a_network_failure_is_unknown(self):
        self.assertFalse(self.find({'success': False, 'error': 'timeout'})['success'])
        self.assertFalse(self.find(None)['success'])

    def test_a_good_answer_with_an_empty_list_is_no_such_transaction(self):
        self.assertEqual(self.find({'error_code': 0, 'message': 'Success', 'transactions': []}),
                         {'success': True, 'transaction': None})
        self.assertEqual(self.find({'transactions': []}), {'success': True, 'transaction': None})

    def test_a_good_answer_with_other_rows_only_is_no_such_transaction(self):
        self.assertEqual(self.find({'error_code': 0, 'transactions': [paid_row(index='999')]}),
                         {'success': True, 'transaction': None})
