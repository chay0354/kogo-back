"""
The uniform-structure export is read by the Tax Authority's software column by
column, so these tests check columns: every record's byte length, the fields at
the positions the spec's tables give, and the figures that must agree across
records and across the two files.

Positions are 1-based and inclusive, as the spec prints them, and are copied
from the spec independently of the module's own layout table, so a slip in
either one shows up here. SimpleTestCase only: the formatter touches no database.
"""
from __future__ import annotations

import io
import zipfile
from collections import Counter
from datetime import date, datetime, time, timezone as dt_timezone
from decimal import Decimal

from django.test import SimpleTestCase

from apps.documents.uniform_format import (
    CARD_REGULAR,
    CREDIT_INVOICE,
    LAYOUTS,
    RECEIPT,
    RECORD_LENGTHS,
    TAX_INVOICE,
    TAX_INVOICE_RECEIPT,
    TRANSACTION_SALE,
    TRANSACTION_SERVICE,
    UniformAddress,
    UniformBusiness,
    UniformDocument,
    UniformFormatError,
    UniformLine,
    UniformPayment,
    amount_field,
    build_uniform_files,
    export_directory,
    identifier_field,
    numeric_field,
    rate_field,
    text_field,
    to_charset,
    uniform_zip,
)

PRIMARY_ID = 123456789012345
GENERATED = datetime(2026, 9, 11, 12, 30)
AUGUST_START, AUGUST_END = date(2026, 8, 1), date(2026, 8, 31)
DIRECTORY = 'OPENFRMT/51650441.26/09111230'
VAT = '516504412'


def col(record: str, first: int, last: int) -> str:
    """Columns first..last of a record, numbered the way the spec numbers them."""
    return record[first - 1:last]


def records(data: bytes) -> list[str]:
    """A file's records, decoded, each without its CR LF."""
    assert data.endswith(b'\r\n'), 'the last record must end in CR LF too'
    return [raw.decode('iso-8859-8') for raw in data[:-2].split(b'\r\n')]


def make_business(**overrides) -> UniformBusiness:
    # The issuer in apps/documents/issuer.py.
    values = dict(
        vat_number=VAT,
        name='קוגומלו גרופ בע"מ',
        software_version='2026.09',
        address=UniformAddress(street='רפאל איתן', house_number='5', city='פתח תקווה'),
        company_number=VAT,
    )
    values.update(overrides)
    return UniformBusiness(**values)


def invoice_receipt(**overrides) -> UniformDocument:
    """320: one lesson line, paid in full by credit card."""
    values = dict(
        type_code=TAX_INVOICE_RECEIPT,
        number='IR-2026-000123',
        issue_date=date(2026, 8, 5),
        issue_time=time(10, 30),
        customer_name='דנה כהן',
        customer_vat_number='039337423',
        customer_phone='050-1234567',
        customer_key='FAM-0042',
        amount_before_discount=Decimal('250.00'),
        amount_after_discount=Decimal('250.00'),
        vat_amount=Decimal('45.00'),
        total_amount=Decimal('295.00'),
        lines=(UniformLine(
            description='שיעור אמנות – אוגוסט', quantity=Decimal('1'), unit_price=Decimal('250.00'),
            line_total=Decimal('250.00'), vat_rate=Decimal('18'), transaction_type=TRANSACTION_SERVICE,
        ),),
        payments=(UniformPayment(
            method='credit_card', amount=Decimal('295.00'), due_date=date(2026, 8, 5),
            card_acquirer=1, card_name='ויזה', card_transaction_type=CARD_REGULAR,
        ),),
    )
    values.update(overrides)
    return UniformDocument(**values)


def tax_invoice() -> UniformDocument:
    """305 to a business customer: two lines and a document discount."""
    return UniformDocument(
        type_code=TAX_INVOICE,
        number='2026-0042',
        issue_date=date(2026, 8, 10),
        customer_name='סטודיו אור בע"מ',
        customer_vat_number='514000002',
        customer_address=UniformAddress(street='הרצל', house_number='10', city='תל אביב - יפו', zip_code='6100000'),
        amount_before_discount=Decimal('500.00'),
        discount=Decimal('50.00'),
        amount_after_discount=Decimal('450.00'),
        vat_amount=Decimal('81.00'),
        total_amount=Decimal('531.00'),
        lines=(
            UniformLine(description='ערכת צבעים', catalog_number='PAINT-01', quantity=Decimal('3'),
                        unit_price=Decimal('40.00'), line_total=Decimal('120.00'), vat_rate=Decimal('18'),
                        transaction_type=TRANSACTION_SALE),
            UniformLine(description='סדנת קרמיקה', quantity=Decimal('1'), unit_price=Decimal('380.00'),
                        line_total=Decimal('380.00'), vat_rate=Decimal('18'), transaction_type=TRANSACTION_SERVICE),
        ),
    )


def credit_note() -> UniformDocument:
    """330 crediting one item of the 305. Positive amounts: the type carries the meaning."""
    return UniformDocument(
        type_code=CREDIT_INVOICE,
        number='CR-2026-000007',
        issue_date=date(2026, 8, 20),
        customer_name='סטודיו אור בע"מ',
        amount_before_discount=Decimal('40.00'),
        amount_after_discount=Decimal('40.00'),
        vat_amount=Decimal('7.20'),
        total_amount=Decimal('47.20'),
        linked_document_type=TAX_INVOICE,
        linked_document_number='2026-0042',
        lines=(UniformLine(description='זיכוי ערכת צבעים', quantity=Decimal('1'), unit_price=Decimal('40.00'),
                           line_total=Decimal('40.00'), vat_rate=Decimal('18')),),
    )


def receipt() -> UniformDocument:
    """400 settling the 305 by check — amounts as הבהרה 4 lays out a receipt."""
    return UniformDocument(
        type_code=RECEIPT,
        number='2026-0043',
        issue_date=date(2026, 8, 25),
        customer_name='סטודיו אור בע"מ',
        amount_before_discount=Decimal('531.00'),
        amount_after_discount=Decimal('531.00'),
        vat_amount=Decimal('0.00'),
        total_amount=Decimal('531.00'),
        payments=(UniformPayment(
            method='check', amount=Decimal('531.00'), due_date=date(2026, 9, 1),
            bank_number='12', branch_number='600', account_number='123-456', check_number='5001',
        ),),
    )


def build(documents=None, *, business=None, generated_at=GENERATED, primary_id=PRIMARY_ID):
    if documents is None:
        documents = [invoice_receipt(), tax_invoice(), credit_note(), receipt()]
    return build_uniform_files(
        business or make_business(), documents,
        period_start=AUGUST_START, period_end=AUGUST_END,
        generated_at=generated_at, primary_id=primary_id,
    )


class FieldHelperTests(SimpleTestCase):
    def test_signed_amounts_match_the_spec_examples(self):
        # 2.3(ו), in X9(5)v99: eight characters including the sign.
        self.assertEqual(amount_field(Decimal('-12345.65'), 8), '-1234565')
        self.assertEqual(amount_field(Decimal('1245.65'), 8), '+0124565')
        self.assertEqual(amount_field(1245, 8), '+0124500')

    def test_zero_is_positive_whatever_its_sign(self):
        self.assertEqual(amount_field(Decimal('0'), 15), '+00000000000000')
        self.assertEqual(amount_field(Decimal('-0.00'), 15), '+00000000000000')
        self.assertEqual(amount_field(Decimal('-0.004'), 15), '+00000000000000')

    def test_amounts_round_half_up_to_the_field(self):
        self.assertEqual(amount_field(Decimal('0.005'), 15), '+00000000000001')
        self.assertEqual(amount_field(Decimal('-2.345'), 15), '-00000000000235')
        self.assertEqual(amount_field(Decimal('1.23456'), 17, 4), '+0000000000012346')

    def test_amount_overflow_raises_instead_of_cutting(self):
        self.assertEqual(amount_field(Decimal('999999999999.99'), 15), '+99999999999999')
        with self.assertRaises(UniformFormatError):
            amount_field(Decimal('1000000000000.00'), 15)
        with self.assertRaises(UniformFormatError):
            amount_field(Decimal('999999999999.995'), 15)  # rounding carries it over
        with self.assertRaises(UniformFormatError):
            amount_field(Decimal('1E+40'), 15)

    def test_amounts_must_be_decimal(self):
        with self.assertRaises(TypeError):
            amount_field(12.5, 15)
        with self.assertRaises(UniformFormatError):
            amount_field(Decimal('NaN'), 15)

    def test_unfilled_amount_is_blank(self):
        self.assertEqual(amount_field(None, 15), ' ' * 15)

    def test_numeric_fields_pad_and_never_cut(self):
        self.assertEqual(numeric_field(42, 9), '000000042')
        self.assertEqual(numeric_field('51-650441-2', 9), '516504412')
        self.assertEqual(numeric_field('0516504412', 9), '516504412')  # a leading zero is padding
        self.assertEqual(numeric_field(None, 4), '0000')
        with self.assertRaises(UniformFormatError):
            numeric_field(1234567890, 9)
        with self.assertRaises(UniformFormatError):
            numeric_field('5165044120', 9)
        with self.assertRaises(UniformFormatError):
            numeric_field(-1, 9)
        with self.assertRaises(UniformFormatError):
            numeric_field('לאומי', 10)

    def test_vat_rate(self):
        self.assertEqual(rate_field(Decimal('18'), 4), '1800')
        self.assertEqual(rate_field(Decimal('15.5'), 4), '1550')  # the spec's own example
        self.assertEqual(rate_field(Decimal('0'), 4), '0000')
        with self.assertRaises(UniformFormatError):
            rate_field(Decimal('100'), 4)
        with self.assertRaises(UniformFormatError):
            rate_field(Decimal('-1'), 4)

    def test_text_is_cut_to_its_column(self):
        self.assertEqual(text_field('קוגומלו גרופ בע"מ', 5), 'קוגומ')
        self.assertEqual(text_field('  פתח תקווה ', 12), 'פתח תקווה   ')
        self.assertEqual(text_field(None, 3), '   ')

    def test_unrepresentable_characters_are_replaced_not_fatal(self):
        self.assertEqual(to_charset('דנה 😀 כהן'), 'דנה ? כהן')
        self.assertEqual(to_charset('בע״מ – ₪100'), 'בע"מ - ש"ח100')
        self.assertEqual(to_charset('שָׁלוֹם'), 'שלום')      # niqqud dropped
        self.assertEqual(to_charset('‏שלום‎'), 'שלום')  # bidi marks dropped
        self.assertEqual(to_charset('Café'), 'Cafe')
        # A line break inside a field would end the record early.
        self.assertEqual(text_field('שורה\r\nשנייה', 12), 'שורה  שנייה ')
        text_field('👍🏽' * 40, 30).encode('iso-8859-8')

    def test_document_numbers_are_never_cut_or_altered(self):
        self.assertEqual(identifier_field('IR-2026-000123', 20), 'IR-2026-000123      ')
        with self.assertRaises(UniformFormatError):
            identifier_field('X' * 21, 20)
        with self.assertRaises(UniformFormatError):
            identifier_field('IR–2026', 20)  # an en dash would become a different number

    def test_export_directory_matches_the_spec_example(self):
        # 2.2: VAT 002233445, 11 September 2008 at 10:25.
        self.assertEqual(export_directory('002233445', datetime(2008, 9, 11, 10, 25)),
                         'OPENFRMT/00223344.08/09111025')


class LayoutTests(SimpleTestCase):
    SPEC_LENGTHS = {'A000': 466, 'SUMMARY': 19, 'A100': 95, 'C100': 444, 'D110': 339, 'D120': 222, 'Z900': 110}

    def test_record_lengths_are_the_spec_table(self):
        self.assertEqual(dict(RECORD_LENGTHS), self.SPEC_LENGTHS)

    def test_every_layout_is_contiguous_and_fills_its_record(self):
        for code, layout in LAYOUTS.items():
            with self.subTest(code=code):
                column = 1
                for number, start, width, _kind in layout:
                    self.assertEqual(start, column, f'{code} field {number}')
                    column += width
                self.assertEqual(column - 1, self.SPEC_LENGTHS[code])


class ExportTests(SimpleTestCase):
    def setUp(self):
        self.files = build()
        self.data = records(self.files.bkmvdata)
        self.ini = records(self.files.ini)

    def record(self, number: int) -> str:
        return self.data[number - 1]

    def test_record_order(self):
        self.assertEqual(
            [line[:4] for line in self.data],
            ['A100', 'C100', 'D110', 'D120', 'C100', 'D110', 'D110', 'C100', 'D110', 'C100', 'D120', 'Z900'],
        )

    def test_every_record_has_its_exact_byte_length(self):
        for raw in self.files.bkmvdata[:-2].split(b'\r\n'):
            with self.subTest(code=raw[:4]):
                self.assertEqual(len(raw), RECORD_LENGTHS[raw[:4].decode()])
        ini_raw = self.files.ini[:-2].split(b'\r\n')
        self.assertEqual(len(ini_raw[0]), 466)
        for raw in ini_raw[1:]:
            self.assertEqual(len(raw), 19)

    def test_records_end_in_cr_lf_and_nothing_else(self):
        for data in (self.files.bkmvdata, self.files.ini):
            self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
            self.assertEqual(data.count(b'\r'), data.count(b'\r\n'))

    def test_running_record_numbers(self):
        for number, line in enumerate(self.data, start=1):
            self.assertEqual(col(line, 5, 13), f'{number:09d}')

    def test_primary_id_is_the_same_in_ini_opening_and_closing(self):
        self.assertEqual(col(self.ini[0], 34, 48), '123456789012345')
        self.assertEqual(col(self.data[0], 23, 37), '123456789012345')
        self.assertEqual(col(self.data[-1], 23, 37), '123456789012345')
        self.assertEqual(self.files.primary_id, PRIMARY_ID)

    def test_opening_record(self):
        a100 = self.data[0]
        self.assertEqual(col(a100, 1, 4), 'A100')
        self.assertEqual(col(a100, 14, 22), VAT)
        self.assertEqual(col(a100, 38, 45), '&OF1.31&')
        self.assertEqual(col(a100, 46, 95), ' ' * 50)

    def test_closing_record_counts_every_record_including_itself(self):
        z900 = self.data[-1]
        self.assertEqual(col(z900, 1, 4), 'Z900')
        self.assertEqual(col(z900, 14, 22), VAT)
        self.assertEqual(col(z900, 38, 45), '&OF1.31&')
        self.assertEqual(col(z900, 46, 60), f'{len(self.data):015d}')
        self.assertEqual(len(self.data), 12)
        self.assertEqual(col(self.ini[0], 10, 24), col(z900, 46, 60))
        self.assertEqual(self.files.total_records, 12)

    def test_ini_summaries_equal_the_actual_record_counts(self):
        actual = Counter(line[:4] for line in self.data)
        summaries = {col(line, 1, 4): int(col(line, 5, 19)) for line in self.ini[1:]}
        self.assertEqual(summaries, dict(actual))
        self.assertEqual(dict(self.files.counts), dict(actual))
        self.assertEqual(summaries, {'A100': 1, 'C100': 4, 'D110': 4, 'D120': 2, 'Z900': 1})
        self.assertEqual(sum(summaries.values()), int(col(self.ini[0], 10, 24)))
        self.assertEqual([line[:4] for line in self.ini[1:]], ['A100', 'C100', 'D110', 'D120', 'Z900'])

    def test_ini_header_fields(self):
        a000 = self.ini[0]
        self.assertEqual(col(a000, 1, 4), 'A000')
        self.assertEqual(col(a000, 5, 9), ' ' * 5)
        self.assertEqual(col(a000, 25, 33), VAT)
        self.assertEqual(col(a000, 49, 56), '&OF1.31&')
        self.assertEqual(col(a000, 57, 64), '00000000')  # in-house software: no registration number
        self.assertEqual(col(a000, 65, 84), 'Kogo CRM'.ljust(20))
        self.assertEqual(col(a000, 85, 104), '2026.09'.ljust(20))
        self.assertEqual(col(a000, 105, 113), VAT)  # the vendor is the business itself
        self.assertEqual(col(a000, 114, 133), 'קוגומלו גרופ בע"מ'.ljust(20))
        self.assertEqual(col(a000, 134, 134), '2')  # multi-year
        self.assertEqual(col(a000, 135, 184), 'OPENFRMT\\51650441.26\\09111230'.ljust(50))
        self.assertEqual(col(a000, 185, 185), '0')  # no books: not applicable
        self.assertEqual(col(a000, 186, 186), '0')
        self.assertEqual(col(a000, 187, 195), VAT)
        self.assertEqual(col(a000, 196, 204), '000000000')
        self.assertEqual(col(a000, 215, 264), 'קוגומלו גרופ בע"מ'.ljust(50))
        self.assertEqual(col(a000, 265, 314), 'רפאל איתן'.ljust(50))
        self.assertEqual(col(a000, 315, 324), '5'.ljust(10))
        self.assertEqual(col(a000, 325, 354), 'פתח תקווה'.ljust(30))
        self.assertEqual(col(a000, 355, 362), ' ' * 8)
        self.assertEqual(col(a000, 363, 366), '0000')
        self.assertEqual(col(a000, 367, 374), '20260801')
        self.assertEqual(col(a000, 375, 382), '20260831')
        self.assertEqual(col(a000, 383, 390), '20260911')
        self.assertEqual(col(a000, 391, 394), '1230')
        self.assertEqual(col(a000, 395, 395), '0')  # Hebrew
        self.assertEqual(col(a000, 396, 396), '1')  # ISO-8859-8-i
        self.assertEqual(col(a000, 397, 416), 'Python zipfile'.ljust(20))
        self.assertEqual(col(a000, 417, 419), 'ILS')
        self.assertEqual(col(a000, 420, 420), '0')
        self.assertEqual(col(a000, 421, 466), ' ' * 46)

    def test_document_header_fields_1200_to_1207(self):
        c100 = self.record(2)
        self.assertEqual(col(c100, 1, 4), 'C100')
        self.assertEqual(col(c100, 5, 13), '000000002')
        self.assertEqual(col(c100, 14, 22), VAT)
        self.assertEqual(col(c100, 23, 25), '320')
        self.assertEqual(col(c100, 26, 45), 'IR-2026-000123'.ljust(20))
        self.assertEqual(col(c100, 46, 53), '20260805')
        self.assertEqual(col(c100, 54, 57), '1030')
        self.assertEqual(col(c100, 58, 107), 'דנה כהן'.ljust(50))

    def test_document_header_customer_amounts_and_link(self):
        c100 = self.record(2)
        self.assertEqual(col(c100, 238, 252), '050-1234567'.ljust(15))
        self.assertEqual(col(c100, 253, 261), '039337423')
        self.assertEqual(col(c100, 262, 269), '00000000')
        self.assertEqual(col(c100, 270, 287), ' ' * 18)  # not an export invoice
        self.assertEqual(col(c100, 288, 302), '+00000000025000')
        self.assertEqual(col(c100, 303, 317), '+00000000000000')
        self.assertEqual(col(c100, 318, 332), '+00000000025000')
        self.assertEqual(col(c100, 333, 347), '+00000000004500')
        self.assertEqual(col(c100, 348, 362), '+00000000029500')
        self.assertEqual(col(c100, 363, 374), '+00000000000')
        self.assertEqual(col(c100, 375, 389), 'FAM-0042'.ljust(15))
        self.assertEqual(col(c100, 400, 400), ' ')
        self.assertEqual(col(c100, 401, 408), '20260805')
        self.assertEqual(col(c100, 425, 431), '0000001')

    def test_document_discount_is_negative(self):
        c100 = self.record(5)
        self.assertEqual(col(c100, 23, 25), '305')
        self.assertEqual(col(c100, 108, 157), 'הרצל'.ljust(50))
        self.assertEqual(col(c100, 158, 167), '10'.ljust(10))
        self.assertEqual(col(c100, 168, 197), 'תל אביב - יפו'.ljust(30))
        self.assertEqual(col(c100, 198, 205), '6100000 ')
        self.assertEqual(col(c100, 288, 302), '+00000000050000')
        self.assertEqual(col(c100, 303, 317), '-00000000005000')
        self.assertEqual(col(c100, 318, 332), '+00000000045000')
        self.assertEqual(col(c100, 333, 347), '+00000000008100')
        self.assertEqual(col(c100, 348, 362), '+00000000053100')
        self.assertEqual(col(c100, 425, 431), '0000002')

    def test_credit_note_amounts_are_positive(self):
        c100 = self.record(8)
        self.assertEqual(col(c100, 23, 25), '330')
        self.assertEqual(col(c100, 288, 302), '+00000000004000')
        self.assertEqual(col(c100, 318, 332), '+00000000004000')
        self.assertEqual(col(c100, 333, 347), '+00000000000720')
        self.assertEqual(col(c100, 348, 362), '+00000000004720')

    def test_lines_tie_to_their_document(self):
        first, second = self.record(6), self.record(7)
        self.assertEqual(col(first, 1, 4), 'D110')
        self.assertEqual(col(first, 23, 25), '305')
        self.assertEqual(col(first, 26, 45), '2026-0042'.ljust(20))
        self.assertEqual(col(first, 46, 49), '0001')
        self.assertEqual(col(second, 46, 49), '0002')
        self.assertEqual(col(first, 50, 72), '000' + ' ' * 20)  # based on no other document
        self.assertEqual(col(first, 73, 73), '2')
        self.assertEqual(col(first, 74, 93), 'PAINT-01'.ljust(20))
        self.assertEqual(col(first, 94, 123), 'ערכת צבעים'.ljust(30))
        self.assertEqual(col(first, 204, 223), 'יחידה'.ljust(20))
        self.assertEqual(col(first, 224, 240), '+0000000000030000')
        self.assertEqual(col(first, 241, 255), '+00000000004000')
        self.assertEqual(col(first, 256, 270), '+00000000000000')
        self.assertEqual(col(first, 271, 285), '+00000000012000')
        self.assertEqual(col(first, 286, 289), '1800')
        self.assertEqual(col(first, 297, 304), '20260810')
        # 1273 on each line equals 1234 on the header.
        self.assertEqual(col(first, 305, 311), col(self.record(5), 425, 431))
        self.assertEqual(col(second, 305, 311), '0000002')

    def test_credit_note_line_names_the_invoice_it_credits(self):
        line = self.record(9)
        self.assertEqual(col(line, 23, 25), '330')
        self.assertEqual(col(line, 26, 45), 'CR-2026-000007'.ljust(20))
        self.assertEqual(col(line, 50, 52), '305')
        self.assertEqual(col(line, 53, 72), '2026-0042'.ljust(20))
        # And that number is the one the invoice's own header carries.
        self.assertEqual(col(line, 53, 72), col(self.record(5), 26, 45))
        self.assertEqual(col(line, 305, 311), col(self.record(8), 425, 431))

    def test_card_payment(self):
        d120 = self.record(4)
        self.assertEqual(col(d120, 1, 4), 'D120')
        self.assertEqual(col(d120, 23, 25), '320')
        self.assertEqual(col(d120, 26, 45), 'IR-2026-000123'.ljust(20))
        self.assertEqual(col(d120, 46, 49), '0001')
        self.assertEqual(col(d120, 50, 50), '3')
        self.assertEqual(col(d120, 51, 95), '0' * 45)  # bank columns are for checks
        self.assertEqual(col(d120, 96, 103), '20260805')
        self.assertEqual(col(d120, 104, 118), '+00000000029500')
        self.assertEqual(col(d120, 119, 119), '1')
        self.assertEqual(col(d120, 120, 139), 'ויזה'.ljust(20))
        self.assertEqual(col(d120, 140, 140), '1')
        self.assertEqual(col(d120, 148, 155), '20260805')
        self.assertEqual(col(d120, 156, 162), col(self.record(2), 425, 431))

    def test_check_payment(self):
        d120 = self.record(11)
        self.assertEqual(col(d120, 23, 25), '400')
        self.assertEqual(col(d120, 26, 45), '2026-0043'.ljust(20))
        self.assertEqual(col(d120, 50, 50), '2')
        self.assertEqual(col(d120, 51, 60), '0000000012')
        self.assertEqual(col(d120, 61, 70), '0000000600')
        self.assertEqual(col(d120, 71, 85), '000000000123456')
        self.assertEqual(col(d120, 86, 95), '0000005001')
        self.assertEqual(col(d120, 96, 103), '20260901')
        self.assertEqual(col(d120, 104, 118), '+00000000053100')
        self.assertEqual(col(d120, 119, 140), '0' + ' ' * 20 + '0')  # card columns are for cards
        self.assertEqual(col(d120, 148, 155), '20260825')
        self.assertEqual(col(d120, 156, 162), '0000004')
        self.assertEqual(col(self.record(10), 425, 431), '0000004')

    def test_hebrew_is_iso_8859_8_in_logical_order(self):
        raw_c100 = self.files.bkmvdata.split(b'\r\n')[1]
        # 'דנה כהן' typed order: dalet 0xE3 first, final nun 0xEF last.
        self.assertEqual(raw_c100[57:64], b'\xe3\xf0\xe4 \xeb\xe4\xef')
        self.assertEqual(col(self.record(3), 94, 123), 'שיעור אמנות - אוגוסט'.ljust(30))
        self.assertIn('קוגומלו גרופ בע"מ'.encode('iso-8859-8'), self.files.ini)

    def test_primary_id_is_drawn_when_not_given(self):
        first = build(primary_id=None)
        second = build(primary_id=None)
        self.assertEqual(len(str(first.primary_id)), 15)
        self.assertNotEqual(first.primary_id, second.primary_id)
        self.assertEqual(col(records(first.ini)[0], 34, 48), str(first.primary_id))
        self.assertEqual(col(records(first.bkmvdata)[0], 23, 37), str(first.primary_id))
        self.assertEqual(col(records(first.bkmvdata)[-1], 23, 37), str(first.primary_id))

    def test_aware_moment_is_read_on_the_israeli_clock(self):
        files = build(generated_at=datetime(2026, 9, 11, 9, 30, tzinfo=dt_timezone.utc))
        self.assertEqual(files.directory, DIRECTORY)  # 09:30 UTC is 12:30 in Israel (IDT)
        self.assertEqual(col(records(files.ini)[0], 391, 394), '1230')

    def test_an_empty_period_still_exports(self):
        files = build([])
        data = records(files.bkmvdata)
        self.assertEqual([line[:4] for line in data], ['A100', 'Z900'])
        self.assertEqual(col(data[-1], 46, 60), '000000000000002')
        self.assertEqual([line[:4] for line in records(files.ini)[1:]], ['A100', 'Z900'])

    def test_cancelled_document_is_flagged(self):
        files = build([invoice_receipt(cancelled=True)])
        self.assertEqual(col(records(files.bkmvdata)[1], 400, 400), '1')

    def test_same_input_same_bytes(self):
        self.assertEqual(build().bkmvdata, self.files.bkmvdata)
        self.assertEqual(build().ini, self.files.ini)


class ValidationTests(SimpleTestCase):
    def test_a_document_outside_the_period_is_refused(self):
        with self.assertRaises(UniformFormatError):
            build([invoice_receipt(issue_date=date(2026, 9, 1))])

    def test_the_period_is_cut_by_the_date_on_the_document(self):
        # Produced on 1 September, dated 31 August (הבהרה 12's example): it belongs to August.
        files = build([invoice_receipt(issue_date=date(2026, 9, 1), document_date=date(2026, 8, 31))])
        c100 = records(files.bkmvdata)[1]
        self.assertEqual(col(c100, 46, 53), '20260901')
        self.assertEqual(col(c100, 401, 408), '20260831')

    def test_unknown_document_type_is_refused(self):
        with self.assertRaises(UniformFormatError):
            invoice_receipt(type_code=999, payments=())

    def test_payments_belong_to_receipts_only(self):
        with self.assertRaises(UniformFormatError):
            invoice_receipt(type_code=TAX_INVOICE)

    def test_unknown_payment_method_is_refused(self):
        with self.assertRaises(UniformFormatError):
            UniformPayment(method='bitcoin', amount=Decimal('1'))

    def test_a_document_number_appears_once(self):
        with self.assertRaises(UniformFormatError):
            build([invoice_receipt(), invoice_receipt()])

    def test_a_base_document_needs_type_and_number(self):
        with self.assertRaises(UniformFormatError):
            invoice_receipt(linked_document_number='2026-0042')

    def test_an_amount_that_overflows_its_column_is_refused(self):
        with self.assertRaisesMessage(UniformFormatError, 'C100 field 1223'):
            build([invoice_receipt(total_amount=Decimal('1000000000000.00'))])

    def test_a_negative_discount_is_refused(self):
        with self.assertRaises(UniformFormatError):
            build([invoice_receipt(discount=Decimal('-5.00'))])

    def test_branches_must_be_named_when_the_business_has_them(self):
        with self.assertRaises(UniformFormatError):
            build([invoice_receipt()], business=make_business(has_branches=True))
        files = build([invoice_receipt(branch_id='PT-01')], business=make_business(has_branches=True))
        self.assertEqual(col(records(files.bkmvdata)[1], 409, 415), 'PT-01  ')
        self.assertEqual(col(records(files.ini)[0], 420, 420), '1')

    def test_a_primary_id_longer_than_15_digits_is_refused(self):
        with self.assertRaises(UniformFormatError):
            build(primary_id=10 ** 15)


class ZipTests(SimpleTestCase):
    def setUp(self):
        self.files = build()
        self.archive = zipfile.ZipFile(io.BytesIO(uniform_zip(self.files, make_business(), GENERATED)))

    def test_paths_follow_the_spec_directory_structure(self):
        self.assertEqual(self.archive.namelist(), [f'{DIRECTORY}/INI.TXT', f'{DIRECTORY}/BKMVDATA.zip'])
        self.assertEqual(self.files.directory, DIRECTORY)

    def test_ini_is_plain_and_bkmvdata_is_compressed_inside(self):
        self.assertEqual(self.archive.read(f'{DIRECTORY}/INI.TXT'), self.files.ini)
        inner = zipfile.ZipFile(io.BytesIO(self.archive.read(f'{DIRECTORY}/BKMVDATA.zip')))
        self.assertEqual(inner.namelist(), ['BKMVDATA.TXT'])
        self.assertEqual(inner.read('BKMVDATA.TXT'), self.files.bkmvdata)

    def test_a_different_moment_would_contradict_the_ini(self):
        with self.assertRaises(UniformFormatError):
            uniform_zip(self.files, make_business(), datetime(2026, 9, 11, 12, 31))
