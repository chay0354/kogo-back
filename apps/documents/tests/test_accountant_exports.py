"""
The accountant's exports: the register CSV carries the allocation number, the
number the uniform file writes, and a second section of income with no
document; the uniform file writes a Kogo number with a series part of at most
five positions (מבנה אחיד 1.31, 2.4(ד)).
"""
import csv
import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from apps.documents.models import DocumentLineItem, FormalDocument
from apps.documents.register import UNDOCUMENTED_FAILED, UNDOCUMENTED_TITLE
from apps.documents.tests.test_register import EXPORT, RegisterFixture
from apps.documents.uniform_export import uniform_number

UNIFORM = '/api/v1/documents/documents/uniform-export/'


class UniformNumberTests(SimpleTestCase):
    def test_a_kogo_number_keeps_its_letters_the_year_and_every_digit(self):
        self.assertEqual(uniform_number('IR-2026-000123'), 'IR26000123')
        self.assertEqual(uniform_number('IRM-2026-000001'), 'IRM26000001')
        self.assertEqual(uniform_number('IRM-2026-1000000'), 'IRM261000000')

    def test_the_series_part_is_at_most_five_positions(self):
        for number in ('IR-2026-000123', 'IRM-2026-121883', 'CR-2027-000001'):
            written = uniform_number(number)
            letters = len(written) - len(written.lstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ'))
            self.assertLessEqual(letters + 2, 5, written)  # letters and the year's two digits
            self.assertLessEqual(len(written), 20)

    def test_other_numbers_are_written_as_printed(self):
        for number in ('2026-0042', '12345', 'INV-20260809-A1B2C3D4', ''):
            self.assertEqual(uniform_number(number), number)


class RegisterCsvTests(RegisterFixture, APITestCase):
    def sections(self, response):
        lines = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
        split = lines.index([])
        header, *body = lines[:split]
        return header, [dict(zip(header, line)) for line in body], lines[split + 1:]

    def invoice(self, number='TI-2026-000001', allocation='123456789'):
        doc = FormalDocument.objects.create(
            document_number=number, document_type='tax_invoice', client_type='existing', child=self.kid,
            document_date=date(2026, 8, 11), subtotal=Decimal('6000.00'), vat_amount=Decimal('1080.00'),
            total_amount=Decimal('7080.00'), allocation_number=allocation,
        )
        DocumentLineItem.objects.create(document=doc, description='סדנה', quantity=1, unit_price=Decimal('6000.00'))
        return doc

    def test_every_document_carries_its_allocation_number_and_its_uniform_number(self):
        self.invoice()
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.client.force_authenticate(self.manager)

        header, rows, _rest = self.sections(self.client.get(EXPORT, {'month': '2026-08'}))

        self.assertEqual(header[:4], ['תאריך', 'מספר מסמך', 'סדרה', 'מספר הקצאה'])
        by_number = {row['מספר מסמך']: row for row in rows}
        self.assertEqual(by_number['TI-2026-000001']['מספר הקצאה'], '123456789')
        self.assertEqual(by_number['TI-2026-000001']['מספר במבנה אחיד'], 'TI26000001')
        self.assertEqual(by_number['IR-2026-000001']['מספר הקצאה'], '')
        self.assertEqual(by_number['IR-2026-000001']['מספר במבנה אחיד'], 'IR26000001')

    def test_income_without_a_document_is_the_second_section(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.lesson_receipt('INV-20260809-A1B2C3D4', amount='120.00')
        self.client.force_authenticate(self.manager)

        _header, rows, rest = self.sections(self.client.get(EXPORT, {'month': '2026-08'}))

        self.assertNotIn('INV-20260809-A1B2C3D4', {row['מספר מסמך'] for row in rows})
        self.assertEqual(rest[0], [UNDOCUMENTED_TITLE])
        columns, *lines = rest[1:]
        self.assertEqual(columns[0], 'תאריך')
        (charge,) = [dict(zip(columns, line)) for line in lines if 'INV-20260809-A1B2C3D4' in line]
        self.assertEqual((charge['סכום'], charge['מצב']), ('120.00', 'נספר'))
        self.assertEqual(lines[-1][0], 'סה"כ ללא מסמך')
        self.assertEqual(lines[-1][8], '120.00')

    def test_a_failure_to_gather_it_is_said_in_the_file(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.client.force_authenticate(self.manager)
        with patch('apps.documents.undocumented_income.collect_undocumented', side_effect=RuntimeError('boom')):
            res = self.client.get(EXPORT, {'month': '2026-08'})
        self.assertEqual(res.status_code, 200)
        _header, rows, rest = self.sections(res)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rest, [[UNDOCUMENTED_TITLE], [UNDOCUMENTED_FAILED]])


class UniformFileNumberTests(RegisterFixture, APITestCase):
    def test_every_record_repeats_the_same_compact_number(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.credit_note(credits='IR-2026-000001')
        self.client.force_authenticate(self.manager)

        import zipfile

        res = self.client.get(UNIFORM, {'month': '2026-08'})
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
        outer = zipfile.ZipFile(io.BytesIO(res.content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(next(n for n in outer.namelist() if n.endswith('BKMVDATA.zip')))))
        records = inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines()

        self.assertNotIn('IR-2026-', '\n'.join(records))
        headers = {line[25:45].strip() for line in records if line.startswith('C100')}
        self.assertEqual(headers, {'IR26000001', 'CR26000001'})
        lines = {line[25:45].strip() for line in records if line.startswith('D110')}
        payments = {line[25:45].strip() for line in records if line.startswith('D120')}
        self.assertLessEqual(lines | payments, headers)
        credit = next(line for line in records if line.startswith('D110') and line[25:45].strip() == 'CR26000001')
        self.assertEqual(credit[52:72].strip(), 'IR26000001')  # 1257, the credited document
