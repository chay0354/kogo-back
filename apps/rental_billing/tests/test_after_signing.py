"""Signing leads to the card step: with billing on, the signing page's answer sends the tenant
to their card page; with it off, or when anything there fails, the page is done and the
contract stays signed. Signed through the real signing endpoint (phase 3).

The second half is the same answer on a GET: a tenant who closed the page and
came back on the same link is taken on to the card rather than left on a dead
end, and the link they get is the one already out — never a fresh one on every
page load.
"""
from datetime import timedelta
from unittest import mock
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.throttling import ScopedRateThrottle

from apps.rental_billing.links import rotate_card_link
from apps.rental_billing.models import TenantCardLink, TenantStandingOrder
from apps.rental_billing.orders import open_standing_order
from apps.rental_billing.tests.factories import mocked_gateway, patch_tranzila
from apps.rentals import signing
from apps.rentals.tests.factories import make_customer, make_tenancy
from apps.rentals.tests.test_signing import FRONTEND, SigningTestCase, page_url

Order = TenantStandingOrder
Link = TenantCardLink
CARD_PAGE = '/api/v1/rental-billing/card/'


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


class SignedPageNextStepTests(SigningTestCase):
    """A GET of a contract that is already signed: where the tenant goes from here."""

    def setUp(self):
        super().setUp()
        self.gateway = mocked_gateway()
        patch_tranzila(self, self.gateway)
        self.send_link()

    def page(self):
        """The signing page as the tenant reads it now."""
        res = self.public.get(page_url(self.token()))
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def card_page(self, url):
        return self.public.get(f'{CARD_PAGE}{url.rsplit("/", 1)[1]}/')

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_coming_back_to_the_signed_link_leads_to_the_card(self):
        signed = self.sign()
        self.assertEqual(signed.data['next'], 'card')

        page = self.page()
        self.assertEqual(page['state'], 'signed')
        self.assertEqual(page['next'], 'card')
        # The very address the signing handed out, not a second one.
        self.assertEqual(page['card_url'], signed.data['card_url'])
        self.assertTrue(page['card_url'].startswith(f'{FRONTEND}/rc/'), page['card_url'])
        # The signed copy is still on the page the tenant is being moved off.
        self.assertEqual(page['pdf_url'], f'/api/v1/rentals/sign/{self.token()}/pdf/')

        # And the address works.
        card = self.card_page(page['card_url'])
        self.assertEqual(card.status_code, 200, card.data)
        self.assertNotIn('error', card.data)
        self.assertEqual(card.data['state'], 'pending_card')
        # Nothing was charged or verified on the way.
        self.assertEqual(self.gateway.mock_calls, [])

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_live_link_is_reused_and_never_rotated_by_a_page_load(self):
        first = self.sign().data['card_url']
        for _ in range(3):
            self.assertEqual(self.page()['card_url'], first)
        self.assertEqual(Link.objects.count(), 1)
        self.assertEqual(Link.objects.get().status, Link.STATUS_PENDING)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_used_or_cancelled_link_is_replaced_once(self):
        """The tenant is never stuck on a link that no longer opens — and the fresh one is then reused too."""
        url = self.sign().data['card_url']
        for gone in (Link.STATUS_USED, Link.STATUS_CANCELLED):
            with self.subTest(gone=gone):
                live = Link.objects.get(status=Link.STATUS_PENDING)
                before = Link.objects.count()
                Link.objects.filter(pk=live.pk).update(status=gone)

                page = self.page()
                self.assertEqual(page['next'], 'card')
                self.assertNotIn(live.token, page['card_url'])
                fresh = Link.objects.get(status=Link.STATUS_PENDING)
                self.assertEqual(fresh.standing_order_id, live.standing_order_id)
                self.assertEqual(Link.objects.count(), before + 1)

                # Reading it again reuses the fresh one rather than rotating again.
                url = page['card_url']
                self.assertEqual(self.page()['card_url'], url)
                self.assertEqual(Link.objects.count(), before + 1)

    @override_settings(RENTAL_BILLING_ENABLED=False)
    def test_with_billing_off_the_signed_page_is_unchanged(self):
        self.sign()
        page = self.page()
        self.assertEqual((page['state'], page['next']), ('signed', 'done'))
        self.assertNotIn('card_url', page)
        self.assertTrue(page['document'])
        self.assertTrue(page['pdf_url'])
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Link.objects.exists())

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_card_already_on_file_is_done(self):
        self.sign()
        Order.objects.filter(tenancy=self.tenancy).update(
            status=Order.STATUS_ACTIVE, tranzila_token='tok', card_expire_month=12, card_expire_year=2030,
        )
        Link.objects.all().update(status=Link.STATUS_USED)

        page = self.page()
        self.assertEqual(page['next'], 'done')
        self.assertNotIn('card_url', page)
        # No link is issued for an order that needs none.
        self.assertEqual(Link.objects.count(), 1)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_an_order_whose_charge_failed_still_needs_a_card(self):
        """Its stored card is the one that was declined — pending_card and failed are the two that need one."""
        self.sign()
        Link.objects.all().update(status=Link.STATUS_USED)
        Order.objects.filter(tenancy=self.tenancy).update(
            status=Order.STATUS_FAILED, tranzila_token='tok_declined', card_expire_month=12, card_expire_year=2030,
        )
        page = self.page()
        self.assertEqual(page['next'], 'card')
        self.assertTrue(page['card_url'])

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_the_page_never_opens_a_standing_order(self):
        """An order the office ended stays ended: reading a signed contract puts nobody into billing."""
        with override_settings(RENTAL_BILLING_ENABLED=False):
            self.sign()
        self.assertFalse(Order.objects.exists())

        page = self.page()
        self.assertEqual((page['state'], page['next']), ('signed', 'done'))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Link.objects.exists())

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_an_ended_order_is_done_and_left_alone(self):
        self.sign()
        Order.objects.filter(tenancy=self.tenancy).update(status=Order.STATUS_ENDED)
        Link.objects.all().update(status=Link.STATUS_CANCELLED)

        self.assertEqual(self.page()['next'], 'done')
        self.assertEqual(Order.objects.get().status, Order.STATUS_ENDED)
        self.assertEqual(Link.objects.count(), 1)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_failure_inside_leaves_the_signed_page_open(self):
        self.sign()
        with patch('apps.rental_billing.links.ensure_card_link', side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.rentals.signing', 'ERROR') as logs:
                page = self.page()
        self.assertEqual((page['state'], page['next']), ('signed', 'done'))
        self.assertNotIn('card_url', page)
        # The contract itself is all still there.
        self.assertTrue(page['document'])
        self.assertEqual(page['signer_name'], 'אור כהן')
        self.assertIn(str(self.contract.pk), logs.output[0])

    @override_settings(RENTAL_BILLING_ENABLED=True, RENTAL_BILLING_BUSINESS_NAME='אין עסק כזה')
    def test_with_the_business_missing_the_page_is_done(self):
        with self.assertLogs('apps.rentals.signing', 'WARNING'):
            self.sign()
        with self.assertLogs('apps.rentals.signing', 'WARNING'):
            page = self.page()
        self.assertEqual(page['next'], 'done')
        self.assertNotIn('card_url', page)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_year_later_the_signed_link_still_leads_to_the_standing_order(self):
        self.sign()
        # A year passes. Straight through the database: signed_at is frozen
        # against any write but the signing itself.
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE rental_contracts SET signed_at = %s WHERE id = %s',
                [timezone.now() - timedelta(days=400), self.contract.pk],
            )
        page = self.page()
        self.assertEqual(page['state'], 'signed')
        self.assertEqual(page['next'], 'card')
        self.assertTrue(page['card_url'])

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_it_never_reaches_another_tenancys_order(self):
        elsewhere = make_tenancy(self.branch, tenant=make_customer('יעל', 'בר', branch=self.branch))
        theirs = open_standing_order(elsewhere)

        self.sign()
        link = Link.objects.get(token=self.page()['card_url'].rsplit('/', 1)[1])
        self.assertEqual(link.standing_order.tenancy_id, self.tenancy.pk)
        self.assertFalse(Link.objects.filter(standing_order=theirs).exists())
        self.assertEqual(Order.objects.get(pk=theirs.pk).status, Order.STATUS_PENDING_CARD)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_the_signed_page_keeps_its_own_throttle(self):
        self.sign()
        rates = {'rental_sign_view': '2/min', 'rental_sign_submit': '5/min'}
        with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', rates):
            self.assertEqual(self.public.get(page_url(self.token())).status_code, 200)
            self.assertEqual(self.public.get(page_url(self.token())).status_code, 200)
            self.assertEqual(self.public.get(page_url(self.token())).status_code, 429)
        # A flood of reads issues one link, and the refused ones issue none.
        self.assertEqual(Link.objects.count(), 1)
