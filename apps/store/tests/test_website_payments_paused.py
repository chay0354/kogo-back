"""
Card payment for website store orders is paused (23.9.2026).

The hosted payment page ran on TRANZILA_TERMINAL — 'realtest', which is not a
terminal of the business — so no store order paid online reached the business.
Until a real terminal is connected and tested, the site is told plainly that
card payment is paused, and nothing is written.
"""
import json
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.store.models import StoreInvoice, StoreProduct

URL = '/api/v1/store/widget/payment/initiate/'


@override_settings(
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)
class WebsiteCardPaymentsPaused(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_INTEGRATION_KEY': 'test-key'}
        StoreProduct.objects.create(
            name='תחפושת', category='ביגוד', sale_price=Decimal('159.00'), cost_price=Decimal('50.00'),
            stock_quantity=10, website_legacy_id=5001, is_active=True,
        )

    def payload(self, order='CG-260923-TEST'):
        return {
            'website_order_number': order,
            'idempotency_key': f'idemp-{order}',
            'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
            'success_url': 'https://shop.example/ok',
            'error_url': 'https://shop.example/fail',
            'customer': {'name': 'לקוחה', 'phone': '0500000000'},
            'items': [{'legacy_id': 5001, 'quantity': 2}],
        }

    def post(self, body):
        return self.client.post(URL, json.dumps(body), content_type='application/json', **self.headers)

    def test_is_off_unless_turned_on_and_sends_the_buyer_to_the_closed_page(self):
        res = self.post(self.payload())

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['payments_paused'])
        self.assertTrue(res.data['iframe_url'].endswith('/store-closed'))
        self.assertNotIn('invoice_id', res.data)

    def test_writes_no_order(self):
        self.post(self.payload())

        self.assertFalse(StoreInvoice.objects.exists())

    def test_a_retry_of_an_order_left_pending_gets_no_payment_page(self):
        with override_settings(STORE_WEBSITE_CARD_PAYMENTS_ENABLED=True, TRANZILA_HOSTED_PAGE_ENABLED=True):
            first = self.post(self.payload())
        self.assertEqual(first.status_code, 201, first.data)

        retry = self.post(self.payload())

        self.assertTrue(retry.data['payments_paused'])
        self.assertTrue(retry.data['iframe_url'].endswith('/store-closed'))

    def test_still_refuses_a_caller_without_the_key(self):
        res = self.client.post(URL, json.dumps(self.payload()), content_type='application/json')

        self.assertNotEqual(res.status_code, 200)
        self.assertNotIn('payments_paused', res.data)

    @override_settings(STORE_WEBSITE_CARD_PAYMENTS_ENABLED=True, TRANZILA_HOSTED_PAGE_ENABLED=True)
    def test_opens_the_payment_page_when_turned_on(self):
        res = self.post(self.payload())

        self.assertEqual(res.status_code, 201, res.data)
        self.assertIn('iframe_url', res.data)
