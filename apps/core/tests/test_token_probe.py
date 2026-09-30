"""The 1 ₪ saved-card test: the page, the report row, one charge, one refund — never the token out."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core import token_probe
from apps.core.models import UserProfile
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import TranzilaTransaction

KEYS = dict(
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive-app-key',
    TRANZILA_SECRET_KEY='cogolive-secret-key',
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb',
    TRANZILA_PROD_PUBLIC_KEY='michal-app-key',
    TRANZILA_PROD_SECRET_KEY='michal-secret-key',
    TRANZILA_HOSTED_PAGE_ENABLED=True,
)
SECRET_TOKEN = 'TOKEN-SHOULD-NEVER-LEAVE'
NK_ROW = {
    'index': '12', 'tranmode': 'NK', 'processor_response_code': '000', 'amount': '100',
    'transaction_date': '2026-09-27', 'transaction_time': '10:00:00',
    'credit_card_token': SECRET_TOKEN, 'expiration_month': '07', 'expiration_year': '29',
}
CHARGED = {'success': True, 'transaction_id': '13', 'confirmation_code': '0001', 'response_code': '000'}
REFUNDED = {'success': True, 'transaction_id': '14', 'confirmation_code': '0002', 'response_code': '000'}


def _found(row=NK_ROW):
    return patch.object(TranzilaService, 'find_transaction', return_value={'success': True, 'transaction': row})


@override_settings(**KEYS)
class ProbeStepsTest(TestCase):
    def test_the_page_is_the_hosted_page_in_a_mode_that_saves_the_card(self):
        with patch.object(TranzilaService, 'create_handshake_token', return_value='thtk-1'):
            page = token_probe.open_page(tranmode='NK')
        self.assertEqual(page['terminal'], 'cogolive')
        self.assertIn('/cogolive/iframenew.php?', page['url'])
        self.assertIn('tranmode=NK', page['url'])
        self.assertIn('sum=1.0', page['url'])
        with self.assertRaises(token_probe.ProbeError):
            token_probe.open_page(tranmode='A')

    def test_the_report_rows_say_whether_a_token_came_back_but_never_show_it(self):
        rows = [
            {**NK_ROW, 'index': '12'},
            {**NK_ROW, 'index': '11', 'credit_card_token': ''},
            {**NK_ROW, 'index': '10', 'tranmode': 'A'},
            {**NK_ROW, 'index': '9', 'tranmode': 'AK'},
        ]
        with patch.object(TranzilaService, 'list_all_transactions', return_value={'success': True, 'transactions': rows}):
            found = token_probe.find_rows()
        self.assertEqual([r['index'] for r in found['rows']], ['12', '11'])
        self.assertEqual([r['has_token'] for r in found['rows']], [True, False])
        self.assertTrue(found['rows'][0]['has_expiry'])
        self.assertNotIn(SECRET_TOKEN, json.dumps(found))

    def test_the_charge_goes_to_cogolivetok_with_cogolive_keys_once(self):
        calls = []

        def fake_charge(service, **kwargs):
            calls.append((service.token_terminal, service.public_key, kwargs))
            return dict(CHARGED)

        with _found(), patch.object(TranzilaService, 'charge_with_token', autospec=True, side_effect=fake_charge):
            result = token_probe.charge(index='12', terminal='cogolivetok')
            with self.assertRaises(token_probe.ProbeError):
                token_probe.charge(index='12', terminal='cogolivetok')

        self.assertEqual(result['outcome'], 'charged')
        self.assertEqual(len(calls), 1)
        terminal, key, kwargs = calls[0]
        self.assertEqual((terminal, key), ('cogolivetok', 'cogolive-app-key'))
        self.assertEqual(kwargs['token'], SECRET_TOKEN)
        self.assertEqual((kwargs['expire_month'], kwargs['expire_year']), (7, 29))
        self.assertEqual(str(kwargs['amount']), '1.00')
        self.assertNotIn(SECRET_TOKEN, json.dumps(result))
        self.assertTrue(TranzilaTransaction.objects.get(idempotency_key='token_probe_charge_12_cogolivetok').is_successful)

    def test_the_check_row_as_the_report_really_shows_it_is_charged(self):
        # An NK page comes back as tranmode 'N', J2, approval 0000000 (29.9.2026).
        row = {**NK_ROW, 'tranmode': 'N', 'txn_type': 'J2', 'authorization_number': '0000000'}
        with patch.object(TranzilaService, 'list_all_transactions', return_value={'success': True, 'transactions': [row]}):
            found = token_probe.find_rows()
        self.assertEqual(found['rows'][0]['tranmode'], 'N')
        self.assertIn('credit_card_token', found['rows'][0]['fields'])
        self.assertNotIn(SECRET_TOKEN, json.dumps(found))
        with _found(row), patch.object(TranzilaService, 'charge_with_token', return_value=dict(CHARGED)) as charge:
            result = token_probe.charge(index='12', terminal='cogolivetok')
        self.assertEqual(result['outcome'], 'charged')
        self.assertEqual(charge.call_args.kwargs['token'], SECRET_TOKEN)

    def test_only_the_hosted_pair_and_only_a_row_that_saved_a_card(self):
        with _found(), self.assertRaises(token_probe.ProbeError):
            token_probe.charge(index='12', terminal='fxpmichalwebtok')
        with _found({**NK_ROW, 'tranmode': 'A'}), self.assertRaises(token_probe.ProbeError):
            token_probe.charge(index='12', terminal='cogolivetok')
        with _found({**NK_ROW, 'credit_card_token': ''}), self.assertRaises(token_probe.ProbeError):
            token_probe.charge(index='12', terminal='cogolivetok')

    def test_a_refused_charge_is_kept_as_the_answer(self):
        refused = {'success': False, 'error': 'Invalid card', 'response_code': '20004',
                   'message': 'Charge failed: Invalid card'}
        with _found(), patch.object(TranzilaService, 'charge_with_token', return_value=refused):
            result = token_probe.charge(index='12', terminal='cogolivetok')
        self.assertEqual(result['outcome'], 'refused')
        self.assertEqual(result['response_code'], '20004')

    def test_the_refund_is_a_credit_on_the_same_terminal_once(self):
        TranzilaTransaction.objects.create(
            transaction_id='13', confirmation_code='0001', transaction_type='charge', response_code='000',
            response_message='', request_data={}, response_data={},
            idempotency_key='token_probe_charge_12_cogolivetok', is_successful=True,
        )
        with _found(), patch.object(TranzilaService, 'refund_transaction', return_value=dict(REFUNDED)) as refund:
            result = token_probe.refund(index='12', terminal='cogolivetok')
            with self.assertRaises(token_probe.ProbeError):
                token_probe.refund(index='12', terminal='cogolivetok')
        self.assertTrue(result['refunded'])
        self.assertEqual(refund.call_count, 1)
        kwargs = refund.call_args.kwargs
        self.assertEqual(kwargs['transaction_id'], '13')
        self.assertEqual(kwargs['terminal_name'], 'cogolivetok')
        self.assertFalse(kwargs['prefer_cancel'])

    def test_a_refused_refund_can_be_tried_again(self):
        TranzilaTransaction.objects.create(
            transaction_id='13', confirmation_code='0001', transaction_type='charge', response_code='000',
            response_message='', request_data={}, response_data={},
            idempotency_key='token_probe_charge_12_cogolivetok', is_successful=True,
        )
        refused = {'success': False, 'error': 'not yet', 'response_code': '23001'}
        with _found(), patch.object(TranzilaService, 'refund_transaction', side_effect=[refused, dict(REFUNDED)]):
            first = token_probe.refund(index='12', terminal='cogolivetok')
            second = token_probe.refund(index='12', terminal='cogolivetok')
        self.assertFalse(first['refunded'])
        self.assertTrue(second['refunded'])

    def test_no_refund_without_a_probe_charge(self):
        with _found(), self.assertRaises(token_probe.ProbeError):
            token_probe.refund(index='12', terminal='cogolivetok')


@override_settings(**KEYS)
class ProbeEndpointTest(TestCase):
    URL = '/api/v1/core/tranzila/token-probe/'

    def _client(self, role):
        user = get_user_model().objects.create_user(username=f'{role}@t.co', email=f'{role}@t.co', password='x')
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_managers_only(self):
        client = self._client(UserProfile.ROLE_WORKER)
        self.assertEqual(client.post(self.URL, {'step': 'find'}, format='json').status_code, 403)

    def test_a_manager_can_look_and_an_unknown_step_is_refused(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        with patch.object(TranzilaService, 'list_all_transactions', return_value={'success': True, 'transactions': [NK_ROW]}):
            res = client.post(self.URL, {'step': 'find'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['rows'][0]['index'], '12')
        self.assertNotIn(SECRET_TOKEN, res.content.decode())
        self.assertEqual(client.post(self.URL, {'step': 'boom'}, format='json').status_code, 400)
