"""
The uniform-structure export reads the register the period report prints, so
every channel's documents reach the Tax Authority's files — and the numbers
that never became a document do not.
"""
import io
import zipfile
from datetime import date
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents.models import DocumentLineItem, FormalDocument
from apps.documents.tests.test_register import RegisterFixture, make_user

URL = '/api/v1/documents/documents/uniform-export/'


class UniformExportTests(RegisterFixture, APITestCase):
    def files(self, response):
        outer = zipfile.ZipFile(io.BytesIO(response.content))
        names = outer.namelist()
        ini = outer.read(next(name for name in names if name.endswith('INI.TXT')))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(next(name for name in names if name.endswith('BKMVDATA.zip')))))
        records = inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines()
        return names, ini.decode('iso-8859-8'), records

    def test_every_channel_lands_in_the_files_and_a_failed_sale_does_not(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        sale = self.store_sale()
        invoice = FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='existing',
            child=self.kid, document_date=date(2026, 8, 11), subtotal=Decimal('200.00'),
            vat_amount=Decimal('36.00'), total_amount=Decimal('236.00'),
        )
        DocumentLineItem.objects.create(
            document=invoice, description='סדנה', quantity=Decimal('1'), unit_price=Decimal('200.00'),
        )
        self.credit_note()
        failed = self.store_sale(status='failed', day=12)
        self.client.force_authenticate(self.manager)

        res = self.client.get(URL, {'month': '2026-08'})

        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
        self.assertEqual(res['Content-Type'], 'application/zip')
        names, ini, records = self.files(res)
        folder = f'OPENFRMT/51650441.{timezone.localdate():%y}/'
        self.assertTrue(all(name.startswith(folder) for name in names), names)
        headers = [line for line in records if line.startswith('C100')]
        self.assertEqual(
            {line[25:45].strip() for line in headers},
            {'IR-2026-000001', sale.invoice_number, 'TI-2026-000001', 'CR-2026-000001'},
        )
        self.assertNotIn(failed.invoice_number, '\n'.join(records))
        # The lesson receipt and the store sale were paid, and each says how.
        self.assertEqual(sum(1 for line in records if line.startswith('D120')), 2)
        self.assertIn('C100' + '4'.rjust(15, '0'), ini)

    def test_a_credit_note_names_the_receipt_it_credits(self):
        self.lesson_receipt('IR-2026-000001')
        self.credit_note(credits='IR-2026-000001')
        self.client.force_authenticate(self.manager)

        _names, _ini, records = self.files(self.client.get(URL, {'month': '2026-08'}))

        credit_line = next(line for line in records if line.startswith('D110') and 'CR-2026-000001' in line)
        self.assertIn('IR-2026-000001', credit_line)

    def test_a_range_across_two_tax_years_is_refused(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'start_date': '2025-12-01', 'end_date': '2026-01-31'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('שנת מס', res.data['error'])

    def test_an_empty_month_still_gives_the_files(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-08'})
        self.assertEqual(res.status_code, 200)
        _names, _ini, records = self.files(res)
        self.assertFalse([line for line in records if line.startswith('C100')])

    def test_a_partner_is_refused(self):
        self.client.force_authenticate(make_user('partner-uniform@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.client.get(URL, {'month': '2026-08'}).status_code, 403)
