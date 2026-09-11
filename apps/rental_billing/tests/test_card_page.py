"""The tenant's card page: the first-charge rules, the failed-card path, and every refusal —
expired, used, in flight, throttled, unsigned — before any gateway call."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.forms.models import model_to_dict
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import Business
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.orders import open_standing_order
from apps.rental_billing.tests.factories import (
    CARD, DECLINE, TIMEOUT, BillingFixture, card_url, make_customer, make_tenancy, sign_contract,
)

Order = TenantStandingOrder
Charge = TenantCharge
Link = TenantCardLink
SEP = date(2026, 9, 1)


@override_settings(RENTAL_BILLING_ENABLED=True)
class CardPageTests(BillingFixture, APITestCase):
    def setUp(self):
        super().setUp()
        today = patch('apps.rental_billing.billing.today_local', return_value=date(2026, 9, 11))
        self.today = today.start()
        self.addCleanup(today.stop)

    def submit(self, link, card=CARD):
        return self.client.post(card_url(link), {'card_details': card}, format='json')

    # ------------------------------------------------------------ first charge

    def test_an_agreement_that_started_charges_the_current_month_now(self):
        order = self.order()
        link = self.link(order)

        preview = self.client.get(card_url(link))
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data['charge_now'])
        self.assertEqual(preview.data['charge_amount'], '1456.78')
        self.assertEqual(preview.data['charge_period'], '2026-09-01')
        self.assertEqual(preview.data['next_charge_date'], '2026-10-10')
        self.assertEqual(preview.data['tenant_name'], 'סטודיו אור')

        res = self.submit(link)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            {key: res.data[key] for key in ('state', 'charged', 'amount', 'next_charge_date')},
            {'state': 'active', 'charged': True, 'amount': '1456.78', 'next_charge_date': '2026-10-10'},
        )
        kwargs = self.gateway.charge_with_card.call_args.kwargs
        self.assertEqual(kwargs['amount'], Decimal('1456.78'))
        self.assertEqual(kwargs['duplicate_guard_key'], f'rental-{order.pk}-2026-09')
        self.gateway.verify_card.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertEqual((order.tranzila_token, order.card_last4), ('tok_new', '0000'))
        self.assertEqual((order.card_expire_month, order.card_expire_year), (12, 2030))
        charge = Charge.objects.get()
        self.assertEqual((charge.period, charge.status, charge.trigger), (SEP, Charge.STATUS_CHARGED, Charge.TRIGGER_CARD))
        self.assertTrue(charge.receipt.document_number.startswith('RT-'))
        link.refresh_from_db()
        self.assertEqual(link.status, Link.STATUS_USED)

    def test_an_agreement_that_starts_later_verifies_the_card_only(self):
        tenancy = make_tenancy(
            self.branch, tenant=make_customer('רון', 'כהן', branch=self.branch), start_date=date(2026, 10, 15),
        )
        order = self.order(tenancy=tenancy)
        link = self.link(order)

        res = self.submit(link)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            {key: res.data[key] for key in ('state', 'charged', 'amount', 'next_charge_date')},
            {'state': 'active', 'charged': False, 'amount': '0.00', 'next_charge_date': '2026-11-10'},
        )
        self.gateway.verify_card.assert_called_once()
        self.gateway.charge_with_card.assert_not_called()
        self.assertFalse(Charge.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.tranzila_token, 'tok_verified')

    def test_a_start_on_the_billing_day_is_charged_on_that_day(self):
        tenancy = make_tenancy(
            self.branch, tenant=make_customer('רון', 'כהן', branch=self.branch), start_date=date(2026, 10, 10),
        )
        link = self.link(self.order(tenancy=tenancy))
        res = self.submit(link)
        self.assertEqual(res.data['next_charge_date'], '2026-10-10')

    # ------------------------------------------------------------ failed card

    def test_a_failed_order_takes_a_new_card_and_pays_the_failed_month(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10), status=Order.STATUS_FAILED)
        failed = self.charge_row(order, date(2026, 10, 1), Charge.STATUS_FAILED, error='declined')
        link = self.link(order)
        self.today.return_value = date(2026, 10, 12)

        res = self.submit(link)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['charged'], res.data['next_charge_date']), (True, '2026-11-10'))
        self.assertEqual(
            self.gateway.charge_with_card.call_args.kwargs['duplicate_guard_key'], f'rental-{order.pk}-2026-10',
        )
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts, failed.trigger), (Charge.STATUS_CHARGED, 2, Charge.TRIGGER_CARD))
        self.assertEqual(Charge.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual((order.status, order.tranzila_token, order.last_error), (Order.STATUS_ACTIVE, 'tok_new', ''))

    def test_a_decline_lets_the_tenant_try_another_card(self):
        order = self.order()
        link = self.link(order)
        self.gateway.charge_with_card.return_value = dict(DECLINE)

        res = self.submit(link)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['error'], 'התשלום לא אושר. נסו כרטיס אחר או פנו למשרד.')
        link.refresh_from_db()
        self.assertEqual((link.status, link.attempts), (Link.STATUS_PENDING, 1))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_CARD)
        self.assertEqual(Charge.objects.get().status, Charge.STATUS_FAILED)

        self.gateway.charge_with_card.return_value = {
            'success': True, 'token': 'tok_second', 'transaction_id': 'T2', 'confirmation_code': 'C2', 'raw_response': {},
        }
        res = self.submit(link)
        self.assertEqual(res.status_code, 200, res.data)
        charge = Charge.objects.get()
        self.assertEqual((charge.status, charge.attempts), (Charge.STATUS_CHARGED, 2))

    def test_an_uncertain_answer_freezes_the_link_and_the_month(self):
        order = self.order()
        link = self.link(order)
        self.gateway.charge_with_card.return_value = dict(TIMEOUT)

        res = self.submit(link)

        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.data['processing'])
        link.refresh_from_db()
        self.assertEqual(link.status, Link.STATUS_REVIEW)
        self.assertEqual(Charge.objects.get().status, Charge.STATUS_REVIEW)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING_CARD)
        self.assertEqual(self.submit(link).status_code, 409)
        self.assertEqual(self.gateway.charge_with_card.call_count, 1)

    # --------------------------------------------------- never twice, never unasked

    def test_a_double_submit_never_reaches_the_gateway_twice(self):
        link = self.link(self.order())
        self.assertEqual(self.submit(link).status_code, 200)
        again = self.submit(link)
        self.assertEqual(again.status_code, 400)
        self.assertTrue(again.data['already_done'])
        self.assertEqual(self.gateway.charge_with_card.call_count, 1)
        self.assertEqual(Charge.objects.count(), 1)

    def test_a_submit_while_another_is_in_flight_is_refused(self):
        link = self.link(self.order(), status=Link.STATUS_PROCESSING, charge_started_at=timezone.now(), attempts=1)
        res = self.submit(link)
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.data['processing'])
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_submit_that_never_came_back_goes_to_review(self):
        link = self.link(
            self.order(), status=Link.STATUS_PROCESSING, charge_started_at=timezone.now() - timedelta(minutes=5), attempts=1,
        )
        self.assertEqual(self.submit(link).status_code, 409)
        link.refresh_from_db()
        self.assertEqual(link.status, Link.STATUS_REVIEW)
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_month_that_is_already_reserved_is_refused(self):
        order = self.order()
        self.charge_row(order, SEP, Charge.STATUS_RESERVED)
        link = self.link(order)
        res = self.submit(link)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.gateway_calls(), 0)
        link.refresh_from_db()
        self.assertEqual((link.status, link.attempts), (Link.STATUS_PENDING, 0))

    def test_a_month_already_paid_keeps_the_card_without_charging(self):
        order = self.order()
        self.charge_row(order, SEP, Charge.STATUS_CHARGED, charged_at=timezone.now())
        res = self.submit(self.link(order))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['charged'], res.data['next_charge_date']), (False, '2026-10-10'))
        self.gateway.charge_with_card.assert_not_called()
        self.gateway.verify_card.assert_called_once()

    # ------------------------------------------------------------ refusals

    def test_an_expired_link_is_refused(self):
        link = self.link(self.order(), created_at=timezone.now() - timedelta(days=15))
        preview = self.client.get(card_url(link))
        self.assertEqual(preview.status_code, 400)
        self.assertIn('פג תוקף', preview.data['error'])
        self.assertEqual(self.submit(link).status_code, 400)
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_link_still_inside_its_14_days_works(self):
        link = self.link(self.order(), created_at=timezone.now() - timedelta(days=13))
        self.assertEqual(self.client.get(card_url(link)).status_code, 200)

    def test_a_used_or_cancelled_link_is_refused(self):
        order = self.order()
        used = self.link(order, status=Link.STATUS_USED)
        res = self.submit(used)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(res.data['already_done'])
        cancelled = self.link(order, status=Link.STATUS_CANCELLED)
        self.assertEqual(self.submit(cancelled).status_code, 400)
        self.assertEqual(self.client.get('/api/v1/rental-billing/card/nope/').status_code, 404)
        self.assertEqual(self.gateway_calls(), 0)

    def test_the_card_page_is_throttled(self):
        link = self.link(self.order())
        rates = {'rental_card_charge': '1/min', 'rental_card_view': '30/min'}
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', rates):
            self.submit(link, card={})
            res = self.submit(link, card={})
        self.assertEqual(res.status_code, 429)
        self.assertEqual(self.gateway_calls(), 0)

    def test_an_order_from_the_signing_page_needs_the_contract_signed(self):
        order = open_standing_order(self.tenancy, source=Order.SOURCE_SIGNING)
        link = self.link(order)
        res = self.submit(link)
        self.assertEqual(res.status_code, 400)
        self.assertIn('החוזה עדיין לא נחתם', res.data['error'])
        self.assertEqual(self.gateway_calls(), 0)

        sign_contract(self.tenancy)
        self.assertEqual(self.submit(link).status_code, 200)

    def test_invalid_card_details_are_refused_before_anything(self):
        link = self.link(self.order())
        res = self.submit(link, card={**CARD, 'card_number': '4580000000000001'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.gateway_calls(), 0)
        link.refresh_from_db()
        self.assertEqual(link.attempts, 0)

    def test_an_order_that_takes_no_card_is_refused(self):
        link = self.link(self.active_order())
        self.assertEqual(self.submit(link).status_code, 400)
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_missing_business_refuses_the_card(self):
        link = self.link(self.order())
        Business.objects.filter(pk=self.business.pk).update(name='עסק אחר')
        self.assertEqual(self.submit(link).status_code, 503)
        self.assertEqual(self.gateway_calls(), 0)

    def test_the_card_number_is_never_stored(self):
        order = self.order()
        link = self.link(order)
        self.assertEqual(self.submit(link).status_code, 200)
        rows = [
            model_to_dict(Order.objects.get(pk=order.pk)),
            model_to_dict(Link.objects.get(pk=link.pk)),
            *(model_to_dict(charge) for charge in Charge.objects.all()),
        ]
        stored = repr(rows)
        self.assertNotIn(CARD['card_number'], stored)
        self.assertNotIn(CARD['cvv'] + "'", stored)
