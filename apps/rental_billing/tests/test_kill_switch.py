"""While RENTAL_BILLING_ENABLED is off nothing reaches Tranzila: not the cron, not the
card page, not the office's retry. The office can still open and edit standing orders."""
from datetime import date

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.rental_billing.billing import charge_due, gateway
from apps.rental_billing.errors import BillingDisabled
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.tests.factories import (
    CARD, CRON_URL, ORDERS_URL, STATUS_URL, BillingFixture, card_url, make_customer, make_tenancy,
)


@override_settings(RENTAL_BILLING_ENABLED=False, CRON_TOKEN='cron-secret')
class KillSwitchTests(BillingFixture, APITestCase):
    def assert_tranzila_untouched(self):
        self.tranzila_class.production.assert_not_called()
        self.assertEqual(self.gateway_calls(), 0)

    def test_the_gateway_itself_refuses(self):
        with self.assertRaises(BillingDisabled):
            gateway()
        self.assert_tranzila_untouched()

    def test_the_cron_touches_nothing(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        summary = charge_due(today=date(2026, 10, 10))
        self.assertTrue(summary['disabled'])
        self.assertFalse(summary['enabled'])
        self.assertEqual(summary['charged'], 0)
        self.assertFalse(TenantCharge.objects.exists())
        self.assert_tranzila_untouched()

    def test_the_cron_endpoint_answers_disabled_and_keeps_its_auth(self):
        self.active_order(next_charge_date=date(2020, 1, 10))
        for call in (self.client.get, self.client.post):
            self.assertEqual(call(CRON_URL).status_code, 401)
            self.assertEqual(call(CRON_URL, HTTP_X_CRON_TOKEN='wrong').status_code, 401)
            res = call(CRON_URL, HTTP_X_CRON_TOKEN='cron-secret')
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.data['summary']['disabled'])
        self.assertFalse(TenantCharge.objects.exists())
        self.assert_tranzila_untouched()

    def test_the_card_page_shows_but_refuses_a_card(self):
        order = self.order()
        link = self.link(order)
        preview = self.client.get(card_url(link))
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data['enabled'])
        res = self.client.post(card_url(link), {'card_details': CARD}, format='json')
        self.assertEqual(res.status_code, 503)
        self.assertTrue(res.data['disabled'])
        order.refresh_from_db()
        self.assertEqual(order.status, TenantStandingOrder.STATUS_PENDING_CARD)
        self.assertEqual(order.tranzila_token, '')
        self.assert_tranzila_untouched()

    def test_the_offices_retry_is_refused(self):
        order = self.active_order(status=TenantStandingOrder.STATUS_FAILED)
        charge = self.charge_row(order, date(2026, 10, 1), TenantCharge.STATUS_FAILED)
        self.client.force_authenticate(self.manager)
        res = self.client.post(f'/api/v1/rental-billing/charges/{charge.pk}/retry/')
        self.assertEqual(res.status_code, 503)
        self.assertTrue(res.data['disabled'])
        charge.refresh_from_db()
        self.assertEqual(charge.status, TenantCharge.STATUS_FAILED)
        self.assert_tranzila_untouched()

    def test_the_office_still_opens_and_edits_orders(self):
        self.client.force_authenticate(self.manager)
        tenancy = make_tenancy(self.branch, tenant=make_customer('רון', 'כהן', branch=self.branch))
        created = self.client.post(ORDERS_URL, {'tenancy_id': str(tenancy.pk)}, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        url = f'{ORDERS_URL}{created.data["id"]}/'
        edited = self.client.patch(url, {'amount_before_vat': '1500.00', 'notes': 'חדש'}, format='json')
        self.assertEqual(edited.status_code, 200, edited.data)
        self.assertEqual(edited.data['amount_before_vat'], '1500.00')
        link = self.client.post(f'{url}card-link/')
        self.assertEqual(link.status_code, 201, link.data)
        self.assertEqual(self.client.post(f'{url}end/').status_code, 200)
        status = self.client.get(STATUS_URL)
        self.assertFalse(status.data['enabled'])
        self.assertTrue(status.data['business_found'])
        self.assert_tranzila_untouched()
