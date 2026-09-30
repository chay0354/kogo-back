"""
Check plans the D2 way: a receipt when the checks arrive, a tax invoice on each
check's day — dated the day it is issued and paid by the check on the receipt —
and a credit note when a check bounces or an unpaid invoice's plan is cancelled.
"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents import settlement
from apps.documents.check_plans import (
    CheckAlreadyBounced,
    bounce_check,
    cancel_check_plan,
    issue_due_check_invoices,
    register_check_plan,
)
from apps.documents.document_pdf import build_document_layout
from apps.documents.models import CheckItem, CheckPlan, DocumentSettlement, FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'
PLANS = '/api/v1/documents/check-plans/'


def check(day, number, amount='350.00', **extra):
    return {'date': day.isoformat(), 'check_number': number, 'amount': amount,
            'bank': 'לאומי', 'branch': '123', 'account_number': '456789', **extra}


def fields(doc):
    return {field.label: field.value for field in build_document_layout(doc).payment_fields}


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class IssueTests(Fixture, TestCase):
    def test_the_receipt_lists_every_check_and_the_due_ones_invoice_is_paid_by_it(self, _mail):
        earlier = self.today - timedelta(days=10)
        plan = register_check_plan(child_id=str(self.kid.id), checks=[
            check(earlier, '1001'), check(self.today + timedelta(days=30), '1002'),
        ])
        rows = list(plan.receipt.payments.order_by('reference').values_list(
            'reference', 'check_bank', 'check_branch', 'check_account', 'check_date'))
        self.assertEqual(rows[0], ('1001', 'לאומי', '123', '456789', earlier))
        self.assertEqual(len(rows), 2)

        item = plan.items.get(check_number='1001')
        invoice = item.tax_invoice
        # Dated the day it is issued, never the check's earlier day; the check's day is in the note.
        self.assertEqual(invoice.document_date, self.today)
        self.assertIn(f'לפירעון {earlier:%d/%m/%Y}', invoice.customer_notes)
        self.assertIn(plan.receipt.document_number, invoice.customer_notes)
        row = DocumentSettlement.objects.get(invoice=invoice)
        self.assertEqual((row.payer_id, row.amount), (plan.receipt_id, Decimal('350.00')))
        self.assertEqual(settlement.balance_of(invoice).status, 'paid')
        page = fields(invoice)
        self.assertEqual(page['סטטוס'], 'שולם')
        self.assertEqual(page['שולם בקבלה'], plan.receipt.document_number)
        self.assertEqual(page['יתרה לתשלום'], '₪0.00')

    def test_a_check_dated_before_the_runs_latest_invoice_does_not_break_the_order(self, _mail):
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        earlier = self.earlier_this_year()
        plan = register_check_plan(child_id=str(self.kid.id), checks=[check(earlier, '2001')])
        item = plan.items.get()
        self.assertEqual(item.status, 'invoiced')
        self.assertEqual(item.tax_invoice.document_date, self.today)

    def test_running_twice_issues_and_settles_once(self, _mail):
        plan = register_check_plan(child_id=str(self.kid.id), checks=[check(self.today, '3001')])
        again = issue_due_check_invoices(plan=plan)
        self.assertEqual(again['issued'], 0)
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), 1)
        self.assertEqual(DocumentSettlement.objects.count(), 1)

    def test_a_later_day_passed_in_does_not_issue_a_check_before_its_date(self, _mail):
        plan = register_check_plan(child_id=str(self.kid.id), checks=[check(self.today + timedelta(days=5), '4001')])
        summary = issue_due_check_invoices(today=self.today + timedelta(days=10), plan=plan)
        self.assertEqual(summary['issued'], 0)
        self.assertEqual(plan.items.get().status, 'pending')

    def test_a_plan_invoice_from_before_settlements_counts_as_paid(self, _mail):
        plan = register_check_plan(child_id=str(self.kid.id), checks=[check(self.today, '5001')])
        invoice = plan.items.get().tax_invoice
        DocumentSettlement.objects.all().delete()  # as production holds it: no row
        self.assertEqual(settlement.balance_of(invoice).status, 'paid')
        self.assertEqual(settlement.applied_amounts([plan.receipt])[plan.receipt_id], Decimal('350.00'))


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class BounceTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.plan = register_check_plan(child_id=str(self.kid.id), checks=[
            check(self.today, '6001'), check(self.today + timedelta(days=30), '6002'),
        ])
        self.cashed = self.plan.items.get(check_number='6001')
        self.later = self.plan.items.get(check_number='6002')

    def test_a_bounced_check_credits_its_invoice_and_voids_its_settlement(self, _mail):
        result = bounce_check(self.plan.pk, self.cashed.pk, reason='אין כיסוי')
        item = CheckItem.objects.get(pk=self.cashed.pk)
        self.assertIsNotNone(item.bounced_at)
        credit = result['credit_note']
        self.assertEqual(item.credit_note, credit)
        self.assertEqual((credit.document_type, credit.total_amount), ('credit_invoice', Decimal('350.00')))
        self.assertEqual(credit.linked_document, item.tax_invoice)
        self.assertEqual(credit.linked_document_number, item.tax_invoice.document_number)
        self.assertIn("צ'ק מס' 6001", credit.credit_reason)
        self.assertIsNotNone(DocumentSettlement.objects.get(invoice=item.tax_invoice).voided_at)
        balance = settlement.balance_of(item.tax_invoice)
        self.assertEqual((balance.paid, balance.open, balance.status), (Decimal('0'), Decimal('0.00'), 'credited'))

    def test_bouncing_twice_is_refused(self, _mail):
        bounce_check(self.plan.pk, self.cashed.pk)
        with self.assertRaises(CheckAlreadyBounced):
            bounce_check(self.plan.pk, self.cashed.pk)
        self.assertEqual(FormalDocument.objects.filter(document_type='credit_invoice').count(), 1)

    def test_a_check_that_bounced_before_its_day_gets_no_invoice(self, _mail):
        bounce_check(self.plan.pk, self.later.pk)
        item = CheckItem.objects.get(pk=self.later.pk)
        self.assertEqual((item.status, item.credit_note_id), ('cancelled', None))
        issue_due_check_invoices(today=self.today + timedelta(days=40), plan=self.plan)
        item.refresh_from_db()
        self.assertIsNone(item.tax_invoice_id)
        self.assertFalse(FormalDocument.objects.filter(document_type='credit_invoice').exists())

    def test_a_replacement_check_is_a_plan_of_its_own(self, _mail):
        result = bounce_check(self.plan.pk, self.cashed.pk, replacement=check(self.today + timedelta(days=7), '6101'))
        replacement = result['replacement_plan']
        self.assertEqual(replacement.receipt.document_type, 'receipt')
        self.assertEqual(replacement.receipt.total_amount, Decimal('350.00'))
        new_item = replacement.items.get()
        self.assertEqual(CheckItem.objects.get(pk=self.cashed.pk).replaced_by, new_item)
        self.assertEqual(new_item.status, 'pending')

    def test_a_replacement_without_a_date_is_refused_and_nothing_changes(self, _mail):
        from apps.documents.check_plans import CheckPlanError

        with self.assertRaises(CheckPlanError):
            bounce_check(self.plan.pk, self.cashed.pk, replacement={'amount': '350'})
        self.assertIsNone(CheckItem.objects.get(pk=self.cashed.pk).bounced_at)
        self.assertFalse(FormalDocument.objects.filter(document_type='credit_invoice').exists())


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CancelTests(Fixture, TestCase):
    def test_cancelling_stops_future_invoices_and_credits_nothing_that_was_paid(self, _mail):
        plan = register_check_plan(child_id=str(self.kid.id), checks=[
            check(self.today, '7001'), check(self.today + timedelta(days=30), '7002'),
        ])
        result = cancel_check_plan(plan.pk)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'cancelled')
        self.assertIsNotNone(plan.cancelled_at)
        self.assertEqual(result['credit_notes'], [])
        self.assertEqual(plan.items.get(check_number='7002').status, 'cancelled')
        self.assertEqual(issue_due_check_invoices(today=self.today + timedelta(days=40))['issued'], 0)

    def test_an_invoice_nothing_paid_is_credited_once(self, _mail):
        # A plan without a receipt: its invoice was never settled.
        plan = CheckPlan.objects.create(child=self.kid, status='active', description='בלי קבלה')
        CheckItem.objects.create(plan=plan, due_date=self.today, amount=Decimal('250.00'), check_number='8001')
        issue_due_check_invoices(plan=plan)
        plan.status = 'active'
        plan.save(update_fields=['status'])
        result = cancel_check_plan(plan.pk)
        self.assertEqual([doc.total_amount for doc in result['credit_notes']], [Decimal('250.00')])
        again = cancel_check_plan(plan.pk)
        self.assertTrue(again['already_cancelled'])
        self.assertEqual(FormalDocument.objects.filter(document_type='credit_invoice').count(), 1)


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ApiTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(make_user('checks-d2@test', UserProfile.ROLE_MANAGER))

    def receipt(self, checks, **extra):
        payload = {
            'document_type': 'receipt', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'document_date': str(self.today),
            'receipt_details': {
                'payment_method': "צ'ק", 'invoice_per_check': True,
                # The form sends amounts as numbers.
                'checks': [{**row, 'amount': float(row['amount']), 'confirmed': True} for row in checks],
            },
        }
        payload.update(extra)
        return self.client.post(CREATE, payload, format='json')

    def test_the_forms_invoice_per_check_makes_a_plan_of_the_receipt(self, _mail):
        res = self.receipt([check(self.today, '9001'), check(self.today + timedelta(days=30), '9002')])
        self.assertEqual(res.status_code, 201, res.data)
        plan = CheckPlan.objects.get(pk=res.data['check_plan_id'])
        self.assertEqual(str(plan.receipt_id), res.data['id'])
        due = plan.items.get(check_number='9001')
        self.assertEqual(due.status, 'invoiced')
        self.assertEqual(DocumentSettlement.objects.get(invoice=due.tax_invoice).payer_id, plan.receipt_id)
        self.assertEqual(plan.items.get(check_number='9002').status, 'pending')

    def test_a_check_without_a_date_refuses_the_whole_receipt(self, _mail):
        res = self.receipt([{'check_number': '9101', 'amount': 100, 'bank': 'לאומי', 'date': ''}])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(FormalDocument.objects.filter(document_type='receipt').exists())
        self.assertFalse(CheckPlan.objects.exists())

    def test_invoice_per_check_is_for_a_private_customer_only(self, _mail):
        res = self.client.post(CREATE, {
            'document_type': 'receipt', 'client_type': 'business', 'business_customer_id': str(self.kid.id),
            'receipt_details': {'payment_method': "צ'ק", 'invoice_per_check': True, 'checks': []},
        }, format='json')
        self.assertEqual(res.status_code, 400, res.data)

    def test_bounce_and_cancel_through_the_api(self, _mail):
        plan = register_check_plan(child_id=str(self.kid.id), checks=[
            check(self.today, '9201'), check(self.today + timedelta(days=30), '9202'),
        ])
        item = plan.items.get(check_number='9201')
        res = self.client.post(f'{PLANS}{plan.id}/bounce/', {'item_id': str(item.id), 'reason': 'חזר'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['credit_note_number'].startswith('CR-'))
        bounced = next(row for row in res.data['plan']['items'] if row['id'] == str(item.id))
        self.assertTrue(bounced['bounced_at'])
        self.assertEqual(bounced['credit_note_number'], res.data['credit_note_number'])

        again = self.client.post(f'{PLANS}{plan.id}/bounce/', {'item_id': str(item.id)}, format='json')
        self.assertEqual(again.status_code, 409, again.data)

        other = register_check_plan(child_id=str(self.kid.id), checks=[check(self.today, '9301')])
        wrong = self.client.post(f'{PLANS}{plan.id}/bounce/', {'item_id': str(other.items.get().id)}, format='json')
        self.assertEqual(wrong.status_code, 404, wrong.data)

        cancelled = self.client.post(f'{PLANS}{plan.id}/cancel/', {'reason': 'עזב'}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual((cancelled.data['status'], cancelled.data['credit_notes']), ('cancelled', []))
        self.assertTrue(cancelled.data['cancelled_at'])


@override_settings(TRANZILA_BILLING_TERMINAL='')
class TwoRunsAtOnceTests(TransactionTestCase):
    """The hourly cron and beat overlapping on one due check: one invoice, one settlement."""

    serialized_rollback = True

    def test_one_invoice_and_one_settlement(self):
        fixture = Fixture()
        fixture.setUp()
        plan = register_check_plan(
            child_id=str(fixture.kid.id), checks=[check(fixture.today + timedelta(days=3), '9901')],
        )
        # The check's day comes: moved back, as time would.
        CheckItem.objects.filter(plan=plan).update(due_date=fixture.today)
        start = threading.Barrier(2)
        results = []

        def run():
            try:
                start.wait(timeout=10)
                results.append(issue_due_check_invoices(plan=plan)['issued'])
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(results), [0, 1], results)
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), 1)
        self.assertEqual(DocumentSettlement.objects.count(), 1)


@override_settings(TRANZILA_BILLING_TERMINAL='')
class PartnerTests(Fixture, APITestCase):
    def test_another_branchs_plan_cannot_be_bounced_or_cancelled(self):
        from apps.rentals.tests.factories import make_branch
        from apps.rentals.tests.factories import make_user as make_scoped_user

        plan = register_check_plan(child_id=str(self.kid.id), checks=[check(self.today + timedelta(days=9), '9501')])
        self.client.force_authenticate(
            make_scoped_user('checks-partner@test', UserProfile.ROLE_PARTNER, [make_branch('אחר')]),
        )
        item = plan.items.get()
        self.assertEqual(self.client.post(f'{PLANS}{plan.id}/bounce/', {'item_id': str(item.id)},
                                          format='json').status_code, 404)
        self.assertEqual(self.client.post(f'{PLANS}{plan.id}/cancel/', {}, format='json').status_code, 404)
        item.refresh_from_db()
        self.assertIsNone(item.bounced_at)
