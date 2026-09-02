from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.recurring_billing import process_due_recurring_charges
from apps.documents.check_plans import issue_due_check_invoices, register_check_plan
from apps.documents.models import CheckItem, CheckPlan, FormalDocument


class CheckPlanServiceTests(TestCase):
    def setUp(self):
        family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=family)
        self.lesson = TestDataFactory.create_lesson()

    def test_register_issues_receipt_and_invoices_due_checks(self):
        today = date.today()
        future = today + timedelta(days=32)
        plan = register_check_plan(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
            description="מנוי צ'קים — בדיקה",
            checks=[
                {
                    'date': today.isoformat(),
                    'bank': 'לאומי',
                    'branch': '123',
                    'account_number': '456789',
                    'check_number': '1001',
                    'amount': '350.00',
                },
                {
                    'date': future.isoformat(),
                    'bank': 'לאומי',
                    'branch': '123',
                    'account_number': '456789',
                    'check_number': '1002',
                    'amount': '350.00',
                },
            ],
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'active')
        self.assertIsNotNone(plan.receipt_id)
        self.assertEqual(plan.receipt.document_type, 'receipt')
        self.assertEqual(plan.receipt.total_amount, Decimal('700.00'))
        self.assertEqual(plan.items.count(), 2)

        due = plan.items.get(check_number='1001')
        later = plan.items.get(check_number='1002')
        self.assertEqual(due.status, 'invoiced')
        self.assertIsNotNone(due.tax_invoice_id)
        self.assertEqual(due.tax_invoice.document_type, 'tax_invoice')
        self.assertEqual(due.tax_invoice.total_amount, Decimal('350.00'))
        self.assertEqual(later.status, 'pending')
        self.assertIsNone(later.tax_invoice_id)

    def test_monthly_cron_issues_future_check_invoice(self):
        today = date.today()
        past = today - timedelta(days=1)
        plan = CheckPlan.objects.create(
            child=self.child,
            lesson=self.lesson,
            description="צ'קים",
            status='active',
        )
        item = CheckItem.objects.create(
            plan=plan,
            due_date=past,
            amount=Decimal('250.00'),
            check_number='2001',
        )
        summary = issue_due_check_invoices(today=today, plan=plan)
        self.assertEqual(summary['issued'], 1)
        item.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(item.status, 'invoiced')
        self.assertEqual(item.tax_invoice.document_type, 'tax_invoice')
        self.assertEqual(plan.status, 'completed')

    def test_running_the_cron_twice_does_not_invoice_a_check_twice(self):
        today = date.today()
        plan = register_check_plan(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
            checks=[{'date': today.isoformat(), 'check_number': '2001', 'amount': '300.00'}],
        )
        first = plan.items.get()
        self.assertEqual(first.status, 'invoiced')
        invoices_before = FormalDocument.objects.filter(document_type='tax_invoice').count()
        summary = issue_due_check_invoices(today=today, plan=plan)
        self.assertEqual(summary['issued'], 0)
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), invoices_before)

    def test_register_rejects_empty_checks(self):
        with self.assertRaises(ValueError):
            register_check_plan(child_id=str(self.child.id), checks=[{'amount': 0}])


class CheckPlanAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager-checks@test.com',
            email='manager-checks@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(
            user=self.user, defaults={'role': UserProfile.ROLE_MANAGER}
        )
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=family)

    def test_create_and_list_check_plan(self):
        today = date.today()
        res = self.client.post(
            '/api/v1/documents/check-plans/',
            {
                'child_id': str(self.child.id),
                'description': "צ'קים משרד",
                'checks': [
                    {
                        'date': today.isoformat(),
                        'bank': 'פועלים',
                        'branch': '12',
                        'account_number': '99',
                        'check_number': '3001',
                        'amount': '180',
                    }
                ],
            },
            format='json',
        )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body['child_name'], self.child.full_name)
        self.assertEqual(body['status'], 'completed')
        self.assertTrue(body['receipt_number'])
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(body['items'][0]['status'], 'invoiced')
        self.assertTrue(body['items'][0]['tax_invoice_number'])

        listed = self.client.get('/api/v1/documents/check-plans/')
        self.assertEqual(listed.status_code, 200, listed.content)
        rows = listed.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], body['id'])

    def test_cancel_pending_items(self):
        future = date.today() + timedelta(days=40)
        plan = register_check_plan(
            child_id=str(self.child.id),
            checks=[{
                'date': future.isoformat(),
                'bank': 'דיסקונט',
                'amount': '100',
                'check_number': '4001',
            }],
        )
        res = self.client.post(f'/api/v1/documents/check-plans/{plan.id}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'cancelled')
        self.assertEqual(plan.items.get().status, 'cancelled')


class CheckPlanCronHookTests(TestCase):
    def setUp(self):
        family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=family)

    @patch('apps.customers.recurring_billing.apply_due_pending_recurring_amounts')
    def test_recurring_cron_issues_due_check_invoices(self, _apply):
        plan = CheckPlan.objects.create(child=self.child, status='active', description='cron')
        CheckItem.objects.create(
            plan=plan,
            due_date=date.today() - timedelta(days=2),
            amount=Decimal('90.00'),
            check_number='5001',
        )
        summary = process_due_recurring_charges(dry_run=False, limit=40)
        self.assertEqual(summary['check_invoices']['issued'], 1)
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), 1)
