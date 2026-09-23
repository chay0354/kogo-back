"""
Tranzila's hosted payment page is off while it runs on the test terminal.

TRANZILA_TERMINAL is 'realtest' — a Tranzila test terminal: "the transactions
are not actually charged" (Tranzila ticket #176819790). Every screen that opened
that page sent a customer to pay nothing: the website store, the till's walk-in
and "secure page" options, and payment links. On 23.9.2026 that was 26 website
orders and 4 till sales the business never collected.

Blocked in one place (TranzilaService.create_payment_request) and refused by
each screen before it writes anything. The flows that charge the business
terminal directly are untouched.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import HOSTED_PAGE_DISABLED_MESSAGE, HostedPageDisabled, TranzilaService
from apps.customers.models import RecurringPayment
from apps.store.models import StoreInvoice, StoreProduct

IFRAME_SETTINGS = dict(
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_BASE_URL='https://direct.tranzila.test',
    TRANZILA_HANDSHAKE_ENABLED=False,
)


@override_settings(**IFRAME_SETTINGS)
class TheHostedPageItself(TestCase):
    def test_is_refused_unless_turned_on(self):
        with self.assertRaises(HostedPageDisabled) as ctx:
            TranzilaService.iframe().create_payment_request(amount=Decimal('10.00'), transaction_id='x')
        self.assertEqual(str(ctx.exception), HOSTED_PAGE_DISABLED_MESSAGE)

    @override_settings(TRANZILA_HOSTED_PAGE_ENABLED=True)
    def test_opens_when_turned_on(self):
        url = TranzilaService.iframe().create_payment_request(amount=Decimal('10.00'), transaction_id='x')
        self.assertIn('/iframe_terminal/iframenew.php', url)


@override_settings(**IFRAME_SETTINGS)
class TheTill(TestCase):
    """store/payment/initiate/ — the till's card button."""

    def setUp(self):
        manager = TestDataFactory.create_user(username='till@test.com', role=UserProfile.ROLE_MANAGER)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')
        self.product = StoreProduct.objects.create(
            name='מכנס קפוארה', category='ביגוד', sale_price=Decimal('90.00'), cost_price=Decimal('30.00'),
            stock_quantity=10, is_active=True,
        )

    def initiate(self, **body):
        payload = {'items': [{'product_id': str(self.product.id), 'quantity': 1}]}
        payload.update(body)
        return self.client.post('/api/v1/store/payment/initiate/', payload, format='json')

    def test_a_walk_in_is_sent_to_direct_card_entry_and_nothing_is_written(self):
        res = self.initiate(customer_info={'name': 'לקוח', 'phone': '0500000000'})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['use_direct_card'])
        self.assertFalse(res.data['success'])
        self.assertFalse(res.data['requires_iframe'])
        self.assertNotIn('iframe_url', res.data)
        self.assertFalse(StoreInvoice.objects.exists())

    def test_a_child_without_a_saved_card_is_sent_to_direct_card_entry(self):
        child = TestDataFactory.create_child(status='active')

        res = self.initiate(child_id=str(child.id))

        self.assertTrue(res.data['use_direct_card'])
        self.assertFalse(StoreInvoice.objects.exists())

    def test_a_child_with_a_saved_card_is_still_charged_on_it(self):
        """The token path charges the business terminal and is not touched."""
        child = TestDataFactory.create_child(status='active')
        RecurringPayment.objects.create(
            child=child, tranzila_token='tok-123', status='active', amount=Decimal('100.00'),
            start_date='2026-09-01', next_billing_date='2026-10-01',
        )
        with patch('apps.core.payment_service.PaymentService.charge_store_with_token',
                   return_value={'success': True}) as charge:
            res = self.initiate(child_id=str(child.id))

        charge.assert_called_once()
        self.assertTrue(res.data['success'])
        self.assertNotIn('use_direct_card', res.data)


@override_settings(
    **IFRAME_SETTINGS,
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='',
    STORE_WEBSITE_CARD_PAYMENTS_ENABLED=True,
)
class TheWebsiteStore(TestCase):
    def setUp(self):
        StoreProduct.objects.create(
            name='תחפושת', category='ביגוד', sale_price=Decimal('159.00'), cost_price=Decimal('50.00'),
            stock_quantity=10, website_legacy_id=7001, is_active=True,
        )

    def test_stays_paused_even_with_card_payments_turned_on(self):
        res = APIClient().post(
            '/api/v1/store/widget/payment/initiate/',
            {
                'website_order_number': 'CG-1', 'idempotency_key': 'k-1',
                'callback_url': 'https://crm.example/cb/', 'customer': {'name': 'x', 'phone': '0500000000'},
                'items': [{'legacy_id': 7001, 'quantity': 1}],
            },
            format='json', HTTP_X_INTEGRATION_KEY='test-key',
        )

        self.assertTrue(res.data['payments_paused'])
        self.assertTrue(res.data['iframe_url'].endswith('/store-closed'))
        self.assertNotIn('iframenew.php', res.data['iframe_url'])
        self.assertFalse(StoreInvoice.objects.exists())


@override_settings(**IFRAME_SETTINGS, CRM_API_BASE_URL='https://api.example.test')
class PaymentLinks(TestCase):
    def test_refuse_before_a_payment_row_is_written(self):
        from apps.core.models import Business
        from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment

        branch = Branch.objects.create(name='Main')
        link = PaymentLink.objects.create(title='מופע', business=Business.objects.create(name='עסק'), branch=branch)
        option = PaymentLinkOption.objects.create(link=link, label='כרטיס', amount=Decimal('50.00'), sort_order=0)

        res = APIClient().post(
            f'/api/v1/payment-links/public/{link.slug}/start/',
            {'option_id': str(option.id), 'payer_name': 'דנה כהן', 'payer_phone': '0501234567'},
            format='json',
        )

        self.assertEqual(res.status_code, 503)
        self.assertFalse(PaymentLinkPayment.objects.exists())
