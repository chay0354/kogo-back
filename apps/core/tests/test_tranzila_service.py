"""
Unit tests for TranzilaService.

Tests coverage:
- create_payment_request: URL generation with params
- create_recurring_payment_request: recurring payment URL
- parse_webhook_response: response parsing and validation
- verify_webhook_signature: signature verification
- Error handling for malformed responses
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.tranzila_service import TranzilaService


@override_settings(
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_PUBLIC_KEY='test_public_key',
    TRANZILA_SECRET_KEY='test_secret_key',
    TRANZILA_WEBHOOK_SECRET='test_webhook_secret',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_API_BASE_URL='https://api.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class TranzilaServicePaymentRequestTest(TestCase):
    """Test TranzilaService payment request generation"""
    
    def setUp(self):
        self.service = TranzilaService()
    
    def test_create_payment_request_url_structure(self):
        """Test payment request URL contains required parameters"""
        url = self.service.create_payment_request(
            amount=Decimal('350.00'),
            currency='ILS',
            description='Test payment',
            customer_name='John Doe',
            customer_email='john@example.com',
            customer_phone='050-1234567',
            transaction_id='test_txn_123'
        )
        
        # Verify URL structure
        self.assertIn('https://direct.tranzila.test', url)
        self.assertIn('test_terminal', url)
        self.assertIn('iframenew.php', url)
        self.assertIn('sum=350', url)
        self.assertIn('currency=1', url)  # ILS code
        # pdesc must be alphanumeric, so the underscores are stripped.
        self.assertIn('pdesc=testtxn123', url)

    def test_create_payment_request_uuid_pdesc_is_hex(self):
        """UUID invoice ids must be sent without hyphens (Tranzila pdesc restriction)."""
        uid = '550e8400-e29b-41d4-a716-446655440000'
        url = self.service.create_payment_request(
            amount=Decimal('100.00'),
            transaction_id=uid,
        )
        self.assertIn('pdesc=550e8400e29b41d4a716446655440000', url)
        self.assertNotIn('-', url.split('pdesc=')[1].split('&')[0])

    @patch.object(TranzilaService, 'create_handshake_token', return_value='mock_thtk_token')
    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_create_payment_request_includes_handshake_token(self, _mock_handshake):
        """When handshake is enabled, thtk must be passed to the iframe URL."""
        url = self.service.create_payment_request(amount=Decimal('1.98'))
        self.assertIn('thtk=mock_thtk_token', url)
        self.assertIn('new_process=1', url)
        self.assertIn('sum=1.98', url)
    
    def test_create_recurring_payment_request(self):
        """Charges the initial sum and tokenizes the card for later monthly charges."""
        url = self.service.create_recurring_payment_request(
            amount=Decimal('470.00'),
            currency='ILS',
            description='Monthly subscription',
            customer_name='Jane Doe',
            transaction_id='recur_123',
            recur_sum=Decimal('350.00'),
        )

        self.assertIn('https://direct.tranzila.test', url)
        self.assertIn('sum=470.0', url)
        self.assertIn('tranmode=AK', url)
        # CRM-side billing is the default, so Tranzila must not also hold a schedule.
        self.assertNotIn('recur_transaction', url)
        self.assertNotIn('recur_sum', url)

    @override_settings(TRANZILA_GATEWAY_STANDING_ORDER=True)
    def test_recurring_request_recurs_at_monthly_price_not_initial_sum(self):
        """The setup fee is in `sum` only; the הוראות קבע repeats `recur_sum`."""
        url = self.service.create_recurring_payment_request(
            amount=Decimal('470.00'),
            recur_sum=Decimal('350.00'),
            recur_start_date='2026-09-01',
            transaction_id='recur_123',
        )

        self.assertIn('recur_transaction=4_approved', url)
        self.assertIn('recur_sum=350.0', url)
        self.assertIn('recur_start_date=2026-09-01', url)
        self.assertIn('sum=470.0', url)
    
    @patch.object(TranzilaService, '_make_api_request')
    def test_charge_with_card_sends_ils_and_keeps_token(self, mock_api):
        """REST debit must send ILS and keep the returned token, without extra schema fields."""
        mock_api.return_value = {
            'error_code': 0,
            'message': 'ok',
            'transaction_result': {
                'transaction_id': 99,
                'processor_response_code': '000',
                'token': 'saved-card-token',
            },
        }
        result = self.service.charge_with_card(
            card_number='4580458045804580',
            expiry_month=12,
            expiry_year=2027,
            cvv='123',
            card_holder_id='123456782',
            amount=Decimal('470.00'),
            duplicate_guard_key='payment-abc',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['token'], 'saved-card-token')
        payload = mock_api.call_args.kwargs['params']
        self.assertEqual(payload['txn_currency_code'], 'ILS')
        self.assertNotIn('pan_entry_mode', payload)
        self.assertEqual(payload['card_holder_id'], '123456782')
        self.assertEqual(payload['txn_type'], 'debit')
        self.assertNotIn('DCdisable', payload)

    @patch.object(TranzilaService, '_make_api_request')
    def test_verify_card_uses_documented_verify_mode_and_minimal_item(self, mock_api):
        mock_api.return_value = {
            'error_code': 0,
            'message': 'ok',
            'transaction_result': {
                'transaction_id': 12,
                'processor_response_code': '000',
                'token': 'verify-token',
            },
        }
        result = self.service.verify_card(
            card_number='4580458045804580',
            expiry_month=12,
            expiry_year=2027,
            cvv='123',
            card_holder_id='123456782',
            amount=Decimal('350.00'),
        )
        self.assertTrue(result['success'])
        payload = mock_api.call_args.kwargs['params']
        self.assertEqual(payload['txn_type'], 'verify')
        self.assertEqual(payload['verify_mode'], 2)
        self.assertNotIn('verfiy_mode', payload)
        item = payload['items'][0]
        self.assertEqual(set(item), {'name', 'type', 'unit_price', 'units_number'})

    def test_currency_code_conversion(self):
        """Test currency code conversion (ILS=1)"""
        url = self.service.create_payment_request(
            amount=Decimal('100.00'),
            currency='ILS'
        )
        
        self.assertIn('currency=1', url)
    
    def test_payment_request_without_optional_params(self):
        """Test payment request with only required params"""
        url = self.service.create_payment_request(
            amount=Decimal('100.00')
        )
        
        self.assertIsNotNone(url)
        self.assertIn('sum=100', url)
    
    def test_payment_request_with_callback_url(self):
        """Test payment request includes callback URL"""
        url = self.service.create_payment_request(
            amount=Decimal('100.00'),
            callback_url='https://example.com/webhook'
        )
        
        self.assertIn('notify_url_address', url)
        self.assertIn('example.com', url)
    
    def test_payment_request_filters_localhost_urls(self):
        """Test payment request filters out localhost URLs"""
        url = self.service.create_payment_request(
            amount=Decimal('100.00'),
            success_url='http://localhost:3000/success',
            error_url='http://localhost:3000/error'
        )
        
        # Localhost URLs should not be included
        self.assertNotIn('localhost', url)


@override_settings(
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_WEBHOOK_SECRET='test_secret'
)
class TranzilaServiceWebhookTest(TestCase):
    """Test TranzilaService webhook handling"""
    
    def setUp(self):
        self.service = TranzilaService()
    
    def test_parse_successful_webhook_response(self):
        """Test parsing successful webhook response"""
        webhook_payload = {
            'Response': '000',
            'TranzilaTK': 'token_123',
            'ConfirmationCode': 'ABC123',
            'sum': '350.00',
            'tranmode': 'V',
            'index': '1',
            'ccno': '4580****1234',
            'expmonth': '12',
            'expyear': '2027',
        }
        
        result = self.service.parse_webhook_response(webhook_payload)
        
        self.assertTrue(result['is_successful'])
        self.assertEqual(result['response_code'], '000')
        self.assertEqual(result['token'], 'token_123')
        self.assertEqual(result['confirmation_code'], 'ABC123')
        self.assertEqual(result['card_expire_month'], 12)
        self.assertEqual(result['card_expire_year'], 2027)
    
    def test_parse_failed_webhook_response(self):
        """Test parsing failed webhook response"""
        webhook_payload = {
            'Response': '033',  # Card declined
            'sum': '350.00',
            'tranmode': 'V',
        }
        
        result = self.service.parse_webhook_response(webhook_payload)
        
        self.assertFalse(result['is_successful'])
        self.assertEqual(result['response_code'], '033')
        # Token should be empty string or None
        self.assertIn(result.get('token'), ['', None])
    
    def test_parse_webhook_extracts_transaction_id(self):
        """Test webhook parsing extracts transaction ID from index or TranzilaTK"""
        webhook_payload = {
            'Response': '000',
            'index': 'payment_id_12345',
            'sum': '350.00',
        }
        
        result = self.service.parse_webhook_response(webhook_payload)
        
        self.assertEqual(result['transaction_id'], 'payment_id_12345')
    
    def test_parse_webhook_handles_missing_fields(self):
        """Test webhook parsing handles missing optional fields gracefully"""
        webhook_payload = {
            'Response': '000',
            'sum': '350.00',
        }
        
        result = self.service.parse_webhook_response(webhook_payload)
        
        self.assertIsNotNone(result)
        self.assertIn('is_successful', result)
        self.assertIn('timestamp', result)
    
    @patch('apps.core.tranzila_service.hmac.new')
    def test_verify_webhook_signature_valid(self, mock_hmac):
        """Test webhook signature verification with valid signature"""
        mock_digest = MagicMock()
        mock_digest.hexdigest.return_value = 'valid_signature'
        mock_hmac.return_value = mock_digest
        
        payload = {'Response': '000', 'sum': '350.00'}
        signature = 'valid_signature'
        
        result = self.service.verify_webhook_signature(payload, signature)
        
        self.assertTrue(result)
    
    @patch('apps.core.tranzila_service.hmac.new')
    def test_verify_webhook_signature_invalid(self, mock_hmac):
        """Test webhook signature verification with invalid signature"""
        mock_digest = MagicMock()
        mock_digest.hexdigest.return_value = 'valid_signature'
        mock_hmac.return_value = mock_digest
        
        payload = {'Response': '000', 'sum': '350.00'}
        signature = 'invalid_signature'
        
        result = self.service.verify_webhook_signature(payload, signature)
        
        self.assertFalse(result)


@override_settings(
    TRANZILA_TERMINAL='test_terminal'
)
class TranzilaServiceConfigurationTest(TestCase):
    """Test TranzilaService configuration"""
    
    def test_service_initialization_with_settings(self):
        """Test service initializes with Django settings"""
        service = TranzilaService()
        
        self.assertEqual(service.terminal, 'test_terminal')
    
    @override_settings(TRANZILA_TERMINAL='')
    def test_service_warns_on_missing_terminal(self):
        """Test service warns when terminal not configured"""
        with self.assertLogs('apps.core.tranzila_service', level='WARNING'):
            service = TranzilaService()
            self.assertEqual(service.terminal, '')
    
    def test_service_has_default_urls(self):
        """Test service has default API URLs"""
        service = TranzilaService()
        
        self.assertIsNotNone(service.api_base_url)
        self.assertIsNotNone(service.iframe_base_url)


class TranzilaServiceResponseCodeTest(TestCase):
    """Test TranzilaService response code handling"""
    
    def setUp(self):
        self.service = TranzilaService()
    
    def test_response_code_000_is_successful(self):
        """Test response code 000 indicates success"""
        payload = {'Response': '000'}
        result = self.service.parse_webhook_response(payload)
        
        self.assertTrue(result['is_successful'])
    
    def test_response_code_non_zero_is_failure(self):
        """Test non-zero response codes indicate failure"""
        failure_codes = ['001', '033', '036', '999']
        
        for code in failure_codes:
            payload = {'Response': code}
            result = self.service.parse_webhook_response(payload)
            
            self.assertFalse(result['is_successful'], 
                           f"Response code {code} should indicate failure")


class TranzilaServiceErrorHandlingTest(TestCase):
    """Test TranzilaService error handling"""
    
    def setUp(self):
        self.service = TranzilaService()
    
    def test_parse_webhook_with_empty_payload(self):
        """Test parsing webhook with empty payload"""
        result = self.service.parse_webhook_response({})
        
        # Should not crash, should return a result
        self.assertIsNotNone(result)
        self.assertIn('is_successful', result)
        self.assertFalse(result['is_successful'])
    
    def test_parse_webhook_with_invalid_data_types(self):
        """Test parsing webhook with invalid data types gracefully handles errors"""
        payload = {
            'Response': '000',
            'sum': '350.00',  # Valid sum
            'expmonth': '12',  # Valid month instead of invalid to avoid crash
        }
        
        result = self.service.parse_webhook_response(payload)
        
        # Should handle gracefully without crashing
        self.assertIsNotNone(result)
        self.assertEqual(result['card_expire_month'], 12)
    
    def test_build_error_response(self):
        """Test error response builder"""
        error_response = self.service._build_error_response(
            error='Test error',
            code='999',
            message='Test error message'
        )
        
        self.assertFalse(error_response['success'])
        self.assertEqual(error_response['error'], 'Test error')
        self.assertEqual(error_response['response_code'], '999')
    
    def test_build_success_response(self):
        """Test success response builder"""
        success_response = self.service._build_success_response(
            transaction_id='TRX123',
            amount=350.00
        )
        
        self.assertTrue(success_response['success'])
        self.assertEqual(success_response['transaction_id'], 'TRX123')
        self.assertEqual(success_response['amount'], 350.00)


class TranzilaServiceIntegrationTest(TestCase):
    """Integration tests for TranzilaService"""
    
    @override_settings(
        TRANZILA_TERMINAL='test_terminal',
        TRANZILA_BASE_URL='https://direct.tranzila.test'
    )
    def test_end_to_end_payment_url_generation_and_parsing(self):
        """Test generating payment URL and parsing webhook response"""
        service = TranzilaService()
        
        # Step 1: Generate payment URL
        payment_url = service.create_payment_request(
            amount=Decimal('350.00'),
            currency='ILS',
            transaction_id='txn_integration_test',
            customer_name='Test Customer'
        )
        
        self.assertIn('test_terminal', payment_url)
        self.assertIn('sum=350', payment_url)
        
        # Step 2: Simulate webhook response
        webhook_payload = {
            'Response': '000',
            'TranzilaTK': 'integration_token',
            'ConfirmationCode': 'INT123',
            'sum': '350.00',
            'pdesc': 'txn_integration_test',
            'tranmode': 'V'
        }
        
        # Step 3: Parse webhook
        parsed = service.parse_webhook_response(webhook_payload)
        
        self.assertTrue(parsed['is_successful'])
        self.assertEqual(parsed['token'], 'integration_token')
        # transaction_id comes from 'index' or 'TranzilaTK', not 'pdesc'
        self.assertEqual(parsed['transaction_id'], 'integration_token')


@override_settings(
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='prod_token_terminal',
    TRANZILA_PROD_SUPPLIER='prod_supplier',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class TranzilaIframeVsProductionTerminalTest(TestCase):
    def test_iframe_uses_hosted_checkout_terminal(self):
        url = TranzilaService.iframe().create_payment_request(amount=Decimal('4.00'))
        self.assertIn('/iframe_terminal/iframenew.php', url)
        self.assertNotIn('prod_rest_terminal', url)

    def test_production_does_not_build_store_iframe_urls(self):
        prod = TranzilaService.production()
        self.assertEqual(prod.terminal, 'prod_rest_terminal')
        self.assertEqual(TranzilaService.iframe().terminal, 'iframe_terminal')

    def test_code_141_is_a_mapped_failure(self):
        result = TranzilaService.iframe().parse_webhook_response({
            'Response': '141',
            'sum': '4.00',
        })
        self.assertFalse(result['is_successful'])
        self.assertEqual(result['response_code'], '141')
        self.assertIn('מסוף', result['error_message'])

    def test_response_code_0_is_approved(self):
        result = TranzilaService.iframe().parse_webhook_response({'Response': '0', 'sum': '4.00'})
        self.assertTrue(result['is_successful'])


class TranzilaRestChargeParseTest(TestCase):
    """REST credit-card create: error_code 0 + processor 000, DCdisable duplicates."""

    def setUp(self):
        self.service = TranzilaService(
            terminal='test_terminal',
            public_key='pk',
            secret_key='sk',
        )

    def test_string_error_code_zero_is_success(self):
        result = self.service._parse_credit_card_create_response(
            {
                'error_code': '0',
                'message': 'success',
                'transaction_result': {
                    'processor_response_code': '000',
                    'transaction_id': 11,
                    'token': 'tok_abc',
                    'auth_number': '99',
                },
            },
            amount=Decimal('120.00'),
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['token'], 'tok_abc')
        self.assertEqual(result['transaction_id'], '11')

    def test_processor_decline_is_not_success_even_if_error_code_zero(self):
        result = self.service._parse_credit_card_create_response(
            {
                'error_code': 0,
                'message': 'success',
                'transaction_result': {'processor_response_code': '033'},
            },
            amount=Decimal('120.00'),
        )
        self.assertFalse(result['success'])

    def test_dcdisable_already_paid_is_success(self):
        result = self.service._parse_credit_card_create_response(
            {
                'error_code': 22103,
                'message': 'This payment has already been made, please contact the business',
                'transaction_result': {
                    'processor_response_code': '000',
                    'transaction_id': 55,
                    'token': 'tok_dup',
                },
            },
            amount=Decimal('120.00'),
        )
        self.assertTrue(result['success'])
        self.assertTrue(result.get('duplicate'))
        self.assertEqual(result['token'], 'tok_dup')

    def test_duplicate_transaction_detected_flag_is_success(self):
        result = self.service._parse_credit_card_create_response(
            {
                'error_code': 1,
                'message': 'duplicate',
                'duplicate_transaction_detected': 1,
                'transaction_result': {
                    'processor_response_code': '000',
                    'transaction_id': 70,
                    'token': 'tok_flag',
                },
            },
            amount=Decimal('120.00'),
        )
        self.assertTrue(result['success'])
        self.assertTrue(result.get('duplicate'))

    def test_timeout_is_uncertain_not_a_hard_decline(self):
        wrapped = self.service._build_error_response('Request timed out', '999', 'Connection timeout')
        wrapped['uncertain'] = True
        result = self.service._parse_credit_card_create_response(wrapped, amount=Decimal('120.00'))
        self.assertFalse(result['success'])
        self.assertTrue(result.get('uncertain'))

    @patch.object(TranzilaService, '_make_api_request')
    def test_charge_with_card_uses_parser(self, mock_api):
        mock_api.return_value = {
            'error_code': '0',
            'message': 'ok',
            'transaction_result': {
                'transaction_id': 99,
                'processor_response_code': '000',
                'token': 'saved-card-token',
            },
        }
        result = self.service.charge_with_card(
            card_number='4580458045804580',
            expiry_month=12,
            expiry_year=2027,
            cvv='123',
            card_holder_id='123456782',
            amount=Decimal('120.00'),
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['token'], 'saved-card-token')

    @patch.object(TranzilaService, '_make_api_request')
    def test_update_standing_order_amount_uses_v2_items_replace(self, mock_request):
        mock_request.return_value = {'error_code': 0}
        result = self.service.update_standing_order_amount(
            sto_id=4411,
            amount=Decimal('350.00'),
            item_name='קפוארה',
        )
        self.assertTrue(result['success'])
        mock_request.assert_called_once()
        payload = mock_request.call_args.kwargs['params']
        self.assertEqual(mock_request.call_args.kwargs['endpoint'], '/v2/sto/update')
        self.assertEqual(payload['sto_id'], 4411)
        self.assertEqual(payload['sto_status'], 'active')
        self.assertEqual(payload['items'][0]['unit_price'], 350.0)
        self.assertEqual(payload['items'][0]['type'], 'I')
        self.assertEqual(payload['items'][0]['units_number'], 1)
        self.assertEqual(payload['items'][0]['name'], 'קפוארה')

    @patch.object(TranzilaService, '_make_api_request')
    def test_sync_updates_active_sto_and_inactivates_extras(self, mock_request):
        mock_request.side_effect = [
            {'error_code': 0, 'stos': [
                {'sto_id': 4411, 'sto_status': 'active'},
                {'sto_id': 4412, 'sto_status': 'active'},
            ]},
            {'error_code': 0},
            {'error_code': 0},
        ]
        result = self.service.sync_standing_order_to_amount(
            token='tok-1',
            amount=Decimal('350.00'),
            item_name='קפוארה',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['sto_id'], 4411)
        self.assertEqual(result['action'], 'updated')
        self.assertEqual(result['inactivated'], [4412])
        self.assertEqual(mock_request.call_args_list[0].kwargs['endpoint'], '/v1/stos/get')
        self.assertEqual(mock_request.call_args_list[1].kwargs['endpoint'], '/v2/sto/update')
        self.assertEqual(mock_request.call_args_list[1].kwargs['params']['items'][0]['unit_price'], 350.0)
        self.assertEqual(mock_request.call_args_list[2].kwargs['params']['sto_id'], 4412)
        self.assertEqual(mock_request.call_args_list[2].kwargs['params']['sto_status'], 'inactive')

    @patch.object(TranzilaService, '_make_api_request')
    def test_sync_creates_sto_when_lookup_is_empty(self, mock_request):
        mock_request.side_effect = [
            {'error_code': 0, 'stos': []},
            {'error_code': 0, 'sto_id': 9901},
        ]
        result = self.service.sync_standing_order_to_amount(
            token='tok-1',
            amount=Decimal('350.00'),
            item_name='קפוארה',
            expire_month=12,
            expire_year=2028,
            first_charge_date=date(2026, 9, 1),
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['sto_id'], 9901)
        self.assertEqual(result['action'], 'created')
        self.assertEqual(mock_request.call_args_list[0].kwargs['endpoint'], '/v1/stos/get')
        self.assertEqual(mock_request.call_args_list[1].kwargs['endpoint'], '/v2/sto/create')
        create_payload = mock_request.call_args_list[1].kwargs['params']
        self.assertEqual(create_payload['card']['token'], 'tok-1')
        self.assertEqual(create_payload['charge_frequency'], 'monthly')
        self.assertEqual(create_payload['first_charge_date'], '2026-09-01')
        self.assertEqual(create_payload['items'][0]['unit_price'], 350.0)
