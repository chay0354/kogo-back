"""
The register reads every channel that issues documents: what the office issues,
and the lesson receipts and store sales numbered in consecutive runs. The period
report and the accountant's export are built on it, so it is tested through them.
"""
import csv
import io
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.core.revenue_service import BRANCHES_BUSINESS_LABEL
from apps.customers.financial_models import Invoice, InvoiceChild
from apps.customers.models import Child, Family
from apps.documents.models import DocumentSeries, FormalDocument
from apps.documents.period_report import build_report
from apps.documents.undocumented_income import collect_undocumented
from apps.store.models import StoreInvoice

User = get_user_model()
EXPORT = '/api/v1/documents/documents/register-export/'
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


def on(day):
    return datetime(2026, 8, day, 12, tzinfo=dt_timezone.utc)


class RegisterFixture:
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        self.family = Family.objects.create(name='משפחת כהן', branch=self.north)
        self.kid = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.manager = make_user('manager-register@test', UserProfile.ROLE_MANAGER)

    def lesson_receipt(self, number, amount='236.00', day=9, branch=None, child=None):
        invoice = Invoice.objects.create(
            invoice_number=number, family=self.family, branch=branch or self.north,
            amount=Decimal(amount), status='paid', payment_method='credit_card',
            payment_type='recurring', payer_name=self.family.name, invoice_date=on(day),
        )
        if child is not None:
            InvoiceChild.objects.create(invoice=invoice, child=child)
        return invoice

    def store_sale(self, amount='49.00', day=10, method='credit_card', status='completed',
                   branch='north', customer_name='קונה', **extra):
        sale = StoreInvoice.objects.create(
            customer_name=customer_name, total_amount=Decimal(amount), payment_method=method,
            payment_status=status, branch=self.north if branch == 'north' else branch, **extra,
        )
        # The sale is stamped with the moment it is saved; date it in August after the fact.
        StoreInvoice.objects.filter(pk=sale.pk).update(issue_date=on(day))
        sale.refresh_from_db()
        return sale

    def credit_note(self, number='CR-2026-000001', credits='IR-2026-000001'):
        return FormalDocument.objects.create(
            document_number=number, document_type='credit_invoice', client_type='existing',
            customer_name='משפחת כהן', document_date=date(2026, 8, 12), subtotal=Decimal('100.00'),
            vat_amount=Decimal('18.00'), total_amount=Decimal('118.00'), linked_document_number=credits,
        )

    def report(self, user=None, group_by='branch'):
        return build_report(user or self.manager, *AUGUST, 'אוגוסט 2026', group_by=group_by)

    @staticmethod
    def rows(report):
        return {row.document_number: row for group in report.groups for row in group.rows}

    def undocumented_references(self):
        income = collect_undocumented(self.manager, *AUGUST)
        return {row.reference for section in income.sections for row in section.rows}


class ChannelsInTheReportTests(RegisterFixture, TestCase):
    def test_a_lesson_receipt_in_the_ir_run_is_a_document(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        report = self.report()
        row = self.rows(report)['IR-2026-000001']
        self.assertEqual(
            (row.document_type, row.net_amount, row.vat_amount, row.total_amount),
            ('combined', Decimal('200.00'), Decimal('36.00'), Decimal('236.00')),
        )
        self.assertEqual(row.customer, 'נועה כהן')
        self.assertEqual([group.title for group in report.groups], ['סניף צפון'])
        self.assertEqual(report.collected_total, Decimal('236.00'))

    def test_a_receipt_with_an_old_number_stays_income_without_a_document(self):
        self.lesson_receipt('INV-20260809-A1B2C3D4')
        self.assertNotIn('INV-20260809-A1B2C3D4', self.rows(self.report()))
        self.assertIn('INV-20260809-A1B2C3D4', self.undocumented_references())

    def test_a_document_is_never_also_income_without_one(self):
        self.lesson_receipt('IR-2026-000001')
        sale = self.store_sale()
        self.assertFalse({'IR-2026-000001', sale.invoice_number} & self.undocumented_references())

    def test_a_store_sale_paid_on_the_spot_is_an_invoice_receipt(self):
        sale = self.store_sale(amount='49.00')
        self.assertTrue(sale.invoice_number.startswith('ST-2026-'))
        row = self.rows(self.report())[sale.invoice_number]
        self.assertEqual(
            (row.document_type, row.vat_amount, row.total_amount),
            ('combined', Decimal('7.47'), Decimal('49.00')),
        )

    def test_a_sale_on_monthly_billing_is_a_transaction_invoice_without_vat(self):
        sale = self.store_sale(amount='120.00', method='monthly_billing', status='pending')
        self.assertTrue(sale.invoice_number.startswith('SD-2026-'))
        report = self.report()
        row = self.rows(report)[sale.invoice_number]
        self.assertEqual(
            (row.document_type, row.vat_amount, row.total_amount),
            ('transaction_invoice', Decimal('0.00'), Decimal('120.00')),
        )
        self.assertEqual(report.revenue_total, Decimal('0.00'))
        self.assertEqual(report.non_fiscal_total, Decimal('120.00'))

    def test_a_sale_with_a_tranzila_copy_is_listed_once_by_the_copy(self):
        sale = self.store_sale()
        copy = FormalDocument.objects.create(
            document_number='1001', document_type='combined', client_type='existing', branch=self.north,
            document_date=date(2026, 8, 10), subtotal=Decimal('49.00'), total_amount=Decimal('49.00'),
        )
        sale.formal_document = copy
        sale.save(update_fields=['formal_document'])
        rows = self.rows(self.report())
        self.assertEqual(set(rows), {'1001'})
        self.assertEqual(rows['1001'].reference, sale.invoice_number)

    def test_a_failed_sale_keeps_its_number_but_is_never_summed(self):
        sale = self.store_sale(status='failed')
        report = self.report()
        self.assertTrue(report.is_empty)
        self.assertEqual([row.document_number for row in report.void_rows], [sale.invoice_number])
        self.assertEqual(report.void_rows[0].void_reason, 'התשלום נכשל')

    def test_a_website_order_awaiting_payment_counts(self):
        sale = self.store_sale(status='pending', branch=None, website_order_number='CG-260810-TEST')
        report = self.report()
        self.assertIn(sale.invoice_number, self.rows(report))
        self.assertEqual(report.void_rows, [])

    def test_a_partner_sees_their_branchs_receipts_and_no_runs(self):
        partner = make_user('partner-register@test', UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.add(self.north)
        partner = User.objects.get(pk=partner.pk)
        self.lesson_receipt('IR-2026-000001', branch=self.north)
        self.lesson_receipt('IR-2026-000002', branch=self.south)
        report = self.report(user=partner)
        self.assertEqual(set(self.rows(report)), {'IR-2026-000001'})
        self.assertEqual(report.continuity, [])

    def test_a_credit_note_comes_off_the_revenue_and_names_its_customer(self):
        self.lesson_receipt('IR-2026-000001')
        self.credit_note()
        report = self.report()
        self.assertEqual(report.revenue_total, Decimal('118.00'))
        credit = self.rows(report)['CR-2026-000001']
        self.assertEqual((credit.customer, credit.reference), ('משפחת כהן', 'IR-2026-000001'))

    def test_a_receipt_and_a_hand_issued_document_for_one_sum_are_pointed_at(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        FormalDocument.objects.create(
            document_number='IRM-2026-000001', document_type='combined', client_type='existing',
            child=self.kid, document_date=date(2026, 8, 9), subtotal=Decimal('200.00'),
            vat_amount=Decimal('36.00'), total_amount=Decimal('236.00'),
        )
        report = self.report()
        self.assertEqual(
            [(pair['number'], pair['other']) for pair in report.possible_duplicates],
            [('IR-2026-000001', 'IRM-2026-000001')],
        )
        rows = self.rows(report)
        self.assertEqual(rows['IR-2026-000001'].duplicate_of, 'IRM-2026-000001')
        self.assertEqual(rows['IRM-2026-000001'].duplicate_of, 'IR-2026-000001')

    def test_by_business_an_untagged_receipt_is_the_branches(self):
        self.lesson_receipt('IR-2026-000001')
        titles = [group.title for group in self.report(group_by='business_unit').groups]
        self.assertEqual(titles, [BRANCHES_BUSINESS_LABEL])

    def test_the_report_carries_the_runs_and_renders_every_block(self):
        from apps.documents.period_report_pdf import generate_period_report_pdf

        self.lesson_receipt('IR-2026-000001', child=self.kid)
        DocumentSeries.objects.create(series='IR', year=2026, counter=2)
        FormalDocument.objects.create(
            document_number='IRM-2026-000001', document_type='combined', client_type='existing',
            child=self.kid, document_date=date(2026, 8, 9), subtotal=Decimal('200.00'),
            vat_amount=Decimal('36.00'), total_amount=Decimal('236.00'),
        )
        self.store_sale(status='failed')
        report = self.report()
        lessons = next(run for run in report.continuity if run.series == 'IR')
        self.assertEqual(lessons.missing, ('IR-2026-000002',))
        self.assertTrue(report.void_rows and report.possible_duplicates)
        self.assertTrue(generate_period_report_pdf(report).startswith(b'%PDF'))


class RegisterExportTests(RegisterFixture, APITestCase):
    def table(self, response):
        header, *body = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        return header, {line[1].lstrip("'"): dict(zip(header, line)) for line in body}

    def test_the_accountant_gets_one_row_per_document(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.credit_note()
        failed = self.store_sale(status='failed')
        self.client.force_authenticate(self.manager)

        res = self.client.get(EXPORT, {'month': '2026-08'})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'text/csv; charset=utf-8')
        self.assertTrue(res.content.startswith(b'\xef\xbb\xbf'))
        header, rows = self.table(res)
        self.assertEqual(header[:3], ['תאריך', 'מספר מסמך', 'סדרה'])
        receipt = rows['IR-2026-000001']
        self.assertEqual(
            (receipt['סדרה'], receipt['קוד מבנה אחיד'], receipt['לפני מע"מ'], receipt['סה"כ']),
            ('IR', '320', '200.00', '236.00'),
        )
        credit = rows['CR-2026-000001']
        self.assertEqual((credit['קוד מבנה אחיד'], credit['סה"כ']), ('330', '-118.00'))
        void = rows[failed.invoice_number]
        self.assertEqual(void['סה"כ'], '0.00')
        self.assertIn('התשלום נכשל', void['מצב'])

    def test_a_name_that_looks_like_a_formula_stays_text(self):
        sale = self.store_sale(customer_name='=HYPERLINK("x")')
        self.client.force_authenticate(self.manager)
        _header, rows = self.table(self.client.get(EXPORT, {'month': '2026-08'}))
        self.assertTrue(rows[sale.invoice_number]['לקוח'].startswith("'="))

    def test_a_partner_is_refused(self):
        partner = make_user('partner-export@test', UserProfile.ROLE_PARTNER)
        self.client.force_authenticate(partner)
        self.assertEqual(self.client.get(EXPORT, {'month': '2026-08'}).status_code, 403)

    def test_a_bad_period_is_a_400(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get(EXPORT, {'month': '2026-13'}).status_code, 400)
