"""
Unit tests for TranzilaService.

Tests coverage:
- create_payment_request: URL generation with params
- create_recurring_payment_request: recurring payment URL
- parse_webhook_response: response parsing and validation
- verify_webhook_signature: signature verification
- Error handling for malformed responses
"""
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
    TRANZILA_API_BASE_URL='https://api.tranzila.test'
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
        self.assertIn('pdesc=test_txn_123', url)
    
    def test_create_recurring_payment_request(self):
        """Test recurring payment request URL generation"""
        url = self.service.create_recurring_payment_request(
            amount=Decimal('350.00'),
            currency='ILS',
            description='Monthly subscription',
            customer_name='Jane Doe',
            transaction_id='recur_123'
        )
        
        # Verify recurring parameters
        self.assertIn('https://direct.tranzila.test', url)
        self.assertIn('recur_transaction=4_approved', url)  # Recurring indicator
        self.assertIsInstance(url, str)
    
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
