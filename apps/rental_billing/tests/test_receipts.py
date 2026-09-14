"""The rental receipt: its own consecutive RT run, what it says, the marks on its PDF, the
e-mail after commit — and that the register, its CSV, continuity(), the uniform export
(type 320) and the period report all carry it, under the 'rentals' channel and סוחרים."""
import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APITestCase

from apps.documents.document_pdf import generate_document_pdf
from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_LINE, ISSUER_NAME
from apps.documents.models import DocumentSeries, FormalDocument
from apps.documents.numbering import SERIES_LABELS, SERIES_RENTAL, continuity, is_rental_number
from apps.documents.period_report import GROUP_BY_UNIT, build_report, month_bounds
from apps.documents.register import CHANNEL_LABELS, CHANNEL_RENTALS, register_rows
from apps.documents.uniform_export import _documents
from apps.rental_billing.billing import charge_due
from apps.rental_billing.models import TenantCharge
from apps.rental_billing.tests.factories import BillingFixture


def pdf_text(pdf_bytes: bytes) -> str:
    return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf_bytes)).pages)


@override_settings(RENTAL_BILLING_ENABLED=True)
class RentalReceiptTests(BillingFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.year = self.today.year
        self.month = month_bounds(self.today.year, self.today.month)

    def charge_two_months(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 10))
        charge_due(today=date(2026, 11, 10))
        return list(TenantCharge.objects.order_by('period'))

    def receipt_rows(self, report):
        return [row for row in register_rows(report) if is_rental_number(row.document_number)]

    def test_receipts_are_numbered_consecutively_in_their_own_run(self):
        charges = self.charge_two_months()
        numbers = [charge.receipt.document_number for charge in charges]
        self.assertEqual(numbers, [f'RT-{self.year}-000001', f'RT-{self.year}-000002'])
        self.assertEqual(DocumentSeries.objects.get(series=SERIES_RENTAL, year=self.year).counter, 2)
        # The office's own run of invoice-receipts is not drawn from.
        self.assertFalse(DocumentSeries.objects.filter(series='IRM').exists())
        self.assertEqual(SERIES_LABELS[SERIES_RENTAL], 'חשבונית מס/קבלה · שכירויות')

    def test_the_receipt_names_the_tenant_and_breaks_out_vat_and_the_card(self):
        (charge, _) = self.charge_two_months()
        doc = FormalDocument.objects.prefetch_related('line_items', 'payments').get(pk=charge.receipt_id)
        self.assertEqual(doc.document_type, 'combined')
        self.assertEqual(doc.business_customer, self.tenancy.tenant)
        self.assertEqual(doc.business.name, 'סוחרים')
        self.assertEqual(doc.branch, self.branch)
        self.assertEqual(
            (doc.subtotal, doc.vat_amount, doc.total_amount, doc.vat_percent),
            (Decimal('1234.56'), Decimal('222.22'), Decimal('1456.78'), Decimal('18')),
        )
        self.assertFalse(doc.prices_include_vat)
        self.assertFalse(doc.tranzila_issued)
        payment = doc.payments.get()
        self.assertEqual((payment.payment_method, payment.card_last_four, payment.amount), ('credit_card', '4242', Decimal('1456.78')))
        self.assertEqual(payment.reference, 'אישור C100')
        self.assertIn('שכירות סטודיו · אוקטובר 2026', doc.line_items.get().description)

    def test_the_pdf_carries_the_mandatory_marks_and_the_tenants_numbers(self):
        tenant = self.tenancy.tenant
        tenant.id_number = '039876545'
        tenant.save(update_fields=['id_number'])
        (charge, _) = self.charge_two_months()
        text = pdf_text(generate_document_pdf(charge.receipt))
        # pypdf drops the full stop that ends a right-to-left run, so the labels are matched without it.
        for mark in ('מקור', 'מסמך ממוחשב', 'עוסק מורשה', 'חשבונית מס/קבלה', 'ת.ז', 'ח.פ. / ע.מ',
                     '039876545', '512345678', 'סטודיו אור', '222.22', '1456.78', 'כרטיס אשראי'):
            self.assertIn(mark, text)
        # תקנה 9א(א)(1): the issuer's line, with "עוסק מורשה" and the number, is printed on the
        # face of a rental receipt (the constant carries the whole line). pypdf cuts a mixed
        # Hebrew-and-digits run after its Hebrew head, so the line is counted by its opening,
        # as apps/store/tests/test_tax_document_rules.py does: once more than on an office document.
        self.assertIn(f'עוסק מורשה {ISSUER_COMPANY_NUMBER}', ISSUER_LINE)
        office = FormalDocument.objects.create(
            document_number='IRM-2026-000009', document_type='combined', client_type='business',
            business_customer=tenant, document_date=date(2026, 9, 1), subtotal=Decimal('1'), total_amount=Decimal('1'),
        )
        office.refresh_from_db()  # the stored columns, as the documents module reads them
        office_text = pdf_text(generate_document_pdf(office))
        # The design the owner asked for (14.9) prints the business block — the issuer,
        # עוסק מורשה and the number — on every document it draws, so a rental receipt no
        # longer carries that line one time more than a document issued by hand.
        self.assertEqual(text.count(ISSUER_NAME), office_text.count(ISSUER_NAME))
        self.assertIn('עוסק מורשה', office_text)

    @override_settings(RESEND_API_KEY='', EMAIL_HOST='smtp.test')
    def test_the_receipt_is_emailed_to_the_tenant_after_commit(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        with patch('apps.rental_billing.receipt_email.send_resend_email') as resend:
            with self.captureOnCommitCallbacks(execute=True):
                charge_due(today=date(2026, 10, 10))
        resend.assert_not_called()
        charge = TenantCharge.objects.get()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['or@example.com'])
        self.assertIn(charge.receipt.document_number, message.subject)
        self.assertIn('מסמך ממוחשב', message.body)
        self.assertIn('****4242', message.body)
        self.assertEqual(message.attachments[0][0], f'{charge.receipt.document_number}.pdf')
        self.assertIsNotNone(charge.receipt_emailed_at)

    def test_an_email_that_fails_leaves_the_charge_and_its_receipt(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        with patch('apps.rental_billing.receipt_email.send_rental_receipt_email', side_effect=RuntimeError('smtp down')):
            with self.captureOnCommitCallbacks(execute=True):
                charge_due(today=date(2026, 10, 10))
        charge = TenantCharge.objects.get()
        self.assertEqual(charge.status, TenantCharge.STATUS_CHARGED)
        self.assertIsNotNone(charge.receipt_id)

    def test_the_register_and_its_csv_list_the_receipts_under_rentals(self):
        charges = self.charge_two_months()
        report = build_report(self.manager, *self.month, 'החודש')
        rows = self.receipt_rows(report)
        self.assertEqual([row.document_number for row in rows], [c.receipt.document_number for c in charges])
        row = rows[0]
        self.assertEqual(row.channel, CHANNEL_RENTALS)
        self.assertEqual(row.channel, 'rentals')
        self.assertEqual((row.business_name, row.payment_method, row.document_type), ('סוחרים', 'credit_card', 'combined'))
        self.assertEqual((row.net_amount, row.vat_amount, row.total_amount), (Decimal('1234.56'), Decimal('222.22'), Decimal('1456.78')))

        self.client.force_authenticate(self.manager)
        res = self.client.get('/api/v1/documents/documents/register-export/', {'month': self.today.strftime('%Y-%m')})
        self.assertEqual(res.status_code, 200)
        lines = list(csv.reader(io.StringIO(res.content.decode('utf-8-sig'))))
        header, body = lines[0], lines[1:]
        found = [dict(zip(header, line)) for line in body if line[1] == charges[0].receipt.document_number]
        self.assertEqual(len(found), 1)
        record = found[0]
        self.assertEqual(record['סדרה'], 'RT')
        self.assertEqual(record['ערוץ'], CHANNEL_LABELS[CHANNEL_RENTALS])
        self.assertEqual(record['עסק'], 'סוחרים')
        self.assertEqual(record['קוד מבנה אחיד'], '320')
        self.assertEqual((record['לפני מע"מ'], record['מע"מ'], record['סה"כ']), ('1234.56', '222.22', '1456.78'))
        self.assertEqual(record['אמצעי תשלום'], 'אשראי')

    def test_continuity_checks_the_rt_run(self):
        self.charge_two_months()
        run = next(run for run in continuity(self.year) if run.series == SERIES_RENTAL)
        self.assertEqual((run.issued, run.first, run.last), (2, f'RT-{self.year}-000001', f'RT-{self.year}-000002'))
        self.assertTrue(run.complete)
        self.assertEqual(run.label, 'חשבונית מס/קבלה · שכירויות')
        # A number the run handed out that no receipt carries is a gap.
        DocumentSeries.next_number(SERIES_RENTAL, self.year)
        run = next(run for run in continuity(self.year) if run.series == SERIES_RENTAL)
        self.assertEqual(run.missing, (f'RT-{self.year}-000003',))

    def test_the_uniform_export_reports_the_receipt_as_type_320(self):
        charges = self.charge_two_months()
        report = build_report(self.manager, *self.month, 'החודש')
        rows = [row for row in register_rows(report) if not row.void]
        documents = {doc.number: doc for doc in _documents(rows)}
        doc = documents[charges[0].receipt.document_number]
        self.assertEqual(doc.type_code, 320)
        self.assertEqual((doc.amount_after_discount, doc.vat_amount, doc.total_amount),
                         (Decimal('1234.56'), Decimal('222.22'), Decimal('1456.78')))
        self.assertEqual(doc.customer_vat_number, '512345678')
        self.assertEqual([payment.method for payment in doc.payments], ['credit_card'])
        self.assertEqual(len(doc.lines), 1)

        self.client.force_authenticate(self.manager)
        res = self.client.get('/api/v1/documents/documents/uniform-export/', {'month': self.today.strftime('%Y-%m')})
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))

    def test_the_period_report_counts_the_receipts(self):
        self.charge_two_months()
        report = build_report(self.manager, *self.month, 'החודש', group_by=GROUP_BY_UNIT)
        group = next(group for group in report.groups if group.title == 'סוחרים')
        self.assertEqual(len(group.rows), 2)
        self.assertEqual(group.collected_total, Decimal('2913.56'))
        self.assertEqual(report.collected_total, Decimal('2913.56'))
        by_branch = build_report(self.manager, *self.month, 'החודש')
        self.assertIn('פלורנטין', [group.title for group in by_branch.groups])
        self.assertIn(SERIES_RENTAL, [run.series for run in by_branch.continuity])
