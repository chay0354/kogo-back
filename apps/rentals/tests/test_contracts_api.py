"""A tenancy's contracts through the API: issue a version, list them, download one, void one — each in a partner's own branches only."""
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.rentals.contracts import build_terms, issue_contract
from apps.rentals.models import RentalContract, Tenancy
from apps.rentals.tests.factories import (
    make_branch, make_customer, make_rental, make_studio, make_tenancy, make_user, sign_directly,
)
from apps.scheduling.models import ScheduleEvent
from apps.scheduling.rental_agreement.terms import terms_sha256

TENANCIES = '/api/v1/rentals/tenancies/'
CONTRACTS = '/api/v1/rentals/contracts/'

CONTRACT_KEYS = {
    'id', 'version', 'status', 'status_label', 'created_at', 'created_by_name',
    'voided_at', 'void_reason', 'terms_sha256', 'pdf_url',
    'sent_at', 'viewed_at', 'signed_at', 'signing_url', 'signing_expires_at',
    'signer_name', 'signature_id', 'signed_pdf_url',
}
CURRENT_KEYS = {
    'id', 'version', 'status', 'status_label', 'created_at', 'is_stale',
    'sent_at', 'viewed_at', 'signed_at', 'signer_name',
}

NO_SLOT = 'אין בהסכם שכירויות פעילות. יש לשייך לפחות שכירות אחת לפני הפקת חוזה'
NO_DATES = 'יש להזין להסכם תאריך התחלה ותאריך סיום לפני הפקת חוזה'
NO_AMOUNT = 'הסכום החודשי בהסכם הוא 0. יש להזין את הסכום המוסכם לפני הפקת חוזה'
ALREADY_SIGNED = 'יש כבר חוזה חתום להסכם הזה'


def contracts_url(tenancy):
    return f'{TENANCIES}{tenancy.id}/contracts/'


def pdf_url(contract_id):
    return f'{CONTRACTS}{contract_id}/pdf/'


def void_url(contract_id):
    return f'{CONTRACTS}{contract_id}/void/'


class ContractApiTestCase(APITestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.manager = make_user('manager-contracts@test', UserProfile.ROLE_MANAGER)
        self.manager.first_name, self.manager.last_name = 'דנה', 'כהן'
        self.manager.save(update_fields=['first_name', 'last_name'])
        self.client.force_authenticate(self.manager)
        self.tenancy = make_tenancy(self.branch)
        self.slot = make_rental(
            self.branch, price='100', days=(0, 2), studio=make_studio(self.branch), tenancy=self.tenancy,
        )

    def issue(self, tenancy=None):
        return self.client.post(contracts_url(tenancy or self.tenancy), {}, format='json')

    def current(self, tenancy=None):
        res = self.client.get(f'{TENANCIES}{(tenancy or self.tenancy).id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return res.data['current_contract']


class IssueTests(ContractApiTestCase):
    def test_issues_the_first_version(self):
        res = self.issue()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(set(res.data), CONTRACT_KEYS)
        self.assertEqual(res.data['version'], 1)
        self.assertEqual(res.data['status'], 'draft')
        self.assertEqual(res.data['status_label'], 'טיוטה')
        self.assertEqual(res.data['created_by_name'], 'דנה כהן')
        self.assertIsNone(res.data['voided_at'])
        self.assertEqual(res.data['void_reason'], '')
        self.assertEqual(res.data['terms_sha256'], terms_sha256(build_terms(self.tenancy)))
        self.assertEqual(res.data['pdf_url'], pdf_url(res.data['id']))

        contract = RentalContract.objects.get(pk=res.data['id'])
        self.assertEqual(contract.created_by_id, self.manager.pk)
        self.assertTrue(bytes(contract.pdf).startswith(b'%PDF'))
        self.assertTrue(contract.pdf_is_intact())

    def test_a_new_version_voids_the_unsigned_one_before_it(self):
        first = self.issue().data
        second = self.issue().data
        self.assertEqual(second['version'], 2)
        voided = RentalContract.objects.get(pk=first['id'])
        self.assertEqual(voided.status, 'void')
        self.assertEqual(voided.void_reason, 'הוחלף בגרסה 2')
        self.assertIsNotNone(voided.voided_at)

        third = self.issue().data
        self.assertEqual(third['version'], 3)
        # A void version stays as it was voided.
        self.assertEqual(RentalContract.objects.get(pk=first['id']).void_reason, 'הוחלף בגרסה 2')
        self.assertEqual(RentalContract.objects.get(pk=second['id']).void_reason, 'הוחלף בגרסה 3')

        listed = self.client.get(contracts_url(self.tenancy))
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([row['version'] for row in listed.data], [3, 2, 1])
        self.assertEqual([row['status'] for row in listed.data], ['draft', 'void', 'void'])
        self.assertEqual([row['status_label'] for row in listed.data], ['טיוטה', 'בוטל', 'בוטל'])
        self.assertEqual(set(listed.data[0]), CONTRACT_KEYS)

    def test_a_sent_or_viewed_version_is_replaced_as_well(self):
        for state in ('sent', 'viewed'):
            with self.subTest(state=state):
                before = self.issue().data
                RentalContract.objects.filter(pk=before['id']).update(status=state)
                after = self.issue().data
                replaced = RentalContract.objects.get(pk=before['id'])
                self.assertEqual(replaced.status, 'void')
                self.assertEqual(replaced.void_reason, f'הוחלף בגרסה {after["version"]}')

    def test_refuses_a_tenancy_a_contract_cannot_state(self):
        cases = [
            ('no slot at all', None, NO_SLOT),
            ('no active slot', lambda t, s: ScheduleEvent.objects.filter(pk=s.pk).update(is_active=False), NO_SLOT),
            ('no start date', lambda t, s: Tenancy.objects.filter(pk=t.pk).update(start_date=None), NO_DATES),
            ('no end date', lambda t, s: Tenancy.objects.filter(pk=t.pk).update(end_date=None), NO_DATES),
            ('amount 0', lambda t, s: Tenancy.objects.filter(pk=t.pk).update(monthly_amount=Decimal('0')), NO_AMOUNT),
        ]
        for label, breaks, message in cases:
            with self.subTest(label):
                tenancy = make_tenancy(self.branch, tenant=make_customer('שוכר', label))
                if breaks is not None:
                    breaks(tenancy, make_rental(self.branch, tenancy=tenancy))
                res = self.issue(tenancy)
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data, {'error': message})
                self.assertFalse(RentalContract.objects.filter(tenancy=tenancy).exists())

    def test_a_signed_contract_is_final(self):
        first = self.issue().data
        sign_directly(RentalContract.objects.get(pk=first['id']))
        res = self.issue()
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': ALREADY_SIGNED})
        self.assertEqual(RentalContract.objects.filter(tenancy=self.tenancy).count(), 1)
        self.assertEqual(RentalContract.objects.get(pk=first['id']).status, 'signed')


class DownloadTests(ContractApiTestCase):
    def test_downloads_the_file_as_it_was_issued(self):
        issued = self.issue().data
        res = self.client.get(pdf_url(issued['id']))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertEqual(res['Content-Disposition'], 'attachment; filename="rental-contract-v1.pdf"')
        self.assertEqual(res.content, bytes(RentalContract.objects.get(pk=issued['id']).pdf))

    def test_a_changed_file_is_never_served(self):
        issued = self.issue().data
        # Changed behind the application's back: the model would refuse it.
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE rental_contracts SET pdf = %s WHERE id = %s', [b'%PDF-1.4 not the issued file', issued['id']],
            )
        with self.assertLogs('apps.rentals.views', 'ERROR') as logs, self.assertLogs('django.request', 'ERROR'):
            res = self.client.get(pdf_url(issued['id']))
        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertNotEqual(res['Content-Type'], 'application/pdf')
        self.assertIn('error', res.data)
        self.assertIn(issued['id'], logs.output[0])

    def test_a_void_version_still_downloads(self):
        first = self.issue().data
        self.issue()
        res = self.client.get(pdf_url(first['id']))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Disposition'], 'attachment; filename="rental-contract-v1.pdf"')


class VoidTests(ContractApiTestCase):
    def test_voids_an_unsigned_version(self):
        issued = self.issue().data
        res = self.client.post(void_url(issued['id']), {'reason': '  טעות בסכום  '}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(set(res.data), CONTRACT_KEYS)
        self.assertEqual(res.data['status'], 'void')
        self.assertEqual(res.data['status_label'], 'בוטל')
        self.assertEqual(res.data['void_reason'], 'טעות בסכום')
        self.assertIsNotNone(res.data['voided_at'])
        self.assertIsNone(self.current())

    def test_a_void_or_signed_version_is_refused(self):
        voided = self.issue().data
        self.client.post(void_url(voided['id']), {'reason': 'x'}, format='json')
        again = self.client.post(void_url(voided['id']), {'reason': 'y'}, format='json')
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(again.data, {'error': 'החוזה כבר בוטל'})
        self.assertEqual(RentalContract.objects.get(pk=voided['id']).void_reason, 'x')

        signed = self.issue().data
        sign_directly(RentalContract.objects.get(pk=signed['id']))
        res = self.client.post(void_url(signed['id']), {'reason': 'z'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': 'אי אפשר לבטל חוזה חתום'})
        self.assertEqual(RentalContract.objects.get(pk=signed['id']).status, 'signed')

    def test_a_reason_too_long_is_refused(self):
        issued = self.issue().data
        res = self.client.post(void_url(issued['id']), {'reason': 'א' * 501}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(RentalContract.objects.get(pk=issued['id']).status, 'draft')


class CurrentContractTests(ContractApiTestCase):
    def test_the_tenancy_names_its_newest_contract_that_is_not_void(self):
        self.assertIsNone(self.current())
        issued = self.issue().data
        current = self.current()
        self.assertEqual(set(current), CURRENT_KEYS)
        self.assertEqual(
            (current['id'], current['version'], current['status'], current['status_label'], current['is_stale']),
            (issued['id'], 1, 'draft', 'טיוטה', False),
        )
        listed = self.client.get(TENANCIES).data
        self.assertEqual(listed[0]['current_contract'], current)

        sign_directly(RentalContract.objects.get(pk=issued['id']))
        self.assertEqual(self.current()['status'], 'signed')

    def test_is_stale_follows_the_amount(self):
        self.issue()
        url = f'{TENANCIES}{self.tenancy.id}/'
        changed = self.client.patch(url, {'monthly_amount': '1300.00'}, format='json')
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.data)
        self.assertTrue(changed.data['current_contract']['is_stale'])
        # Back to what the contract says: it matches again.
        restored = self.client.patch(url, {'monthly_amount': '1234.56'}, format='json')
        self.assertFalse(restored.data['current_contract']['is_stale'])

    def test_is_stale_follows_the_slots(self):
        self.issue()
        ScheduleEvent.objects.filter(pk=self.slot.pk).update(price_per_session=Decimal('150'))
        self.assertTrue(self.current()['is_stale'])
        # A new version states the slot as it is now.
        self.issue()
        current = self.current()
        self.assertEqual((current['version'], current['is_stale']), (2, False))

        unlinked = self.client.post(
            f'{TENANCIES}{self.tenancy.id}/unlink-slot/', {'slot_id': str(self.slot.id)}, format='json',
        )
        self.assertEqual(unlinked.status_code, status.HTTP_200_OK, unlinked.data)
        self.assertTrue(unlinked.data['current_contract']['is_stale'])

    def test_the_list_costs_the_same_queries_however_many_contracts_and_renders_no_pdf(self):
        def list_tenancies():
            refuse_to_render = mock.patch(
                'apps.rentals.contracts.generate_tenancy_contract_pdf',
                side_effect=AssertionError('a PDF was rendered to list tenancies'),
            )
            with refuse_to_render, CaptureQueriesContext(connection) as queries:
                res = self.client.get(TENANCIES)
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            return res.data, queries.captured_queries

        issue_contract(self.tenancy, self.manager)
        # Once to warm up: the first request also loads the signed-in user's profile, which then stays on the user.
        list_tenancies()
        _rows, with_one = list_tenancies()
        for i in range(3):
            tenancy = make_tenancy(self.branch, tenant=make_customer('שוכר', str(i)))
            make_rental(self.branch, tenancy=tenancy, studio=make_studio(self.branch, f'סטודיו {i + 2}'))
            issue_contract(tenancy, self.manager)
        rows, with_four = list_tenancies()

        self.assertEqual(len(rows), 4)
        self.assertEqual(len(with_four), len(with_one))
        self.assertEqual([row['current_contract']['is_stale'] for row in rows], [False] * 4)
        # The stored file is never read to list tenancies.
        self.assertFalse([q['sql'] for q in with_four if '"rental_contracts"."pdf"' in q['sql']])

    def test_a_tenancy_with_contracts_is_not_deleted(self):
        self.issue()
        self.client.post(f'{TENANCIES}{self.tenancy.id}/unlink-slot/', {'slot_id': str(self.slot.id)}, format='json')
        res = self.client.delete(f'{TENANCIES}{self.tenancy.id}/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': 'להסכם הזה כבר הופקו חוזים, ולכן אי אפשר למחוק אותו'})
        self.assertTrue(Tenancy.objects.filter(pk=self.tenancy.pk).exists())


class ContractScopingTests(ContractApiTestCase):
    """A partner reaches the contracts of their own branches' tenancies, and no others, on every endpoint."""

    def setUp(self):
        super().setUp()
        self.theirs = make_branch('רמת אביב')
        self.their_tenancy = make_tenancy(self.theirs, tenant=make_customer('יעל', 'בר', branch=self.theirs))
        make_rental(self.theirs, tenancy=self.their_tenancy)
        self.their_contract = issue_contract(self.their_tenancy, self.manager)
        self.partner = make_user('partner-contracts@test', UserProfile.ROLE_PARTNER, branches=[self.branch])
        self.client.force_authenticate(self.partner)

    def reach(self, tenancy, contract_id):
        return (
            self.client.get(contracts_url(tenancy)),
            self.issue(tenancy),
            self.client.get(pdf_url(contract_id)),
            self.client.post(void_url(contract_id), {'reason': 'x'}, format='json'),
        )

    def test_another_branchs_contracts_are_not_found(self):
        for res in self.reach(self.their_tenancy, self.their_contract.id):
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            list(RentalContract.objects.filter(tenancy=self.their_tenancy).values_list('status', flat=True)),
            ['draft'],
        )

    def test_a_partner_works_the_contracts_of_their_own_branches(self):
        issued = self.issue()
        self.assertEqual(issued.status_code, status.HTTP_201_CREATED, issued.data)
        self.assertEqual(self.client.get(contracts_url(self.tenancy)).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(pdf_url(issued.data['id'])).status_code, status.HTTP_200_OK)
        voided = self.client.post(void_url(issued.data['id']), {'reason': 'x'}, format='json')
        self.assertEqual(voided.status_code, status.HTTP_200_OK, voided.data)

    def test_a_partner_with_no_branch_reaches_none(self):
        mine = issue_contract(self.tenancy, self.manager)
        self.client.force_authenticate(make_user('partner-nobranch-contracts@test', UserProfile.ROLE_PARTNER))
        for res in self.reach(self.tenancy, mine.id):
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_instructors_are_refused(self):
        mine = issue_contract(self.tenancy, self.manager)
        self.client.force_authenticate(make_user('worker-contracts@test', UserProfile.ROLE_WORKER, branches=[self.branch]))
        for res in self.reach(self.tenancy, mine.id):
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(RentalContract.objects.get(pk=mine.pk).status, 'draft')
