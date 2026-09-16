"""Phase 5: the tenant is sent their card link on WhatsApp (rental-card-update).

Two ways in — the monthly run's decline, and the office's own send — and the
rule that matters more than either: the send never changes what the money did.

ManyChat is mocked in BillingFixture (self.whatsapp) at the same seam every
other send is mocked at, with the HTTP client underneath it rigged to fail the
test. Tranzila is mocked there too.
"""
from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatService
from apps.core.models import UserProfile
from apps.rental_billing.billing import charge_due
from apps.rental_billing.card_whatsapp import NO_LINK, NO_PHONE, send_card_link_whatsapp
from apps.rental_billing.models import TenantCardLink, TenantStandingOrder
from apps.rental_billing.tests.factories import (
    DECLINE, ORDERS_URL, TIMEOUT, BillingFixture, make_branch, make_user,
)

FRONTEND = 'https://crm.example.com'
SENT = {'sent': True, 'method': 'flow', 'subscriber_id': 42}
Order = TenantStandingOrder
Link = TenantCardLink


def send_url(order_id):
    return f'{ORDERS_URL}{order_id}/send-card-link/'


@override_settings(CRM_FRONTEND_URL=FRONTEND)
class CardLinkWhatsAppTests(BillingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    # ---- the service ----

    def test_sends_the_live_link_with_the_orders_own_monthly_total(self):
        order = self.order()
        link = self.link(order)
        self.whatsapp.return_value = dict(SENT)

        result = send_card_link_whatsapp(order)

        self.assertTrue(result['sent'])
        kwargs = self.whatsapp.call_args.kwargs
        self.assertEqual(kwargs['kind'], ManyChatService.REGISTRATION_KIND_RENTAL_CARD_UPDATE)
        self.assertEqual(kwargs['phone'], '050-1234567')
        self.assertEqual(kwargs['parent_name'], 'סטודיו אור')
        extra = kwargs['extra_fields']
        # The same field names the courses' card links write, so one User Field set serves both.
        self.assertEqual(extra['kogo_card_update_url'], f'{FRONTEND}/rc/{link.token}')
        self.assertEqual(extra['kogo_card_update_token'], link.token)
        self.assertEqual(extra['kogo_amount'], '1456.78')
        self.assertTrue(extra['kogo_support_phone'])

    def test_an_order_with_no_live_link_sends_nothing(self):
        result = send_card_link_whatsapp(self.order())
        self.assertEqual(result, {'sent': False, 'reason': NO_LINK})
        self.whatsapp.assert_not_called()

    def test_a_used_or_cancelled_link_is_not_a_live_one(self):
        order = self.order()
        self.link(order, status=Link.STATUS_CANCELLED)
        self.assertEqual(send_card_link_whatsapp(order)['reason'], NO_LINK)
        self.whatsapp.assert_not_called()

    def test_a_tenant_with_no_phone_sends_nothing(self):
        order = self.order()
        self.link(order)
        order.tenant.phone = ''
        order.tenant.save(update_fields=['phone'])
        self.assertEqual(send_card_link_whatsapp(order)['reason'], NO_PHONE)
        self.whatsapp.assert_not_called()

    def test_sending_twice_sends_the_same_url_and_never_rotates_the_link(self):
        order = self.order()
        link = self.link(order)
        self.whatsapp.return_value = dict(SENT)

        send_card_link_whatsapp(order)
        send_card_link_whatsapp(order)

        urls = {call.kwargs['extra_fields']['kogo_card_update_url'] for call in self.whatsapp.call_args_list}
        self.assertEqual(urls, {f'{FRONTEND}/rc/{link.token}'})
        self.assertEqual(Link.objects.count(), 1)

    # ---- the monthly run ----

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_decline_opens_a_card_link_and_sends_it(self):
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.whatsapp.return_value = dict(SENT)

        summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual(summary['failed'], 1, summary)
        link = Link.objects.get(standing_order=order)
        self.whatsapp.assert_called_once()
        kwargs = self.whatsapp.call_args.kwargs
        self.assertEqual(kwargs['kind'], ManyChatService.REGISTRATION_KIND_RENTAL_CARD_UPDATE)
        self.assertEqual(kwargs['extra_fields']['kogo_card_update_token'], link.token)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_charge_that_went_through_sends_nothing(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        summary = charge_due(today=date(2026, 10, 10))
        self.assertEqual(summary['charged'], 1, summary)
        self.whatsapp.assert_not_called()

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_an_unknown_answer_sends_nothing_the_office_decides_first(self):
        # A month in review may already be paid; telling the tenant their charge
        # failed and asking for another card would be the wrong message twice over.
        self.gateway.charge_with_token.return_value = dict(TIMEOUT)
        self.active_order(next_charge_date=date(2026, 10, 10))
        summary = charge_due(today=date(2026, 10, 10))
        self.assertEqual(summary['review'], 1, summary)
        self.whatsapp.assert_not_called()

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_a_send_that_blows_up_never_undoes_the_charge_or_fails_the_run(self):
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.whatsapp.side_effect = RuntimeError('ManyChat is down')

        summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual(summary['failed'], 1, summary)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_FAILED)
        self.assertTrue(Link.objects.filter(standing_order=order, status=Link.STATUS_PENDING).exists())

    def test_with_the_switch_off_the_run_sends_nothing_at_all(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        summary = charge_due(today=date(2026, 10, 10))
        self.assertTrue(summary['disabled'])
        self.whatsapp.assert_not_called()
        self.assertEqual(self.gateway_calls(), 0)

    # ---- the office's own send ----

    def test_the_endpoint_sends_and_answers_with_the_order(self):
        order = self.order()
        self.link(order)
        self.whatsapp.return_value = dict(SENT)

        res = self.client.post(send_url(order.pk), {}, format='json')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['whatsapp']['sent'])
        self.assertEqual(res.data['standing_order']['id'], str(order.pk))

    def test_the_endpoint_works_with_the_switch_off(self):
        # A message is not a charge. The page the link opens refuses on its own.
        order = self.order()
        self.link(order)
        self.whatsapp.return_value = dict(SENT)
        res = self.client.post(send_url(order.pk), {}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

    def test_without_a_link_the_endpoint_refuses_and_says_what_to_do(self):
        res = self.client.post(send_url(self.order().pk), {}, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('צרו קישור', res.data['error'])
        self.whatsapp.assert_not_called()

    def test_a_refusal_from_manychat_is_a_502_never_a_silent_success(self):
        order = self.order()
        self.link(order)
        self.whatsapp.return_value = {'sent': False, 'reason': 'send_flow_failed', 'error': 'boom'}
        res = self.client.post(send_url(order.pk), {}, format='json')
        self.assertEqual(res.status_code, 502, res.data)
        self.assertEqual(res.data['whatsapp']['reason'], 'send_flow_failed')

    def test_a_worker_may_not_send(self):
        order = self.order()
        self.link(order)
        self.client.force_authenticate(make_user('worker-card-wa@test', UserProfile.ROLE_WORKER))
        res = self.client.post(send_url(order.pk), {}, format='json')
        self.assertEqual(res.status_code, 403)
        self.whatsapp.assert_not_called()

    def test_a_partner_of_another_branch_never_reaches_the_order(self):
        order = self.order()
        self.link(order)
        other = make_branch('רמת אביב')
        partner = make_user('partner-card-wa@test', UserProfile.ROLE_PARTNER, branches=[other])
        self.client.force_authenticate(partner)
        res = self.client.post(send_url(order.pk), {}, format='json')
        self.assertEqual(res.status_code, 404)
        self.whatsapp.assert_not_called()

    def test_no_request_ever_reaches_manychat_itself(self):
        # The seam above is a mock; this proves nothing slipped past it.
        order = self.order()
        self.link(order)
        with patch('apps.rental_billing.card_whatsapp.ManyChatService.notify_registration',
                   wraps=ManyChatService(api_key='x').notify_registration):
            result = send_card_link_whatsapp(order)
        # Unconfigured in tests, so it stops before any HTTP call is made.
        self.assertFalse(result['sent'])
