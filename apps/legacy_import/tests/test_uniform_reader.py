"""
Another software's מבנה אחיד files, read back. The files are written by kogo's
own writer (uniform_format.build_uniform_files) — the same layouts the reader
cuts fields by — as another software would write them: its own name in the
INI, plain numbers, every kind of document it issues.
"""
import io
import zipfile
from datetime import date, datetime, time
from decimal import Decimal

from django.test import SimpleTestCase

from apps.documents.uniform_format import (
    UniformAddress,
    UniformBusiness,
    UniformDocument,
    UniformLine,
    UniformPayment,
    build_uniform_files,
    uniform_zip,
)
from apps.legacy_import.reader import ImportFileError
from apps.legacy_import.uniform_reader import find_files, read_ini, read_uniform

MOMENT = datetime(2026, 1, 10, 9, 30)
BUSINESS = UniformBusiness(
    vat_number='516504412', name='עסק הדגמה', software_version='7.1', software_name='Rivhit',
    vendor_name='ספק הדגמה', vendor_vat_number='512345678',
)


def doc(type_code, number, total, **extra):
    base = dict(
        type_code=type_code, number=str(number), issue_date=date(2025, 3, 1), issue_time=time(10, 0),
        document_date=date(2025, 3, 2), customer_name='מתנ"ס הדגמה', customer_vat_number='512345678',
        customer_address=UniformAddress(street='הרצל', house_number='5', city='פתח תקווה'),
        customer_phone='050-0000001', customer_key='C17',
        amount_before_discount=total, amount_after_discount=total, vat_amount=Decimal('0'), total_amount=total,
    )
    base.update(extra)
    return UniformDocument(**base)


def export(documents, business=BUSINESS):
    files = build_uniform_files(business, documents, period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
                                generated_at=MOMENT, primary_id=123456789012345)
    return files, uniform_zip(files, business, MOMENT)


DOCUMENTS = [
    doc(305, 40001, Decimal('1180.00'), amount_before_discount=Decimal('1000.00'),
        amount_after_discount=Decimal('1000.00'), vat_amount=Decimal('180.00'),
        lines=[UniformLine(description='הדרכת קפוארה', quantity=Decimal('1'), unit_price=Decimal('1000'),
                           line_total=Decimal('1000'), vat_rate=Decimal('18'))]),
    doc(400, 33001, Decimal('1180.00'), withholding_tax=Decimal('50.00'),
        payments=[UniformPayment(method='bank_transfer', amount=Decimal('1180.00'))]),
    doc(320, 70001, Decimal('236.00'), amount_before_discount=Decimal('200.00'),
        amount_after_discount=Decimal('200.00'), vat_amount=Decimal('36.00'),
        payments=[UniformPayment(method='credit_card', amount=Decimal('236.00'), due_date=date(2025, 3, 2))]),
    doc(330, 41001, Decimal('118.00'), amount_before_discount=Decimal('100.00'),
        amount_after_discount=Decimal('100.00'), vat_amount=Decimal('18.00'),
        linked_document_type=305, linked_document_number='40001',
        lines=[UniformLine(description='זיכוי', quantity=Decimal('1'), unit_price=Decimal('100'),
                           line_total=Decimal('100'), vat_rate=Decimal('18'))]),
    doc(300, 60001, Decimal('500.00'), cancelled=True),
    doc(200, 90001, Decimal('0.00')),  # a delivery note: not a sale, not imported
    doc(320, 'IR26000123', Decimal('10.00')),  # a number with letters, as kogo itself writes them
]


class UniformReaderTests(SimpleTestCase):
    def setUp(self):
        self.files, self.zip = export(DOCUMENTS)

    def test_every_sales_document_comes_back(self):
        result = read_uniform(self.zip, source_system='rivhit')
        by_number = {row['number']: row for row in result.rows}
        self.assertEqual(
            sorted((row['doc_type'], row['number']) for row in result.rows),
            [('combined', 70001), ('combined', 26000123), ('credit_invoice', 41001), ('receipt', 33001),
             ('tax_invoice', 40001), ('transaction_invoice', 60001)],
        )
        invoice = by_number[40001]
        self.assertEqual((invoice['date'], invoice['type_label']), ('2025-03-02', 'חשבונית מס'))
        self.assertEqual((invoice['amount_before_vat'], invoice['vat_amount'], invoice['invoice_total']),
                         ('1000.00', '180.00', '1180.00'))
        self.assertEqual(invoice['details'], 'הדרכת קפוארה')
        self.assertEqual((invoice['first_name'], invoice['id_number'], invoice['phone']),
                         ('מתנ"ס הדגמה', '512345678', '0500000001'))
        self.assertEqual((invoice['city'], invoice['address']), ('פתח תקווה', 'הרצל 5'))
        receipt = by_number[33001]
        self.assertEqual((receipt['receipt_total'], receipt['withholding'], receipt['payment_type']),
                         ('1180.00', '50.00', 'העברה בנקאית'))
        self.assertIsNone(receipt['amount_before_vat'])
        self.assertEqual(by_number[70001]['payment_type'], 'כרטיס אשראי')
        credit = by_number[41001]
        self.assertEqual((credit['credit_total'], credit['linked_document']), ('118.00', 'חשבונית מס 40001'))
        self.assertEqual(by_number[60001]['status'], 'מבוטל')
        self.assertEqual(by_number[26000123]['original_number'], 'IR26000123')

    def test_what_is_not_a_sale_is_named_and_left_out(self):
        result = read_uniform(self.zip, source_system='rivhit')
        self.assertEqual(len(result.skipped), 1)
        self.assertIn('200', result.skipped[0]['reason'])
        self.assertIn('תעודת משלוח', result.skipped[0]['reason'])

    def test_the_ini_says_whose_books_and_which_software(self):
        info = read_uniform(self.zip, source_system='rivhit').info
        self.assertEqual((info['software'], info['vat_number'], info['business_name']), ('Rivhit', '516504412', 'עסק הדגמה'))
        self.assertEqual((info['period_start'], info['period_end']), ('2025-01-01', '2025-12-31'))
        self.assertEqual(info['records'], {'C100': 7, 'D110': 2, 'D120': 2})
        self.assertEqual(info['warnings'], [])
        self.assertEqual(read_ini(self.files.ini)['declared']['C100'], 7)

    def test_bkmvdata_alone_and_zipped_without_its_folders(self):
        alone = read_uniform(self.files.bkmvdata, source_system='rivhit')
        self.assertEqual(len(alone.rows), 6)
        self.assertIn('INI.TXT', alone.info['warnings'][0])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('INI.TXT', self.files.ini)
            archive.writestr('BKMVDATA.TXT', self.files.bkmvdata)
        self.assertEqual(len(read_uniform(buffer.getvalue(), source_system='rivhit').rows), 6)

    def test_an_ini_that_disagrees_with_the_data_is_flagged(self):
        truncated = b'\r\n'.join(line for line in self.files.bkmvdata.split(b'\r\n') if b'41001' not in line)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('INI.TXT', self.files.ini)
            archive.writestr('BKMVDATA.TXT', truncated)
        warnings = read_uniform(buffer.getvalue(), source_system='rivhit').info['warnings']
        self.assertTrue(any('C100' in w for w in warnings))

    def test_what_is_not_the_uniform_structure_is_refused_in_hebrew(self):
        for content, word in ((self.files.ini, 'INI'), (b'hello', 'מבנה אחיד'), (self._zip_without_data(), 'BKMVDATA')):
            with self.subTest(word):
                with self.assertRaises(ImportFileError) as caught:
                    read_uniform(content, source_system='x')
                self.assertIn(word, str(caught.exception))

    def test_the_files_are_found_at_any_depth(self):
        found = find_files(self.zip)
        self.assertEqual(len(found.data), 1)
        directory = found.data[0][0]
        self.assertIn(directory, found.ini)
        self.assertTrue(directory.startswith('OPENFRMT/51650441.26/'))

    def _zip_without_data(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('readme.txt', 'nothing')
        return buffer.getvalue()
