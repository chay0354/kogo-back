"""Another software's columns: the suggestion, the office's mapping, and the rows it reads."""
from datetime import datetime

from django.test import SimpleTestCase

from apps.legacy_import.columns import (
    clean_mapping,
    describe,
    generic_type_key,
    missing_fields,
    parse_money,
    parse_table,
    payment_label,
    read_date,
    read_number,
    suggest_mapping,
)
from apps.legacy_import.reader import ImportFileError, Sheet

HEBREW = ['סוג מסמך', 'מס\' מסמך', 'תאריך', 'שם לקוח', 'ח.פ', 'דוא"ל', 'טלפון נייד', 'סכום לפני מע"מ',
          'מע"מ', 'סה"כ כולל מע"מ', 'אמצעי תשלום', 'מספר הקצאה', 'מסמך מקושר', 'תיאור', 'סיסמה']
ENGLISH = ['Document Type', 'Invoice No', 'Invoice Date', 'Client Name', 'VAT ID', 'E-mail', 'Mobile',
           'Subtotal', 'VAT', 'Total', 'Payment Method', 'Allocation Number', 'Related Document', 'Notes']


def sheet(rows, headers=HEBREW):
    return Sheet(headers=list(headers), rows=[list(r) + [None] * (len(headers) - len(r)) for r in rows])


class SuggestionTests(SimpleTestCase):
    def test_hebrew_headers(self):
        mapping = suggest_mapping(HEBREW)
        self.assertEqual({key: HEBREW[i] for key, i in mapping.items() if i is not None}, {
            'doc_type': 'סוג מסמך', 'number': "מס' מסמך", 'date': 'תאריך', 'customer_name': 'שם לקוח',
            'customer_id': 'ח.פ', 'email': 'דוא"ל', 'phone': 'טלפון נייד', 'amount_before_vat': 'סכום לפני מע"מ',
            'vat': 'מע"מ', 'total': 'סה"כ כולל מע"מ', 'payment_method': 'אמצעי תשלום',
            'allocation_number': 'מספר הקצאה', 'linked_document': 'מסמך מקושר', 'details': 'תיאור',
        })

    def test_english_headers(self):
        mapping = suggest_mapping(ENGLISH)
        self.assertEqual({key: ENGLISH[i] for key, i in mapping.items() if i is not None}, {
            'doc_type': 'Document Type', 'number': 'Invoice No', 'date': 'Invoice Date',
            'customer_name': 'Client Name', 'customer_id': 'VAT ID', 'email': 'E-mail', 'phone': 'Mobile',
            'amount_before_vat': 'Subtotal', 'vat': 'VAT', 'total': 'Total', 'payment_method': 'Payment Method',
            'allocation_number': 'Allocation Number', 'linked_document': 'Related Document', 'details': 'Notes',
        })

    def test_a_password_column_is_neither_suggested_sampled_nor_mappable(self):
        described = describe(sheet([['קבלה', '1', '01/01/2025', 'א', '', '', '', '', '', '10', '', '', '', '', 'pw1']]))
        password = described['columns'][-1]
        self.assertTrue(password['sensitive'])
        self.assertEqual(password['samples'], [])
        self.assertNotIn(len(HEBREW) - 1, described['suggested'].values())
        with self.assertRaises(ImportFileError):
            clean_mapping({'number': len(HEBREW) - 1}, HEBREW)

    def test_the_type_columns_values_come_with_their_suggested_types(self):
        described = describe(sheet([['חשבונית מס', '1', '01/01/2025'], ['Receipt', '2', '01/01/2025'],
                                    ['משהו אחר', '3', '01/01/2025']]))
        self.assertEqual(described['suggested_types'],
                         {'חשבונית מס': 'tax_invoice', 'Receipt': 'receipt', 'משהו אחר': ''})

    def test_a_mapping_that_points_twice_or_nowhere_is_refused(self):
        with self.assertRaises(ImportFileError):
            clean_mapping({'number': 1, 'date': 1}, HEBREW)
        with self.assertRaises(ImportFileError):
            clean_mapping({'number': 99}, HEBREW)
        with self.assertRaises(ImportFileError):
            clean_mapping({'number': 'x'}, HEBREW)
        self.assertEqual(clean_mapping({'number': '1', 'date': None}, HEBREW)['number'], 1)

    def test_what_a_mapping_still_lacks(self):
        self.assertEqual(len(missing_fields({})), 4)
        self.assertEqual(missing_fields({'number': 0, 'date': 1, 'total': 2}, fixed_doc_type='receipt'), [])


class ValueTests(SimpleTestCase):
    def test_type_names_from_any_software(self):
        cases = {
            'חשבונית מס קבלה': 'combined', 'חשבונית מס/קבלה': 'combined', 'Tax Invoice Receipt': 'combined',
            'חשבונית מס': 'tax_invoice', 'Tax Invoice': 'tax_invoice', '305': 'tax_invoice',
            'קבלה': 'receipt', 'Receipt': 'receipt', '400': 'receipt',
            'חשבונית זיכוי': 'credit_invoice', 'Credit Note': 'credit_invoice', '330': 'credit_invoice',
            'חשבון עסקה': 'transaction_invoice', 'Proforma Invoice': 'transaction_invoice', '300': 'transaction_invoice',
            'חשבונית מס זיכוי מס\' 12': 'credit_invoice', 'הצעת מחיר': '', '': '',
        }
        for label, expected in cases.items():
            with self.subTest(label):
                self.assertEqual(generic_type_key(label), expected)

    def test_payment_methods(self):
        self.assertEqual(payment_label('Visa'), 'כרטיס אשראי')
        self.assertEqual(payment_label('כרטיס אשראי - תשלומים'), 'כרטיס אשראי')
        self.assertEqual(payment_label("צ'ק"), 'המחאה')
        self.assertEqual(payment_label('Cash'), 'מזומן')
        self.assertEqual(payment_label('העברה בנקאית'), 'העברה בנקאית')
        self.assertEqual(payment_label('ביט'), 'ביט')
        self.assertEqual(payment_label('-'), '')

    def test_amounts_numbers_and_dates(self):
        self.assertEqual(str(parse_money('₪1,180.50')), '1180.50')
        self.assertEqual(str(parse_money('(100)')), '-100.00')
        self.assertEqual(str(parse_money('100-')), '-100.00')
        self.assertIsNone(parse_money('abc'))
        self.assertEqual(read_number(1234.0), (1234, ''))
        self.assertEqual(read_number('0015'), (15, '0015'))
        self.assertEqual(read_number('INV-0015'), (15, 'INV-0015'))
        self.assertEqual(read_number('ללא'), (None, 'ללא'))
        self.assertEqual(read_date('2025-01-05T10:00:00').isoformat(), '2025-01-05')
        self.assertEqual(read_date('2025/01/05').isoformat(), '2025-01-05')
        self.assertEqual(read_date('05.01.25').isoformat(), '2025-01-05')
        self.assertEqual(read_date(datetime(2025, 1, 5)).isoformat(), '2025-01-05')
        self.assertIsNone(read_date(12))


class ParseTests(SimpleTestCase):
    def rows(self, records, **kwargs):
        table = sheet(records)
        return parse_table(table, suggest_mapping(table.headers), source_system='greeninvoice', **kwargs)

    def test_a_row_in_the_imports_shape(self):
        rows, skipped, unknown = self.rows([
            ['חשבונית מס', '1001', '05/01/2025', 'מתנ"ס הדגמה', '512345678', 'Office@Example.test', '050-000-0001',
             '1000', '180', '1180', '', '123456789', '', 'הדרכה'],
        ])
        self.assertEqual((skipped, unknown), ([], []))
        row = rows[0]
        self.assertEqual((row['doc_type'], row['number'], row['date']), ('tax_invoice', 1001, '2025-01-05'))
        self.assertEqual((row['invoice_total'], row['receipt_total'], row['credit_total']), ('1180.00', '0.00', '0.00'))
        self.assertEqual((row['amount_before_vat'], row['vat_amount']), ('1000.00', '180.00'))
        self.assertEqual((row['first_name'], row['id_number'], row['customer_key']), ('מתנ"ס הדגמה', '512345678', '512345678'))
        self.assertEqual((row['email'], row['phone']), ('office@example.test', '0500000001'))
        self.assertEqual((row['allocation_number'], row['details']), ('123456789', 'הדרכה'))
        # Every key the previous software's rows have, so the commit reads both the same way.
        from apps.legacy_import.tests.helpers import row as tazman_row
        self.assertTrue(set(tazman_row()) <= set(row))

    def test_the_total_from_its_parts_and_a_credit_kept_positive(self):
        rows, _, _ = self.rows([
            ['קבלה', '1', '01/01/2025', 'א', '', '', '', '', '', '500'],
            ['חשבונית זיכוי', '2', '01/01/2025', 'א', '', '', '', '-100', '-18', ''],
            ['חשבונית מס קבלה', '3', '01/01/2025', 'א', '', '', '', '100', '18', ''],
        ])
        receipt, credit, combined = rows
        self.assertEqual((receipt['receipt_total'], receipt['invoice_total']), ('500.00', '0.00'))
        self.assertEqual((credit['credit_total'], credit['amount_before_vat'], credit['vat_amount']), ('118.00', '100.00', '18.00'))
        self.assertEqual((combined['invoice_total'], combined['receipt_total']), ('118.00', '118.00'))

    def test_unknown_types_are_counted_and_the_office_can_name_them(self):
        records = [['הצעת מחיר', '1', '01/01/2025', 'א', '', '', '', '', '', '10'],
                   ['הצעת מחיר', '2', '01/01/2025', 'א', '', '', '', '', '', '10']]
        rows, skipped, unknown = self.rows(records)
        self.assertEqual((rows, unknown), ([], [{'label': 'הצעת מחיר', 'count': 2}]))
        self.assertEqual(len(skipped), 2)
        rows, _, unknown = self.rows(records, type_values={'הצעת מחיר': 'transaction_invoice'})
        self.assertEqual([r['doc_type'] for r in rows], ['transaction_invoice'] * 2)
        self.assertEqual(unknown, [])

    def test_one_type_for_a_file_without_a_type_column(self):
        headers = ['מספר קבלה', 'תאריך', 'סכום']
        table = sheet([['7', '01/01/2025', '50']], headers)
        mapping = suggest_mapping(headers)
        self.assertIsNone(mapping['doc_type'])
        with self.assertRaises(ImportFileError):
            parse_table(table, mapping, source_system='x')
        rows, _, _ = parse_table(table, mapping, source_system='x', fixed_doc_type='receipt')
        self.assertEqual((rows[0]['doc_type'], rows[0]['type_label'], rows[0]['receipt_total']), ('receipt', 'קבלה', '50.00'))

    def test_skipped_rows_say_why(self):
        _, skipped, _ = self.rows([
            ['קבלה', '', '01/01/2025', 'א', '', '', '', '', '', '1'],
            ['קבלה', '5', 'לא תאריך', 'א', '', '', '', '', '', '1'],
            ['קבלה', '6', '01/01/2025', 'א', '', '', '', '', '', '1'],
            ['קבלה', '6', '02/01/2025', 'א', '', '', '', '', '', '1'],
        ])
        self.assertEqual([s['reason'] for s in skipped], ['אין מספר מסמך', 'אין תאריך', 'מסמך כפול בקובץ'])
        self.assertEqual([s['row'] for s in skipped], [2, 3, 5])

    def test_another_softwares_customer_number_is_its_own(self):
        headers = ['סוג מסמך', 'מספר', 'תאריך', 'סכום', 'מספר לקוח']
        table = sheet([['קבלה', '1', '01/01/2025', '5', '17']], headers)
        rows, _, _ = parse_table(table, suggest_mapping(headers), source_system='icount')
        self.assertEqual(rows[0]['customer_key'], 'ext:icount:17')
