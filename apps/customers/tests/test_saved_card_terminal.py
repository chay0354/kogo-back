"""
A saved card is charged on the terminal it was made on, with that terminal's keys.

Until 25.9.2026 every saved card sat on the michal pair (fxpmichalweb /
fxpmichalwebtok) and every token charge went out through production(). New
cards will be saved on cogolivetok, whose keys are refused on the michal
terminals (20002) and the other way round. RecurringPayment.tranzila_terminal
says where a card lives; '' is the michal pair.

And in the monthly run, a problem on our side — a terminal with no keys, our
key refused, a card with no expiry — is not a decline: the standing order stays
active, the child keeps its status, and no "update your card" message goes out.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings

from apps.core.tranzila_service import (
    TOKEN_CHARGED,
    TOKEN_DECLINED,
    TOKEN_REQUEST_REJECTED,
    TOKEN_SETUP_PROBLEM,
    TOKEN_UNKNOWN,
    TranzilaService,
    token_charge_outcome,
)
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.recurring_billing import REQUEST_REJECTED, SETUP_PROBLEM, process_due_recurring_charges
from apps.customers.tests.test_charge_survives_receipt_failure import _due_standing_order

KEYS = dict(
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb',
    TRANZILA_PROD_PUBLIC_KEY='michal-app-key',
    TRANZILA_PROD_SECRET_KEY='michal-secret-key',
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive-app-key',
    TRANZILA_SECRET_KEY='cogolive-secret-key',
    TRANZILA_DCDISABLE_ENABLED=False,
)

CHARGE_URL = '/v1/transaction/credit_card/create'

APPROVED = {
    'error_code': 0,
    'message': 'Success',
    'transaction_result': {
        'processor_response_code': '000',
        'transaction_id': '4411',
        'auth_number': '0098798',
        'token': 'card_token_1',
    },
}
DECLINED = {
    'error_code': 0,
    'message': 'Declined',
    'transaction_result': {'processor_response_code': '004', 'transaction_id': '4412'},
}
KEY_REFUSED = {'error_code': 20002, 'message': 'Authorization failed'}
# What eight monthly charges got back on 1.9.2026, with HTTP 400.
SCHEMA_REJECTED = {'error_code': 20004, 'message': 'Json does not match validation schema'}
# What a bank decline really looks like: the request was fine ("Success"), the
# card company's code is the refusal.
BANK_DECLINED = {
    'error_code': 0,
    'message': 'Success',
    'transaction_result': {'processor_response_code': '036', 'transaction_id': '4413'},
}


class _Answer:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError('no JSON')
        return self._body


NO_BODY = None  # an HTTP error page: nothing that parses as JSON


class _Gateway:
    """Stands in for requests.post in tranzila_service and keeps every charge sent."""

    def __init__(self, status_code=200, body=APPROVED):
        self.status_code = status_code
        self.body = body
        self.charges = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        if url.endswith(CHARGE_URL):
            self.charges.append({'payload': json, 'app_key': (headers or {}).get('X-tranzila-api-app-key')})
            return _Answer(self.status_code, self.body)
        return _Answer(404, None)


def _run(gateway):
    with patch('apps.core.tranzila_service.requests.post', side_effect=gateway), \
            patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'), \
            patch('apps.customers.card_update.send_card_update_whatsapp') as whatsapp:
        summary = process_due_recurring_charges()
    return summary, whatsapp


@override_settings(**KEYS)
class ClientForSavedCardTests(TestCase):
    def test_an_old_card_gets_exactly_the_production_client(self):
        client = TranzilaService.for_saved_card('')
        reference = TranzilaService.production()
        for attr in ('terminal', 'token_terminal', 'supplier', 'public_key', 'secret_key'):
            self.assertEqual(getattr(client, attr), getattr(reference, attr), attr)

    def test_a_cogolivetok_card_gets_cogolive_keys(self):
        client = TranzilaService.for_saved_card('cogolivetok')
        self.assertEqual(client.token_terminal, 'cogolivetok')
        self.assertEqual(client.public_key, 'cogolive-app-key')

    def test_a_terminal_without_keys_gets_no_client(self):
        self.assertIsNone(TranzilaService.for_saved_card('someone-elses'))


class OutcomeTests(TestCase):
    def test_each_answer_means_what_it_says(self):
        cases = [
            ({'success': True, 'transaction_id': '1'}, TOKEN_CHARGED),
            ({'success': False, 'error': 'x', 'response_code': '999', 'never_sent': True}, TOKEN_SETUP_PROBLEM),
            ({'success': False, 'error': 'Authorization failed', 'response_code': '20002',
              'message': 'Charge failed: Authorization failed'}, TOKEN_SETUP_PROBLEM),
            ({'success': False, 'error': 'Declined', 'response_code': '0',
              'message': 'Charge failed: Declined'}, TOKEN_DECLINED),
            ({'success': False, 'error': 'הכרטיס נדחה', 'response_code': '004'}, TOKEN_DECLINED),
            ({'success': False, 'error': 'x', 'response_code': '20004'}, TOKEN_REQUEST_REJECTED),
            ({'success': False, 'error': 'x', 'response_code': '20001', 'request_rejected': True}, TOKEN_REQUEST_REJECTED),
            ({'success': False, 'error': 'Request timed out', 'response_code': '999', 'uncertain': True}, TOKEN_UNKNOWN),
            ({'success': False, 'error': 'HTTP 502', 'response_code': '999', 'uncertain': False}, TOKEN_UNKNOWN),
            ({'success': False, 'error': 'Unexpected error', 'response_code': '999'}, TOKEN_UNKNOWN),
            ({'success': False, 'error': 'declined'}, TOKEN_UNKNOWN),
            (None, TOKEN_UNKNOWN),
        ]
        for result, expected in cases:
            self.assertEqual(token_charge_outcome(result), expected, result)


@override_settings(**KEYS)
class MonthlyRunRoutingTests(TestCase):
    def test_an_old_card_sends_the_same_request_as_before(self):
        recurring = _due_standing_order()
        gateway = _Gateway()
        summary, _ = _run(gateway)

        self.assertEqual(summary['charged'], 1)
        self.assertEqual(len(gateway.charges), 1)
        sent = gateway.charges[0]
        self.assertEqual(sent['app_key'], 'michal-app-key')
        payload = dict(sent['payload'])
        items = payload.pop('items')
        self.assertEqual(payload, {
            'terminal_name': 'fxpmichalwebtok',
            'txn_type': 'debit',
            'txn_currency_code': 'ILS',
            'expire_month': 12,
            'expire_year': 2030,
            'card_number': 'card_token_1',
        })
        self.assertEqual(sum(Decimal(str(i['unit_price'])) * i['units_number'] for i in items), Decimal('260.00'))
        txn = TranzilaTransaction.objects.get(idempotency_key__startswith=f'recurring_{recurring.id}_')
        self.assertTrue(txn.is_successful)
        self.assertEqual(txn.tranzila_terminal, '')

    def test_a_cogolivetok_card_is_charged_there_with_its_own_keys(self):
        recurring = _due_standing_order()
        recurring.tranzila_terminal = 'cogolivetok'
        recurring.save(update_fields=['tranzila_terminal'])
        gateway = _Gateway()
        summary, _ = _run(gateway)

        self.assertEqual(summary['charged'], 1)
        self.assertEqual(gateway.charges[0]['payload']['terminal_name'], 'cogolivetok')
        self.assertEqual(gateway.charges[0]['app_key'], 'cogolive-app-key')
        txn = TranzilaTransaction.objects.get(idempotency_key__startswith=f'recurring_{recurring.id}_')
        self.assertEqual(txn.tranzila_terminal, 'cogolivetok')

    def test_a_real_decline_still_stops_the_order_and_asks_for_a_new_card(self):
        recurring = _due_standing_order()
        summary, whatsapp = _run(_Gateway(body=DECLINED))

        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'failed')
        self.assertEqual(recurring.child.status, 'payment_problem')
        whatsapp.assert_called_once()
        self.assertEqual(summary['setup_problems'], 0)
        # The card company's code is the reason — not Tranzila's outer message,
        # which on a decline is just the request's own status.
        reason = Payment.objects.get(payment_type='recurring_subscription', status='failed').failure_reason
        self.assertIn('004', reason)
        self.assertIn('חברת האשראי', reason)


@override_settings(**KEYS)
class SetupProblemTests(TestCase):
    def _assert_untouched(self, recurring, status_before, whatsapp):
        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.child.status, status_before)
        whatsapp.assert_not_called()
        self.assertFalse(
            TranzilaTransaction.objects.filter(idempotency_key__startswith=f'recurring_{recurring.id}_').exists(),
            'a claim left behind would stop every later run',
        )

    def test_a_terminal_with_no_keys_is_skipped_before_anything_is_written(self):
        recurring = _due_standing_order()
        recurring.tranzila_terminal = 'someone-elses'
        recurring.save(update_fields=['tranzila_terminal'])
        before = recurring.child.status
        gateway = _Gateway()
        summary, whatsapp = _run(gateway)

        self.assertEqual(gateway.charges, [])
        self.assertEqual(summary['setup_problems'], 1)
        self.assertEqual(summary['failed'], 0)
        self.assertIn(SETUP_PROBLEM, summary['errors'][-1])
        self.assertFalse(Payment.objects.filter(payment_type='recurring_subscription', status__in=['pending', 'failed']).exists())
        self._assert_untouched(recurring, before, whatsapp)

    def test_our_key_refused_is_not_a_decline_and_the_terminal_is_not_tried_again(self):
        first = _due_standing_order()
        second = _due_standing_order()
        before = first.child.status
        gateway = _Gateway(status_code=401, body=KEY_REFUSED)
        summary, whatsapp = _run(gateway)

        self.assertEqual(len(gateway.charges), 1, 'one refusal is enough to know the keys are wrong')
        self.assertEqual(summary['setup_problems'], 2)
        self.assertEqual(summary['failed'], 0)
        self._assert_untouched(first, before, whatsapp)
        self._assert_untouched(second, before, whatsapp)
        tried = Payment.objects.get(payment_type='recurring_subscription', status='cancelled')
        self.assertIn(SETUP_PROBLEM, tried.failure_reason)

    @override_settings(TRANZILA_PROD_PUBLIC_KEY='', TRANZILA_PROD_SECRET_KEY='')
    def test_missing_keys_on_the_michal_pair_send_nothing_and_tell_nobody(self):
        recurring = _due_standing_order()
        before = recurring.child.status
        gateway = _Gateway()
        summary, whatsapp = _run(gateway)

        self.assertEqual(gateway.charges, [])
        self.assertEqual(summary['setup_problems'], 1)
        self._assert_untouched(recurring, before, whatsapp)

    def test_a_card_with_no_expiry_is_skipped_without_a_message(self):
        recurring = _due_standing_order()
        recurring.card_expire_year = None
        recurring.save(update_fields=['card_expire_year'])
        before = recurring.child.status
        gateway = _Gateway()
        summary, whatsapp = _run(gateway)

        self.assertEqual(gateway.charges, [])
        self.assertEqual(summary['setup_problems'], 1)
        self._assert_untouched(recurring, before, whatsapp)

    def test_an_answer_that_says_nothing_certain_keeps_the_month_claimed(self):
        recurring = _due_standing_order()
        summary, whatsapp = _run(_Gateway(status_code=502, body=NO_BODY))

        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertNotEqual(recurring.child.status, 'payment_problem')
        whatsapp.assert_not_called()
        self.assertTrue(TranzilaTransaction.objects.filter(
            idempotency_key__startswith=f'recurring_{recurring.id}_', is_successful=False,
        ).exists())
        self.assertEqual(Payment.objects.get(payment_type='recurring_subscription', status='processing').final_amount,
                         Decimal('260.00'))
        self.assertEqual(summary['setup_problems'], 0)


class ColumnDefaultTests(TestCase):
    """A push migrates production while the old code still runs: its inserts must not fail."""

    def test_every_terminal_column_has_a_database_default(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name, column_default, is_nullable
                FROM information_schema.columns
                WHERE column_name = 'tranzila_terminal'
                  AND table_name IN ('recurring_payments', 'tranzila_transactions', 'payment_link_payments')
                """
            )
            found = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        self.assertEqual(set(found), {'recurring_payments', 'tranzila_transactions', 'payment_link_payments'})
        for table, (default, nullable) in found.items():
            self.assertEqual(nullable, 'NO', table)
            self.assertTrue(default and default.startswith("''"), f'{table}: {default!r}')


@override_settings(**KEYS)
class MorningBriefTests(TestCase):
    """The office hears about a setup problem the morning after — the parents never do."""

    def _check(self):
        from apps.core.daily_brief import check_saved_card_setup
        from apps.customers.tests.test_charge_survives_receipt_failure import _today

        return check_saved_card_setup(_today())

    def test_quiet_when_every_saved_card_has_keys(self):
        recurring = _due_standing_order()
        recurring.tranzila_terminal = 'cogolivetok'
        recurring.save(update_fields=['tranzila_terminal'])
        self.assertEqual(self._check().severity, 'green')

    def test_cards_on_a_terminal_with_no_keys_are_red(self):
        recurring = _due_standing_order()
        recurring.tranzila_terminal = 'someone-elses'
        recurring.save(update_fields=['tranzila_terminal'])
        item = self._check()
        self.assertEqual(item.severity, 'red')
        self.assertEqual(item.rows[0]['label'], 'מסוף someone-elses')

    def test_the_last_run_that_met_one_is_red_with_the_children(self):
        from django.utils import timezone

        from apps.customers.models import CronHeartbeat

        recurring = _due_standing_order()
        CronHeartbeat.objects.create(summary={
            'setup_problems': 1,
            'errors': [f'{recurring.id}: {SETUP_PROBLEM} — Authorization failed'],
        })
        item = self._check()
        self.assertEqual(item.severity, 'red')
        self.assertEqual(item.rows[0]['label'], recurring.child.full_name)
        self.assertIn('Authorization failed', item.rows[0]['detail'])

        # A day later, or a dry run, is not this morning's news.
        CronHeartbeat.objects.update(invoked_at=timezone.now() - timedelta(days=2))
        CronHeartbeat.objects.create(dry_run=True, summary={'setup_problems': 1, 'errors': []})
        self.assertEqual(self._check().severity, 'green')


@override_settings(**KEYS)
class DeclineReasonTests(TestCase):
    """
    What a refused charge is recorded as (27.9.2026).

    Tranzila's reply to a bank decline still says error_code 0, "Success": the
    request was fine. We stored that "Success" as the reason — 37 declines read
    "Success", the card company's code was lost, and parents were shown
    "Success" as the error.
    """

    def _charge(self, status_code, body):
        with patch('apps.core.tranzila_service.requests.post', side_effect=_Gateway(status_code, body)):
            return TranzilaService.for_saved_card('').charge_with_token(
                token='card_token_1', amount=Decimal('250.00'), expire_month=12, expire_year=2030,
            )

    def test_a_bank_decline_keeps_the_card_companys_code_and_says_what_happened(self):
        result = self._charge(200, BANK_DECLINED)
        self.assertFalse(result['success'])
        self.assertEqual(result['response_code'], '036')
        self.assertIn('תוקף הכרטיס פג', result['error'])
        self.assertIn('לא בוצע חיוב', result['error'])
        self.assertNotIn('Success', result['error'])
        self.assertEqual(token_charge_outcome(result), TOKEN_DECLINED)

    def test_an_unlisted_code_is_still_a_decline_with_its_code(self):
        body = {**BANK_DECLINED, 'transaction_result': {'processor_response_code': '051'}}
        result = self._charge(200, body)
        self.assertIn('051', result['error'])
        self.assertEqual(token_charge_outcome(result), TOKEN_DECLINED)

    def test_a_request_tranzila_could_not_read_is_ours_not_the_cards(self):
        result = self._charge(400, SCHEMA_REJECTED)
        self.assertFalse(result['success'])
        self.assertEqual(result['response_code'], '20004')
        self.assertIn('לא חויב', result['error'])
        self.assertEqual(token_charge_outcome(result), TOKEN_REQUEST_REJECTED)

    def test_our_key_refused_is_still_a_setup_problem(self):
        self.assertEqual(token_charge_outcome(self._charge(401, KEY_REFUSED)), TOKEN_SETUP_PROBLEM)

    def test_an_approval_is_unchanged(self):
        self.assertEqual(token_charge_outcome(self._charge(200, APPROVED)), TOKEN_CHARGED)


class _Sequence(_Gateway):
    """Answers each charge with the next body in turn."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def __call__(self, url, json=None, headers=None, timeout=None):
        if url.endswith(CHARGE_URL):
            status_code, body = self.answers.pop(0)
            self.charges.append({'payload': json})
            return _Answer(status_code, body)
        return _Answer(404, None)


@override_settings(**KEYS)
class RejectedRequestTests(TestCase):
    """
    A monthly charge Tranzila refused as malformed is not a decline.

    1.9.2026: eight standing orders came back 20004. Each was treated as a
    declined card — the order failed, the child was flagged בעיה באשראי, the
    parent was asked to update a card that was fine.
    """

    def test_nothing_is_marked_on_the_customer_and_no_message_goes_out(self):
        recurring = _due_standing_order()
        before = recurring.child.status
        summary, whatsapp = _run(_Gateway(400, SCHEMA_REJECTED))

        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.child.status, before)
        whatsapp.assert_not_called()
        payment = Payment.objects.get(payment_type='recurring_subscription', status='cancelled')
        self.assertTrue(payment.failure_reason.startswith(REQUEST_REJECTED))
        self.assertIn('20004', payment.failure_reason)
        self.assertFalse(
            TranzilaTransaction.objects.filter(idempotency_key__startswith=f'recurring_{recurring.id}_').exists(),
            'a claim left behind would stop every later month',
        )
        self.assertEqual(summary['setup_problems'], 1)

    def test_it_does_not_stop_the_other_cards_on_the_terminal(self):
        """Unlike a refused key, it is about this one request."""
        first, second = _due_standing_order(), _due_standing_order()
        gateway = _Sequence((400, SCHEMA_REJECTED), (200, APPROVED))
        _run(gateway)

        self.assertEqual(len(gateway.charges), 2)
        statuses = sorted(Payment.objects.filter(payment_type='recurring_subscription', status__in=['cancelled', 'completed'])
                          .exclude(description='מנוי').values_list('status', flat=True))
        self.assertEqual(statuses, ['cancelled', 'completed'])

    def test_the_same_request_is_not_sent_again_later_the_same_day(self):
        _due_standing_order()
        gateway = _Gateway(400, SCHEMA_REJECTED)
        _run(gateway)
        _run(gateway)
        self.assertEqual(len(gateway.charges), 1)

    def test_a_real_bank_decline_still_asks_for_a_new_card(self):
        recurring = _due_standing_order()
        _summary, whatsapp = _run(_Gateway(200, BANK_DECLINED))

        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'failed')
        self.assertEqual(recurring.child.status, 'payment_problem')
        whatsapp.assert_called_once()
        reason = Payment.objects.get(payment_type='recurring_subscription', status='failed').failure_reason
        self.assertIn('036', reason)
        self.assertNotIn('Success', reason)

