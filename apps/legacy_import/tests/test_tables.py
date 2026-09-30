"""Another software's table: CSV in its encodings and separators, and .xlsx without openpyxl."""
import io
import unittest
from datetime import datetime

from django.test import SimpleTestCase

from apps.legacy_import.reader import ImportFileError
from apps.legacy_import.tables import decode_text, detect_delimiter, file_kind, read_table
from apps.legacy_import.tests.helpers import SAMPLE_XLS, csv_bytes, xlsx_bytes

TABLE = [
    ['סוג מסמך', 'מספר', 'תאריך', 'שם לקוח', 'סה"כ'],
    ['חשבונית מס', '1001', '05/01/2025', 'מתנ"ס הדגמה', '1,180.00'],
    ['קבלה', '2001', '06/01/2025', 'יוסי דוגמה', '500'],
]


class CsvTests(SimpleTestCase):
    def test_the_encodings_a_hebrew_csv_comes_in(self):
        for label, content in (
            ('utf-8', csv_bytes(TABLE)),
            ('utf-8 with a BOM', csv_bytes(TABLE, bom=True)),
            ('windows-1255', csv_bytes(TABLE, encoding='cp1255')),
            ('utf-16 (Excel unicode text)', '﻿'.encode('utf-16-le') + csv_bytes(TABLE, '\t').decode().encode('utf-16-le')),
        ):
            with self.subTest(label):
                sheet = read_table(content)
                self.assertEqual(sheet.headers, TABLE[0])
                self.assertEqual(sheet.rows[0][3], 'מתנ"ס הדגמה')
                self.assertEqual(len(sheet.rows), 2)

    def test_the_separators(self):
        for delimiter in (',', ';', '\t', '|'):
            with self.subTest(repr(delimiter)):
                sheet = read_table(csv_bytes(TABLE, delimiter=delimiter))
                self.assertEqual(sheet.headers, TABLE[0])
                self.assertEqual(sheet.rows[1][4], '500')
        # Excel's own hint on the first line.
        self.assertEqual(detect_delimiter('sep=;\na;b\n'), ';')
        sheet = read_table(b'sep=;\r\n' + csv_bytes(TABLE, delimiter=';'))
        self.assertEqual(sheet.headers, TABLE[0])

    def test_a_title_line_above_the_headers_is_passed_over(self):
        content = csv_bytes([['דוח מסמכים 01/2025'], []] + TABLE)
        sheet = read_table(content)
        self.assertEqual(sheet.headers, TABLE[0])
        self.assertEqual(len(sheet.rows), 2)

    def test_short_rows_are_padded_and_an_empty_file_refused(self):
        sheet = read_table('a,b,c\r\n1\r\n'.encode())
        self.assertEqual(sheet.rows, [['1', None, None]])
        with self.assertRaises(ImportFileError):
            read_table(b'')
        with self.assertRaises(ImportFileError):
            read_table(b'only-one-cell\r\n')

    def test_windows_1255_is_the_fallback_for_bytes_that_are_not_utf8(self):
        self.assertEqual(decode_text('קבלה'.encode('cp1255')), 'קבלה')
        self.assertEqual(decode_text('קבלה'.encode('utf-8')), 'קבלה')


class XlsxTests(SimpleTestCase):
    def test_shared_and_inline_strings_numbers_and_dates(self):
        content = xlsx_bytes([
            ['סוג מסמך', 'מספר', 'תאריך', 'שם לקוח', 'סה"כ'],
            ['חשבונית מס', 1001, datetime(2025, 1, 5), 'inline:מתנ"ס הדגמה', 1180.5],
        ])
        self.assertEqual(file_kind(content), 'xlsx')
        sheet = read_table(content, 'docs.xlsx')
        self.assertEqual(sheet.headers, ['סוג מסמך', 'מספר', 'תאריך', 'שם לקוח', 'סה"כ'])
        row = sheet.rows[0]
        self.assertEqual(row[0], 'חשבונית מס')
        self.assertEqual(row[1], 1001.0)
        self.assertEqual(row[2].date().isoformat(), '2025-01-05')
        self.assertEqual(row[3], 'מתנ"ס הדגמה')
        self.assertEqual(row[4], 1180.5)

    def test_a_broken_xlsx_is_refused_in_hebrew(self):
        for content in (b'PK\x03\x04' + b'\x00' * 50, xlsx_bytes([])):
            with self.subTest(content[:8]):
                with self.assertRaises(ImportFileError) as caught:
                    read_table(content)
                self.assertTrue(str(caught.exception))

    @unittest.skipUnless(__import__('importlib').util.find_spec('openpyxl'), 'openpyxl is not installed here')
    def test_a_workbook_written_by_openpyxl(self):
        # openpyxl is not a kogo dependency; where it happens to be installed, it
        # writes the file a real Excel-like tool would, and the reader must read it.
        import openpyxl

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(['Document type', 'Number', 'Date', 'Customer', 'Total'])
        sheet.append(['Tax invoice', 5001, datetime(2024, 12, 31), 'Studio Test Ltd', 236])
        sheet['C2'].number_format = 'yyyy-mm-dd'
        buffer = io.BytesIO()
        book.save(buffer)
        read = read_table(buffer.getvalue())
        self.assertEqual(read.headers, ['Document type', 'Number', 'Date', 'Customer', 'Total'])
        self.assertEqual(read.rows[0][2].date().isoformat(), '2024-12-31')
        self.assertEqual(read.rows[0][3], 'Studio Test Ltd')


class XlsTests(SimpleTestCase):
    def test_an_xls_goes_through_the_same_reader_as_the_previous_softwares_export(self):
        sheet = read_table(SAMPLE_XLS.read_bytes())
        self.assertIn('מספר המסמך', sheet.headers)
        self.assertEqual(len(sheet.rows), 6)
