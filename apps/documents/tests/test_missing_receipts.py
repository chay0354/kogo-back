"""The missing-receipts screen: the same list as `check_invoices`, and the same late issue.

A receipt issued from the screen must be the one `check_invoices --fix` would
have issued — dated today, in payment order, marked late, never mailed — and
pressing the button twice must not give a charge two receipts.
"""
import codecs
import csv
import io
import re
import uuid
from datetime import datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import Payment
from apps.documents.missing_receipts import CSV_COLUMNS, payments_without_invoice
from apps.documents.models import DocumentSeries

User = get_user_model()

URL = '/api/v1/documents/missing-receipts/'
EXPORT = f'{URL}export/'
ISSUE = f'{URL}issue/'

UUID_LINE = re.compile(r'^\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s', re.M)


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


class _Base(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-missing@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

        course = TestDataFactory.create_course(name='ג׳ודו מתחילים')
        self.lesson = TestDataFactory.create_lesson(course=course)
        self.branch = course.branch
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)
        self.year = timezone.localdate().year

    def at(self, year, month, day):
        return timezone.make_aware(datetime(year, month, day, 10, 0))

    def payment(self, when, *, amount='236.00', status='completed'):
        return Payment.objects.create(
            child=self.child, family=self.family, lesson=self.lesson, branch=self.branch,
            payment_type='recurring_subscription', status=status,
            base_amount=Decimal(amount), discount_amount=Decimal('0.00'), final_amount=Decimal(amount),
            payment_date=when,
        )

    def command_ids(self, *args):
        out = StringIO()
        call_command('check_invoices', *args, stdout=out)
        return UUID_LINE.findall(out.getvalue())

    def issue(self, ids, confirm='הפק'):
        return self.client.post(ISSUE, {'payment_ids': [str(i) for i in ids], 'confirm': confirm}, format='json')


class ReportTest(_Base):
    """The screen lists what the command lists — one definition of "missing"."""

    def setUp(self):
        super().setUp()
        self.january = self.payment(self.at(self.year, 1, 15))
        self.february = self.payment(self.at(self.year, 2, 20), amount='120.50')
        self.last_year = self.payment(self.at(self.year - 1, 12, 30))
        covered = self.payment(self.at(self.year, 3, 1))
        PaymentService()._create_invoice_from_payment(covered, None, send_email=False)
        self.payment(self.at(self.year, 3, 2), status='failed')
        self.payment(self.at(self.year, 3, 3), amount='0.00')

    def test_the_report_matches_the_commands_findings(self):
        this_year = self.client.get(URL, {'year': self.year})
        last_year = self.client.get(URL, {'year': self.year - 1})

        self.assertEqual(this_year.status_code, 200, this_year.data)
        on_screen = [row['payment_id'] for row in this_year.data['rows'] + last_year.data['rows']]
        self.assertEqual(sorted(on_screen), sorted(self.command_ids('--all')))
        self.assertEqual(sorted(on_screen), sorted(str(p.id) for p in payments_without_invoice()))

    def test_a_year_lists_its_own_charges_oldest_first_with_count_and_total(self):
        data = self.client.get(URL, {'year': self.year}).data

        self.assertEqual([row['payment_id'] for row in data['rows']], [str(self.january.id), str(self.february.id)])
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['total'], '356.50')
        row = data['rows'][0]
        self.assertEqual(row['paid_at'][:10], f'{self.year}-01-15')
        self.assertEqual(row['family_name'], self.family.name)
        self.assertEqual(row['child_name'], self.child.full_name)
        self.assertEqual(row['amount'], '236.00')
        self.assertEqual((row['channel'], row['channel_label']), ('standing_order', 'הוראת קבע'))
        self.assertEqual(row['method'], '')
        self.assertEqual(data['next_number'], f'IR-{self.year}-000002')

    def test_the_year_defaults_to_this_one(self):
        self.assertEqual(self.client.get(URL).data['year'], self.year)

    def test_a_year_that_is_not_one_is_refused(self):
        res = self.client.get(URL, {'year': 'שנה'})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['error'], 'שנה לא תקינה')

    def test_continuity_names_a_number_no_receipt_carries(self):
        row = DocumentSeries.objects.get(series='IR', year=self.year)
        row.counter += 1
        row.save(update_fields=['counter'])

        runs = {run['series']: run for run in self.client.get(URL, {'year': self.year}).data['continuity']}

        self.assertEqual(runs['IR']['missing'], [f'IR-{self.year}-000002'])
        self.assertFalse(runs['IR']['complete'])


class ExportTest(_Base):
    """The accountant's copy: Excel-ready, and no cell that runs as a formula."""

    def test_the_csv_carries_the_rows_with_a_bom_and_formula_safe_cells(self):
        self.family.name = '=HYPERLINK("http://x")'
        self.family.save(update_fields=['name'])
        payment = self.payment(self.at(self.year, 4, 5))

        res = self.client.get(EXPORT, {'year': self.year})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res['Content-Type'].startswith('text/csv'))
        self.assertIn(f'missing-receipts-{self.year}.csv', res['Content-Disposition'])
        self.assertTrue(res.content.startswith(codecs.BOM_UTF8))
        rows = list(csv.reader(io.StringIO(res.content.decode('utf-8-sig'))))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], f'05/04/{self.year}')
        self.assertEqual(rows[1][1], '\'=HYPERLINK("http://x")')
        self.assertEqual(rows[1][4], '236.00')
        self.assertEqual(rows[1][7], str(payment.id))


class IssueTest(_Base):
    """What `check_invoices --fix` issues, from a button — and only after the word is typed."""

    def setUp(self):
        super().setUp()
        self.older = self.payment(self.at(self.year, 1, 15))
        self.newer = self.payment(self.at(self.year, 2, 20))

    def test_receipts_are_dated_today_in_payment_order_and_marked_late(self):
        res = self.issue([self.newer.id, self.older.id])

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['issued'], [
            {'payment_id': str(self.older.id), 'number': f'IR-{self.year}-000001'},
            {'payment_id': str(self.newer.id), 'number': f'IR-{self.year}-000002'},
        ])
        self.assertEqual(res.data['skipped'], [])
        for payment in (self.older, self.newer):
            invoice = Invoice.objects.get(payment=payment)
            self.assertEqual(timezone.localtime(invoice.invoice_date).date(), timezone.localdate())
            log = InvoiceActivityLog.objects.get(invoice=invoice, action='issued_late')
            self.assertEqual(log.details['money_received_at'][:10], payment.payment_date.date().isoformat())
            self.assertEqual(log.details['document_issued_at'][:10], timezone.now().date().isoformat())
            self.assertFalse(log.details['backdated'])
            self.assertFalse(log.details['emailed'])
            # Who issued it, as the audit trail of the receipt.
            self.assertEqual(log.details['issued_by_user_id'], str(self.manager.pk))
            self.assertEqual(log.details['issued_by'], self.manager.get_username())

    def test_issuing_twice_issues_once(self):
        self.issue([self.older.id, self.newer.id])
        again = self.issue([self.older.id, self.newer.id])

        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(again.data['issued'], [])
        self.assertEqual({row['reason'] for row in again.data['skipped']}, {'has_receipt'})
        self.assertEqual(Invoice.objects.filter(payment__in=[self.older, self.newer]).count(), 2)
        self.assertEqual(DocumentSeries.objects.get(series='IR', year=self.year).counter, 2)

    def test_a_charge_that_got_its_receipt_meanwhile_is_skipped(self):
        PaymentService()._create_invoice_from_payment(self.older, None, send_email=False)
        stranger = uuid.uuid4()

        res = self.issue([self.older.id, self.newer.id, stranger])

        self.assertEqual([row['payment_id'] for row in res.data['issued']], [str(self.newer.id)])
        self.assertEqual(
            {(row['payment_id'], row['reason']) for row in res.data['skipped']},
            {(str(self.older.id), 'has_receipt'), (str(stranger), 'not_found')},
        )

    def test_the_command_skips_what_the_screen_issued(self):
        self.issue([self.older.id])

        self.assertEqual(self.command_ids('--all'), [str(self.newer.id)])

    def test_without_the_exact_word_nothing_is_issued(self):
        for confirm in ('', 'הפקה', 'כן', None):
            payload = {'payment_ids': [str(self.older.id)]}
            if confirm is not None:
                payload['confirm'] = confirm
            res = self.client.post(ISSUE, payload, format='json')
            self.assertEqual(res.status_code, 400, confirm)
            self.assertIn('הפק', res.data['error'])
        self.assertFalse(Invoice.objects.exists())
        self.assertFalse(DocumentSeries.objects.filter(series='IR').exists())

    def test_a_request_without_payments_or_with_a_bad_id_is_refused(self):
        self.assertEqual(self.issue([]).status_code, 400)
        self.assertEqual(self.issue(['not-a-uuid']).status_code, 400)
        self.assertFalse(Invoice.objects.exists())

    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email')
    def test_no_customer_is_mailed(self, mock_send):
        # on_commit callbacks run here, so a queued receipt mail would be sent.
        with self.captureOnCommitCallbacks(execute=True):
            res = self.issue([self.older.id, self.newer.id])

        self.assertEqual(len(res.data['issued']), 2)
        mock_send.assert_not_called()
        self.assertEqual(mail.outbox, [])


class PermissionTest(_Base):
    """Managers only — a partner or a worker is refused at the endpoint, not just in the UI."""

    def test_partner_and_worker_are_refused(self):
        payment = self.payment(self.at(self.year, 1, 15))
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            self.client.force_authenticate(make_user(f'{role}-missing@test', role))
            self.assertEqual(self.client.get(URL).status_code, 403, role)
            self.assertEqual(self.client.get(EXPORT).status_code, 403, role)
            self.assertEqual(self.issue([payment.id]).status_code, 403, role)
        self.assertFalse(Invoice.objects.exists())

    def test_an_anonymous_caller_is_refused(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertIn(self.issue([uuid.uuid4()]).status_code, (401, 403))
