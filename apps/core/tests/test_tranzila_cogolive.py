"""
What the move of the hosted page to cogolive relies on in TranzilaService:
the report lookup by transaction number, the report's amount in agorot, the
wallet buttons, and a client per terminal with that terminal's own keys.
"""
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase, override_settings

from apps.core.tranzila_service import TranzilaService, report_transaction_amount, wallet_params

TERMINALS = dict(
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive_pk',
    TRANZILA_SECRET_KEY='cogolive_sk',
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb',
    TRANZILA_PROD_PUBLIC_KEY='michal_pk',
    TRANZILA_PROD_SECRET_KEY='michal_sk',
)
# Top-level shape of /v1/transactions (cogolive, 23.9.2026).
REPORT = {
    'rows': 1, 'total': 1, 'user_defined': [],
    'transactions': [{'index': '1', 'amount': '500', 'processor_response_code': '000', 'tranmode': 'A'}],
}


class ReportAmountTest(SimpleTestCase):
    def test_the_report_speaks_agorot(self):
        self.assertEqual(report_transaction_amount({'amount': '500'}), Decimal('5.00'))
        self.assertEqual(report_transaction_amount({'amount': '31800'}), Decimal('318.00'))
        self.assertEqual(report_transaction_amount({'amount': '199'}), Decimal('1.99'))

    def test_no_amount_is_no_amount(self):
        self.assertIsNone(report_transaction_amount({}))
        self.assertIsNone(report_transaction_amount({'amount': 'x'}))
        self.assertIsNone(report_transaction_amount(None))
        # `sum` is the notify's echo in shekels, never read as the report's amount.
        self.assertIsNone(report_transaction_amount({'sum': '5.00'}))


@override_settings(**TERMINALS)
class FindTransactionTest(SimpleTestCase):
    def test_asks_this_terminal_by_number(self):
        service = TranzilaService.iframe()
        with patch.object(TranzilaService, '_make_api_request', return_value=REPORT) as api:
            found = service.find_transaction('1')

        self.assertTrue(found['success'])
        self.assertEqual(found['transaction']['amount'], '500')
        self.assertEqual(api.call_args.kwargs['params'], {'terminal_name': 'cogolive', 'transaction_index': 1})
        self.assertEqual(api.call_args.kwargs['endpoint'], '/v1/transactions')

    def test_another_number_in_the_answer_is_not_a_match(self):
        with patch.object(TranzilaService, '_make_api_request', return_value=REPORT):
            found = TranzilaService.iframe().find_transaction('2')
        self.assertEqual(found, {'success': True, 'transaction': None})

    def test_a_refused_key_is_not_an_empty_report(self):
        refused = {'error_code': 20002, 'message': 'Authorization failed'}
        with patch.object(TranzilaService, '_make_api_request', return_value=refused):
            found = TranzilaService.iframe().find_transaction('1')
        self.assertFalse(found['success'])

    def test_no_answer_is_not_an_empty_report(self):
        timeout = {'success': False, 'error': 'Request timed out', 'uncertain': True}
        with patch.object(TranzilaService, '_make_api_request', return_value=timeout):
            found = TranzilaService.iframe().find_transaction('1')
        self.assertFalse(found['success'])

    def test_a_number_that_is_not_a_number_asks_nothing(self):
        with patch.object(TranzilaService, '_make_api_request') as api:
            found = TranzilaService.iframe().find_transaction('txn-1')
        self.assertEqual(found, {'success': True, 'transaction': None})
        api.assert_not_called()


@override_settings(**TERMINALS)
class ForTerminalTest(SimpleTestCase):
    def test_each_terminal_gets_its_own_keys(self):
        for name, key in (
            ('cogolive', 'cogolive_pk'), ('cogolivetok', 'cogolive_pk'),
            ('fxpmichalweb', 'michal_pk'), ('fxpmichalwebtok', 'michal_pk'),
        ):
            service = TranzilaService.for_terminal(name)
            self.assertEqual((service.terminal, service.token_terminal, service.public_key), (name, name, key))

    def test_an_unknown_terminal_gets_no_client(self):
        self.assertIsNone(TranzilaService.for_terminal('realtest'))
        self.assertIsNone(TranzilaService.for_terminal(''))


@override_settings(**TERMINALS, TRANZILA_HOSTED_PAGE_ENABLED=True, TRANZILA_HANDSHAKE_ENABLED=False)
class WalletButtonsTest(SimpleTestCase):
    def query(self, **kwargs):
        url = TranzilaService.iframe().create_payment_request(
            amount=Decimal('49.00'), transaction_id='5b0c1f6e-2f4f-4c9b-9a53-1f1e9d0c7a11', **kwargs,
        )
        # Tests point TRANZILA_BASE_URL at a dead local port; the path is the terminal's.
        self.assertEqual(urlparse(url).path, '/cogolive/iframenew.php', url)
        return parse_qs(urlparse(url).query)

    @override_settings(TRANZILA_WALLETS=['bit', 'google_pay'])
    def test_a_store_purchase_offers_the_wallets(self):
        query = self.query(offer_wallets=True)
        self.assertEqual(query['bit_pay'], ['1'])
        self.assertEqual(query['google_pay'], ['1'])

    @override_settings(TRANZILA_WALLETS=['bit', 'google_pay'])
    def test_other_payments_do_not(self):
        query = self.query()
        self.assertNotIn('bit_pay', query)
        self.assertNotIn('google_pay', query)

    @override_settings(TRANZILA_WALLETS=[])
    def test_no_wallet_until_one_is_turned_on(self):
        query = self.query(offer_wallets=True)
        self.assertNotIn('bit_pay', query)
        self.assertNotIn('google_pay', query)

    @override_settings(TRANZILA_WALLETS=['bit', 'apple_pay'])
    def test_apple_pay_has_no_parameter(self):
        self.assertEqual(wallet_params(), {'bit_pay': '1'})
