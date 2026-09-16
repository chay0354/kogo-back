"""Phase 5: the office sends a tenant their signing link on WhatsApp (rental-contract).

Nothing here may reach ManyChat. The seam is the one every other send is
mocked at — ManyChatService.notify_registration, imported into the module doing
the sending — and the patches assert on the kwargs that would have gone out.
"""
from unittest.mock import patch

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.manychat_service import ManyChatService
from apps.core.models import UserProfile
from apps.rentals.contract_whatsapp import NO_LINK, NO_PHONE, send_contract_link_whatsapp
from apps.rentals.contracts import issue_contract
from apps.rentals.models import RentalContract
from apps.rentals.signing import cancel_signing_link, issue_signing_link
from apps.rentals.tenant_whatsapp import build_tenant_whatsapp_context
from apps.rentals.tests.factories import (
    make_branch, make_customer, make_rental, make_studio, make_tenancy, make_user,
)

FRONTEND = 'https://crm.example.com'
CONTRACTS = '/api/v1/rentals/contracts/'
SENT = {'sent': True, 'method': 'flow', 'subscriber_id': 42}

SEAM = 'apps.rentals.contract_whatsapp.ManyChatService.notify_registration'


def send_url(contract_id):
    return f'{CONTRACTS}{contract_id}/send-whatsapp/'


@override_settings(CRM_FRONTEND_URL=FRONTEND)
class ContractWhatsAppTests(APITestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.manager = make_user('manager-contract-wa@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.tenancy = make_tenancy(self.branch)
        make_rental(self.branch, price='100', days=(0,), studio=make_studio(self.branch), tenancy=self.tenancy)
        self.contract = issue_contract(self.tenancy, self.manager)

    def linked(self):
        return issue_signing_link(self.contract)

    # ---- the service ----

    def test_sends_the_live_link_with_the_tenant_fields(self):
        contract = self.linked()
        with patch(SEAM, return_value=dict(SENT)) as send:
            result = send_contract_link_whatsapp(contract)
        self.assertTrue(result['sent'])
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['kind'], ManyChatService.REGISTRATION_KIND_RENTAL_CONTRACT)
        self.assertEqual(kwargs['phone'], '050-1234567')
        self.assertEqual(kwargs['parent_name'], 'סטודיו אור')
        self.assertEqual(kwargs['branch_name'], 'פלורנטין')
        extra = kwargs['extra_fields']
        self.assertEqual(extra['kogo_rental_sign_url'], f'{FRONTEND}/s/{contract.sign_token}')
        # The amount the tenant is being asked to sign for, off the frozen terms.
        self.assertEqual(extra['kogo_amount'], str(contract.terms['monthly_total']))
        self.assertTrue(extra['kogo_support_phone'])

    def test_a_contract_with_no_live_link_sends_nothing(self):
        with patch(SEAM) as send:
            result = send_contract_link_whatsapp(self.contract)
        self.assertEqual(result, {'sent': False, 'reason': NO_LINK})
        send.assert_not_called()

    def test_a_withdrawn_link_sends_nothing(self):
        contract = self.linked()
        cancel_signing_link(contract)
        with patch(SEAM) as send:
            result = send_contract_link_whatsapp(RentalContract.objects.get(pk=contract.pk))
        self.assertEqual(result['reason'], NO_LINK)
        send.assert_not_called()

    def test_a_tenant_with_no_phone_sends_nothing(self):
        contract = self.linked()
        self.tenancy.tenant.phone = '   '
        self.tenancy.tenant.save(update_fields=['phone'])
        with patch(SEAM) as send:
            result = send_contract_link_whatsapp(RentalContract.objects.get(pk=contract.pk))
        self.assertEqual(result['reason'], NO_PHONE)
        send.assert_not_called()

    def test_the_phone_is_the_tenants_now_not_the_one_frozen_into_the_contract(self):
        # The office fixes a wrong number by editing the tenant; re-sending has
        # to reach the corrected one, not the one the contract was issued with.
        contract = self.linked()
        self.assertEqual(contract.terms['tenant']['phone'], '050-1234567')
        self.tenancy.tenant.phone = '052-7654321'
        self.tenancy.tenant.save(update_fields=['phone'])
        with patch(SEAM, return_value=dict(SENT)) as send:
            send_contract_link_whatsapp(RentalContract.objects.get(pk=contract.pk))
        self.assertEqual(send.call_args.kwargs['phone'], '052-7654321')

    def test_sending_twice_sends_the_same_url_and_never_rotates_the_link(self):
        contract = self.linked()
        with patch(SEAM, return_value=dict(SENT)) as send:
            send_contract_link_whatsapp(contract)
            send_contract_link_whatsapp(RentalContract.objects.get(pk=contract.pk))
        urls = {call.kwargs['extra_fields']['kogo_rental_sign_url'] for call in send.call_args_list}
        self.assertEqual(urls, {f'{FRONTEND}/s/{contract.sign_token}'})
        self.assertEqual(RentalContract.objects.get(pk=contract.pk).sign_token, contract.sign_token)

    # ---- the endpoint ----

    def test_the_office_endpoint_sends_and_answers_with_the_contract(self):
        contract = self.linked()
        with patch(SEAM, return_value=dict(SENT)) as send:
            res = self.client.post(send_url(contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertTrue(res.data['whatsapp']['sent'])
        self.assertEqual(res.data['contract']['id'], str(contract.pk))
        send.assert_called_once()

    def test_without_a_link_the_endpoint_refuses_and_says_what_to_do(self):
        with patch(SEAM) as send:
            res = self.client.post(send_url(self.contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn('צרו קישור', res.data['error'])
        send.assert_not_called()

    def test_a_refusal_from_manychat_is_a_502_never_a_silent_success(self):
        contract = self.linked()
        with patch(SEAM, return_value={'sent': False, 'reason': 'lookup_failed', 'error': 'boom'}):
            res = self.client.post(send_url(contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_502_BAD_GATEWAY, res.data)
        self.assertEqual(res.data['whatsapp']['reason'], 'lookup_failed')

    def test_a_worker_may_not_send(self):
        contract = self.linked()
        worker = make_user('worker-contract-wa@test', UserProfile.ROLE_WORKER)
        self.client.force_authenticate(worker)
        with patch(SEAM) as send:
            res = self.client.post(send_url(contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        send.assert_not_called()

    def test_a_partner_of_another_branch_never_reaches_the_contract(self):
        contract = self.linked()
        other = make_branch('רמת אביב')
        partner = make_user('partner-contract-wa@test', UserProfile.ROLE_PARTNER, branches=[other])
        self.client.force_authenticate(partner)
        with patch(SEAM) as send:
            res = self.client.post(send_url(contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        send.assert_not_called()


class TenantContextTests(APITestCase):
    """The fields a tenant's message carries, whatever the office typed on the record."""

    def test_no_phone_is_the_one_reason_there_is_no_context(self):
        self.assertIsNone(build_tenant_whatsapp_context(make_customer(phone='')))
        self.assertIsNone(build_tenant_whatsapp_context(None))

    def test_the_tenant_name_fills_both_name_fields_so_neither_renders_empty(self):
        branch = make_branch('פלורנטין')
        branch.address = 'הרצל 1'
        branch.save(update_fields=['address'])
        ctx = build_tenant_whatsapp_context(
            make_customer('סטודיו', 'אור', phone='050-1234567'), branch=branch,
        )
        self.assertEqual(ctx['parent_name'], 'סטודיו אור')
        self.assertEqual(ctx['child_name'], 'סטודיו אור')
        self.assertEqual(ctx['course_name'], 'שכירות סטודיו')
        self.assertEqual(ctx['branch_name'], 'פלורנטין')
        self.assertEqual(ctx['location'], 'הרצל 1')
        # An agreement has no weekly hour; empty values are skipped in ManyChat.
        self.assertEqual((ctx['day_name'], ctx['start_time'], ctx['end_time']), ('', '', ''))

    def test_lookup_names_include_the_whole_name_and_each_word(self):
        ctx = build_tenant_whatsapp_context(make_customer('סטודיו', 'אור', phone='0501234567'))
        self.assertEqual(ctx['lookup_names'][0], 'סטודיו אור')
        self.assertIn('סטודיו', ctx['lookup_names'])
        self.assertIn('אור', ctx['lookup_names'])

    def test_without_a_branch_there_is_no_branch_name(self):
        ctx = build_tenant_whatsapp_context(make_customer(phone='0501234567'))
        self.assertEqual(ctx['branch_name'], '')
        self.assertEqual(ctx['location'], '')
