"""A partner reaches their own branches' standing orders and charges, and nothing else;
another branch's is not found (404). A worker is refused (403)."""
from datetime import date

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.rental_billing.models import TenantCharge
from apps.rental_billing.tests.factories import (
    CHARGES_URL, ORDERS_URL, STATUS_URL, BillingFixture, make_branch, make_customer, make_tenancy, make_user,
)

OCT = date(2026, 10, 1)


@override_settings(RENTAL_BILLING_ENABLED=True)
class ScopingTests(BillingFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.elsewhere = make_branch('רמת אביב')
        self.my_order = self.active_order()
        self.my_charge = self.charge_row(self.my_order, OCT, TenantCharge.STATUS_REVIEW)
        their_tenancy = make_tenancy(self.elsewhere, tenant=make_customer('יעל', 'בר', branch=self.elsewhere))
        self.their_tenancy = their_tenancy
        self.their_order = self.active_order(tenancy=their_tenancy)
        self.their_charge = self.charge_row(self.their_order, OCT, TenantCharge.STATUS_FAILED)
        self.partner = make_user('partner-rental-billing@test', UserProfile.ROLE_PARTNER, branches=[self.branch])
        self.worker = make_user('worker-rental-billing@test', UserProfile.ROLE_WORKER)

    def test_a_partner_lists_only_their_branches(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual([row['id'] for row in self.client.get(ORDERS_URL).data], [str(self.my_order.pk)])
        self.assertEqual([row['id'] for row in self.client.get(CHARGES_URL).data], [str(self.my_charge.pk)])
        self.assertEqual(self.client.get(ORDERS_URL, {'branch': str(self.elsewhere.pk)}).data, [])

    def test_another_branchs_order_and_charges_are_not_found(self):
        self.client.force_authenticate(self.partner)
        order = f'{ORDERS_URL}{self.their_order.pk}/'
        charge = f'{CHARGES_URL}{self.their_charge.pk}/'
        for res in (
            self.client.get(order),
            self.client.patch(order, {'notes': 'x'}, format='json'),
            self.client.post(f'{order}pause/'),
            self.client.post(f'{order}resume/'),
            self.client.post(f'{order}end/'),
            self.client.post(f'{order}card-link/'),
            self.client.get(f'{order}charges/'),
            self.client.get(charge),
            self.client.post(f'{charge}retry/'),
            self.client.post(f'{charge}mark-charged/', {'transaction_id': 'T'}, format='json'),
            self.client.post(f'{charge}void/', {'reason': 'x'}, format='json'),
            self.client.post(f'{charge}issue-receipt/'),
        ):
            self.assertEqual(res.status_code, 404, res.request['PATH_INFO'])
        self.their_charge.refresh_from_db()
        self.assertEqual(self.their_charge.status, TenantCharge.STATUS_FAILED)
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_partner_cannot_open_an_order_on_another_branchs_tenancy(self):
        self.client.force_authenticate(self.partner)
        other = make_tenancy(self.elsewhere, tenant=make_customer('דן', 'לב', branch=self.elsewhere))
        self.assertEqual(self.client.post(ORDERS_URL, {'tenancy_id': str(other.pk)}, format='json').status_code, 404)
        mine = make_tenancy(self.branch, tenant=make_customer('דנה', 'שמש', branch=self.branch))
        self.assertEqual(self.client.post(ORDERS_URL, {'tenancy_id': str(mine.pk)}, format='json').status_code, 201)

    def test_a_partner_acts_on_their_own_branch(self):
        self.client.force_authenticate(self.partner)
        res = self.client.post(f'{CHARGES_URL}{self.my_charge.pk}/void/', {'reason': 'לא חויב'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

    def test_a_worker_is_refused(self):
        self.client.force_authenticate(self.worker)
        for url in (ORDERS_URL, CHARGES_URL, STATUS_URL, f'{ORDERS_URL}{self.my_order.pk}/'):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post(f'{CHARGES_URL}{self.my_charge.pk}/void/', {'reason': 'x'}).status_code, 403)

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(self.client.get(ORDERS_URL).status_code, (401, 403))
