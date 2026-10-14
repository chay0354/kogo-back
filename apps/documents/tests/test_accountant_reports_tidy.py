"""
The accountant's files, made readable (14.10.2026).

The September files were right in their sums once the double count was gone,
and still hard to read: numbers broken over two lines, a Tranzila section that
reported a gap that was not one, refunds with no credit note left out of the
take, and a CSV that Excel opened left to right with no totals. These hold the
new shape: what each file says, and that its totals agree with the report's.
"""
import csv
import io
import re
import zipfile
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.checkout_invoice import CHECKOUT_LINES_ACTION
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import Child, Family, Payment, TranzilaTransaction
from apps.documents.models import FormalDocument
from apps.documents.period_report import build_report
from apps.documents.tests.test_period_report import make_user
from apps.documents.undocumented_income import collect_undocumented, uncredited_refund_rows

SEPT = (date(2026, 9, 1), date(2026, 9, 30))


def at(day, hour=12):
    return datetime(2026, 9, day, hour, tzinfo=dt_timezone.utc)


class Fixture(TestCase):
    def setUp(self):
        city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='סניף צפון', city=city)
        self.family = Family.objects.create(name='משפחת צפוני', branch=self.branch)
        self.kid = Child.objects.create(
            family=self.family, first_name='נועה', last_name='צפוני',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        manager = make_user('manager-tidy@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(manager).objects.get(pk=manager.pk)

    def charge(self, number, amount, day):
        payment = Payment.objects.create(
            child=self.kid, family=self.family, branch=self.branch, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal(amount), final_amount=Decimal(amount), payment_date=at(day),
        )
        invoice = Invoice.objects.create(
            invoice_number=number, family=self.family, branch=self.branch, payment=payment,
            amount=Decimal(amount), status='paid', payment_method='credit_card', payment_type='recurring',
            payer_name=self.family.name, invoice_date=at(day),
        )
        invoice.children.create(child=self.kid)
        return payment, invoice

    def refund(self, payment, amount, day, index='90001'):
        return TranzilaTransaction.objects.create(
            transaction_id=index, transaction_type='refund', response_code='000', is_successful=True,
            idempotency_key=f'refund_claim_payment_{payment.id}', request_data={'amount': amount},
            response_timestamp=at(day),
        )

    def credit_note(self, number, linked, amount='100.00'):
        return FormalDocument.objects.create(
            document_number=number, document_type='credit_invoice', client_type='existing', child=self.kid,
            branch=self.branch, document_date=date(2026, 9, 20), subtotal=Decimal(amount),
            total_amount=Decimal(amount), linked_document_number=linked,
        )

    def report(self):
        report = build_report(self.manager, *SEPT, 'ספטמבר 2026')
        report.undocumented = collect_undocumented(self.manager, *SEPT)
        return report


class UncreditedRefundTests(Fixture):
    def test_a_refund_with_no_credit_note_is_listed_and_comes_off_the_take(self):
        payment, invoice = self.charge('INV-20260901-FAM-AAAA0001', '350.00', 1)
        self.refund(payment, '350.00', 3, index='90349')

        rows = uncredited_refund_rows(None, *SEPT)

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].reference, rows[0].detail, rows[0].amount),
                         ('INV-20260901-FAM-AAAA0001', '90349', Decimal('350.00')))
        report = self.report()
        self.assertEqual(report.uncredited_refunds_total, Decimal('350.00'))
        self.assertEqual(report.all_income_total, Decimal('0.00'))

    def test_a_refund_its_credit_note_covers_is_not_listed_twice(self):
        payment, invoice = self.charge('IR-2026-000001', '250.00', 16)
        self.refund(payment, '250.00', 16)
        self.credit_note('CR-2026-000001', 'IR-2026-000001', '250.00')

        self.assertEqual(uncredited_refund_rows(None, *SEPT), [])

    def test_a_charge_inside_a_family_receipt_is_found_through_the_receipt(self):
        first, receipt = self.charge('INV-20260902-FAM-BBBB0002', '525.00', 2)
        second = Payment.objects.create(
            child=self.kid, family=self.family, branch=self.branch, payment_type='recurring_subscription',
            status='completed', base_amount=Decimal('250.00'), final_amount=Decimal('250.00'), payment_date=at(2),
        )
        InvoiceActivityLog.objects.create(invoice=receipt, action=CHECKOUT_LINES_ACTION,
                                          details={'lines': [], 'payment_ids': [str(first.id), str(second.id)]})
        self.refund(second, '250.00', 8)

        rows = uncredited_refund_rows(None, *SEPT)
        self.assertEqual([row.reference for row in rows], ['INV-20260902-FAM-BBBB0002'])

        self.credit_note('CR-2026-000002', 'INV-20260902-FAM-BBBB0002', '250.00')
        self.assertEqual(uncredited_refund_rows(None, *SEPT), [])

    def test_a_refund_outside_the_period_is_left_out(self):
        payment, _invoice = self.charge('INV-20260901-FAM-CCCC0003', '300.00', 1)
        TranzilaTransaction.objects.create(
            transaction_id='90002', transaction_type='refund', response_code='000', is_successful=True,
            idempotency_key=f'refund_claim_payment_{payment.id}', request_data={'amount': '300.00'},
            response_timestamp=datetime(2026, 10, 2, 12, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(uncredited_refund_rows(None, *SEPT), [])


class PdfReadabilityTests(Fixture):
    def test_money_has_thousands_and_one_way_of_writing_a_minus(self):
        from apps.documents.period_report_pdf import _money

        self.assertEqual(_money(Decimal('41353')), '₪41,353.00')
        self.assertEqual(_money(Decimal('-72.03')), '-₪72.03')

    def test_a_long_number_shrinks_instead_of_breaking(self):
        from apps.documents.period_report_pdf import COL_WIDTHS, _ensure_fonts_registered, _rtl_cell, _styles

        _ensure_fonts_registered()
        cell = _rtl_cell('IR-2026-000123456', _styles()['td_num'], COL_WIDTHS[1] * 0.6)
        self.assertNotIn('<br/>', cell.text)
        self.assertLess(cell.style.fontSize, _styles()['td_num'].fontSize)

    def test_the_tables_fit_the_printable_width(self):
        from reportlab.lib.units import cm

        from apps.documents import period_report_pdf as pdf

        for widths in (pdf.COL_WIDTHS, pdf.UNDOC_WIDTHS, pdf.INCOME_WIDTHS, pdf.REFUND_WIDTHS):
            self.assertAlmostEqual(sum(widths), 18 * cm, places=3)

    def test_nothing_issued_through_tranzila_is_one_line_not_a_gap(self):
        from apps.documents.period_report_pdf import _ensure_fonts_registered, _reconciliation_block, _styles

        _ensure_fonts_registered()
        self.charge('IR-2026-000001', '250.00', 16)
        report = self.report()
        report.reconciliation = {
            'reachable': True, 'agrees': True,
            'tranzila': {'count': 0, 'total': 0}, 'local': {'count': 0, 'total': 0},
            'only_in_tranzila': [], 'only_in_crm': [], 'unmatchable': [],
        }
        texts = [para.text for para in _reconciliation_block(report, _styles()) if hasattr(para, 'text')]
        joined = ' '.join(texts)
        self.assertNotIn('0.00', joined)
        self.assertTrue(texts, 'the section says why there is nothing to compare')

    def test_the_report_renders_with_its_overview_and_refunds(self):
        from apps.documents.period_report_pdf import generate_period_report_pdf

        payment, _invoice = self.charge('INV-20260901-FAM-DDDD0004', '350.00', 1)
        self.refund(payment, '350.00', 3)
        self.assertTrue(generate_period_report_pdf(self.report()).startswith(b'%PDF'))


class ReconciliationScopeTests(TestCase):
    def test_only_documents_issued_through_tranzila_are_compared(self):
        from unittest.mock import patch

        from apps.core.tranzila_ledger import reconcile_period

        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='existing',
            customer_name='לקוח', branch=branch, document_date=date(2026, 9, 10),
            subtotal=Decimal('100.00'), total_amount=Decimal('118.00'),
        )
        with patch('apps.core.tranzila_ledger._tranzila_client') as client:
            client.return_value.list_documents.return_value = {'success': True, 'documents': []}
            result = reconcile_period(*SEPT)
        self.assertEqual(result['local']['count'], 0)
        self.assertEqual(result['only_in_crm'], [])
        self.assertTrue(result['agrees'])


class RegisterFilesTests(Fixture):
    def setUp(self):
        super().setUp()
        self.charge('IR-2026-000001', '236.00', 16)
        payment, _ = self.charge('INV-20260901-FAM-EEEE0005', '350.00', 1)
        self.refund(payment, '350.00', 3, index='90349')
        self.credit_note('CR-2026-000001', 'IR-2026-000001', '100.00')

    def test_the_csv_has_its_totals_its_refunds_and_a_summary_that_matches(self):
        from apps.documents.register import register_csv

        report = self.report()
        lines = list(csv.reader(io.StringIO(register_csv(report).decode('utf-8-sig'))))
        documents_total = next(line for line in lines if line[:1] == ['סה"כ מסמכים'])
        self.assertEqual(documents_total[11], f'{report.totals.net_of_credits:.2f}')
        self.assertIn(['זיכויים בכרטיס שלא הופקה להם חשבונית זיכוי'], lines)
        self.assertIn(['סה"כ זיכויים ללא חשבונית זיכוי', '', '', '', '', '-350.00'], lines)
        self.assertIn(['סה"כ כסף שנכנס בתקופה', f'{report.all_income_total:.2f}'], lines)
        subtotal = next(line for line in lines if len(line) > 1 and line[1].startswith('סה"כ חיובי חוגים'))
        self.assertEqual(subtotal[8], '350.00')

    def test_the_workbook_reads_right_to_left_with_a_summary_first(self):
        from apps.documents.register import register_xlsx

        report = self.report()
        archive = zipfile.ZipFile(io.BytesIO(register_xlsx(report)))
        workbook = archive.read('xl/workbook.xml').decode()
        self.assertEqual(re.findall(r'<sheet name="([^"]+)"', workbook),
                         ['סיכום', 'מסמכים', 'ללא מסמך', 'זיכויים ללא חשבונית'])
        documents = archive.read('xl/worksheets/sheet2.xml').decode()
        self.assertIn('rightToLeft="1"', documents)
        self.assertIn('state="frozen"', documents)
        self.assertIn('<autoFilter', documents)
        self.assertIn('IR-2026-000001', documents)
        summary = archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn(f'<v>{report.all_income_total:.2f}</v>', summary)
        self.assertIn('showGridLines="0"', summary)

    def test_the_endpoint_answers_the_workbook_on_request(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.manager)
        res = client.get('/api/v1/documents/documents/register-export/',
                         {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'output': 'xlsx'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('.xlsx', res['Content-Disposition'])
        self.assertTrue(res.content.startswith(b'PK'))
        csv_res = client.get('/api/v1/documents/documents/register-export/',
                             {'start_date': '2026-09-01', 'end_date': '2026-09-30'})
        self.assertEqual(csv_res['Content-Type'], 'text/csv; charset=utf-8')


class MissingReceiptsCsvTests(TestCase):
    def test_an_empty_year_says_so_and_totals_zero(self):
        from apps.documents.missing_receipts import missing_receipts_csv

        lines = list(csv.reader(io.StringIO(
            missing_receipts_csv({'year': 2026, 'count': 0, 'total': '0.00', 'rows': []}).decode('utf-8-sig'),
        )))
        self.assertEqual(lines[1], ['אין חיובים ללא קבלה בשנת 2026'])
        self.assertEqual(lines[2][:2], ['סה"כ', '0 חיובים'])
