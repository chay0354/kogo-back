"""The documents register: numbering, late issue, the child's list, and filters.

Each class pins one rule from הוראות ניהול פנקסי חשבונות, so a failure here
names the rule that broke, not just the line.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import Business, UserProfile
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_ledger import list_ledger_documents
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import Payment
from apps.documents.models import DocumentSeries
from apps.store.models import StoreInvoice

User = get_user_model()


class _Base(APITestCase):
    def setUp(self):
        user = User.objects.create_user(username='manager-register@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.business = Business.objects.create(name='עסק הדרכה')
        self.course_type = TestDataFactory.create_course_type(name='ג׳ודו')
        self.course = TestDataFactory.create_course(name='ג׳ודו מתחילים', course_type=self.course_type)
        self.course.min_age, self.course.max_age, self.course.business = 6, 9, self.business
        self.course.save(update_fields=['min_age', 'max_age', 'business'])
        self.instructor = TestDataFactory.create_instructor(branch=self.course.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor)
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)

    def _payment(self, *, days_ago=0, amount='236.00'):
        return Payment.objects.create(
            child=self.child, family=self.family, lesson=self.lesson, branch=self.course.branch,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal(amount), discount_amount=Decimal('0.00'), final_amount=Decimal(amount),
            payment_date=timezone.now() - timedelta(days=days_ago),
        )


class NumberingTest(_Base):
    """סעיף 18(א)(3): consecutive, never repeating; one series per document type."""

    def test_lesson_receipts_are_numbered_consecutively(self):
        first = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        second = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)

        year = timezone.localdate().year
        self.assertEqual(first.invoice_number, f'IR-{year}-000001')
        self.assertEqual(second.invoice_number, f'IR-{year}-000002')

    def test_store_sales_on_monthly_billing_have_their_own_series(self):
        paid = StoreInvoice.objects.create(total_amount=Decimal('49.00'), payment_method='cash')
        billed = StoreInvoice.objects.create(total_amount=Decimal('49.00'), payment_method='monthly_billing')

        self.assertTrue(paid.invoice_number.startswith('ST-'))
        self.assertTrue(billed.invoice_number.startswith('SD-'))


class LateIssueTest(_Base):
    """A charge that never got its receipt is issued late — dated today, by default."""

    def _run(self, *args):
        out = StringIO()
        call_command('check_invoices', *args, stdout=out)
        return out.getvalue()

    def test_a_late_receipt_is_dated_today_and_says_when_the_money_came(self):
        payment = self._payment(days_ago=40)

        self._run('--all', '--fix')

        invoice = Invoice.objects.get(payment=payment)
        self.assertEqual(invoice.invoice_date.date(), timezone.localdate())
        log = InvoiceActivityLog.objects.get(invoice=invoice, action='issued_late')
        self.assertEqual(log.details['money_received_at'][:10], payment.payment_date.date().isoformat())
        self.assertFalse(log.details['emailed'])

    def test_backdating_into_an_empty_series_keeps_the_payment_date(self):
        payment = self._payment(days_ago=40)

        self._run('--all', '--fix', '--backdate')

        self.assertEqual(Invoice.objects.get(payment=payment).invoice_date.date(), payment.payment_date.date())

    def test_backdating_behind_a_later_document_is_refused(self):
        PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        self._payment(days_ago=40)

        with self.assertRaises(CommandError):
            self._run('--all', '--fix', '--backdate')

    def test_a_hole_in_a_series_is_reported(self):
        PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        row = DocumentSeries.objects.get(series='IR')
        row.counter += 1  # a number handed out with no document behind it
        row.save(update_fields=['counter'])

        self.assertIn('חור בסדרה', self._run('--all'))

    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email')
    def test_a_late_run_mails_nobody_unless_asked(self, mock_send):
        self._payment(days_ago=10)

        self._run('--all', '--fix')

        mock_send.assert_not_called()


class ChildDocumentsTest(_Base):
    """תוספת ח׳: each student linked to their documents, with the PDF one click away."""

    def test_the_child_card_lists_every_document_newest_first(self):
        old = PaymentService()._create_invoice_from_payment(
            self._payment(days_ago=30), None, send_email=False, invoice_date=timezone.now() - timedelta(days=30),
        )
        new = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        StoreInvoice.objects.create(child=self.child, total_amount=Decimal('49.00'), payment_method='cash',
                                    payment_status='completed')

        res = self.client.get(f'/api/v1/customers/children/{self.child.id}/documents/')

        self.assertEqual(res.status_code, 200, res.data)
        rows = res.data['documents']
        self.assertEqual({row['kind'] for row in rows}, {'receipt', 'store'})
        receipt_numbers = [row['document_number'] for row in rows if row['kind'] == 'receipt']
        self.assertEqual(receipt_numbers, [new.invoice_number, old.invoice_number])
        self.assertEqual(rows[0]['date'], timezone.localdate().isoformat())

    def test_a_receipt_downloads_from_its_own_route(self):
        invoice = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        row = self.client.get(f'/api/v1/customers/children/{self.child.id}/documents/').data['documents'][0]

        res = self.client.get(f"/api/v1{row['download_url']}")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertIn(invoice.invoice_number, res['Content-Disposition'])


class LedgerDimensionsTest(_Base):
    """One filter bar slices documents and charges the same way."""

    def test_a_receipt_row_carries_business_course_age_and_instructor(self):
        invoice = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        today = timezone.localdate()

        row = next(
            r for r in list_ledger_documents(start_date=today, end_date=today, local_only=True)['documents']
            if r['document_number'] == invoice.invoice_number
        )

        self.assertEqual(row['business_id'], str(self.business.id))
        self.assertEqual(row['course_type_id'], str(self.course_type.id))
        self.assertEqual(row['age_key'], '6-9')
        self.assertEqual(row['age_label'], 'גילאי 6–9')
        self.assertEqual(row['instructor_id'], str(self.instructor.id))

    def test_charges_filter_by_course_type_and_business(self):
        self._payment()
        other = TestDataFactory.create_lesson(course=TestDataFactory.create_course(name='אחר'))
        Payment.objects.create(
            child=self.child, family=self.family, lesson=other, payment_type='one_time', status='completed',
            base_amount=Decimal('50'), discount_amount=Decimal('0'), final_amount=Decimal('50'),
        )

        by_type = self.client.get('/api/v1/customers/payments/ledger/', {'course_type': str(self.course_type.id)})
        branches_only = self.client.get('/api/v1/customers/payments/ledger/', {'business': 'branches'})

        self.assertEqual(by_type.status_code, 200, by_type.data)
        self.assertEqual({r['course_type_id'] for r in by_type.data['results']}, {str(self.course_type.id)})
        self.assertTrue(all(r['business_id'] is None for r in branches_only.data['results']))
