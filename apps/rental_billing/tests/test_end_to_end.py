"""Phase 7: the whole tenant path walked once, on real rows, through the real endpoints.

    the office creates a tenancy and links the studio slot it rents
    → issues the contract and its signing link
    → the tenant opens /s/<token>, reads it and signs
    → the same answer carries them on to /rc/<token>, where they enter a card
    → the standing order opens and holds the card
    → the daily run charges the first month and issues its RT receipt
    → the same run again, the same day, charges nothing

That last step is the one this test exists for. Two guards have to hold at
once: UNIQUE(tenancy, period), and the fact that the month is written and
committed as 'reserved' before Tranzila is ever called (billing.reserve_month).
Either on its own would let a second run through in the window the other
covers, so they are asserted together — one charge row, one receipt, one call
on the wire.

The agreement starts next month on purpose. That is what makes the daily run
charge the first period rather than the card page: a card entered before the
agreement has begun is only verified, and the first charge waits for the
billing day (apps/rental_billing/card.py).

Nothing leaves the machine. Tranzila is a MagicMock with the real client rigged
to fail the test, ManyChat and the receipt e-mail likewise (tests/factories.py).
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from rest_framework.test import APIClient

from apps.core.models import Business, UserProfile
from apps.documents.models import FormalDocument
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.tests.factories import CARD, mocked_gateway, patch_manychat, patch_tranzila
from apps.rentals.models import RentalContract, Tenancy
from apps.rentals.tests.factories import make_branch, make_rental, make_studio, make_user
from apps.signatures.models import Signature
from apps.signatures.tests.helpers import png_data_url

FRONTEND = 'https://crm.example.com'
CRON_TOKEN = 'cron-token-for-tests'

TENANCIES = '/api/v1/rentals/tenancies/'
CONTRACTS = '/api/v1/rentals/contracts/'
SIGN = '/api/v1/rentals/sign/'
CARD_PAGE = '/api/v1/rental-billing/card/'
CRON = '/api/v1/rental-billing/cron/charge/'

# The office signs the agreement in September for a tenancy that starts in October.
SIGNING_DAY = date(2026, 9, 11)
FIRST_BILLING_DAY = date(2026, 10, 10)
FIRST_PERIOD = date(2026, 10, 1)

Order = TenantStandingOrder
Charge = TenantCharge


@override_settings(
    CRM_FRONTEND_URL=FRONTEND,
    RENTAL_BILLING_ENABLED=True,
    CRON_TOKEN=CRON_TOKEN,
)
class TenantPathEndToEndTests(TestCase):
    def setUp(self):
        # The public pages are throttled per address, and the counter is cached.
        cache.clear()
        self.branch = make_branch('פלורנטין')
        self.studio = make_studio(self.branch)
        self.business, _ = Business.objects.get_or_create(name='סוחרים')
        self.manager = make_user('manager-e2e@test', UserProfile.ROLE_MANAGER)
        self.office = APIClient()
        self.office.force_authenticate(self.manager)
        self.tenant_client = APIClient()
        self.cron_client = APIClient()
        self.gateway = mocked_gateway()
        patch_tranzila(self, self.gateway)
        self.whatsapp = patch_manychat(self)

    def _today(self, day: date):
        """What the server thinks today is, for everything that does not take it as an argument."""
        patcher = patch('apps.rental_billing.billing.today_local', return_value=day)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def gateway_calls(self) -> int:
        return (
            self.gateway.charge_with_token.call_count
            + self.gateway.charge_with_card.call_count
            + self.gateway.verify_card.call_count
        )

    def run_cron(self):
        res = self.cron_client.post(CRON, {}, format='json', HTTP_X_CRON_TOKEN=CRON_TOKEN)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data['summary']

    def test_from_a_new_tenancy_to_a_charged_month_and_a_run_that_charges_nothing(self):
        today = self._today(SIGNING_DAY)

        # ---- the office: a tenancy, its slot, its contract, its link ----
        res = self.office.post(TENANCIES, {
            'branch': str(self.branch.id),
            'monthly_amount': '1234.56',
            'billing_day': 10,
            'start_date': '2026-10-01',
            'end_date': '2027-09-30',
            'tenant': {
                'first_name': 'סטודיו', 'last_name': 'אור', 'company_number': '51-234567-8',
                'phone': '050-1234567', 'email': 'or@example.com', 'address': 'הרצל 1',
            },
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        tenancy = Tenancy.objects.get(pk=res.data['id'])

        slot = make_rental(
            self.branch, price='400', days=(0,), studio=self.studio,
            contract=(date(2026, 10, 1), date(2027, 9, 30)), event_date=date(2026, 10, 4),
        )
        res = self.office.post(f'{TENANCIES}{tenancy.pk}/link-slots/', {'slot_ids': [str(slot.id)]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

        res = self.office.post(f'{TENANCIES}{tenancy.pk}/contracts/', {}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        contract_id = res.data['id']
        res = self.office.post(f'{CONTRACTS}{contract_id}/signing-link/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        sign_url = res.data['signing_url']
        self.assertTrue(sign_url.startswith(f'{FRONTEND}/s/'), sign_url)
        sign_token = sign_url.rsplit('/', 1)[1]

        # ---- the tenant: reads the contract, then signs it ----
        page = self.tenant_client.get(f'{SIGN}{sign_token}/')
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data['state'], 'open')
        self.assertEqual(page.data['monthly_total'], '1456.78')
        # The link carries no date: it is not closed by time (docs, phase 3).
        self.assertIsNone(page.data['expires_at'])
        self.assertEqual(RentalContract.objects.get(pk=contract_id).status, RentalContract.STATUS_VIEWED)

        signed = self.tenant_client.post(f'{SIGN}{sign_token}/', {
            'signer_name': 'אור כהן',
            'signer_id_number': '123456782',
            'signature': png_data_url(4),
            'accept': True,
        }, format='json', HTTP_USER_AGENT='Mozilla/5.0 (Tenant phone)')
        self.assertEqual(signed.status_code, 200, signed.data)

        contract = RentalContract.objects.get(pk=contract_id)
        self.assertEqual(contract.status, RentalContract.STATUS_SIGNED)
        self.assertTrue(contract.signed_pdf_is_intact())
        self.assertEqual(Signature.objects.get().kind, Signature.KIND_RENTAL_CONTRACT)
        self.assertEqual(Tenancy.objects.get(pk=tenancy.pk).status, Tenancy.STATUS_SIGNED)
        # Nothing reached Tranzila on the way to a signature.
        self.assertEqual(self.gateway_calls(), 0)

        # ---- the same answer carries them on to the card, on one link ----
        self.assertEqual(signed.data['next'], 'card')
        card_url = signed.data['card_url']
        self.assertTrue(card_url.startswith(f'{FRONTEND}/rc/'), card_url)
        card_token = card_url.rsplit('/', 1)[1]
        order = Order.objects.get(tenancy=tenancy)
        self.assertEqual((order.source, order.status), (Order.SOURCE_SIGNING, Order.STATUS_PENDING_CARD))
        self.assertEqual(TenantCardLink.objects.get(token=card_token).standing_order_id, order.pk)

        preview = self.tenant_client.get(f'{CARD_PAGE}{card_token}/')
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertNotIn('error', preview.data)
        # The agreement has not started, so the card is only verified, and the
        # first charge waits for the billing day.
        self.assertFalse(preview.data['charge_now'])
        self.assertEqual(preview.data['monthly_total'], '1456.78')
        self.assertEqual(preview.data['next_charge_date'], FIRST_BILLING_DAY.isoformat())
        self.assertIsNone(preview.data['expires_at'])

        entered = self.tenant_client.post(f'{CARD_PAGE}{card_token}/', {'card_details': CARD}, format='json')
        self.assertEqual(entered.status_code, 200, entered.data)
        self.assertTrue(entered.data['success'])
        self.assertFalse(entered.data['charged'])
        self.assertEqual(entered.data['next_charge_date'], FIRST_BILLING_DAY.isoformat())

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertTrue(order.has_card)
        self.assertEqual((order.tranzila_token, order.card_last4), ('tok_verified', '0000'))
        self.assertEqual(order.next_charge_date, FIRST_BILLING_DAY)
        self.assertEqual(TenantCardLink.objects.get(token=card_token).status, TenantCardLink.STATUS_USED)
        # Verified, never charged: no month exists yet and no receipt was issued.
        self.assertEqual(self.gateway.verify_card.call_count, 1)
        self.assertEqual(self.gateway.charge_with_card.call_count, 0)
        self.assertFalse(Charge.objects.exists())

        # ---- the daily run, on the billing day ----
        today.stop()
        self._today(FIRST_BILLING_DAY)

        summary = self.run_cron()
        self.assertEqual(
            (summary['charged'], summary['receipts'], summary['failed'], summary['review']), (1, 1, 0, 0), summary,
        )

        charge = Charge.objects.get()
        self.assertEqual(charge.period, FIRST_PERIOD)
        self.assertEqual(charge.status, Charge.STATUS_CHARGED)
        self.assertEqual(charge.tenancy_id, tenancy.pk)
        self.assertEqual((charge.amount_before_vat, charge.vat_amount, charge.total), (123456, 22222, 145678))
        self.assertEqual(charge.business, self.business)
        self.assertEqual(charge.transaction_id, 'T100')
        sent = self.gateway.charge_with_token.call_args.kwargs
        self.assertEqual(sent['token'], 'tok_verified')
        self.assertEqual(sent['amount'], Decimal('1456.78'))
        self.assertEqual(sent['duplicate_guard_key'], f'rental-{order.pk}-2026-10')

        receipt = charge.receipt
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.document_number.startswith('RT-'))
        self.assertEqual(receipt.document_type, 'combined')
        self.assertEqual(receipt.business_customer_id, tenancy.tenant_id)
        self.assertEqual(receipt.business_id, self.business.id)
        self.assertEqual(receipt.total_amount, Decimal('1456.78'))
        # Dated the day it is issued, and numbered from that day's year, so a
        # receipt never carries a date earlier than documents numbered before it.
        self.assertEqual(receipt.document_date, timezone.localdate())
        self.assertEqual(receipt.customer_notes, '')

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))

        charged_calls = self.gateway.charge_with_token.call_count
        self.assertEqual(charged_calls, 1)

        # ---- the same run again, the same day ----
        again = self.run_cron()

        self.assertEqual((again['charged'], again['failed'], again['review']), (0, 0, 0), again)
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(FormalDocument.objects.filter(document_number__startswith='RT-').count(), 1)
        # Not one more request on the wire. Three things have to be wrong at once
        # for a second charge: the order is no longer due (its next charge is
        # November), the tenancy was already sent to Tranzila today, and the
        # month row exists. The test below strips the first two away and leaves
        # the month row alone to hold the line.
        self.assertEqual(self.gateway.charge_with_token.call_count, charged_calls)
        self.assertEqual(Charge.objects.get().transaction_id, 'T100')

    def test_the_month_guard_holds_even_when_the_once_a_day_rule_does_not(self):
        """
        The second run stripped of the guard that would hide the one being tested.

        The daily run already refuses a tenancy it sent to Tranzila today, so a
        plain second run proves nothing about UNIQUE(tenancy, period) on its
        own. Here the reservation is backdated so the day rule lets the run
        through, and the month row is what stops it.
        """
        from apps.rental_billing.billing import charge_due

        self._today(FIRST_BILLING_DAY)
        tenancy = self._tenancy_with_a_card()

        charge_due(today=FIRST_BILLING_DAY)
        charge = Charge.objects.get()
        self.assertEqual(charge.status, Charge.STATUS_CHARGED)
        calls = self.gateway.charge_with_token.call_count

        # As if the charge had been made on an earlier run of the same month.
        Charge.objects.filter(pk=charge.pk).update(reserved_at=charge.reserved_at.replace(year=2026, month=10, day=1))
        Order.objects.filter(tenancy=tenancy).update(next_charge_date=FIRST_BILLING_DAY)

        summary = charge_due(today=FIRST_BILLING_DAY)

        self.assertEqual(summary['charged'], 0, summary)
        self.assertEqual(Charge.objects.count(), 1)
        self.assertEqual(self.gateway.charge_with_token.call_count, calls)

    def _tenancy_with_a_card(self) -> Tenancy:
        """A tenancy whose order already holds a verified card, due on the billing day."""
        from apps.rentals.tests.factories import make_tenancy

        tenancy = make_tenancy(self.branch, start_date=date(2026, 10, 1), end_date=date(2027, 9, 30))
        Order.objects.create(
            tenancy=tenancy, tenant=tenancy.tenant, branch=self.branch, business=self.business,
            amount_before_vat=Decimal('1234.56'), billing_day=10, start_date=date(2026, 10, 1),
            end_date=date(2027, 9, 30), status=Order.STATUS_ACTIVE, tranzila_token='tok_verified',
            card_expire_month=12, card_expire_year=2030, card_last4='0000',
            next_charge_date=FIRST_BILLING_DAY,
        )
        return tenancy
