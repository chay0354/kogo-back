"""
A partner reaches their own branches' documents and plans only (apps/documents/partner_scope.py).

The list is filtered; another branch's document is a 404 in every action that
finds one by id (detail, PDF, allocation number, reminder, plan cancel);
creating for another branch is refused; the documents ledger shows the
partner's local rows and never asks Tranzila. Managers are unchanged.
"""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice
from apps.customers.models import BusinessCustomer, Child, Family
from apps.documents.models import CashPlan, CheckItem, CheckPlan, FormalDocument
from apps.documents.tests.test_register import RegisterFixture, make_user

DOCS = '/api/v1/documents/documents/'
CREATE = f'{DOCS}create-document/'
LEDGER = f'{DOCS}tranzila/'
CHECKS = '/api/v1/documents/check-plans/'
CASH = '/api/v1/documents/cash-plans/'


def rows_of(response):
    data = response.data
    return data['results'] if isinstance(data, dict) and 'results' in data else data


class PartnerFixture(RegisterFixture):
    def setUp(self):
        super().setUp()
        self.south_family = Family.objects.create(name='משפחת לוי', branch=self.south)
        self.south_kid = Child.objects.create(
            family=self.south_family, first_name='דן', last_name='לוי',
            birth_date=date(2015, 5, 5), gender='male', status='active',
        )
        # Numbers of the closed shared run, so the documents a test issues through
        # the API (TI-…) never meet them.
        self.north_doc = self.document('2026-0101', branch=self.north, child=self.kid)
        self.south_doc = self.document('2026-0102', branch=self.south, child=self.south_kid)
        # No branch of its own: filed under the child's family's branch (north).
        self.family_doc = self.document('2026-0103', branch=None, child=self.kid, kind='receipt')
        self.partner = self.partner_of(self.north)

    def partner_of(self, *branches, name='partner-docs@test'):
        user = make_user(name, UserProfile.ROLE_PARTNER)
        user.profile.assigned_branches.set(branches)
        return user

    @staticmethod
    def document(number, *, branch, child, kind='tax_invoice'):
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='existing', child=child, branch=branch,
            document_date=date(2026, 8, 10), subtotal=Decimal('100.00'), vat_amount=Decimal('18.00'),
            total_amount=Decimal('118.00'),
        )

    def payload(self, child, **extra):
        body = {
            'document_type': 'tax_invoice',
            'client_type': 'existing',
            'child_id': str(child.id),
            'invoice_details': {
                'document_date': '2026-09-02',
                'line_items': [{'description': 'חוג ספטמבר', 'quantity': 1, 'price': '100.00'}],
            },
        }
        body.update(extra)
        return body


class PartnerDocumentsTests(PartnerFixture, APITestCase):
    def test_the_list_holds_only_the_partners_branches(self):
        self.client.force_authenticate(self.partner)
        res = self.client.get(DOCS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {row['document_number'] for row in rows_of(res)},
            {'2026-0101', '2026-0103'},
        )

    def test_the_manager_still_sees_every_branch(self):
        self.client.force_authenticate(self.manager)
        numbers = {row['document_number'] for row in rows_of(self.client.get(DOCS))}
        self.assertEqual(numbers, {'2026-0101', '2026-0102', '2026-0103'})

    def test_another_branchs_document_is_not_found_in_any_action(self):
        self.client.force_authenticate(self.partner)
        south = f'{DOCS}{self.south_doc.id}/'
        self.assertEqual(self.client.get(south).status_code, 404)
        self.assertEqual(self.client.get(f'{south}pdf/').status_code, 404)
        self.assertEqual(
            self.client.post(f'{south}allocation-number/', {'allocation_number': '123456789'}, format='json')
            .status_code, 404,
        )
        with patch('apps.documents.views.send_mail') as mail:
            self.assertEqual(self.client.post(f'{south}send-reminder/').status_code, 404)
        mail.assert_not_called()
        self.south_doc.refresh_from_db()
        self.assertEqual(self.south_doc.allocation_number, '')

    def test_their_own_documents_open(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual(self.client.get(f'{DOCS}{self.north_doc.id}/').status_code, 200)
        self.assertEqual(self.client.get(f'{DOCS}{self.family_doc.id}/pdf/').status_code, 200)
        res = self.client.post(f'{DOCS}{self.north_doc.id}/allocation-number/',
                               {'allocation_number': '123456789'}, format='json')
        self.assertEqual(res.status_code, 200)

    def test_a_partner_with_no_branch_sees_nothing(self):
        self.client.force_authenticate(self.partner_of(name='partner-none@test'))
        self.assertEqual(rows_of(self.client.get(DOCS)), [])
        self.assertEqual(self.client.get(f'{DOCS}{self.north_doc.id}/').status_code, 404)

    def test_a_partner_cannot_create_for_another_branchs_child(self):
        self.client.force_authenticate(self.partner)
        res = self.client.post(CREATE, self.payload(self.south_kid), format='json')
        self.assertEqual(res.status_code, 404)
        self.assertFalse(FormalDocument.objects.filter(child=self.south_kid).exclude(pk=self.south_doc.pk).exists())

    def test_a_partner_cannot_file_a_document_under_another_branch(self):
        self.client.force_authenticate(self.partner)
        res = self.client.post(CREATE, self.payload(self.kid, branch_id=str(self.south.id)), format='json')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(FormalDocument.objects.count(), 3)

    def test_a_partner_creates_for_their_own_child(self):
        self.client.force_authenticate(self.partner)
        res = self.client.post(CREATE, self.payload(self.kid), format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=res.data['id']).branch_id, self.north.id)

    def test_a_merchant_with_no_branch_takes_the_partners_only_branch(self):
        merchant = BusinessCustomer.objects.create(first_name='עסק', last_name='בלי סניף')
        self.client.force_authenticate(self.partner)
        body = self.payload(self.kid, client_type='business', business_customer_id=str(merchant.id))
        body.pop('child_id')
        res = self.client.post(CREATE, body, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.branch_id, self.north.id)
        # ...and so it is in their list afterwards.
        self.assertIn(doc.document_number, {row['document_number'] for row in rows_of(self.client.get(DOCS))})

    def test_a_partner_of_two_branches_is_asked_to_choose_one(self):
        merchant = BusinessCustomer.objects.create(first_name='עסק', last_name='בלי סניף')
        self.client.force_authenticate(self.partner_of(self.north, self.south, name='partner-two@test'))
        body = self.payload(self.kid, client_type='business', business_customer_id=str(merchant.id))
        body.pop('child_id')
        self.assertEqual(self.client.post(CREATE, body, format='json').status_code, 400)

    def test_a_credit_note_for_another_branchs_document_is_refused(self):
        self.client.force_authenticate(self.partner)
        body = {
            'document_type': 'credit_invoice', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': '2026-09-02', 'linked_invoice_id': self.south_doc.document_number,
                'credit_reason': 'החזר', 'credit_amount_before_vat': '10.00',
            },
        }
        self.assertEqual(self.client.post(CREATE, body, format='json').status_code, 403)

    def test_the_manager_creates_as_before(self):
        self.client.force_authenticate(self.manager)
        res = self.client.post(CREATE, self.payload(self.south_kid), format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=res.data['id']).branch_id, self.south.id)


class PartnerPlansTests(PartnerFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.north_checks = CheckPlan.objects.create(child=self.kid, branch=self.north, description='צפון')
        self.south_checks = CheckPlan.objects.create(child=self.south_kid, branch=self.south, description='דרום')
        CheckItem.objects.create(plan=self.south_checks, due_date=date(2026, 11, 1), amount=Decimal('100'))
        self.north_cash = CashPlan.objects.create(
            child=self.kid, branch=self.north, total_amount=Decimal('480'), monthly_amount=Decimal('240'),
        )
        self.south_cash = CashPlan.objects.create(
            child=self.south_kid, branch=self.south, total_amount=Decimal('480'), monthly_amount=Decimal('240'),
        )

    def test_check_plans_are_filtered_and_another_branchs_cannot_be_cancelled(self):
        self.client.force_authenticate(self.partner)
        listed = self.client.get(CHECKS)
        self.assertEqual({row['id'] for row in listed.data}, {str(self.north_checks.id)})
        self.assertEqual(self.client.get(f'{CHECKS}{self.south_checks.id}/').status_code, 404)
        self.assertEqual(self.client.post(f'{CHECKS}{self.south_checks.id}/cancel/').status_code, 404)
        self.south_checks.refresh_from_db()
        self.assertEqual(self.south_checks.status, 'active')

    def test_cash_plans_are_filtered(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual({row['id'] for row in self.client.get(CASH).data}, {str(self.north_cash.id)})
        self.assertEqual(self.client.get(f'{CASH}{self.south_cash.id}/').status_code, 404)

    def test_the_manager_sees_every_plan(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(len(self.client.get(CHECKS).data), 2)
        self.assertEqual(len(self.client.get(CASH).data), 2)

    def test_no_plan_for_another_branchs_child(self):
        self.client.force_authenticate(self.partner)
        checks = self.client.post(CHECKS, {
            'child_id': str(self.south_kid.id),
            'checks': [{'date': '2026-10-01', 'bank': '12', 'branch': '600', 'account_number': '1',
                        'check_number': '1', 'amount': '100'}],
        }, format='json')
        cash = self.client.post(CASH, {
            'child_id': str(self.south_kid.id), 'total_amount': '480', 'monthly_amount': '240',
        }, format='json')
        self.assertEqual((checks.status_code, cash.status_code), (404, 404))
        self.assertEqual((CheckPlan.objects.count(), CashPlan.objects.count()), (2, 2))

    def test_no_plan_for_a_lesson_in_another_branch(self):
        course = TestDataFactory.create_course(branch=self.south)
        lesson = TestDataFactory.create_lesson(course=course)
        self.client.force_authenticate(self.partner)
        res = self.client.post(CASH, {
            'child_id': str(self.kid.id), 'lesson_id': str(lesson.id),
            'total_amount': '480', 'monthly_amount': '240',
        }, format='json')
        self.assertEqual(res.status_code, 403)


class PartnerLedgerTests(PartnerFixture, APITestCase):
    def setUp(self):
        super().setUp()
        on = datetime(2026, 8, 10, 12, tzinfo=dt_timezone.utc)
        Invoice.objects.create(
            invoice_number='IR-2026-000001', family=self.family, branch=self.north, amount=Decimal('236'),
            status='paid', payment_method='credit_card', invoice_date=on,
        )
        Invoice.objects.create(
            invoice_number='IR-2026-000002', family=self.south_family, branch=self.south, amount=Decimal('236'),
            status='paid', payment_method='credit_card', invoice_date=on,
        )
        self.store_sale(branch=self.south)

    def numbers(self, response):
        self.assertEqual(response.status_code, 200)
        return {row['document_number'] for row in response.data['documents']}

    def test_a_partner_gets_their_branches_rows_and_tranzila_is_never_asked(self):
        self.client.force_authenticate(self.partner)
        with patch('apps.core.tranzila_ledger._tranzila_client', side_effect=AssertionError('no Tranzila')) as client:
            res = self.client.get(LEDGER, {'start_date': '2026-08-01', 'end_date': '2026-08-31'})
        client.assert_not_called()
        self.assertEqual(self.numbers(res), {'2026-0101', '2026-0103', 'IR-2026-000001'})

    def test_the_manager_still_gets_every_row(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(LEDGER, {'start_date': '2026-08-01', 'end_date': '2026-08-31', 'local_only': '1'})
        numbers = self.numbers(res)
        self.assertTrue({'2026-0101', '2026-0102', '2026-0103',
                         'IR-2026-000001', 'IR-2026-000002'} <= numbers)
        self.assertEqual(len(numbers), 6)  # and the south store sale
