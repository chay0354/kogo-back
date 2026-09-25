"""
No CVV and no full card number is ever kept or handed back.

Tranzila's REST answer echoes the request (original_request) — for a typed
card that includes the CVV — and the whole answer used to be stored. 759 rows
held a CVV on 25.9.2026.
"""
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.card_data import holds_card_data, scrub_card_data
from apps.core.payment_service import _expiry_and_token_from_tranzila_payload, terminal_for_payment_refund
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import TranzilaTransaction

# The shape of a typed-card REST answer: the request echoed back.
ECHO = {
    'error_code': 0,
    'message': 'Success',
    'transaction_result': {'processor_response_code': '000', 'transaction_id': '487573',
                           'ConfirmationCode': '0012345', 'token': 'tok4580abcd1234'},
    'original_request': {'terminal_name': 'fxpmichalweb', 'card_number': 'tok4580abcd1234',
                         'cvv': '123', 'expire_month': 12, 'expire_year': 2028,
                         'items': [{'name': 'מנוי', 'unit_price': 1}]},
}


class ScrubTest(SimpleTestCase):
    def test_the_cvv_goes_and_what_refunds_read_stays(self):
        clean = scrub_card_data(ECHO)
        self.assertNotIn('cvv', clean['original_request'])
        self.assertEqual(clean['original_request']['card_number'], 'tok4580abcd1234')
        self.assertEqual(clean['original_request']['expire_month'], 12)
        self.assertEqual(clean['original_request']['terminal_name'], 'fxpmichalweb')
        self.assertEqual(clean['transaction_result'], ECHO['transaction_result'])
        self.assertIn('cvv', ECHO['original_request'], 'the input is not changed in place')

    def test_any_nesting_and_any_spelling(self):
        payload = {'a': [{'b': {'CVV': '999', 'Cvv2': '1', 'mycvv': '2', 'keep': 'x'}}]}
        self.assertEqual(scrub_card_data(payload), {'a': [{'b': {'keep': 'x'}}]})

    def test_a_full_card_number_is_masked_a_token_is_not(self):
        self.assertEqual(scrub_card_data({'card_number': '4580458045804580'}), {'card_number': '************4580'})
        self.assertEqual(scrub_card_data({'ccno': 4580458045804580}), {'ccno': '************4580'})
        self.assertEqual(scrub_card_data({'card_number': 'tok4580abcd1234'}), {'card_number': 'tok4580abcd1234'})
        self.assertEqual(scrub_card_data({'card_number': '4580'}), {'card_number': '4580'})

    def test_holds_card_data(self):
        self.assertTrue(holds_card_data(ECHO))
        self.assertFalse(holds_card_data(scrub_card_data(ECHO)))


@override_settings(TRANZILA_TERMINAL='t', TRANZILA_PUBLIC_KEY='pk', TRANZILA_SECRET_KEY='sk')
class GatewayAnswerTest(SimpleTestCase):
    def test_a_rest_answer_reaches_no_caller_with_a_cvv(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return ECHO

        with patch('apps.core.tranzila_service.requests.post', return_value=FakeResponse()):
            answer = TranzilaService()._make_api_request({'x': 1}, '/v1/transaction/credit_card/create')
        self.assertNotIn('cvv', answer['original_request'])

    def test_a_charge_result_carries_no_cvv(self):
        with patch.object(TranzilaService, '_make_api_request', return_value=ECHO):
            result = TranzilaService().charge_with_card(
                card_number='4580458045804580', expiry_month=12, expiry_year=2028, cvv='123',
                card_holder_id='123456782', amount=Decimal('1.00'),
            )
        self.assertTrue(result['success'], result)
        self.assertFalse(holds_card_data(result))


class StoredRowTest(TestCase):
    def test_a_row_is_written_without_card_data_whoever_hands_it_in(self):
        row = TranzilaTransaction.objects.create(
            transaction_id='1', idempotency_key='k1', request_data={'cvv': '123', 'card_number': '4580458045804580'},
            response_data=ECHO, is_successful=True,
        )
        row.refresh_from_db()
        self.assertFalse(holds_card_data(row.request_data))
        self.assertFalse(holds_card_data(row.response_data))

    def test_refunds_still_read_expiry_token_and_terminal(self):
        row = TranzilaTransaction.objects.create(transaction_id='1', idempotency_key='k2', response_data=ECHO)

        class Payment:
            tranzila_transaction = row

        self.assertEqual(_expiry_and_token_from_tranzila_payload(Payment()), (12, 2028, 'tok4580abcd1234'))
        self.assertEqual(terminal_for_payment_refund(Payment()), 'fxpmichalweb')


class ScrubCommandTest(TestCase):
    def setUp(self):
        # Rows as they were kept before the fix: written around save().
        for n in range(3):
            row = TranzilaTransaction.objects.create(transaction_id=str(n), idempotency_key=f'old{n}')
            TranzilaTransaction.objects.filter(pk=row.pk).update(response_data=ECHO)
        TranzilaTransaction.objects.create(transaction_id='9', idempotency_key='clean', response_data={'ok': 1})

    def run_command(self, *args):
        out = StringIO()
        call_command('scrub_card_data', *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_counts_and_changes_nothing(self):
        out = self.run_command()
        self.assertIn('DRY RUN', out)
        self.assertIn('with_card_data=3', out)
        self.assertEqual(sum(holds_card_data(r.response_data) for r in TranzilaTransaction.objects.all()), 3)
        self.assertNotIn('123', out)

    def test_apply_cleans_every_row_and_keeps_the_rest(self):
        out = self.run_command('--apply')
        self.assertIn('rewritten=3 remaining=0', out)
        for row in TranzilaTransaction.objects.all():
            self.assertFalse(holds_card_data(row.response_data))
        kept = TranzilaTransaction.objects.get(idempotency_key='old0').response_data
        self.assertEqual(kept['original_request']['expire_year'], 2028)
        self.assertIn('rewritten=0', self.run_command('--apply'))
