"""
The manager's read-only "check a transaction against Tranzila" tool.

It must answer from Tranzila's own report, compare it with our record on every
point a real payment has to agree on, and never hand out the card, token or
expiry the report row carries.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import TranzilaService
from apps.store.models import StoreInvoice

URL = '/api/v1/core/tranzila/transaction-check/'
TERMINALS = dict(
    TRANZILA_TERMINAL='cogolive', TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive_pk', TRANZILA_SECRET_KEY='cogolive_sk',
    TRANZILA_PROD_TERMINAL='fxpmichalweb', TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb', TRANZILA_PROD_PUBLIC_KEY='michal_pk', TRANZILA_PROD_SECRET_KEY='michal_sk',
)


def report_row(moment, **overrides):
    local = moment.astimezone(ZoneInfo('Asia/Jerusalem'))
    row = {
        'index': '2', 'amount': '100', 'processor_response_code': '000', 'tranmode': 'A',
        'authorization_number': '0098798',
        'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S'),
        # What must never leave the server:
        'credit_card_token': 'secret-token-4242', 'expiration_month': '07', 'expiration_year': '29',
        'credit_card_owner_id': '123456789',
    }
    row.update(overrides)
    return row


@override_settings(**TERMINALS)
class TranzilaCheckTest(TestCase):
    def setUp(self):
        cache.clear()
        manager = TestDataFactory.create_user(username='m@test.com', role=UserProfile.ROLE_MANAGER)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')
        self.invoice = StoreInvoice.objects.create(
            customer_name='בדיקת cogolive', total_amount=Decimal('1.00'), payment_method='credit_card',
            payment_status='completed', tranzila_transaction_id='2', tranzila_confirmation_code='0098798',
            tranzila_terminal='cogolive',
        )

    def ask(self, params, found):
        seen = []

        def find(service, index):
            seen.append((service.terminal, service.public_key, str(index)))
            return found

        with patch.object(TranzilaService, 'find_transaction', autospec=True, side_effect=find):
            res = self.client.get(URL, params)
        return res, seen

    def test_an_invoice_is_checked_on_its_own_terminal_and_everything_agrees(self):
        res, seen = self.ask({'invoice': self.invoice.invoice_number},
                             {'success': True, 'transaction': report_row(timezone.now())})

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(seen, [('cogolive', 'cogolive_pk', '2')])
        data = res.json()
        self.assertEqual(data['tranzila']['amount'], '1.00')
        self.assertEqual({c['check']: c['ok'] for c in data['comparison']}, {
            'approved': True, 'is_charge': True, 'amount': True, 'approval_number': True, 'after_order': True,
        })

    def test_the_card_token_expiry_and_id_never_leave_the_server(self):
        res, _ = self.ask({'invoice': self.invoice.invoice_number},
                          {'success': True, 'transaction': report_row(timezone.now())})
        body = res.content.decode()
        for secret in ('secret-token-4242', 'expiration', '123456789', 'credit_card'):
            self.assertNotIn(secret, body)

    def test_a_mismatch_is_named(self):
        row = report_row(timezone.now() - timedelta(hours=3), tranmode='N', amount='500',
                         authorization_number='0000001')
        res, _ = self.ask({'invoice': self.invoice.invoice_number}, {'success': True, 'transaction': row})
        checks = {c['check']: c['ok'] for c in res.json()['comparison']}
        self.assertEqual(checks, {
            'approved': True, 'is_charge': False, 'amount': False, 'approval_number': False, 'after_order': False,
        })

    def test_a_number_on_a_michal_terminal_uses_the_michal_keys(self):
        _, seen = self.ask({'terminal': 'fxpmichalweb', 'index': '487573'}, {'success': True, 'transaction': None})
        self.assertEqual(seen, [('fxpmichalweb', 'michal_pk', '487573')])

    def test_tranzila_not_answering_is_said_not_guessed(self):
        res, _ = self.ask({'terminal': 'cogolive', 'index': '2'}, {'success': False, 'error': 'Request timed out'})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()['tranzila']['reachable'])
        self.assertEqual(res.json()['comparison'], [])

    def test_bad_requests_ask_nothing(self):
        for params in ({'terminal': 'realtest', 'index': '2'}, {'terminal': 'cogolive', 'index': 'abc'},
                       {'invoice': 'ST-0000-000000'}):
            res, seen = self.ask(params, {'success': True, 'transaction': None})
            self.assertEqual(res.status_code, 400, params)
            self.assertEqual(seen, [])

    def test_managers_only(self):
        worker = TestDataFactory.create_user(username='w@test.com', role=UserProfile.ROLE_WORKER)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=worker).key}')
        with patch.object(TranzilaService, 'find_transaction') as find:
            res = client.get(URL, {'terminal': 'cogolive', 'index': '2'})
        self.assertEqual(res.status_code, 403)
        find.assert_not_called()
        self.assertEqual(APIClient().get(URL, {'terminal': 'cogolive', 'index': '2'}).status_code, 401)
