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
import re
import requests
import secrets
import time
import uuid
from datetime import date
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Tranzila notify/callback: success only when Response == "000".
# https://docs.tranzila.com — webhook POST includes Response, sum, pdesc, TranzilaTK, etc.
TRANZILA_RESPONSE_MESSAGES: dict[str, str] = {
    '000': 'אושר',
    '033': 'הכרטיס נדחה',
    '036': 'פג תוקף',
    '039': 'מספר כרטיס שגוי',
    '141': 'המסוף אינו מורשה לסלוק את סוג הכרטיס',
    '800': 'העסקה בוטלה',
    '900': 'אימות 3D Secure נכשל',
    '903': 'חשד להונאה',
    '951': 'שגיאת פרוטוקול',
    '952': 'התשלום לא הושלם',
    '954': 'התשלום נכשל',
    '955': 'שגיאת סטטוס תשלום',
    '959': 'התשלום לא הושלם בהצלחה',
}

TRANZILA_APPROVED_CODES = frozenset({'000', '0'})

# REST /v1/transaction/credit_card/create: application success is error_code 0
# (int or string). Docs: https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
TRANZILA_REST_OK_CODES = frozenset({'0', '000'})

# DCdisable duplicate of an already-approved charge. Tranzila docs: the second
# attempt is not a new decline — "This payment has already been made/paid".
TRANZILA_DUPLICATE_PAID_SNIPPETS = (
    'already been made',
    'already been paid',
    'already been charged',
    'duplicate transaction',
    'double transaction',
    'התשלום כבר בוצע',
    'העסקה כבר בוצעה',
)


def is_tranzila_approved(code) -> bool:
    """True when Tranzila reports the card transaction as approved."""
    return str(code or '').strip() in TRANZILA_APPROVED_CODES


def is_tranzila_rest_ok(error_code) -> bool:
    """True when the REST envelope error_code is success (0 or '0')."""
    if error_code is None or error_code is False:
        return False
    return str(error_code).strip() in TRANZILA_REST_OK_CODES


def is_tranzila_duplicate_paid(response: Optional[Dict]) -> bool:
    """
    True when Tranzila refused a second charge because DCdisable already succeeded.

    https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
    Iframe notify may also send duplicate_transaction_detected=1 with the original
    bank response (https://docs.tranzila.com/docs/payments-and-billing/iframe-integration).
    """
    if not isinstance(response, dict):
        return False
    txn = response.get('transaction_result')
    txn = txn if isinstance(txn, dict) else {}
    for source in (response, txn):
        flag = source.get('duplicate_transaction_detected')
        if flag in (1, '1', True, 'true', 'True'):
            return True
    parts = []
    for source in (response, txn):
        for key in ('message', 'error', 'status_msg', 'reason'):
            val = source.get(key)
            if val:
                parts.append(str(val))
    blob = ' '.join(parts).lower()
    return any(snippet in blob for snippet in TRANZILA_DUPLICATE_PAID_SNIPPETS)


def is_tranzila_uncertain_gateway_error(response: Optional[Dict]) -> bool:
    """Timeouts/connection errors — the card may already have been charged."""
    if not isinstance(response, dict):
        return False
    if response.get('uncertain'):
        return True
    error = str(response.get('error') or response.get('message') or '').lower()
    return any(token in error for token in ('timeout', 'timed out', 'connection error', 'cannot connect'))


def pdesc_for_tranzila(transaction_id: str) -> str:
    """
    Tranzila pdesc must be alphanumeric — UUIDs are sent without hyphens.
    Webhook handlers should use invoice_id_from_pdesc() to decode.
    """
    raw = str(transaction_id or '').strip()
    if not raw:
        return ''
    try:
        return uuid.UUID(raw).hex
    except ValueError:
        return re.sub(r'[^a-zA-Z0-9]', '', raw)[:64]


def invoice_id_from_pdesc(pdesc: str) -> str:
    """Restore a Django UUID string from Tranzila pdesc (hex or standard UUID)."""
    raw = (pdesc or '').strip()
    if not raw:
        return ''
    try:
        if len(raw) == 32 and '-' not in raw:
            return str(uuid.UUID(hex=raw))
        return str(uuid.UUID(raw))
    except ValueError:
        return raw


# Placeholder values shipped in .env.example / local demos. A live terminal must never
# see these, so charge paths refuse them instead of producing a gateway error.
MOCK_CREDENTIAL_VALUES = {
    'mock-terminal', 'mock_terminal',
    'mock-supplier', 'mock_supplier', 'your-tranzila-terminal',
    'your-tranzila-public-key', 'your-tranzila-secret-key', 'mock-key', 'changeme',
}

# Tranzila has spelled the saved-card field differently across API versions.
TOKEN_RESPONSE_KEYS = ('token', 'card_token', 'TranzilaTK', 'tranzila_token', 'ccno_token')


def is_mock_credential(value: str) -> bool:
    return (value or '').strip().lower() in MOCK_CREDENTIAL_VALUES


def extract_card_token(*sources: Optional[Dict]) -> str:
    """First non-empty saved-card token found across Tranzila response dicts."""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in TOKEN_RESPONSE_KEYS:
            token = str(source.get(key) or '').strip()
            if token:
                return token
    return ''


def default_notify_url() -> str:
    """
    Webhook URL handed to Tranzila when a caller does not supply one.

    Without it Tranzila never calls back, so iframe payments would stay pending
    forever. Empty when CRM_API_BASE_URL is unset (local dev).
    """
    base = (getattr(settings, 'CRM_API_BASE_URL', '') or '').strip().rstrip('/')
    if not base or 'localhost' in base or '127.0.0.1' in base:
        return ''
    return f"{base}/api/v1/customers/payments/webhook/"


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
    
    def __init__(
        self,
        terminal: Optional[str] = None,
        token_terminal: Optional[str] = None,
        supplier: Optional[str] = None,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        """
        Initialize Tranzila service with configuration from Django settings.

        Any credential can be overridden per-instance (e.g. to use a different
        terminal/credential set than the default settings-based one) by passing
        it explicitly; omitted kwargs fall back to the existing settings-based
        defaults. Use `TranzilaService.iframe()` for hosted iframe checkout
        and `TranzilaService.production()` for REST token/card charges.
        """
        self.terminal = terminal if terminal is not None else getattr(settings, 'TRANZILA_TERMINAL', '')
        self.token_terminal = (
            token_terminal if token_terminal is not None
            else getattr(settings, 'TRANZILA_TOKEN_TERMINAL', self.terminal)
        )
        self.supplier = supplier if supplier is not None else getattr(settings, 'TRANZILA_SUPPLIER', '')
        self.public_key = public_key if public_key is not None else getattr(settings, 'TRANZILA_PUBLIC_KEY', '')
        self.secret_key = secret_key if secret_key is not None else getattr(settings, 'TRANZILA_SECRET_KEY', '')
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

    @classmethod
    def iframe(cls) -> 'TranzilaService':
        """Hosted iframe checkout (B2C store, in-store fallback, widget iframe).

        Uses TRANZILA_TERMINAL. Do not send iframe charges through production() —
        that REST terminal is not authorized to clear card brands in the iframe
        (Tranzila response 141).
        """
        return cls()

    @classmethod
    def production(cls) -> 'TranzilaService':
        """REST token/card charges against the production Tranzila terminal."""
        return cls(
            terminal=settings.TRANZILA_PROD_TERMINAL,
            token_terminal=settings.TRANZILA_PROD_TOKEN_TERMINAL,
            supplier=settings.TRANZILA_PROD_SUPPLIER,
            public_key=settings.TRANZILA_PROD_PUBLIC_KEY,
            secret_key=settings.TRANZILA_PROD_SECRET_KEY,
        )

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
    # Configuration Guards
    # ============================================================================

    def credential_error(self) -> Optional[str]:
        """
        Reason this instance cannot charge, or None when it can.

        Called before every money movement so a half-configured deploy fails
        with an actionable message instead of an opaque gateway rejection.
        """
        if not self.terminal:
            return 'TRANZILA_TERMINAL not configured'
        if not self.public_key or not self.secret_key:
            return 'REST API credentials not configured'
        for name, value in (
            ('TRANZILA_TERMINAL', self.terminal),
            ('TRANZILA_PUBLIC_KEY', self.public_key),
            ('TRANZILA_SECRET_KEY', self.secret_key),
        ):
            if is_mock_credential(value):
                return f'{name} still holds a placeholder value'
        return None

    def _apply_duplicate_guard(self, payload: Dict, key: str) -> None:
        """
        Add DCdisable so Tranzila rejects a repeat of the same charge for 24h.

        Only sent when the terminal has field 20 configured for it, otherwise the
        value would land in an unrelated user-defined field.
        https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
        """
        if not key:
            return
        if not getattr(settings, 'TRANZILA_DCDISABLE_ENABLED', False):
            return
        payload['DCdisable'] = str(key)[:254]

    def live_readiness(self) -> Dict:
        """
        Structured report of everything that must be true before live keys work.

        Consumed by the `check_tranzila` management command; performs one
        handshake call so terminal-side configuration is verified too.
        """
        checks: list[Dict] = []

        def add(name: str, ok: bool, detail: str, blocking: bool = True):
            checks.append({'name': name, 'ok': ok, 'detail': detail, 'blocking': blocking})

        credential_error = self.credential_error()
        add('credentials', credential_error is None, credential_error or 'terminal + API keys present')
        add(
            'token_terminal',
            bool(self.token_terminal),
            self.token_terminal or 'TRANZILA_TOKEN_TERMINAL missing — monthly billing cannot charge saved cards',
        )
        add(
            'webhook_secret',
            bool(self.webhook_secret),
            'configured' if self.webhook_secret else 'TRANZILA_WEBHOOK_SECRET missing — webhook callbacks are unverified',
        )
        notify_url = default_notify_url()
        add(
            'notify_url',
            bool(notify_url),
            notify_url or 'CRM_API_BASE_URL missing — iframe payments would never be confirmed',
        )
        add(
            'environment',
            self.environment == 'production',
            f"TRANZILA_ENVIRONMENT={self.environment}",
            blocking=False,
        )
        add(
            'billing_terminal',
            bool(getattr(settings, 'TRANZILA_BILLING_TERMINAL', '')),
            'configured' if getattr(settings, 'TRANZILA_BILLING_TERMINAL', '')
            else 'TRANZILA_BILLING_TERMINAL empty — tax documents are skipped',
            blocking=False,
        )

        if credential_error:
            add('handshake', False, 'skipped — credentials incomplete')
        else:
            try:
                thtk = self.create_handshake_token(Decimal('1.00'))
                add('handshake', bool(thtk), 'terminal accepted handshake' if thtk else 'handshake rejected by Tranzila')
            except Exception as exc:
                add('handshake', False, f'handshake raised: {exc}')

        blocking_failures = [c['name'] for c in checks if c['blocking'] and not c['ok']]
        return {
            'ready': not blocking_failures,
            'terminal': self.terminal,
            'environment': self.environment,
            'blocking_failures': blocking_failures,
            'checks': checks,
        }
    
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
            'sum': self._format_iframe_sum(amount),
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
        notify_url = callback_url or default_notify_url()
        if notify_url and 'localhost' not in notify_url and '127.0.0.1' not in notify_url:
            params['notify_url_address'] = notify_url
        elif not notify_url:
            logger.warning(
                "Tranzila iframe built without notify_url_address; set CRM_API_BASE_URL "
                "so payments are confirmed by webhook"
            )
        if transaction_id:
            params['cred_type'] = '1'
            params['pdesc'] = pdesc_for_tranzila(transaction_id)
        
        params.update(extra_params)
        return params

    def _format_iframe_sum(self, amount: Decimal) -> float:
        """Tranzila iframe + handshake sum must match exactly (e.g. 1.98)."""
        return float(amount.quantize(Decimal('0.01')))

    def create_handshake_token(self, amount: Decimal, terminal_name: str | None = None) -> Optional[str]:
        """
        Handshake V2 — required when enabled on the Tranzila terminal.
        Without thtk, iframe payments fail with "Illegal Operation XXXXXX".
        https://docs.tranzila.com/docs/payments-and-billing/handshake-v2
        """
        terminal = terminal_name or self.terminal
        if not terminal or not self.public_key or not self.secret_key:
            logger.warning("Handshake skipped: terminal or API keys missing")
            return None

        payload = {
            'terminal_name': terminal,
            'sum': self._format_iframe_sum(amount),
        }
        result = self._make_api_request(payload, endpoint='/v2/handshake/create')
        if isinstance(result, dict) and result.get('error_code') == 0 and result.get('thtk'):
            self._log_api_call("HANDSHAKE_OK", amount=amount, terminal=terminal)
            return str(result['thtk'])

        logger.error("Tranzila handshake failed: %s", result)
        return None
    
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
        handshake_enabled = getattr(settings, 'TRANZILA_HANDSHAKE_ENABLED', True)
        if handshake_enabled and self.public_key and self.secret_key:
            thtk = self.create_handshake_token(amount)
            if not thtk:
                raise RuntimeError(
                    'Tranzila handshake failed. Check TRANZILA_PUBLIC_KEY / TRANZILA_SECRET_KEY '
                    'and that Handshake is enabled on your terminal.'
                )
            extra_params = {**extra_params, 'thtk': thtk, 'new_process': '1'}

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
        recur_sum: Optional[Decimal] = None,
        recur_payments: Optional[int] = None,
        recur_start_date: Optional[str] = None,
        customer_choice: bool = True,
        z_field: Optional[str] = None,
        **extra_params
    ) -> str:
        """
        Iframe URL that charges the first payment and opens a הוראות קבע.

        `amount` is the initial charge, which normally includes דמי רישום;
        `recur_sum` is the ongoing monthly amount. They differ, so recur_sum must be
        sent — otherwise Tranzila would repeat the setup fee every month.

        tranmode 'AK' means "standard transaction + create token", so the notify
        callback carries a TranzilaTK the CRM can charge later.
        https://docs.tranzila.com/docs/payments-and-billing/iframe-integration

        Who performs the monthly charge depends on TRANZILA_GATEWAY_STANDING_ORDER:
        recur_* parameters are only sent when Tranzila owns the schedule. Sending them
        while apps.customers.recurring_billing also runs would bill the parent twice.
        """
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
        params['tranmode'] = 'AK'

        gateway_managed = getattr(settings, 'TRANZILA_GATEWAY_STANDING_ORDER', False)
        if gateway_managed:
            recurring_params = {'recur_transaction': '4_approved'}
            if recur_sum is not None:
                recurring_params['recur_sum'] = self._format_iframe_sum(Decimal(str(recur_sum)))
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
        params['supplier'] = self.token_terminal

        # The handshake is bound to a terminal + sum. This URL bills through the token
        # terminal, so it needs its own thtk or Tranzila answers "Illegal Operation".
        if getattr(settings, 'TRANZILA_HANDSHAKE_ENABLED', True) and self.public_key and self.secret_key:
            thtk = self.create_handshake_token(amount, terminal_name=self.token_terminal)
            if not thtk:
                raise RuntimeError(
                    'Tranzila handshake failed for the token terminal. Check '
                    'TRANZILA_TOKEN_TERMINAL and that Handshake is enabled on it.'
                )
            params['thtk'] = thtk

        query_string = urlencode(params)
        full_url = f"{self.iframe_base_url.rstrip('/')}/{self.token_terminal}/iframenew.php?{query_string}"

        self._log_api_call(
            "CREATE_RECURRING",
            amount=amount,
            recur_sum=recur_sum,
            frequency=recurring_frequency,
            gateway_managed=gateway_managed,
            payments=recur_payments or 'unlimited'
        )
        
        return full_url
    
    # ============================================================================
    # REST API v1 Methods (Token Charges, Refunds, Cancellations)
    # ============================================================================
    
    def _parse_credit_card_create_response(self, response: Dict, *, amount: Decimal, last4: str = '') -> Dict:
        """
        Normalize POST /v1/transaction/credit_card/create into our success/error dict.

        Approval requires REST error_code 0 *and* processor_response_code 000 when
        present. A DCdisable duplicate of an already-approved charge is success,
        not a new decline — otherwise the widget shows 'failed' after the card
        was actually charged.
        https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
        """
        if not isinstance(response, dict):
            return self._build_error_response('Invalid gateway response')

        local_error = (
            response.get('success') is False
            and 'transaction_result' not in response
            and response.get('error_code') is None
        )
        if local_error:
            out = dict(response)
            out['uncertain'] = is_tranzila_uncertain_gateway_error(response)
            return out

        txn = response.get('transaction_result')
        txn = txn if isinstance(txn, dict) else {}
        processor_code = (
            txn.get('processor_response_code')
            or txn.get('Response')
            or response.get('Response')
        )
        error_code = response.get('error_code')
        duplicate = is_tranzila_duplicate_paid(response)
        processor_ok = (not processor_code) or is_tranzila_approved(processor_code)
        approved = (is_tranzila_rest_ok(error_code) and processor_ok) or (
            duplicate and processor_ok
        )

        if approved:
            if duplicate:
                logger.info(
                    "Tranzila DCdisable duplicate treated as original success (last4=%s txn=%s)",
                    last4, txn.get('transaction_id'),
                )
            return self._build_success_response(
                transaction_id=str(txn.get('transaction_id') or response.get('transaction_id') or ''),
                confirmation_code=txn.get('ConfirmationCode', txn.get('auth_number', '')),
                token=extract_card_token(txn, response),
                amount=float(amount),
                response_code=str(processor_code or '000'),
                message=response.get('message', 'Charge successful'),
                raw_response=response,
                duplicate=duplicate,
            )

        error_msg = response.get('message') or response.get('error') or 'Unknown error'
        logger.error(
            "Card charge declined: error_code=%s processor=%s message=%s last4=%s duplicate=%s",
            error_code, processor_code, error_msg, last4, duplicate,
        )
        return self._build_error_response(
            error_msg,
            str(error_code if error_code is not None else (processor_code or 'N/A')),
            f'Charge failed: {error_msg}',
        )

    def charge_with_token(
        self,
        token: str,
        amount: Decimal,
        description: str = '',
        transaction_id: str = '',
        items: list = None,
        expire_month: int = None,
        expire_year: int = None,
        duplicate_guard_key: str = ''
    ) -> Dict:
        """Charge a stored token using REST API v1."""
        if not token:
            logger.error("Cannot charge: No token provided")
            return self._build_error_response('No Tranzila token available')
        
        credential_error = self.credential_error()
        if credential_error:
            logger.error("Cannot charge token: %s", credential_error)
            return self._build_error_response(credential_error)
        
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
            'txn_currency_code': 'ILS',
            'expire_month': expire_month,
            'expire_year': expire_year,
            'card_number': token,
            'items': items
        }

        self._apply_duplicate_guard(payload, duplicate_guard_key)

        self._log_api_call("CHARGE_TOKEN", amount=amount, token=token)
        
        try:
            response = self._make_api_request(
                params=payload,
                endpoint='/v1/transaction/credit_card/create'
            )
            
            parsed = self._parse_credit_card_create_response(
                response, amount=amount, last4=str(token)[-4:],
            )
            if parsed.get('success') and 'token' not in parsed:
                parsed['token'] = extract_card_token(
                    (response.get('transaction_result') or {}), response,
                )
            return parsed

        except Exception as e:
            logger.error(f"Exception during token charge: {str(e)}", exc_info=True)
            out = self._build_error_response(str(e), message='Charge failed - exception')
            out['uncertain'] = True
            return out
    
    def charge_with_card(
        self,
        card_number: str,
        expiry_month: int,
        expiry_year: int,
        cvv: str,
        card_holder_id: str,
        amount: Decimal,
        description: str = '',
        items: list = None,
        installments: int = 1,
        duplicate_guard_key: str = ''
    ) -> Dict:
        """
        Charge a credit card directly using card details.

        Sending a full card number is only permitted for PCI DSS certified
        integrations; everything else must tokenize through the iframe or hosted
        fields first and charge via charge_with_token.
        https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
        """
        credential_error = self.credential_error()
        if credential_error:
            return self._build_error_response(credential_error)

        if not items:
            items = [{
                'name': description or 'Store Purchase',
                'type': 'I',
                'unit_price': float(amount),
                'units_number': 1
            }]

        payload = {
            'terminal_name': self.terminal,
            'txn_type': 'debit',
            'txn_currency_code': 'ILS',
            'card_number': card_number,
            'expire_month': expiry_month,
            'expire_year': expiry_year,
            'cvv': cvv,
            'items': items,
        }
        # Schema requires exactly 9 digits; an empty string is a 20004 mismatch.
        if card_holder_id and len(str(card_holder_id)) == 9:
            payload['card_holder_id'] = str(card_holder_id)

        if installments and installments > 1:
            payload['payment_plan'] = 8
            payload['installments_number'] = installments

        if description:
            payload['remarks'] = description

        self._apply_duplicate_guard(payload, duplicate_guard_key)

        self._log_api_call("CHARGE_CARD", amount=amount, last4=str(card_number)[-4:])

        try:
            response = self._make_api_request(
                params=payload,
                endpoint='/v1/transaction/credit_card/create'
            )

            return self._parse_credit_card_create_response(
                response, amount=amount, last4=str(card_number)[-4:],
            )

        except Exception as e:
            logger.error(f"Exception during card charge: {str(e)}", exc_info=True)
            out = self._build_error_response(str(e), message='Charge failed - exception')
            out['uncertain'] = True
            return out

    def verify_card(
        self,
        card_number: str,
        expiry_month: int,
        expiry_year: int,
        cvv: str,
        card_holder_id: str = '',
        amount: Decimal = Decimal('1.00'),
        description: str = '',
        duplicate_guard_key: str = ''
    ) -> Dict:
        """
        Validate a card and return a reusable token WITHOUT taking any money (J2).

        txn_type 'verify' with verify_mode 2 only checks the card against the issuer.
        Tranzila's current REST schema uses the correctly spelled `verify_mode`
        and a minimal item (name/type/unit_price/units_number). Extra item fields
        or the old `verfiy_mode` typo return application error 20004.
        https://docs.tranzila.com/docs/payments-and-billing/tranzila-transactions-api-1/create-a-credit-card-transaction
        """
        credential_error = self.credential_error()
        if credential_error:
            return self._build_error_response(credential_error)

        payload = {
            'terminal_name': self.terminal,
            'txn_type': 'verify',
            'verify_mode': 2,
            'txn_currency_code': 'ILS',
            'card_number': card_number,
            'expire_month': expiry_month,
            'expire_year': expiry_year,
            'cvv': cvv,
            'items': [{
                'name': (description or 'אימות כרטיס')[:50],
                'type': 'I',
                'unit_price': float(amount) if amount and amount > 0 else 1.0,
                'units_number': 1,
            }],
        }
        # Schema requires exactly 9 digits; an empty string is a 20004 mismatch.
        if card_holder_id and len(str(card_holder_id)) == 9:
            payload['card_holder_id'] = str(card_holder_id)

        if description:
            payload['remarks'] = description

        self._apply_duplicate_guard(payload, duplicate_guard_key)

        self._log_api_call("VERIFY_CARD", amount=amount, last4=str(card_number)[-4:])

        try:
            response = self._make_api_request(
                params=payload,
                endpoint='/v1/transaction/credit_card/create'
            )

            parsed = self._parse_credit_card_create_response(
                response, amount=Decimal('0.00'), last4=str(card_number)[-4:],
            )
            if parsed.get('success'):
                parsed['amount'] = 0.0
                if not parsed.get('token'):
                    logger.error(
                        "Card verification succeeded but returned no token (last4=%s) — "
                        "terminal %s may not have token creation enabled for verify transactions",
                        str(card_number)[-4:], self.terminal,
                    )
            return parsed

        except Exception as e:
            logger.error(f"Exception during card verification: {str(e)}", exc_info=True)
            out = self._build_error_response(str(e), message='Card verification failed - exception')
            out['uncertain'] = True
            return out

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
        token: str,
    ) -> Dict:
        """
        Cancel a recurring payment (STO) on Tranzila.

        Flow (two steps, per tranzila-main-api.yaml):
        1. POST /stos/get  — look up the STO integer sto_id by the TranzilaTK token
        2. POST /sto/update — set sto_status="inactive" on that sto_id

        Note: /stos/get is a paid Tranzila module. If it returns an error, the
        cancellation cannot proceed automatically and must be done via the
        Tranzila dashboard.

        Args:
            token: TranzilaTK token stored on RecurringPayment.tranzila_token

        Returns:
            Dict with cancellation result
        """
        if not token:
            logger.error("Cannot cancel: No token provided")
            return {
                **self._build_error_response('No Tranzila token available'),
                'manual_cancellation_required': True,
            }

        self._log_api_call("CANCEL_RECURRING_LOOKUP", token=token)

        # Step 1: resolve the integer sto_id from the token
        lookup_response = self._make_api_request(
            params={'terminal_name': self.token_terminal, 'token': token},
            endpoint='/stos/get',
        )

        if lookup_response.get('error_code') != 0:
            msg = lookup_response.get('message', 'STO lookup failed')
            logger.error(f"STO lookup failed for token {token[:10]}...: {msg}")
            return {
                **self._build_error_response(msg, str(lookup_response.get('error_code', '999'))),
                'manual_cancellation_required': True,
            }

        stos = lookup_response.get('stos') or []
        if not stos:
            logger.error(f"No STO found for token {token[:10]}...")
            return {
                **self._build_error_response('No STO found for this token'),
                'manual_cancellation_required': True,
            }

        # Use the first active STO (there should only be one per token)
        active_stos = [s for s in stos if s.get('sto_status') == 'active']
        sto = active_stos[0] if active_stos else stos[0]
        sto_id = sto.get('sto_id')

        if not sto_id:
            logger.error(f"STO found but has no sto_id: {sto}")
            return {
                **self._build_error_response('STO record missing sto_id'),
                'manual_cancellation_required': True,
            }

        self._log_api_call("CANCEL_RECURRING_UPDATE", sto_id=sto_id)

        # Step 2: set the STO to inactive
        update_response = self._make_api_request(
            params={
                'terminal_name': self.token_terminal,
                'sto_id': int(sto_id),
                'sto_status': 'inactive',
            },
            endpoint='/sto/update',
        )

        if update_response.get('error_code') != 0:
            msg = update_response.get('message', 'STO update failed')
            logger.error(f"STO update failed for sto_id={sto_id}: {msg}")
            return self._build_error_response(msg, str(update_response.get('error_code', '999')))

        logger.info(f"STO {sto_id} successfully set to inactive for token {token[:10]}...")
        return self._build_success_response(sto_id=sto_id, sto_status='inactive')

    def _normalize_card_year(self, year: Optional[int]) -> Optional[int]:
        if year is None:
            return None
        year = int(year)
        if year < 100:
            year += 2000
        return year

    def _sto_item(self, *, name: str, amount: Decimal) -> Dict:
        return {
            'name': (name or 'מנוי חודשי')[:80],
            'unit_price': float(Decimal(str(amount)).quantize(Decimal('0.01'))),
            'units_number': 1,
            'type': 'I',
            'currency_code': 'ILS',
            'price_type': 'G',
        }

    def _split_il_phone(self, phone: str) -> tuple[str, str]:
        digits = re.sub(r'\D', '', phone or '')
        if digits.startswith('972'):
            digits = digits[3:]
        if digits.startswith('0') and len(digits) >= 9:
            return digits[:3], digits[3:]
        return '050', digits

    def list_standing_orders(self, token: str) -> Dict:
        """POST /stos/get — standing orders for this card token."""
        if not token:
            return self._build_error_response('No Tranzila token available')
        response = self._make_api_request(
            params={'terminal_name': self.token_terminal, 'token': token},
            endpoint='/stos/get',
        )
        if is_tranzila_rest_ok(response.get('error_code')):
            return self._build_success_response(stos=response.get('stos') or [], raw=response)
        return self._build_error_response(
            response.get('message') or response.get('error') or 'STO lookup failed',
            str(response.get('error_code') or response.get('response_code') or '999'),
            response.get('message') or 'לא נמצאו הוראות קבע בטרנזילה',
        )

    def update_standing_order_amount(self, *, sto_id: int, amount: Decimal, item_name: str) -> Dict:
        """POST /v2/sto/update — replace items so Tranzila recalculates the monthly amount."""
        self._log_api_call('STO_UPDATE_AMOUNT', sto_id=sto_id, amount=str(amount))
        response = self._make_api_request(
            params={
                'terminal_name': self.token_terminal,
                'sto_id': int(sto_id),
                'sto_status': 'active',
                'items': [self._sto_item(name=item_name, amount=amount)],
                'updated_by_user': 'kogo-crm',
                'response_language': 'hebrew',
            },
            endpoint='/v2/sto/update',
        )
        if is_tranzila_rest_ok(response.get('error_code')):
            return self._build_success_response(sto_id=int(sto_id), amount=str(amount), raw=response)
        return self._build_error_response(
            response.get('message') or response.get('error') or 'STO update failed',
            str(response.get('error_code') or '999'),
        )

    def inactivate_standing_order(self, sto_id: int) -> Dict:
        self._log_api_call('STO_INACTIVATE', sto_id=sto_id)
        response = self._make_api_request(
            params={
                'terminal_name': self.token_terminal,
                'sto_id': int(sto_id),
                'sto_status': 'inactive',
                'updated_by_user': 'kogo-crm',
            },
            endpoint='/v2/sto/update',
        )
        if is_tranzila_rest_ok(response.get('error_code')):
            return self._build_success_response(sto_id=int(sto_id), sto_status='inactive')
        return self._build_error_response(
            response.get('message') or response.get('error') or 'STO inactivate failed',
            str(response.get('error_code') or '999'),
        )

    def create_standing_order(
        self,
        *,
        amount: Decimal,
        token: str,
        expire_month: int,
        expire_year: int,
        charge_dom: int,
        first_charge_date: date,
        item_name: str,
        client_name: str,
        client_phone: str = '',
    ) -> Dict:
        """POST /v2/sto/create — new Tranzila standing order on a saved card token."""
        area, number = self._split_il_phone(client_phone)
        expire_year = self._normalize_card_year(expire_year)
        self._log_api_call('STO_CREATE', amount=str(amount), charge_dom=charge_dom)
        response = self._make_api_request(
            params={
                'terminal_name': self.token_terminal,
                'sto_payments_number': 9999,
                'charge_frequency': 'monthly',
                'first_charge_date': first_charge_date.isoformat(),
                'charge_dom': max(1, min(int(charge_dom or 1), 28)),
                'currency_code': 'ILS',
                'items': [self._sto_item(name=item_name, amount=amount)],
                'card': {
                    'token': token,
                    'expire_month': int(expire_month),
                    'expire_year': int(expire_year),
                },
                'client': {
                    'name': (client_name or 'לקוח')[:80],
                    'phone_country_code': '972',
                    'phone_area_code': area,
                    'phone_number': number,
                },
                'created_by_user': 'kogo-crm',
                'response_language': 'hebrew',
            },
            endpoint='/v2/sto/create',
        )
        sto_id = response.get('sto_id')
        if is_tranzila_rest_ok(response.get('error_code')) and sto_id:
            return self._build_success_response(sto_id=int(sto_id), raw=response)
        return self._build_error_response(
            response.get('message') or response.get('error') or 'STO create failed',
            str(response.get('error_code') or '999'),
        )

    def sync_standing_order_to_amount(
        self,
        *,
        token: str,
        amount: Decimal,
        item_name: str,
        expire_month: Optional[int] = None,
        expire_year: Optional[int] = None,
        charge_dom: int = 1,
        first_charge_date: Optional[date] = None,
        client_name: str = '',
        client_phone: str = '',
    ) -> Dict:
        """
        Make Tranzila charge `amount` monthly for this token.

        Updates one existing active STO via V2 items replace; inactivates extras;
        creates an STO if none exist.
        """
        if not token:
            return self._build_error_response('אין טוקן כרטיס בטרנזילה')

        lookup = self.list_standing_orders(token)
        if not lookup.get('success'):
            lookup['manual_cancellation_required'] = True
            return lookup

        active = [
            sto for sto in (lookup.get('stos') or [])
            if str(sto.get('sto_status') or '').lower() in ('', 'active')
        ]
        if not active:
            active = [sto for sto in (lookup.get('stos') or []) if sto.get('sto_id')]

        inactivated = []
        if active:
            keep = active[0]
            sto_id = keep.get('sto_id')
            updated = self.update_standing_order_amount(
                sto_id=int(sto_id),
                amount=amount,
                item_name=item_name,
            )
            if not updated.get('success'):
                return updated
            for extra in active[1:]:
                extra_id = extra.get('sto_id')
                if not extra_id:
                    continue
                inactivated_res = self.inactivate_standing_order(int(extra_id))
                if inactivated_res.get('success'):
                    inactivated.append(int(extra_id))
            return self._build_success_response(
                sto_id=int(sto_id),
                action='updated',
                inactivated=inactivated,
            )

        if not expire_month or not expire_year or not first_charge_date:
            return self._build_error_response(
                'אין הוראת קבע בטרנזילה, וחסר תוקף כרטיס ליצירת הוראה חדשה',
            )
        created = self.create_standing_order(
            amount=amount,
            token=token,
            expire_month=int(expire_month),
            expire_year=int(expire_year),
            charge_dom=charge_dom,
            first_charge_date=first_charge_date,
            item_name=item_name,
            client_name=client_name,
            client_phone=client_phone,
        )
        if not created.get('success'):
            return created
        return self._build_success_response(
            sto_id=created['sto_id'],
            action='created',
            inactivated=[],
        )
    
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
        """
        Parse and normalize a Tranzila notify/callback POST body.

        Success: ``Response == '000'`` (documented across Tranzila iframe + notify callbacks).
        Failure: any other ``Response`` code (e.g. 033 declined, 952 not completed, 954 failed).
        """
        response_code = str(payload.get('Response', '') or '').strip()
        raw_error = (payload.get('error') or payload.get('errormessage') or '').strip()
        mapped_error = TRANZILA_RESPONSE_MESSAGES.get(response_code, '')
        approved = is_tranzila_approved(response_code)
        error_message = raw_error or mapped_error or (
            f'קוד שגיאה {response_code}' if response_code and not approved else ''
        )

        return {
            'transaction_id': payload.get('index', payload.get('TranzilaTK', '')),
            'response_code': response_code,
            'confirmation_code': payload.get('ConfirmationCode', ''),
            'amount': self._parse_amount(payload.get('sum', '0')),
            'currency': payload.get('currency', 'ILS'),
            'card_last4': payload.get('ccno', '')[-4:] if payload.get('ccno') else '',
            'card_type': payload.get('cardtype', ''),
            'token': payload.get('TranzilaTK', ''),
            'card_expire_month': int(payload.get('expmonth', 0)) if payload.get('expmonth') else None,
            'card_expire_year': int(payload.get('expyear', 0)) if payload.get('expyear') else None,
            'is_successful': approved,
            'error_message': error_message,
            'timestamp': timezone.now(),
            'raw_payload': payload,
        }
    
    def get_sto_status(self, token: str) -> Dict:
        """
        Look up a standing order (STO) by its TranzilaTK token.

        Uses POST /stos/get per tranzila-main-api.yaml.
        Returns the list of matching STO records (may be empty).
        Note: /stos/get is a paid Tranzila module.
        """
        self._log_api_call("GET_STO_STATUS", token=token)
        return self._make_api_request(
            params={'terminal_name': self.token_terminal, 'token': token},
            endpoint='/stos/get',
        )
    
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

    def billing_terminal_name(self) -> str:
        """Terminal used for tax documents; falls back to the payment terminal."""
        explicit = (getattr(settings, 'TRANZILA_BILLING_TERMINAL', '') or '').strip()
        return explicit or (self.terminal or '')

    def list_transactions(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        page: Optional[int] = None,
    ) -> Dict:
        """
        Past transactions for this terminal in a date range.

        https://docs.tranzila.com/docs/reports/track-transaction-data/get-transaction-information
        """
        payload = {
            'terminal_name': self.terminal,
            'transaction_start_date': start_date.isoformat(),
            'transaction_end_date': (end_date or start_date).isoformat(),
        }
        if page:
            payload['page'] = page
        return self._make_api_request(params=payload, endpoint='/v1/transactions')

    @staticmethod
    def extract_list_rows(response: Dict, *keys: str) -> list:
        """First array on a Tranzila list payload (transactions, documents, data)."""
        if not isinstance(response, dict):
            return []
        search = keys or ('transactions', 'documents', 'data', 'result', 'rows')
        for key in search:
            value = response.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []

    def list_all_transactions(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        max_pages: int = 20,
    ) -> Dict:
        """Every transaction in the date range, paging 1000 rows at a time."""
        if self.credential_error():
            return {'success': False, 'error': self.credential_error(), 'transactions': []}

        end = end_date or start_date
        collected: list = []
        page = None
        for page_num in range(1, max_pages + 1):
            response = self.list_transactions(start_date, end, page=page)
            if not isinstance(response, dict) or response.get('success') is False:
                error = (
                    (response or {}).get('error')
                    if isinstance(response, dict)
                    else 'Tranzila transaction list failed'
                )
                if collected:
                    logger.error("Tranzila list_transactions page %s failed after partial fetch: %s", page_num, error)
                    break
                return {'success': False, 'error': error, 'transactions': []}
            rows = self.extract_list_rows(response, 'transactions', 'data', 'result', 'rows')
            collected.extend(rows)
            total = response.get('total')
            try:
                total_n = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_n = None
            if len(rows) < 1000:
                break
            if total_n is not None and len(collected) >= total_n:
                break
            page = page_num + 1
        return {'success': True, 'transactions': collected}

    def list_documents(self, start_date: date, end_date: date) -> Dict:
        """
        Formal tax documents (invoices / receipts) for the billing terminal.

        https://docs.tranzila.com/docs/payments-and-billing
        """
        if self.credential_error():
            return {'success': False, 'error': self.credential_error(), 'documents': []}
        terminal = self.billing_terminal_name()
        if not terminal:
            return {'success': False, 'error': 'No billing terminal configured', 'documents': []}

        payload = {
            'terminal_names': [terminal],
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
        }
        response = self._make_billing_request(payload, '/api/documents_db/get_documents')
        if not isinstance(response, dict):
            return {'success': False, 'error': 'Invalid documents response', 'documents': []}

        status_code = response.get('status_code')
        if status_code not in (0, '0', None) and not response.get('success'):
            return {
                'success': False,
                'error': response.get('status_msg') or response.get('error') or response,
                'documents': [],
            }
        documents = self.extract_list_rows(response, 'documents', 'data', 'result')
        return {'success': True, 'documents': documents, 'raw': response}
    
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
            
            try:
                response_data = response.json()
            except ValueError as e:
                response_data = None
                logger.error(f"Failed to parse JSON: {str(e)}")

            if response.status_code not in [200, 201]:
                logger.error(f"Tranzila API error: HTTP {response.status_code}")
                if response.status_code == 401:
                    logger.error("Authentication failed - check API keys")
                # Application JSON (error_code / transaction_result) can arrive on
                # HTTP 4xx — including DCdisable "already paid". Parse it upstream
                # instead of wrapping it as a generic gateway failure.
                if isinstance(response_data, dict) and (
                    'error_code' in response_data or 'transaction_result' in response_data
                ):
                    return response_data
                if isinstance(response_data, dict):
                    return self._build_error_response(
                        response_data.get('message', f'HTTP {response.status_code}'),
                        str(response_data.get('code', response_data.get('error_code', '999'))),
                        response_data.get('message', 'API request failed'),
                    )
                return self._build_error_response(
                    f'HTTP {response.status_code}',
                    '999',
                    'API request failed',
                )

            if not isinstance(response_data, dict):
                return self._build_error_response('Invalid JSON response', '999', 'Invalid response format')
            return response_data

        except requests.exceptions.Timeout:
            logger.error("Tranzila API request timed out")
            out = self._build_error_response('Request timed out', '999', 'Connection timeout')
            out['uncertain'] = True
            return out
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Tranzila API connection error: {str(e)}")
            out = self._build_error_response(
                f'Connection error: {str(e)}', '999', 'Cannot connect to payment gateway',
            )
            out['uncertain'] = True
            return out
        except Exception as e:
            logger.error(f"Unexpected error in Tranzila API request: {str(e)}", exc_info=True)
            return self._build_error_response(str(e), '999', 'Unexpected error')

    def _make_billing_request(self, payload: dict, endpoint: str) -> dict:
        """POST to billing5.tranzila.com — same auth headers as the main API."""
        from django.conf import settings
        billing_base = getattr(settings, 'TRANZILA_BILLING_BASE_URL', 'https://billing5.tranzila.com')
        url = f"{billing_base}{endpoint}"

        nonce = secrets.token_bytes(40).hex()
        request_time = str(int(time.time()))
        access_token_signature = self._generate_access_token_signature(nonce, request_time)

        headers = {
            'X-tranzila-api-access-token': access_token_signature,
            'X-tranzila-api-app-key': self.public_key,
            'X-tranzila-api-nonce': nonce,
            'X-tranzila-api-request-time': request_time,
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            data = response.json()
            if response.status_code not in (200, 201):
                logger.error(f"Tranzila billing API error {response.status_code}: {data}")
                return {'success': False, 'error': data}
            return data
        except Exception as e:
            logger.error(f"Tranzila billing API exception: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    TRANZILA_PAYMENT_METHOD = {
        'credit_card': 1,
        'cash': 5,
        'check': 3,
        'bank_transfer': 2,
    }

    TRANZILA_PDF_PUBLIC_BASE = 'https://my.tranzila.com/api/get_financial_document'

    @classmethod
    def parse_billing_document_response(cls, result: dict) -> dict:
        """Normalize Tranzila billing create_document JSON into a flat dict."""
        if not isinstance(result, dict):
            return {'success': False, 'error': 'invalid response'}

        status_code = result.get('status_code')
        if status_code not in (0, '0', None) and not result.get('success'):
            if status_code is not None and status_code != 0:
                return {
                    'success': False,
                    'error': result.get('status_msg') or result.get('error') or result,
                }

        document = result.get('document') or {}
        doc_id = (
            result.get('doc_id')
            or document.get('id')
            or result.get('document_id')
        )
        retrieval_key = result.get('retrieval_key') or document.get('retrieval_key') or ''
        document_number = result.get('document_number') or document.get('number') or ''

        if doc_id or retrieval_key or result.get('success'):
            pdf_url = result.get('pdf_url') or ''
            if not pdf_url and retrieval_key:
                pdf_url = f'{cls.TRANZILA_PDF_PUBLIC_BASE}/{retrieval_key}'
            return {
                'success': True,
                'doc_id': str(doc_id or ''),
                'retrieval_key': str(retrieval_key or ''),
                'document_number': str(document_number or ''),
                'pdf_url': pdf_url,
            }

        return {'success': False, 'error': result.get('status_msg') or result.get('error') or result}

    def create_formal_document(
        self,
        terminal_name: str,
        document_type: str,
        document_date: str,
        items: list,
        payments: list,
        vat_percent: float = 18.0,
        *,
        client_name: str = '',
        client_email: str = '',
        client_phone: str = '',
        prices_include_vat: bool = True,
        document_language: str = 'heb',
    ) -> dict:
        """
        Issue a formal document (invoice/receipt/credit note) via Tranzila billing API.
        document_type: 'IN' | 'IR' | 'RE' | 'DI'
        """
        payload = {
            'terminal_name': terminal_name,
            'document_type': document_type,
            'document_date': document_date,
            'vat_percent': vat_percent,
            'document_language': document_language,
            'document_currency_code': 'ILS',
            'items': [
                {
                    'name': item.get('name') or item.get('description', 'פריט'),
                    'unit_price': float(item.get('unit_price', item.get('price', 0))),
                    'units_number': float(item.get('units_number', item.get('quantity', 1))),
                    'price_type': 'G' if prices_include_vat else 'N',
                }
                for item in items
            ],
        }
        if client_name:
            payload['client_name'] = client_name
        if client_email:
            payload['client_email'] = client_email
        if client_phone:
            payload['client_phone'] = client_phone

        if payments:
            tranzila_payments = []
            for payment in payments:
                method = payment.get('payment_method')
                if isinstance(method, str):
                    method = self.TRANZILA_PAYMENT_METHOD.get(method, 1)
                tranzila_payments.append({
                    'payment_method': int(method),
                    'amount': float(payment.get('amount', 0)),
                })
            payload['payments'] = tranzila_payments

        result = self._make_billing_request(payload, '/api/documents_db/create_document')
        logger.info(f"Tranzila create_formal_document ({document_type}): {result}")
        return result

    def get_formal_document_pdf(self, terminal_name: str, document_id: str) -> bytes | None:
        """Download official PDF bytes for a Tranzila document."""
        from django.conf import settings
        import secrets
        import time

        billing_base = getattr(settings, 'TRANZILA_BILLING_BASE_URL', 'https://billing5.tranzila.com')
        url = f'{billing_base}/api/documents_db/get_document'

        nonce = secrets.token_bytes(40).hex()
        request_time = str(int(time.time()))
        access_token_signature = self._generate_access_token_signature(nonce, request_time)
        headers = {
            'X-tranzila-api-access-token': access_token_signature,
            'X-tranzila-api-app-key': self.public_key,
            'X-tranzila-api-nonce': nonce,
            'X-tranzila-api-request-time': request_time,
            'Content-Type': 'application/json',
        }
        payload = {
            'terminal_name': terminal_name,
            'document_id': str(document_id),
            'response_language': 'heb',
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            content_type = (response.headers.get('Content-Type') or '').lower()
            if 'application/pdf' in content_type:
                return response.content
            logger.warning(
                'Tranzila get_document did not return PDF for %s: %s',
                document_id,
                response.text[:300],
            )
        except Exception as exc:
            logger.error('Tranzila get_document failed for %s: %s', document_id, exc, exc_info=True)
        return None

    def create_credit_note(
        self,
        terminal_name: str,
        original_doc_id: str,
        credit_amount: float,
        reason: str = '',
    ) -> dict:
        """
        Issue a credit note cancelling an existing Tranzila document.
        Uses update_document with relation_type=2.
        """
        payload = {
            'terminal_name': terminal_name,
            'doc_id': original_doc_id,
            'relation_type': 2,
            'credit_amount': credit_amount,
            'reason': reason,
        }
        result = self._make_billing_request(payload, '/api/documents_db/update_document')
        logger.info(f"Tranzila create_credit_note for doc {original_doc_id}: {result}")
        return result