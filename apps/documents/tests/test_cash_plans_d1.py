"""
Cash plans after D1: what happens to the plans production already holds (mode
NULL — a receipt, then a document a month), and cancelling either kind.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents import service, settlement
from apps.documents.cash_plans import (
    CashPlanError,
    cancel_cash_plan,
    issue_due_cash_documents,
    month_first,
    register_cash_plan,
    unused_amount,
)
from apps.documents.models import CashPlan, CashPlanMonth, DocumentSettlement, FormalDocument
from apps.documents.numbering import israel_today
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

PLANS = '/api/v1/documents/cash-plans/'


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


class OldPlanFixture(Fixture):
    def old_plan(self, months=3, monthly='240.00', monthly_document_type='combined'):
        """A plan as the original design left it in production: a receipt for all of it, months to come."""
        total = Decimal(monthly) * months
        receipt = service.create_receipt({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'receipt_details': {'payment_method': 'מזומן', 'cash_amount': str(total)},
        })
        plan = CashPlan.objects.create(
            child=self.kid, total_amount=total, monthly_amount=Decimal(monthly), receipt=receipt,
            monthly_document_type=monthly_document_type, branch=self.kid.family.branch,
        )
        day = month_first(self.today)
        for _ in range(months):
            CashPlanMonth.objects.create(plan=plan, due_date=day, amount=Decimal(monthly))
            day = next_month(day)
        return plan


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class OlderPlansTests(OldPlanFixture, TestCase):
    def test_a_month_that_comes_due_gets_a_tax_invoice_paid_by_the_receipt_not_a_second_irm(self, _mail):
        plan = self.old_plan()
        summary = issue_due_cash_documents(plan_id=plan.id)
        self.assertEqual((summary['issued'], summary['covered']), (1, 0))
        month = plan.months.get(due_date=month_first(self.today))
        invoice = month.document
        self.assertEqual(invoice.document_type, 'tax_invoice')
        self.assertEqual(invoice.document_date, israel_today())
        self.assertEqual(invoice.total_amount, Decimal('240.00'))
        self.assertEqual(invoice.payments.count(), 0)
        self.assertIn(plan.receipt.document_number, invoice.customer_notes)
        self.assertEqual(DocumentSettlement.objects.get(invoice=invoice).payer, plan.receipt)
        self.assertEqual(settlement.balance_of(invoice).status, 'paid')
        # The cash is counted once: the receipt, and no invoice-receipt beside it.
        self.assertFalse(FormalDocument.objects.filter(document_type='combined').exists())

    def test_running_twice_issues_nothing_twice(self, _mail):
        plan = self.old_plan()
        issue_due_cash_documents(plan_id=plan.id)
        again = issue_due_cash_documents(plan_id=plan.id)
        self.assertEqual((again['issued'], again['covered']), (0, 0))
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), 1)
        self.assertEqual(DocumentSettlement.objects.count(), 1)

    def test_a_month_issued_before_settlements_counts_as_paid(self, _mail):
        plan = self.old_plan(monthly_document_type='tax_invoice')
        issue_due_cash_documents(plan_id=plan.id)
        DocumentSettlement.objects.all().delete()  # as production holds it: no row
        invoice = plan.months.get(due_date=month_first(self.today)).document
        self.assertEqual(settlement.balance_of(invoice).status, 'paid')

    def test_cancelling_an_older_plan_credits_nothing_and_says_why(self, _mail):
        plan = self.old_plan()
        issue_due_cash_documents(plan_id=plan.id)
        result = cancel_cash_plan(plan.pk, reason='עזבו')
        self.assertIsNone(result['credit_note'])
        self.assertEqual(result['unused_amount'], Decimal('480.00'))
        self.assertIn(plan.receipt.document_number, result['message'])
        self.assertFalse(FormalDocument.objects.filter(document_type='credit_invoice').exists())
        self.assertEqual(issue_due_cash_documents(today=self.today + timedelta(days=70))['issued'], 0)

    def test_an_older_plan_refuses_a_refund_amount(self, _mail):
        plan = self.old_plan()
        with self.assertRaises(CashPlanError):
            cancel_cash_plan(plan.pk, refund_amount='100')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'active')


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CancelUpfrontTests(Fixture, TestCase):
    def register(self, months=10):
        return register_cash_plan(
            child_id=str(self.kid.id), total_amount=str(240 * months), monthly_amount='240',
            start_month=month_first(self.today),
        )

    def test_the_months_not_begun_are_credited_against_the_irm(self, _mail):
        plan = self.register()
        expected = unused_amount(plan)
        self.assertEqual(expected, Decimal('2160.00'))  # nine months after this one
        result = cancel_cash_plan(plan.pk, reason='עברו דירה')
        credit = result['credit_note']
        self.assertEqual((credit.total_amount, credit.linked_document), (expected, plan.receipt))
        self.assertEqual(credit.linked_document_number, plan.receipt.document_number)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'cancelled')
        self.assertIsNotNone(plan.cancelled_at)

    def test_cancelling_twice_credits_once(self, _mail):
        plan = self.register()
        cancel_cash_plan(plan.pk)
        again = cancel_cash_plan(plan.pk)
        self.assertTrue(again['already_cancelled'])
        self.assertEqual(FormalDocument.objects.filter(document_type='credit_invoice').count(), 1)

    def test_the_office_may_refund_another_sum_or_nothing(self, _mail):
        plan = self.register()
        self.assertEqual(cancel_cash_plan(plan.pk, refund_amount='500.00')['credit_note'].total_amount,
                         Decimal('500.00'))
        other = self.register()
        self.assertIsNone(cancel_cash_plan(other.pk, refund_amount='0')['credit_note'])

    def test_more_than_the_irm_is_refused_and_the_plan_stays(self, _mail):
        plan = self.register(months=2)
        with self.assertRaises(ValueError):
            cancel_cash_plan(plan.pk, refund_amount='999.00')
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'active')
        self.assertFalse(FormalDocument.objects.filter(document_type='credit_invoice').exists())


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ApiTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(make_user('cash-d1@test', UserProfile.ROLE_MANAGER))

    def test_register_then_cancel(self, _mail):
        res = self.client.post(PLANS, {
            'child_id': str(self.kid.id), 'total_amount': '720', 'monthly_amount': '240',
            'start_month': str(month_first(self.today)),
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data['mode'], res.data['receipt_document_type']), ('upfront', 'combined'))
        self.assertEqual(res.data['unused_amount'], '480.00')

        cancelled = self.client.post(f"{PLANS}{res.data['id']}/cancel/", {'reason': 'עזבו'}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data['status'], 'cancelled')
        self.assertTrue(cancelled.data['credit_note_number'].startswith('CR-'))
        self.assertEqual(cancelled.data['unused_amount'], '480.00')

    def test_a_bad_refund_is_a_readable_error(self, _mail):
        res = self.client.post(PLANS, {
            'child_id': str(self.kid.id), 'total_amount': '240', 'monthly_amount': '240',
            'start_month': str(month_first(self.today)),
        }, format='json')
        bad = self.client.post(f"{PLANS}{res.data['id']}/cancel/", {'refund_amount': '-5'}, format='json')
        self.assertEqual(bad.status_code, 400, bad.data)
        self.assertTrue(bad.data['error'])


@override_settings(TRANZILA_BILLING_TERMINAL='')
class OlderPlansReportTests(OldPlanFixture, TestCase):
    def test_the_report_counts_the_cash_recorded_again_and_writes_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        plan = self.old_plan()
        # An IRM a month, as the original design issued them.
        month = plan.months.order_by('due_date').first()
        irm = FormalDocument.objects.create(
            document_number=f'IRM-{self.year}-000900', document_type='combined', client_type='existing',
            child=self.kid, document_date=self.today, total_amount=Decimal('240.00'),
        )
        CashPlanMonth.objects.filter(pk=month.pk).update(status='invoiced', document=irm)
        register_cash_plan(child_id=str(self.kid.id), total_amount='240', monthly_amount='240')  # upfront: not listed
        before = FormalDocument.objects.count()

        out = StringIO()
        call_command('report_older_cash_plans', stdout=out)
        text = out.getvalue()
        self.assertIn(f'{plan.pk},active,{plan.receipt.document_number},720.00,1,240.00,0,2,480.00', text)
        self.assertIn('Older cash plans: 1', text)
        self.assertEqual(FormalDocument.objects.count(), before)
