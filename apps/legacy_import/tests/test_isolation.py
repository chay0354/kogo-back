"""
A document from the previous software is history, never one of kogo's own: it
is in no number run, in no register, report or export, and in no dashboard
income. Each of those is run here with legacy documents in the same month —
of every type, numbered like kogo's own runs would be — and must not see them.
"""
import io
import zipfile
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.core.models import Business, BusinessCategory
from apps.core.revenue_service import aggregate_income_by_business
from apps.documents.models import DocumentSeries, FormalDocument
from apps.documents.numbering import continuity
from apps.documents.register import channel_documents
from apps.documents.tests.test_register import AUGUST, RegisterFixture
from apps.legacy_import.models import LegacyDocument

UNIFORM = '/api/v1/documents/documents/uniform-export/'
REGISTER = '/api/v1/documents/documents/register-export/'


class LegacyDocumentsFixture(RegisterFixture):
    def setUp(self):
        super().setUp()
        self.business = Business.objects.create(name='עסק בדיקה')
        self.category = BusinessCategory.objects.create(business=self.business, name='כללי')
        # One of every type, dated inside August 2026 and numbered 1, the way a
        # kogo run's first number would read.
        for doc_type in ('combined', 'tax_invoice', 'receipt', 'transaction_invoice', 'credit_invoice'):
            LegacyDocument.objects.create(
                original_type=doc_type, doc_type=doc_type, number=1, document_date=date(2026, 8, 10),
                invoice_total=Decimal('1000.00'), receipt_total=Decimal('1000.00'), credit_total=Decimal('0.00'),
                customer_name='לקוח ישן', business=self.business, business_category=self.category,
                branch=self.north,
            )
        # And one document kogo issued, so each check has something it does see.
        DocumentSeries.objects.create(series='TI', year=2026, counter=1)
        self.kogo_doc = FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='business',
            customer_name='לקוח', document_date=date(2026, 8, 11), subtotal=Decimal('100.00'),
            vat_amount=Decimal('18.00'), total_amount=Decimal('118.00'), branch=self.north,
            business=self.business, business_category=self.category,
        )


class IsolationTests(LegacyDocumentsFixture, TestCase):
    def test_numbering_continuity_sees_only_kogos_runs(self):
        runs = continuity(2026)
        self.assertEqual([(run.series, run.issued, run.missing) for run in runs], [('TI', 1, ())])

    def test_the_register_channels_and_the_period_report(self):
        self.assertEqual(channel_documents(None, *AUGUST), [])
        report = self.report()
        self.assertEqual(set(self.rows(report)), {'TI-2026-000001'})
        self.assertEqual(report.revenue_total, Decimal('118.00'))  # kogo's one document, not the ₪5,000 of history

    def test_the_dashboards_income_by_business(self):
        buckets = aggregate_income_by_business(*AUGUST)
        self.assertEqual(
            [(b['business_name'], b['revenue']) for b in buckets if b['business_name'] == 'עסק בדיקה'],
            [('עסק בדיקה', 118.0)],
        )
        self.assertEqual(sum(b['revenue'] for b in buckets), 118.0)


class ExportIsolationTests(LegacyDocumentsFixture, APITestCase):
    def test_the_uniform_format_export(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(UNIFORM, {'month': '2026-08'})
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
        outer = zipfile.ZipFile(io.BytesIO(res.content))
        inner_name = next(name for name in outer.namelist() if name.endswith('BKMVDATA.zip'))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))
        records = inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines()
        headers = [line for line in records if line.startswith('C100')]
        self.assertEqual([line[25:45].strip() for line in headers], ['TI-2026-000001'])

    def test_the_register_export(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(REGISTER, {'month': '2026-08'})
        self.assertEqual(res.status_code, 200)
        text = res.content.decode('utf-8-sig')
        self.assertIn('TI-2026-000001', text)
        self.assertNotIn('לקוח ישן', text)
