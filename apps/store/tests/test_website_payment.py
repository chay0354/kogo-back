"""B2C website checkout → CRM iframe initiate / webhook failure handling."""
import json
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.payment_service import PaymentService, parse_store_cart_notes
from apps.store.models import StoreInvoice, StoreProduct


@override_settings(
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class WebsitePaymentInitiateTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_INTEGRATION_KEY': 'test-key'}
        self.product = StoreProduct.objects.create(
            name='חולצה',
            category='ביגוד',
            sale_price=Decimal('4.00'),
            cost_price=Decimal('1.00'),
            stock_quantity=10,
            website_legacy_id=4399,
            is_active=True,
        )

    def _payload(self, order_number='CG-260824-WQQ5', **extra):
        body = {
            'website_order_number': order_number,
            'idempotency_key': extra.pop('idempotency_key', f'idemp-{order_number}'),
            'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
            'success_url': 'https://shop.example/checkout/complete?order=' + order_number,
            'error_url': 'https://shop.example/checkout/complete?order=' + order_number,
            'customer': {'name': 'דור סער', 'email': 'doreden8@gmail.com', 'phone': '0500000000'},
            'items': [{'legacy_id': 4399, 'quantity': 1}],
        }
        body.update(extra)
        return body

    def test_initiate_uses_iframe_terminal_not_production_rest_terminal(self):
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 201, res.content)
        url = res.json()['iframe_url']
        self.assertIn('/iframe_terminal/iframenew.php', url)
        self.assertNotIn('prod_rest_terminal', url)

    def test_failed_invoice_with_cart_notes_can_be_retried(self):
        first = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(first.status_code, 201, first.content)
        invoice = StoreInvoice.objects.get(website_order_number='CG-260824-WQQ5')
        cart = invoice.notes
        invoice.payment_status = 'failed'
        invoice.save(update_fields=['payment_status'])

        retry = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(),
            format='json',
            **self.headers,
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'pending')
        self.assertEqual(invoice.notes, cart)
        self.assertIn('/iframe_terminal/iframenew.php', retry.json()['iframe_url'])

    def test_failed_invoice_without_cart_notes_requires_new_order(self):
        invoice = StoreInvoice.objects.create(
            customer_name='דור סער',
            customer_phone='0500000000',
            customer_email='doreden8@gmail.com',
            total_amount=Decimal('4.00'),
            payment_method='credit_card',
            payment_status='failed',
            website_order_number='CG-260824-BROKEN',
            website_idempotency_key='idemp-broken',
            notes='Payment failed: קוד שגיאה 141',
        )
        res = self.client.post(
            '/api/v1/store/widget/payment/initiate/',
            self._payload(order_number='CG-260824-BROKEN', idempotency_key='idemp-broken'),
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 400)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')


@override_settings(WEBSITE_INTEGRATION_URL='', WEBSITE_INTEGRATION_API_KEY='')
class StoreWebhookFailurePreservesCartTest(TestCase):
    def test_failed_webhook_does_not_overwrite_cart_json(self):
        cart = [{'product_id': '11111111-1111-1111-1111-111111111111', 'quantity': 1}]
        invoice = StoreInvoice.objects.create(
            customer_name='דור סער',
            total_amount=Decimal('4.00'),
            payment_method='credit_card',
            payment_status='pending',
            website_order_number='CG-260824-WQQ5',
            notes=json.dumps(cart),
        )
        result = PaymentService().complete_store_purchase_from_webhook(
            invoice_id=str(invoice.id),
            tranzila_response={
                'is_successful': False,
                'response_code': '141',
                'error_message': 'המסוף אינו מורשה לסלוק את סוג הכרטיס',
                'transaction_id': '',
                'confirmation_code': '',
            },
        )
        self.assertFalse(result['success'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertEqual(parse_store_cart_notes(invoice.notes), cart)
