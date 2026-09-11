"""Signing leads to the card step: with billing on, the signing page's answer sends the tenant
to their card page; with it off, or when anything there fails, the page is done and the
contract stays signed. Signed through the real signing endpoint (phase 3)."""
from unittest.mock import patch

from django.test import override_settings

from apps.rental_billing.links import rotate_card_link
from apps.rental_billing.models import TenantCardLink, TenantStandingOrder
from apps.rental_billing.orders import open_standing_order
from apps.rental_billing.tests.factories import mocked_gateway, patch_tranzila
from apps.rentals.tests.test_signing import FRONTEND, SigningTestCase

Order = TenantStandingOrder
Link = TenantCardLink


class AfterSigningTests(SigningTestCase):
    def setUp(self):
        super().setUp()
        self.gateway = mocked_gateway()
        patch_tranzila(self, self.gateway)
        self.send_link()

    def card_token(self, res) -> str:
        url = res.data['card_url']
        self.assertTrue(url.startswith(f'{FRONTEND}/rc/'), url)
        return url.rsplit('/', 1)[1]

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_with_billing_on_the_page_goes_on_to_the_card(self):
        res = self.sign()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['next'], 'card')
        token = self.card_token(res)
        order = Order.objects.get(tenancy=self.tenancy)
        self.assertEqual((order.source, order.status), (Order.SOURCE_SIGNING, Order.STATUS_PENDING_CARD))
        self.assertEqual(Link.objects.get(token=token).standing_order, order)

        # The link works: the card page opens on it, and the contract it asks for is signed.
        page = self.public.get(f'/api/v1/rental-billing/card/{token}/')
        self.assertEqual(page.status_code, 200, page.data)
        self.assertNotIn('error', page.data)
        self.assertEqual(page.data['state'], 'pending_card')
        self.assertEqual(page.data['monthly_total'], '1456.78')
        # Nothing was charged or verified on the way.
        self.assertEqual(self.gateway.mock_calls, [])

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_an_open_order_is_reused_and_its_link_rotated(self):
        existing = open_standing_order(self.tenancy)
        old = rotate_card_link(existing)

        res = self.sign()

        self.assertEqual(res.data['next'], 'card')
        self.assertEqual(Order.objects.filter(tenancy=self.tenancy).count(), 1)
        self.assertEqual(Link.objects.get(token=self.card_token(res)).standing_order, existing)
        old.refresh_from_db()
        self.assertEqual(old.status, Link.STATUS_CANCELLED)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_an_order_that_already_has_a_card_needs_no_card_step(self):
        open_standing_order(self.tenancy)
        Order.objects.filter(tenancy=self.tenancy).update(
            status=Order.STATUS_ACTIVE, tranzila_token='tok', card_expire_month=12, card_expire_year=2030,
        )
        res = self.sign()
        self.assertEqual(res.data['next'], 'done')
        self.assertFalse(Link.objects.exists())

    @override_settings(RENTAL_BILLING_ENABLED=False)
    def test_with_billing_off_the_page_is_done(self):
        res = self.sign()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['next'], 'done')
        self.assertNotIn('card_url', res.data)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Link.objects.exists())
        self.assertEqual(self.fresh().status, 'signed')

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_failure_opening_the_order_never_fails_the_signing(self):
        with patch('apps.rental_billing.orders.open_standing_order', side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.rentals.signing', 'ERROR') as logs:
                res = self.sign()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['next'], 'done')
        self.assertEqual(self.fresh().status, 'signed')
        self.assertIsNotNone(self.fresh().signature_id)
        self.assertIn(str(self.contract.pk), logs.output[0])
        self.assertFalse(Order.objects.exists())

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_failure_issuing_the_link_leaves_no_half_opened_order(self):
        with patch('apps.rental_billing.links.rotate_card_link', side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.rentals.signing', 'ERROR'):
                res = self.sign()
        self.assertEqual((res.status_code, res.data['next']), (200, 'done'))
        self.assertEqual(self.fresh().status, 'signed')
        # The order and its link are opened together or not at all.
        self.assertFalse(Order.objects.exists())

    @override_settings(RENTAL_BILLING_ENABLED=True, RENTAL_BILLING_BUSINESS_NAME='אין עסק כזה')
    def test_with_the_business_missing_the_page_is_done(self):
        with self.assertLogs('apps.rentals.signing', 'WARNING'):
            res = self.sign()
        self.assertEqual(res.data['next'], 'done')
        self.assertFalse(Order.objects.exists())
