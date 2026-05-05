"""
Tranzila Integration Service

This service handles all communication with the Tranzila payment gateway API.
It provides methods for creating payment requests, managing recurring payments,
and processing webhook callbacks.

Reference: https://docs.tranzila.com/docs/payments-billing/4oeojzoc0teuf-create-payment-requests
"""
import hashlib
import hmac
import json
import logging
import requests
import secrets
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class TranzilaService:
    """
    Service for interacting with Tranzila payment gateway.
    
    Handles:
    - Payment request generation (iframe URLs)
    - Token-based charges (REST API v1)
    - Refunds and cancellations
    - Webhook signature verification
    - Response parsing
    """
    
    def __init__(self):
        """Initialize Tranzila service with configuration from Django settings."""
        self.terminal = getattr(settings, 'TRANZILA_TERMINAL', '')
        self.token_terminal = getattr(settings, 'TRANZILA_TOKEN_TERMINAL', self.terminal)
        self.supplier = getattr(settings, 'TRANZILA_SUPPLIER', '')
        self.public_key = getattr(settings, 'TRANZILA_PUBLIC_KEY', '')
        self.secret_key = getattr(settings, 'TRANZILA_SECRET_KEY', '')
        self.webhook_secret = getattr(settings, 'TRANZILA_WEBHOOK_SECRET', '')
        
        # API endpoints
        self.api_base_url = getattr(settings, 'TRANZILA_API_BASE_URL', 'https://api.tranzila.com')
        self.iframe_base_url = getattr(settings, 'TRANZILA_BASE_URL', 'https://direct.tranzila.com')
        self.environment = getattr(settings, 'TRANZILA_ENVIRONMENT', 'development')
        
        if not self.terminal:
            logger.warning("TRANZILA_TERMINAL not configured")
        if not self.public_key:
            logger.warning("TRANZILA_PUBLIC_KEY not configured - REST API calls will fail")
        if not self.secret_key:
            logger.warning("TRANZILA_SECRET_KEY not configured - REST API calls will fail")
    
    # ============================================================================
    # Logging Utilities
    # ============================================================================
    
    def _log_api_call(self, operation: str, **kwargs):
        """Centralized logging for API operations."""
        log_parts = [f"[{operation}]"]
        for key, value in kwargs.items():
            if 'token' in key.lower() and value:
                # Mask sensitive data
                value = f"{value[:10]}..." if len(value) > 10 else "***"
            log_parts.append(f"{key}={value}")
        logger.info(" ".join(log_parts))
    
    def _build_error_response(self, error: str, code: str = '999', message: str = None) -> Dict:
        """Build standardized error response."""
        return {
            'success': False,
            'error': error,
            'response_code': code,
            'message': message or f'Operation failed: {error}'
        }
    
    def _build_success_response(self, **kwargs) -> Dict:
        """Build standardized success response."""
        response = {'success': True}
        response.update(kwargs)
        return response
    
    # ============================================================================
    # Iframe Payment Methods
    # ============================================================================
    
    def _build_payment_params(
        self,
        amount: Decimal,
        currency: str = 'ILS',
        description: str = '',
        customer_name: str = '',
        customer_email: str = '',
        customer_phone: str = '',
        success_url: str = '',
        error_url: str = '',
        callback_url: str = '',
        transaction_id: str = '',
        **extra_params
    ) -> Dict:
        """Build payment parameters dict for Tranzila iframe."""
        params = {
            'supplier': self.terminal,
            'sum': float(amount),
            'currency': self._get_currency_code(currency),
            'tranmode': 'A',
            'trBgColor': 'ffffff',
            'trTextColor': '000000',
            'buttonColor': '4CAF50',
            'company': 'cogomelo',
            'country': 'Israel',
            'zip': '0000000',
            'address': 'N/A',
            'city': 'N/A'
        }
        
        if description:
            params['remarks'] = description
        if customer_name:
            params['contact'] = customer_name
        if customer_email:
            params['email'] = customer_email
        if customer_phone:
            params['phone'] = customer_phone
        if success_url and 'localhost' not in success_url:
            params['success_url_address'] = success_url
        if error_url and 'localhost' not in error_url:
            params['fail_url_address'] = error_url
        if callback_url:
            params['notify_url_address'] = callback_url
        if transaction_id:
            params['cred_type'] = '1'
            params['pdesc'] = transaction_id
        
        params.update(extra_params)
        return params
    
    def create_payment_request(
        self,
        amount: Decimal,
        currency: str = 'ILS',
        description: str = '',
        customer_name: str = '',
        customer_email: str = '',
        customer_phone: str = '',
        success_url: str = '',
        error_url: str = '',
        callback_url: str = '',
        transaction_id: str = '',
        **extra_params
    ) -> str:
        """Create iframe payment URL for one-time payment."""
        params = self._build_payment_params(
            amount=amount,
            currency=currency,
            description=description,
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            success_url=success_url,
            error_url=error_url,
            callback_url=callback_url,
            transaction_id=transaction_id,
            **extra_params
        )
        
        query_string = urlencode(params)
        payment_url = f"{self.iframe_base_url.rstrip('/')}/{self.terminal}/iframenew.php?{query_string}"
        
        self._log_api_call("CREATE_PAYMENT", amount=amount, currency=currency)
        return payment_url
    
    def create_recurring_payment_request(
        self,
        amount: Decimal,
        currency: str = 'ILS',
        description: str = '',
        customer_name: str = '',
        customer_email: str = '',
        customer_phone: str = '',
        success_url: str = '',
        error_url: str = '',
        callback_url: str = '',
        transaction_id: str = '',
        recurring_frequency: str = 'monthly',
        recur_payments: Optional[int] = None,
        recur_start_date: Optional[str] = None,
        customer_choice: bool = True,
        z_field: Optional[str] = None,
        **extra_params
    ) -> Tuple[str, Dict]:
        """Create iframe payment URL for recurring payment setup."""
        params = self._build_payment_params(
            amount=amount,
            currency=currency,
            description=description,
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            success_url=success_url,
            error_url=error_url,
            callback_url=callback_url,
            transaction_id=transaction_id,
        )
        
        recurring_params = {'recur_transaction': '4_approved'}
        
        if recur_payments is not None:
            recurring_params['recur_payments'] = str(recur_payments)
        if recur_start_date:
            recurring_params['recur_start_date'] = recur_start_date
        if z_field:
            if not z_field.isdigit() or len(z_field) > 8:
                logger.warning(f"Invalid Z_field value: {z_field}. Must be numeric and max 8 digits.")
            else:
                recurring_params['Z_field'] = z_field
        
        params.update(recurring_params)
        params.update(extra_params)
        
        query_string = urlencode(params)
        full_url = f"{self.iframe_base_url.rstrip('/')}/{self.terminal}/iframenew.php?{query_string}"
        
        self._log_api_call(
            "CREATE_RECURRING",
            amount=amount,
            frequency=recurring_frequency,
            payments=recur_payments or 'unlimited'
        )
        
        return full_url
    
    # ============================================================================
    # REST API v1 Methods (Token Charges, Refunds, Cancellations)
    # ============================================================================
    
    def charge_with_token(
        self,
        token: str,
        amount: Decimal,
        description: str = '',
        transaction_id: str = '',
        items: list = None,
        expire_month: int = None,
        expire_year: int = None
    ) -> Dict:
        """Charge a stored token using REST API v1."""
        if not token:
            logger.error("Cannot charge: No token provided")
            return self._build_error_response('No Tranzila token available')
        
        if not self.public_key or not self.secret_key:
            logger.error("REST API credentials not configured")
            return self._build_error_response('REST API credentials not configured')
        
        if not items:
            items = [{
                'name': description or 'Store Purchase',
                'type': 'I',
                'unit_price': float(amount),
                'units_number': 1
            }]
                
        payload = {
            'terminal_name': self.token_terminal,
            'txn_type': 'debit',
            'expire_month': expire_month,
            'expire_year': expire_year,
            'card_number': token,
            'items': items
        }
        
        self._log_api_call("CHARGE_TOKEN", amount=amount, token=token)
        
        try:
            response = self._make_api_request(
                params=payload,
                endpoint='/v1/transaction/credit_card/create'
            )
            
            error_code = response.get('error_code')
            
            if error_code == 0:
                transaction_result = response.get('transaction_result', {})
                return self._build_success_response(
                    transaction_id=str(transaction_result.get('transaction_id', '')),
                    confirmation_code=transaction_result.get('ConfirmationCode', transaction_result.get('auth_number', '')),
                    amount=float(amount),
                    response_code=transaction_result.get('processor_response_code', '000'),
                    message=response.get('message', 'Charge successful'),
                    raw_response=response
                )
            else:
                error_msg = response.get('message', 'Unknown error')
                logger.error(f"Charge failed: {error_code} - {error_msg}")
                return self._build_error_response(
                    error_msg,
                    str(error_code) if error_code is not None else 'N/A',
                    f'Charge failed: {error_msg}'
                )
                
        except Exception as e:
            logger.error(f"Exception during token charge: {str(e)}", exc_info=True)
            return self._build_error_response(str(e), message='Charge failed - exception')
    
    def refund_transaction(
        self,
        transaction_id: str,
        authorization_number: str,
        card_expire_month: int = None,
        card_expire_year: int = None,
        token: str = None,
        amount: Optional[Decimal] = None,
        currency: str = 'ILS',
        reason: str = '',
        items: list = None
    ) -> Dict:
        """Refund a transaction using REST API v1."""
        if not transaction_id:
            logger.error("Cannot refund: No transaction ID provided")
            return self._build_error_response('No transaction ID available')
        
        if not authorization_number:
            logger.error("Cannot refund: No authorization number provided")
            return self._build_error_response('No authorization number available')
        
        if not card_expire_month or not card_expire_year:
            logger.error("Cannot refund: Missing card expiration date")
            return self._build_error_response('Card expiration date required')
        
        if not items:
            items = [{
                'name': reason or 'Refund',
                'type': 'I',
                'unit_price': float(amount) if amount else 0.0,
                'units_number': 1
            }]
        
        payload = {
            'terminal_name': self.token_terminal,
            'txn_type': 'credit',
            'reference_txn_id': int(transaction_id),
            'authorization_number': authorization_number,
            'expire_month': card_expire_month,
            'expire_year': card_expire_year,
            'card_number': token,
            'items': items,
            'remarks': reason if reason else 'Refund'
        }
        
        self._log_api_call("REFUND", txn_id=transaction_id, auth=authorization_number, amount=amount)
        
        try:
            response = self._make_api_request(
                params=payload,
                endpoint='/v1/transaction/credit_card/create'
            )
            
            error_code = response.get('error_code')
            
            if error_code == 0:
                transaction_result = response.get('transaction_result', {})
                logger.info(f"Refund successful: txn_id={transaction_id}")
                return self._build_success_response(
                    transaction_id=str(transaction_result.get('transaction_id', '')),
                    confirmation_code=transaction_result.get('ConfirmationCode', transaction_result.get('auth_number', '')),
                    response_code=transaction_result.get('processor_response_code', '000'),
                    message='Refund processed successfully',
                    raw_response=response
                )
            else:
                error_msg = response.get('message', 'Unknown error')
                logger.error(f"Refund failed: {error_code} - {error_msg}")
                return self._build_error_response(
                    error_msg,
                    str(error_code),
                    f'Refund failed: {error_msg}'
                )
                
        except Exception as e:
            logger.error(f"Exception during refund: {str(e)}", exc_info=True)
            return self._build_error_response(str(e), message='Refund failed - exception')
    
    def cancel_recurring_payment(
        self,
        transaction_id: str,
        authorization_number: str,
        token: str = None,
        recurring_index: Optional[str] = None
    ) -> Dict:
        """
        Cancel a recurring payment using Tranzila STO API.
        
        TODO: Needs to be implemented with https://api.tranzila.com/v2/sto/update
        as mentioned in Tranzila documentation.
        
        The implementation should use the STO (Secure Token Object) update endpoint
        to update the token status to cancelled/inactive.
        
        Reference: https://api.tranzila.com/v2/sto/update
        
        Expected implementation:
        - Use POST request to /v2/sto/update
        - Include terminal_name, token, and status parameters
        - Handle authentication with API keys
        - Parse response and return success/error
        
        Args:
            transaction_id: Original transaction ID (may not be needed for v2 API)
            authorization_number: Authorization number (may not be needed for v2 API)
            token: Tranzila token/STO identifier
            recurring_index: Recurring payment index
            
        Returns:
            Dict with cancellation result
        """
        if not token:
            logger.error("Cannot cancel: No token provided")
            return {
                **self._build_error_response('No Tranzila token available'),
                'manual_cancellation_required': True
            }
        
        self._log_api_call("CANCEL_RECURRING_NOT_IMPLEMENTED", token=token)
        
        logger.warning(f"Cancel recurring payment not yet implemented. Token: {token[:10]}...")
        
        return {
            **self._build_error_response(
                'Cancel recurring payment needs to be implemented using Tranzila v2 STO API',
                '999',
                'Feature not yet implemented'
            ),
            'manual_cancellation_required': True
        }
    
    # ============================================================================
    # Webhook & Response Processing
    # ============================================================================
    
    def verify_webhook_signature(self, payload: Dict, signature: str) -> bool:
        """Verify the authenticity of a Tranzila webhook callback."""
        if not self.webhook_secret:
            logger.warning("TRANZILA_WEBHOOK_SECRET not configured, skipping verification")
            return True
        
        payload_string = ''.join(str(v) for v in sorted(payload.values()))
        expected_signature = hmac.new(
            self.webhook_secret.encode(),
            payload_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        is_valid = hmac.compare_digest(expected_signature, signature)
        
        if not is_valid:
            logger.warning("Invalid webhook signature received")
        
        return is_valid
    
    def parse_webhook_response(self, payload: Dict) -> Dict:
        """Parse and normalize a Tranzila webhook response."""
        return {
            'transaction_id': payload.get('index', payload.get('TranzilaTK', '')),
            'response_code': payload.get('Response', ''),
            'confirmation_code': payload.get('ConfirmationCode', ''),
            'amount': self._parse_amount(payload.get('sum', '0')),
            'currency': payload.get('currency', 'ILS'),
            'card_last4': payload.get('ccno', '')[-4:] if payload.get('ccno') else '',
            'card_type': payload.get('cardtype', ''),
            'token': payload.get('TranzilaTK', ''),
            'card_expire_month': int(payload.get('expmonth', 0)) if payload.get('expmonth') else None,
            'card_expire_year': int(payload.get('expyear', 0)) if payload.get('expyear') else None,
            'is_successful': payload.get('Response', '') == '000',
            'error_message': payload.get('error', ''),
            'timestamp': timezone.now(),
            'raw_payload': payload,
        }
    
    def get_transaction_status(self, transaction_id: str) -> Dict:
        """Query Tranzila for transaction status."""
        params = {
            'supplier': self.terminal,
            'index': transaction_id,
            'tranmode': 'Q',
        }
        
        self._log_api_call("QUERY_STATUS", transaction_id=transaction_id)
        
        try:
            response = self._make_api_request(params)
            return response
        except Exception as e:
            logger.error(f"Error querying transaction: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'transaction_id': transaction_id,
                'status': 'unknown'
            }
    
    # ============================================================================
    # Helper Methods
    # ============================================================================
    
    def _get_currency_code(self, currency: str) -> str:
        """Convert currency string to Tranzila currency code."""
        currency_map = {
            'ILS': '1',
            'USD': '2',
            'EUR': '3',
            'GBP': '4',
        }
        return currency_map.get(currency.upper(), '1')
    
    def _parse_amount(self, amount_str: str) -> Decimal:
        """Parse amount from Tranzila format (agorot) to Decimal."""
        try:
            amount_agorot = int(amount_str)
            return Decimal(amount_agorot) / 100
        except (ValueError, TypeError):
            return Decimal('0.00')
    
    def _generate_access_token_signature(self, nonce: str, request_time: str) -> str:
        """Generate HMAC signature for X-tranzila-api-access-token header."""
        hmac_key = (self.secret_key + request_time + nonce).encode('utf-8')
        hmac_message = self.public_key.encode('utf-8')
        
        signature = hmac.new(
            key=hmac_key,
            msg=hmac_message,
            digestmod=hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _make_api_request(self, params: Dict, endpoint: str = '/v1/transactions') -> Dict:
        """Make an actual HTTP POST request to Tranzila RESTful API."""
        api_url = f"{self.api_base_url}{endpoint}"
        
        nonce = secrets.token_bytes(40).hex()
        request_time = str(int(time.time()))
        access_token_signature = self._generate_access_token_signature(nonce, request_time)
        
        headers = {
            'X-tranzila-api-access-token': access_token_signature,
            'X-tranzila-api-app-key': self.public_key,
            'X-tranzila-api-nonce': nonce,
            'X-tranzila-api-request-time': request_time,
            'Content-Type': 'application/json',
            'Connection': 'keep-alive',
            'User-Agent': 'Kogomalo-Payment-System/1.0'
        }
        
        try:
            response = requests.post(
                api_url,
                json=params,
                headers=headers,
                timeout=30
            )
            
            if response.status_code not in [200, 201]:
                logger.error(f"Tranzila API error: HTTP {response.status_code}")
                
                if response.status_code == 401:
                    logger.error("Authentication failed - check API keys")
                
                try:
                    error_data = response.json()
                    return self._build_error_response(
                        error_data.get('message', f'HTTP {response.status_code}'),
                        str(error_data.get('code', '999')),
                        error_data.get('message', 'API request failed')
                    )
                except:
                    return self._build_error_response(
                        f'HTTP {response.status_code}',
                        '999',
                        'API request failed'
                    )
            
            # Parse JSON response
            try:
                response_data = response.json()
            except ValueError as e:
                logger.error(f"Failed to parse JSON: {str(e)}")
                return self._build_error_response('Invalid JSON response', '999', 'Invalid response format')
            
            # Return REST API v1 response as-is
            return response_data
            
        except requests.exceptions.Timeout:
            logger.error("Tranzila API request timed out")
            return self._build_error_response('Request timed out', '999', 'Connection timeout')
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Tranzila API connection error: {str(e)}")
            return self._build_error_response(f'Connection error: {str(e)}', '999', 'Cannot connect to payment gateway')
        except Exception as e:
            logger.error(f"Unexpected error in Tranzila API request: {str(e)}", exc_info=True)
            return self._build_error_response(str(e), '999', 'Unexpected error')