"""The office's API: opening an order from a tenancy, editing it (never the card), its
life (pause, resume, end), card links, the charges list, and the decisions on a charge."""
from datetime import date

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.rental_billing.billing import charge_due
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.tests.factories import (
    CHARGES_URL, CRON_URL, DECLINE, ORDERS_URL, STATUS_URL, BillingFixture, make_customer, make_tenancy,
)

Order = TenantStandingOrder
Charge = TenantCharge
OCT = date(2026, 10, 1)


def order_url(order, action=''):
    return f'{ORDERS_URL}{order.pk}/{action}'


def charge_url(charge, action=''):
    return f'{CHARGES_URL}{charge.pk}/{action}'


@override_settings(RENTAL_BILLING_ENABLED=True)
class OfficeApiTests(BillingFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_an_order_opens_from_a_tenancy_with_its_terms(self):
        res = self.client.post(ORDERS_URL, {'tenancy_id': str(self.tenancy.pk)}, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        data = res.data
        self.assertEqual(data['status'], 'pending_card')
        self.assertEqual(data['status_label'], 'ממתינה לכרטיס')
        self.assertEqual(data['source'], 'office')
        self.assertEqual((data['amount_before_vat'], data['vat_amount'], data['monthly_total']), ('1234.56', '222.22', '1456.78'))
        self.assertEqual(data['billing_day'], 10)
        self.assertEqual((data['start_date'], data['end_date']), ('2026-09-01', '2027-08-31'))
        self.assertEqual(data['business_name'], 'סוחרים')
        self.assertEqual(data['branch_name'], 'פלורנטין')
        self.assertFalse(data['has_card'])
        self.assertNotIn('tranzila_token', data)
        self.assertIsNone(data['next_charge_date'])

        again = self.client.post(ORDERS_URL, {'tenancy_id': str(self.tenancy.pk)}, format='json')
        self.assertEqual(again.status_code, 400)
        self.assertIn('כבר יש הוראת קבע פתוחה', again.data['error'])

    def test_an_order_may_override_the_tenancys_terms(self):
        res = self.client.post(ORDERS_URL, {
            'tenancy_id': str(self.tenancy.pk), 'amount_before_vat': '900.00', 'billing_day': 3, 'end_date': None,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data['amount_before_vat'], res.data['billing_day'], res.data['end_date']), ('900.00', 3, None))
        bad = self.client.post(ORDERS_URL, {
            'tenancy_id': str(make_tenancy(self.branch, tenant=make_customer('א', 'ב')).pk), 'billing_day': 31,
        }, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_edit_changes_only_the_allowed_fields(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        res = self.client.patch(order_url(order), {
            'amount_before_vat': '1500.00', 'billing_day': 15, 'end_date': '2027-01-31', 'notes': 'הערה',
        }, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['amount_before_vat'], '1500.00')
        self.assertEqual(res.data['next_charge_date'], '2026-10-15')
        self.assertEqual((res.data['end_date'], res.data['notes']), ('2027-01-31', 'הערה'))

        for body in ({'tranzila_token': 'someone-elses'}, {'status': 'active'}, {'card_last4': '1111'}):
            refused = self.client.patch(order_url(order), body, format='json')
            self.assertEqual(refused.status_code, 400, body)
        self.assertEqual(self.client.patch(order_url(order), {'billing_day': 29}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(order_url(order), {'amount_before_vat': '0'}, format='json').status_code, 400)
        order.refresh_from_db()
        self.assertEqual((order.tranzila_token, order.card_last4, order.billing_day), ('tok_saved', '4242', 15))

    def test_pause_resume_and_end(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.assertEqual(self.client.post(order_url(order, 'pause/')).data['status'], 'paused')
        self.assertEqual(self.client.post(order_url(order, 'pause/')).status_code, 400)
        self.assertEqual(self.client.post(order_url(order, 'resume/')).data['status'], 'active')
        self.link(order)
        ended = self.client.post(order_url(order, 'end/'))
        self.assertEqual(ended.data['status'], 'ended')
        self.assertFalse(TenantCardLink.objects.filter(status=TenantCardLink.STATUS_PENDING).exists())
        self.assertEqual(self.client.post(order_url(order, 'end/')).status_code, 400)
        self.assertEqual(self.client.patch(order_url(order), {'amount_before_vat': '1.00'}, format='json').status_code, 400)

    def test_a_card_link_is_created_and_rotated(self):
        order = self.order()
        first = self.client.post(order_url(order, 'card-link/'))
        self.assertEqual(first.status_code, 201, first.data)
        self.assertIn('/rc/', first.data['url'])
        second = self.client.post(order_url(order, 'card-link/'))
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data['url'], second.data['url'])
        self.assertEqual(TenantCardLink.objects.get(pk=first.data['id']).status, TenantCardLink.STATUS_CANCELLED)
        listed = self.client.get(order_url(order))
        self.assertEqual(listed.data['card_link']['url'], second.data['url'])

        in_flight = TenantCardLink.objects.get(pk=second.data['id'])
        TenantCardLink.objects.filter(pk=in_flight.pk).update(
            status=TenantCardLink.STATUS_PROCESSING, charge_started_at=timezone.now(),
        )
        self.assertEqual(self.client.post(order_url(order, 'card-link/')).status_code, 409)
        active = self.active_order(tenancy=make_tenancy(self.branch, tenant=make_customer('ר', 'כ')))
        self.assertEqual(self.client.post(order_url(active, 'card-link/')).status_code, 400)

    def test_the_charges_list_shows_receipt_status_and_tag(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 10))
        self.charge_row(order, date(2026, 9, 1), Charge.STATUS_CHARGED, charged_at=timezone.now())

        res = self.client.get(CHARGES_URL)

        self.assertEqual(res.status_code, 200)
        self.assertEqual([row['period'] for row in res.data], ['2026-10-01', '2026-09-01'])
        october = res.data[0]
        self.assertEqual((october['status'], october['status_label']), ('charged', 'חויב'))
        self.assertTrue(october['receipt']['document_number'].startswith('RT-'))
        self.assertIn('/pdf/', october['receipt']['pdf_url'])
        self.assertEqual((october['business_name'], october['total'], october['total_agorot']), ('סוחרים', '1456.78', 145678))
        self.assertEqual(october['tenant_name'], 'סטודיו אור')
        self.assertFalse(october['needs_receipt'])
        self.assertTrue(res.data[1]['needs_receipt'])

        needing = self.client.get(CHARGES_URL, {'needs_receipt': '1'})
        self.assertEqual([row['period'] for row in needing.data], ['2026-09-01'])
        self.assertEqual(len(self.client.get(CHARGES_URL, {'period': '2026-10'}).data), 1)
        self.assertEqual(len(self.client.get(order_url(order, 'charges/')).data), 2)

        issued = self.client.post(charge_url(Charge.objects.get(period=date(2026, 9, 1)), 'issue-receipt/'))
        self.assertEqual(issued.status_code, 200, issued.data)
        self.assertTrue(issued.data['receipt']['document_number'].startswith('RT-'))

    def test_retry_now_on_a_failed_month(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=date(2026, 10, 10))
        charge = Charge.objects.get()
        self.gateway.charge_with_token.return_value = {
            'success': True, 'transaction_id': 'T300', 'confirmation_code': 'C300', 'raw_response': {},
        }

        res = self.client.post(charge_url(charge, 'retry/'))

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['outcome'], 'charged')
        self.assertEqual(res.data['charge']['status'], 'charged')
        self.assertEqual(res.data['charge']['attempts'], 2)
        self.assertTrue(res.data['charge']['receipt']['document_number'].startswith('RT-'))
        keys = {call.kwargs['duplicate_guard_key'] for call in self.gateway.charge_with_token.call_args_list}
        self.assertEqual(keys, {f'rental-{order.pk}-2026-10'})
        order.refresh_from_db()
        self.assertEqual((order.status, order.next_charge_date, order.last_error), ('active', date(2026, 11, 10), ''))

        self.assertEqual(self.client.post(charge_url(charge, 'retry/')).status_code, 400)
        self.assertEqual(self.gateway.charge_with_token.call_count, 2)

    def test_a_review_charge_marked_as_charged_gets_its_receipt(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        charge = self.charge_row(order, OCT, Charge.STATUS_REVIEW, error='Read timed out')

        self.assertEqual(self.client.post(charge_url(charge, 'mark-charged/'), {}, format='json').status_code, 400)
        res = self.client.post(charge_url(charge, 'mark-charged/'), {
            'transaction_id': 'TX-9', 'confirmation_code': '0012345', 'note': 'נמצא בטרנזילה',
        }, format='json')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['status'], res.data['transaction_id']), ('charged', 'TX-9'))
        self.assertTrue(res.data['receipt']['document_number'].startswith('RT-'))
        self.assertEqual(res.data['resolution_note'], 'נמצא בטרנזילה')
        self.assertTrue(res.data['resolved_by_name'])
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))
        self.assertEqual(self.gateway_calls(), 0)
        # A charge that is not in review cannot be marked.
        self.assertEqual(
            self.client.post(charge_url(charge, 'mark-charged/'), {'transaction_id': 'X'}, format='json').status_code, 400,
        )

    def test_a_review_charge_can_be_voided(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        charge = self.charge_row(order, OCT, Charge.STATUS_REVIEW)
        self.assertEqual(self.client.post(charge_url(charge, 'void/'), {}, format='json').status_code, 400)
        res = self.client.post(charge_url(charge, 'void/'), {'reason': 'לא נמצאה עסקה בטרנזילה'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['status'], 'voided')
        self.assertIsNone(res.data['receipt'])
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))
        # A voided month is final: the next run does not send it again.
        charge_due(today=date(2026, 10, 10))
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_charged_month_cannot_be_voided_or_retried(self):
        order = self.active_order()
        charge = self.charge_row(order, OCT, Charge.STATUS_CHARGED, charged_at=timezone.now())
        self.assertEqual(self.client.post(charge_url(charge, 'void/'), {'reason': 'x'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(charge_url(charge, 'retry/')).status_code, 400)
        self.assertEqual(self.gateway_calls(), 0)

    @override_settings(CRON_TOKEN='cron-secret')
    def test_the_cron_endpoint_charges_on_get_as_vercel_calls_it(self):
        self.active_order(next_charge_date=date(2020, 1, 10))
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(CRON_URL).status_code, 401)
        self.assertEqual(self.gateway_calls(), 0)
        res = self.client.get(CRON_URL, HTTP_AUTHORIZATION='Bearer cron-secret')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['summary']['charged'], 1)
        # POST is the same endpoint: the month is taken, nothing is charged twice.
        again = self.client.post(CRON_URL, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual((again.status_code, again.data['summary']['charged']), (200, 0))
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)

    def test_status(self):
        res = self.client.get(STATUS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {key: res.data[key] for key in ('enabled', 'business_name', 'business_found')},
            {'enabled': True, 'business_name': 'סוחרים', 'business_found': True},
        )
