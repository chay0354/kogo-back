"""Cash paid up front: a receipt now, a document on the 1st of each month."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.documents.cash_plans import (
    issue_due_cash_documents,
    plan_months,
    preview,
    register_cash_plan,
    schedule,
)
from apps.documents.models import CashPlan, CashPlanMonth, FormalDocument

User = get_user_model()
URL = '/api/v1/documents/cash-plans/'


def _child():
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    return TestDataFactory.create_child(family=family)


class ScheduleTests(TestCase):
    def test_a_whole_number_of_months(self):
        self.assertEqual(plan_months(Decimal('2400'), Decimal('240')), 10)

    def test_a_remainder_rounds_up_and_the_last_month_is_smaller(self):
        rows = schedule(date(2026, 9, 1), plan_months(Decimal('500'), Decimal('240')),
                        Decimal('240'), Decimal('500'))
        self.assertEqual([r['amount'] for r in rows],
                         [Decimal('240.00'), Decimal('240.00'), Decimal('20.00')])

    def test_the_months_add_up_to_exactly_the_cash_taken(self):
        for total, monthly in (('2400', '240'), ('500', '240'), ('1000', '333.33'), ('99.99', '10')):
            rows = schedule(date(2026, 9, 1), plan_months(Decimal(total), Decimal(monthly)),
                            Decimal(monthly), Decimal(total))
            self.assertEqual(sum(r['amount'] for r in rows), Decimal(total), f'{total}/{monthly}')

    def test_the_months_are_the_first_of_consecutive_months(self):
        rows = schedule(date(2026, 11, 1), 3, Decimal('100'), Decimal('300'))
        self.assertEqual([r['due_date'] for r in rows],
                         [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)])

    def test_a_start_mid_month_still_files_on_the_first(self):
        rows = schedule(date(2026, 9, 17), 2, Decimal('100'), Decimal('200'))
        self.assertEqual(rows[0]['due_date'], date(2026, 9, 1))

    def test_preview_writes_nothing(self):
        data = preview(total_amount='2400', monthly_amount='240', start_month=date(2026, 9, 1))
        self.assertEqual(data['months'], 10)
        self.assertEqual(FormalDocument.objects.count(), 0)
        self.assertEqual(CashPlan.objects.count(), 0)

    def test_a_zero_monthly_amount_is_refused(self):
        with self.assertRaises(ValueError):
            plan_months(Decimal('2400'), Decimal('0'))


class RegisterTests(TestCase):
    def setUp(self):
        self.child = _child()
        self.lesson = TestDataFactory.create_lesson()

    def _register(self, total='2400', monthly='240', **kw):
        return register_cash_plan(
            child_id=str(self.child.id), total_amount=total, monthly_amount=monthly,
            lesson_id=str(self.lesson.id), start_month=date(2026, 9, 1), **kw,
        )

    def test_a_receipt_for_the_whole_sum_is_issued_at_once(self):
        plan = self._register()
        self.assertIsNotNone(plan.receipt)
        self.assertEqual(plan.receipt.document_type, 'receipt')
        self.assertEqual(plan.receipt.total_amount, Decimal('2400.00'))

    def test_the_months_are_laid_out(self):
        plan = self._register()
        self.assertEqual(plan.months.count(), 10)
        self.assertEqual(plan.months.first().due_date, date(2026, 9, 1))

    def test_months_already_past_get_their_document_immediately(self):
        """Registering in the middle of a year must not leave the past unissued."""
        plan = self._register()
        issued = plan.months.filter(status='invoiced').count()
        self.assertGreater(issued, 0)
        self.assertTrue(
            all(m.due_date <= date.today() for m in plan.months.filter(status='invoiced'))
        )

    def test_a_future_month_waits(self):
        plan = self._register()
        future = plan.months.filter(due_date__gt=date.today())
        self.assertTrue(all(m.status == 'pending' for m in future))

    def test_the_monthly_document_is_the_type_that_was_chosen(self):
        plan = self._register(monthly_document_type='tax_invoice')
        month = plan.months.filter(status='invoiced').first()
        self.assertIsNotNone(month)
        self.assertEqual(month.document.document_type, 'tax_invoice')

    def test_the_default_monthly_document_is_the_combined_one(self):
        plan = self._register()
        month = plan.months.filter(status='invoiced').first()
        self.assertEqual(month.document.document_type, 'combined')

    def test_a_month_is_never_issued_twice(self):
        plan = self._register()
        before = FormalDocument.objects.count()
        issue_due_cash_documents(plan_id=plan.id)
        issue_due_cash_documents(plan_id=plan.id)
        self.assertEqual(FormalDocument.objects.count(), before)

    def test_zero_cash_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            self._register(total='0')
        self.assertEqual(CashPlan.objects.count(), 0)
        self.assertEqual(FormalDocument.objects.count(), 0)

    def test_a_plan_whose_months_are_all_issued_is_completed(self):
        plan = register_cash_plan(
            child_id=str(self.child.id), total_amount='240', monthly_amount='240',
            lesson_id=str(self.lesson.id), start_month=date(2026, 1, 1),
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'completed')

    def test_the_monthly_document_carries_the_regular_price_not_the_whole_sum(self):
        plan = self._register()
        month = plan.months.filter(status='invoiced').first()
        self.assertEqual(month.document.total_amount, Decimal('240.00'))


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(username='mgr-cash@x.com', email='mgr-cash@x.com',
                                        password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        self.child = _child()
        self.lesson = TestDataFactory.create_lesson()

    def test_preview_endpoint_issues_nothing(self):
        res = self.client.post(f'{URL}preview/', {
            'total_amount': '2400', 'monthly_amount': '240', 'start_month': '2026-09-01',
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['months'], 10)
        self.assertEqual(FormalDocument.objects.count(), 0)

    def test_registering_through_the_api(self):
        res = self.client.post(URL, {
            'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id),
            'total_amount': '2400', 'monthly_amount': '240', 'start_month': '2026-09-01',
        }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['months_total'], 10)
        self.assertTrue(res.data['receipt_number'])

    def test_a_bad_amount_is_a_readable_error(self):
        res = self.client.post(URL, {
            'child_id': str(self.child.id), 'total_amount': '0', 'monthly_amount': '240',
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_plans_can_be_listed_for_one_child(self):
        self.client.post(URL, {
            'child_id': str(self.child.id), 'total_amount': '480', 'monthly_amount': '240',
            'start_month': '2026-09-01',
        }, format='json')
        res = self.client.get(URL, {'child_id': str(self.child.id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)

    def test_a_worker_cannot_register_cash(self):
        worker = User.objects.create_user(username='w-cash@x.com', email='w-cash@x.com',
                                          password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=worker, defaults={'role': UserProfile.ROLE_WORKER})
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=worker).key}')
        res = c.post(URL, {'child_id': str(self.child.id), 'total_amount': '240',
                           'monthly_amount': '240'}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(CashPlan.objects.count(), 0)


class CronTests(TestCase):
    def test_the_monthly_run_picks_up_a_month_that_has_come_due(self):
        from apps.customers.recurring_billing import process_due_recurring_charges

        child = _child()
        plan = register_cash_plan(
            child_id=str(child.id), total_amount='480', monthly_amount='240',
            start_month=date(2026, 1, 1),
        )
        CashPlanMonth.objects.filter(plan=plan).update(status='pending', document=None, invoiced_at=None)
        CashPlan.objects.filter(pk=plan.pk).update(status='active')

        summary = process_due_recurring_charges(dry_run=False, limit=40)
        self.assertIn('cash_documents', summary)
        self.assertEqual(summary['cash_documents']['issued'], 2)

    def test_a_dry_run_issues_nothing(self):
        from apps.customers.recurring_billing import process_due_recurring_charges

        child = _child()
        plan = register_cash_plan(
            child_id=str(child.id), total_amount='480', monthly_amount='240',
            start_month=date(2026, 1, 1),
        )
        CashPlanMonth.objects.filter(plan=plan).update(status='pending', document=None, invoiced_at=None)
        before = FormalDocument.objects.count()
        summary = process_due_recurring_charges(dry_run=True, limit=40)
        self.assertTrue(summary['cash_documents']['dry_run'])
        self.assertEqual(FormalDocument.objects.count(), before)
