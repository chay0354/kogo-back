"""The .xls itself: synthetic workbooks built by fixtures/build_fixtures.py."""
import json

from django.test import SimpleTestCase

from apps.legacy_import.parser import read_rows
from apps.legacy_import.reader import ImportFileError, read_sheet
from apps.legacy_import.tests.helpers import CORRUPT_XLS, FIXTURE_PASSWORD, SAMPLE_XLS


class ReaderTests(SimpleTestCase):
    def test_the_workbook_with_the_real_exports_container_flaw_is_read(self):
        # Without ignore_workbook_corruption xlrd raises "Workbook corruption:
        # seen[0] == 4" on this file — the flaw the owner's export has.
        import io

        import xlrd

        with self.assertRaises(xlrd.compdoc.CompDocError):
            xlrd.open_workbook(file_contents=CORRUPT_XLS.read_bytes(), logfile=io.StringIO())
        sheet = read_sheet(CORRUPT_XLS.read_bytes())
        self.assertEqual(len(sheet.headers), 35)
        self.assertEqual(len(sheet.rows), 6)

    def test_dates_come_back_as_dates_and_numbers_as_numbers(self):
        sheet = read_sheet(SAMPLE_XLS.read_bytes())
        date_col = sheet.headers.index('תאריך')
        id_col = sheet.headers.index('ת"ז \\ ע"מ \\ ח"פ')
        self.assertEqual(sheet.rows[0][date_col].date().isoformat(), '2025-01-05')
        self.assertEqual(sheet.rows[0][id_col], 12345678.0)

    def test_columns_that_are_not_kept_are_never_read(self):
        sheet = read_sheet(SAMPLE_XLS.read_bytes(), keep=lambda headers: {headers.index('שם פרטי')})
        password_col = sheet.headers.index('סיסמת כניסה לאפליקציה')
        self.assertTrue(all(r[password_col] is None for r in sheet.rows))
        self.assertTrue(all(r[0] for r in sheet.rows))

    def test_the_export_becomes_rows_without_the_password(self):
        rows, skipped = read_rows(CORRUPT_XLS.read_bytes())
        self.assertEqual(skipped, [])
        self.assertEqual(len(rows), 6)
        stored = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(FIXTURE_PASSWORD, stored)
        self.assertNotIn('1980', stored)  # the birth date column
        self.assertNotIn('039999999', stored)  # the home phone
        self.assertEqual(
            sorted({(r['doc_type'], r['number']) for r in rows}),
            [('combined', 70001), ('combined', 70002), ('credit_invoice', 41001),
             ('receipt', 33001), ('tax_invoice', 40001), ('transaction_invoice', 60001)],
        )
        office = next(r for r in rows if r['number'] == 40001)
        self.assertEqual(office['first_name'], 'מתנ"ס')
        self.assertEqual(office['id_number'], '512345678')
        self.assertEqual(office['payment_type'], '')
        parent = next(r for r in rows if r['number'] == 70001)
        self.assertEqual((parent['id_number'], parent['card_last_four'], parent['date']), ('012345678', '1234', '2025-01-05'))
        showman = next(r for r in rows if r['number'] == 60001)
        self.assertEqual((showman['phone'], showman['customer_key']), ('0521111111', 'ext:303'))

    def test_a_file_that_is_not_an_xls_is_refused_in_hebrew(self):
        for content in (b'not a spreadsheet', b'PK\x03\x04' + b'\x00' * 100):
            with self.subTest(content[:4]):
                with self.assertRaises(ImportFileError) as caught:
                    read_sheet(content)
                self.assertIn('קובץ', str(caught.exception))
