"""Row-level rules of the import, on synthetic rows: no spreadsheet, no database."""
import json
from datetime import datetime

from django.test import SimpleTestCase

from apps.legacy_import.parser import (
    NEVER_READ,
    classify,
    column_indexes,
    customer_key,
    customers_from_rows,
    name_changes,
    normalise_id,
    normalise_phone,
    parse_card,
    parse_date,
    parse_sheet,
    type_key,
    type_table,
)
from apps.legacy_import.reader import ImportFileError
from apps.legacy_import.tests.helpers import HEADER, row, sheet


class IdNormalisationTests(SimpleTestCase):
    def test_an_id_that_lost_its_leading_zero_gets_it_back(self):
        # The export stores the ת"ז as a number: 012345678 arrives as 12345678.0.
        self.assertEqual(normalise_id(12345678.0), '012345678')
        self.assertEqual(normalise_id('12345678'), '012345678')

    def test_five_to_nine_digits_are_padded_to_nine(self):
        self.assertEqual(normalise_id(12345.0), '000012345')
        self.assertEqual(normalise_id(123456789.0), '123456789')

    def test_placeholders_are_not_ids(self):
        self.assertEqual(normalise_id(0.0), '')
        self.assertEqual(normalise_id('1234'), '')
        self.assertEqual(normalise_id('000000000'), '')
        self.assertEqual(normalise_id(None), '')

    def test_dashes_and_spaces_are_dropped_and_a_passport_kept(self):
        self.assertEqual(normalise_id('51-234567-8'), '512345678')
        self.assertEqual(normalise_id('ab123456'), 'AB123456')


class CellTests(SimpleTestCase):
    def test_a_mobile_is_digits_with_its_leading_zero(self):
        self.assertEqual(normalise_phone(521111111.0), '0521111111')
        self.assertEqual(normalise_phone('+972 52-111-1111'), '0521111111')
        self.assertEqual(normalise_phone(972521111111.0), '0521111111')
        self.assertEqual(normalise_phone('050-0000001'), '0500000001')
        self.assertEqual(normalise_phone('12'), '')

    def test_card_digits_and_dashes(self):
        self.assertEqual(parse_card(1234.0), '1234')
        self.assertEqual(parse_card(123.0), '0123')
        self.assertEqual(parse_card('-'), '')

    def test_dates_from_a_date_cell_a_serial_or_text(self):
        self.assertEqual(parse_date(datetime(2025, 3, 1, 10, 30)).isoformat(), '2025-03-01')
        self.assertEqual(parse_date(45717.4).isoformat(), '2025-03-01')
        self.assertEqual(parse_date('01/03/2025').isoformat(), '2025-03-01')
        self.assertIsNone(parse_date('not a date'))

    def test_document_type_names(self):
        self.assertEqual(type_key('חשבונית מס קבלה'), 'combined')
        self.assertEqual(type_key('חשבונית מס / קבלה'), 'combined')
        self.assertEqual(type_key('חשבון עיסקה'), 'transaction_invoice')
        self.assertEqual(type_key('חשבונית מס זיכוי'), 'credit_invoice')
        self.assertEqual(type_key('הצעת מחיר'), '')


class CustomerKeyTests(SimpleTestCase):
    def test_id_then_external_number_then_email_then_phone(self):
        self.assertEqual(customer_key('012345678', '7', 'a@b.co', '0500000000'), '012345678')
        self.assertEqual(customer_key('', '7', 'a@b.co', '0500000000'), 'ext:7')
        self.assertEqual(customer_key('', '', 'a@b.co', '0500000000'), 'a@b.co')
        self.assertEqual(customer_key('', '', '', '0500000000'), '0500000000')
        self.assertEqual(customer_key('', '', '', ''), '')

    def test_one_id_under_two_external_numbers_is_one_customer(self):
        customers = customers_from_rows([
            row(id_number='012345678', ext_number='1'),
            row(id_number='012345678', ext_number='2'),
        ])
        self.assertEqual(list(customers), ['012345678'])
        self.assertEqual(customers['012345678'].ext_numbers, ['1', '2'])


class LatestWinsTests(SimpleTestCase):
    def test_the_newest_document_gives_the_details(self):
        customers = customers_from_rows([
            row(ext_number='5', date='2025-05-01', number=20, first_name='רוני', last_name='חדש',
                email='new@example.test', phone='0500000002', address='רחוב חדש 2', city='עיר חדשה'),
            row(ext_number='5', date='2024-01-01', number=10, first_name='רוני', last_name='ישן',
                email='old@example.test', phone='0500000001', address='רחוב ישן 1'),
        ])
        customer = customers['ext:5']
        self.assertEqual((customer.first_name, customer.last_name), ('רוני', 'חדש'))
        self.assertEqual(customer.email, 'new@example.test')
        self.assertEqual(customer.phone, '0500000002')
        self.assertEqual(customer.full_address, 'רחוב חדש 2, עיר חדשה')
        self.assertEqual(customer.latest['number'], 20)

    def test_a_blank_on_the_newest_document_does_not_erase_what_was_known(self):
        customers = customers_from_rows([
            row(ext_number='5', date='2024-01-01', email='kept@example.test'),
            row(ext_number='5', date='2025-01-01', email=''),
        ])
        self.assertEqual(customers['ext:5'].email, 'kept@example.test')

    def test_same_day_the_higher_number_wins(self):
        customers = customers_from_rows([
            row(ext_number='5', date='2025-01-01', number=11, last_name='אחרון'),
            row(ext_number='5', date='2025-01-01', number=10, last_name='ראשון'),
        ])
        self.assertEqual(customers['ext:5'].last_name, 'אחרון')


class NameChangeTests(SimpleTestCase):
    def test_a_customer_under_two_names_is_reported_old_to_new(self):
        customers = customers_from_rows([
            row(id_number='012345678', date='2023-01-01', first_name='סטודיו', last_name='ישן'),
            row(id_number='012345678', date='2024-01-01', first_name='סטודיו', last_name='ישן'),
            row(id_number='012345678', date='2025-01-01', first_name='סטודיו', last_name='חדש'),
            row(id_number='087654321', first_name='אחר', last_name='בדיקה'),
        ])
        changes = name_changes(customers)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['old_names'], ['סטודיו ישן'])
        self.assertEqual(changes[0]['new_name'], 'סטודיו חדש')
        self.assertEqual(changes[0]['documents'], 3)

    def test_spacing_and_quotes_are_not_a_new_name(self):
        customers = customers_from_rows([
            row(ext_number='9', date='2024-01-01', first_name='מתנ״ס', last_name='הדגמה'),
            row(ext_number='9', date='2025-01-01', first_name='מתנ"ס ', last_name=' הדגמה'),
        ])
        self.assertEqual(name_changes(customers), [])


class ClassificationTests(SimpleTestCase):
    def kind(self, *rows):
        customer = customers_from_rows(list(rows))
        return next(iter(customer.values())).kind

    def test_a_parent_paying_lessons_by_card(self):
        self.assertEqual(self.kind(row(), row()), 'parent')

    def test_any_document_issued_by_hand_is_business(self):
        for doc_type in ('tax_invoice', 'transaction_invoice', 'receipt', 'credit_invoice'):
            with self.subTest(doc_type):
                self.assertEqual(self.kind(row(), row(doc_type=doc_type)), 'business')

    def test_an_invoice_receipt_paid_otherwise_than_by_card(self):
        self.assertEqual(self.kind(row(payment_type='העברה בנקאית')), 'business')
        self.assertEqual(self.kind(row(payment_type='')), 'business')

    def test_a_company_number(self):
        self.assertEqual(self.kind(row(id_number='512345678')), 'business')
        self.assertEqual(self.kind(row(id_number='312345678')), 'parent')

    def test_an_organisation_name(self):
        for name in ('אולפן בע"מ', 'עמותת הדגמה', 'מתנ״ס הדגמה', 'עיריית הדגמה', 'מועצה אזורית',
                     'בית ספר הדגמה', 'ביה"ס הדגמה', 'קאנטרי הדגמה', 'מרכז הדגמה', 'אגודת הדגמה'):
            with self.subTest(name):
                self.assertEqual(self.kind(row(first_name=name, last_name='')), 'business')

    def test_the_reasons_are_given(self):
        customers = customers_from_rows([row(id_number='512345678', doc_type='tax_invoice')])
        customer = customers['512345678']
        self.assertEqual(classify(customer, [row(doc_type='tax_invoice')])[1][:1], ['document_type'])
        self.assertIn('company_number', customer.reasons)


class TypeTableTests(SimpleTestCase):
    def test_first_last_and_what_the_span_is_missing(self):
        rows = [
            row(doc_type='tax_invoice', number=100, date='2024-01-01'),
            row(doc_type='tax_invoice', number=104, date='2024-06-01'),
            row(doc_type='tax_invoice', number=101, date='2024-02-01'),
            row(doc_type='combined', number=5000, date='2025-01-01'),
        ]
        table = {entry['doc_type']: entry for entry in type_table(rows)}
        self.assertEqual(
            {k: table['tax_invoice'][k] for k in ('count', 'first_number', 'last_number', 'last_date', 'missing_in_span')},
            {'count': 3, 'first_number': 100, 'last_number': 104, 'last_date': '2024-06-01', 'missing_in_span': 2},
        )
        self.assertEqual([entry['doc_type'] for entry in type_table(rows)], ['combined', 'tax_invoice'])


class SheetTests(SimpleTestCase):
    def record(self, **overrides):
        base = {
            HEADER['first_name']: 'דנה', HEADER['last_name']: 'בדיקה', HEADER['type_label']: 'חשבונית מס קבלה',
            HEADER['number']: 70001.0, HEADER['date']: datetime(2025, 1, 5, 10, 30), HEADER['id_number']: 12345678.0,
            HEADER['ext_number']: 101.0, HEADER['location']: 'כפר סבא', HEADER['invoice_total']: 236.0,
            HEADER['receipt_total']: 236.0, HEADER['credit_total']: 0.0, HEADER['details']: 'הדרכה במתנ&#34;ס',
            'סיסמת כניסה לאפליקציה': 'SECRET-PW-123', 'תאריך לידה': '01/01/1980', 'פקס': '039999998',
            'טלפון בבית': '039999999',
        }
        base.update(overrides)
        return base

    def test_columns_are_found_by_name_in_any_order(self):
        order = list(reversed(list(HEADER.values()))) + list(NEVER_READ)
        rows, skipped = parse_sheet(sheet([self.record()], order=order))
        self.assertEqual(skipped, [])
        self.assertEqual(rows[0]['number'], 70001)
        self.assertEqual(rows[0]['id_number'], '012345678')
        self.assertEqual(rows[0]['customer_key'], '012345678')

    def test_html_entities_are_decoded(self):
        rows, _ = parse_sheet(sheet([self.record()], extra_headers=NEVER_READ))
        self.assertEqual(rows[0]['details'], 'הדרכה במתנ"ס')

    def test_the_password_birth_date_fax_and_home_phone_are_never_in_a_row(self):
        rows, _ = parse_sheet(sheet([self.record()], extra_headers=NEVER_READ))
        stored = json.dumps(rows, ensure_ascii=False)
        for secret in ('SECRET-PW-123', '1980', '039999998', '039999999'):
            self.assertNotIn(secret, stored)
        self.assertFalse({'password', 'birth_date', 'fax', 'home_phone'} & set(rows[0]))

    def test_a_file_missing_essential_columns_is_refused_naming_them(self):
        headers = [h for h in HEADER.values() if h not in ('מספר המסמך', 'מיקום')]
        with self.assertRaises(ImportFileError) as caught:
            column_indexes(headers)
        self.assertIn('מספר המסמך', str(caught.exception))
        self.assertIn('מיקום', str(caught.exception))

    def test_unreadable_rows_are_skipped_by_row_number_only(self):
        rows, skipped = parse_sheet(sheet([
            self.record(),
            self.record(**{HEADER['type_label']: 'הצעת מחיר'}),
            self.record(**{HEADER['number']: None, HEADER['type_label']: 'קבלה'}),
            self.record(),  # the same (type, number) again
        ], extra_headers=NEVER_READ))
        self.assertEqual(len(rows), 1)
        self.assertEqual([s['row'] for s in skipped], [3, 4, 5])
        self.assertEqual(set(skipped[0]), {'row', 'reason'})
