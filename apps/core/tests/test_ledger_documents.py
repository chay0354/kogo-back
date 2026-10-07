"""What reaches the documents page, and what each row says about where it came from.

A store sale produces a numbered tax document whatever the customer paid with, so
all of them belong in the ledger — the old query demanded a Tranzila transaction
and quietly dropped every cash and monthly-billing sale.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.tranzila_ledger import list_ledger_documents
from apps.store.models import StoreInvoice


class LedgerIncludesEveryStoreSaleTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.card = StoreInvoice.objects.create(
            invoice_number='ST-CARD-1',
            customer_name='קונה אשראי',
            total_amount=Decimal('149.00'),
            payment_method='credit_card',
            payment_status='completed',
            tranzila_transaction_id='TRX-1',
        )
        self.cash = StoreInvoice.objects.create(
            invoice_number='ST-CASH-1',
            customer_name='קונה מזומן',
            total_amount=Decimal('49.00'),
            payment_method='cash',
            payment_status='completed',
        )
        self.monthly = StoreInvoice.objects.create(
            invoice_number='ST-MONTH-1',
            customer_name='חיוב חודשי',
            total_amount=Decimal('60.00'),
            payment_method='monthly_billing',
            payment_status='pending',
        )

    def _numbers(self) -> set[str]:
        result = list_ledger_documents(
            start_date=self.today - timedelta(days=7),
            end_date=self.today,
            local_only=True,
        )
        return {row['document_number'] for row in result['documents']}

    def test_a_cash_sale_is_listed(self):
        self.assertIn('ST-CASH-1', self._numbers())

    def test_a_monthly_billing_sale_is_listed(self):
        self.assertIn('ST-MONTH-1', self._numbers())

    def test_a_card_sale_is_still_listed(self):
        self.assertIn('ST-CARD-1', self._numbers())


class LedgerRowSaysWhereItCameFromTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def _row(self, number: str) -> dict:
        result = list_ledger_documents(
            start_date=self.today - timedelta(days=7),
            end_date=self.today,
            local_only=True,
        )
        return next(row for row in result['documents'] if row['document_number'] == number)

    def test_a_website_order_is_marked_as_a_delivery(self):
        StoreInvoice.objects.create(
            invoice_number='ST-WEB-1',
            customer_name='רותי ניסן',
            website_order_number='CG-260830-ABCD',
            shipping_address='הרצל 12, כפר סבא',
            total_amount=Decimal('149.00'),
            payment_method='credit_card',
            payment_status='completed',
        )

        row = self._row('ST-WEB-1')

        self.assertEqual(row['origin'], 'store_website')
        self.assertEqual(row['origin_label'], 'חנות · אתר')
        self.assertEqual(row['website_order_number'], 'CG-260830-ABCD')

    def test_a_counter_sale_is_marked_as_a_branch_sale(self):
        StoreInvoice.objects.create(
            invoice_number='ST-COUNTER-1',
            customer_name='קונה בסניף',
            total_amount=Decimal('49.00'),
            payment_method='cash',
            payment_status='completed',
        )

        row = self._row('ST-COUNTER-1')

        self.assertEqual(row['origin'], 'store_counter')
        self.assertEqual(row['origin_label'], 'חנות · סניף')
        self.assertEqual(row['payment_method_label'], 'מזומן')


class CancelledReceiptIsNotADebtTest(TestCase):
    """The collection tab chases open balances — a cancelled receipt must not be one."""

    def test_a_cancelled_receipt_has_no_open_balance(self):
        from apps.core.tests.test_fixtures import TestDataFactory
        from apps.customers.financial_models import Invoice

        family = TestDataFactory.create_family()
        Invoice.objects.create(
            invoice_number='IR-TEST-CANCELLED', family=family, amount=Decimal('236.00'),
            status='cancelled', invoice_date=timezone.now(),
        )
        today = timezone.localdate()

        row = next(
            r for r in list_ledger_documents(start_date=today, end_date=today, local_only=True)['documents']
            if r['document_number'] == 'IR-TEST-CANCELLED'
        )

        self.assertEqual(row['open_balance'], 0.0)


class WhatWentOutLastComesFirstTest(TestCase):
    """
    The documents page lists the newest document first (owner, 7.10.2026).

    A document's date is a day and its number runs in its own type's series, so
    neither says which of the day's documents went out last: a tax invoice
    issued a minute ago stood under the morning's store receipts. Every row now
    carries the moment it was issued, and the list is ordered by it.
    """

    def setUp(self):
        from datetime import datetime, time

        from apps.documents.models import FormalDocument

        self.today = timezone.localdate()

        def at(hour, minute=0):
            return timezone.make_aware(datetime.combine(self.today, time(hour, minute)))

        # A store sale of the morning: its number sorts above the others' as text.
        morning = StoreInvoice.objects.create(
            invoice_number='ST-900', customer_name='קונה', total_amount=Decimal('49.00'),
            payment_method='cash', payment_status='completed',
        )
        StoreInvoice.objects.filter(pk=morning.pk).update(issue_date=at(8))
        # A tax invoice at noon, and a receipt a minute ago — in two other series.
        FormalDocument.objects.create(
            document_number='IN-0007', document_type='tax_invoice', client_type='new',
            document_date=self.today, subtotal=Decimal('100'), vat_amount=Decimal('18'),
            total_amount=Decimal('118'), issued_at=at(12),
        )
        FormalDocument.objects.create(
            document_number='CR-0002', document_type='receipt', client_type='new',
            document_date=self.today, subtotal=Decimal('50'), vat_amount=Decimal('0'),
            total_amount=Decimal('50'), issued_at=at(15, 30),
        )
        # Issued this afternoon for yesterday's date: it went out after the noon invoice.
        FormalDocument.objects.create(
            document_number='IN-0006', document_type='tax_invoice', client_type='new',
            document_date=self.today - timedelta(days=1), subtotal=Decimal('100'),
            vat_amount=Decimal('18'), total_amount=Decimal('118'), issued_at=at(14),
        )

    def _rows(self):
        return list_ledger_documents(
            start_date=self.today - timedelta(days=7), end_date=self.today, local_only=True,
        )['documents']

    def test_the_list_runs_from_the_last_document_issued_to_the_first(self):
        self.assertEqual(
            [row['document_number'] for row in self._rows()],
            ['CR-0002', 'IN-0006', 'IN-0007', 'ST-900'],
        )

    def test_every_row_says_when_it_went_out_as_one_comparable_moment(self):
        from datetime import datetime, timezone as dt_timezone

        moments = [row['issued_at'] for row in self._rows()]

        self.assertTrue(all(moments), moments)
        parsed = [datetime.fromisoformat(moment) for moment in moments]
        self.assertTrue(all(moment.utcoffset() == dt_timezone.utc.utcoffset(None) for moment in parsed))
        self.assertEqual(parsed, sorted(parsed, reverse=True))

    def test_a_document_issued_before_the_moment_was_kept_shows_when_its_row_was_made(self):
        from apps.documents.models import FormalDocument

        old = FormalDocument.objects.create(
            document_number='IN-0001', document_type='tax_invoice', client_type='new',
            document_date=self.today, subtotal=Decimal('10'), vat_amount=Decimal('1.8'),
            total_amount=Decimal('11.8'),
        )

        row = next(row for row in self._rows() if row['document_number'] == 'IN-0001')

        self.assertIsNone(old.issued_at)
        self.assertTrue(row['issued_at'])


class TheMomentOfADocumentTest(TestCase):
    def test_a_time_with_no_zone_is_the_studios_own(self):
        from apps.core.tranzila_ledger import _issued_at

        # Tranzila's list gives "2026-10-07 09:15:00": Israel time, three hours ahead of UTC in October.
        self.assertEqual(_issued_at('2026-10-07 09:15:00'), '2026-10-07T06:15:00+00:00')

    def test_a_bare_day_or_nothing_is_no_moment(self):
        from apps.core.tranzila_ledger import _issued_at

        self.assertEqual(_issued_at('2026-10-07'), '')
        self.assertEqual(_issued_at(''), '')
        self.assertEqual(_issued_at(None), '')
        self.assertEqual(_issued_at(timezone.localdate()), '')


class ARowNamesItsSignedOriginalTest(TestCase):
    """
    The file the documents page hands out is a copy, and a copy carries no seal.
    The owner issued a tax invoice, opened it from the page and saw no signature
    (7.10.2026) — the original had been signed and mailed that same second. Each
    row now says which signed original is its own, so the page can show it.
    """

    def setUp(self):
        from apps.documents.models import FormalDocument, SignedOriginal

        self.today = timezone.localdate()
        self.SignedOriginal = SignedOriginal
        for number in ('TI-1', 'TI-2', 'TI-3', 'TI-4'):
            FormalDocument.objects.create(
                document_number=number, document_type='tax_invoice', client_type='new',
                document_date=self.today, subtotal=Decimal('100'), vat_amount=Decimal('18'),
                total_amount=Decimal('118'),
            )
        # A signed row carries its bytes and their fingerprint (the table's own rule).
        signed_file = {'signed_at': timezone.now(), 'pdf': b'%PDF-signed', 'size': 11, 'sha256': 'a' * 64}
        self.signed = SignedOriginal.objects.create(number='TI-1', kind='formal', source_id='a', **signed_file)
        # Waiting to be signed; and a copy made for the archive, which is not an original.
        SignedOriginal.objects.create(number='TI-2', kind='formal', source_id='b')
        SignedOriginal.objects.create(
            number='TI-3', kind='formal', source_id='c', purpose=SignedOriginal.PURPOSE_ARCHIVE, **signed_file,
        )

    def _rows(self, **more):
        rows = list_ledger_documents(
            start_date=self.today - timedelta(days=1), end_date=self.today, local_only=True, **more,
        )['documents']
        return {row['document_number']: row for row in rows}

    def test_a_signed_original_is_named_with_the_moment_it_was_signed(self):
        row = self._rows()['TI-1']

        self.assertEqual(row['signed_original_id'], str(self.signed.id))
        self.assertTrue(row['signed_at'].endswith('+00:00'), row['signed_at'])

    def test_an_unsigned_one_an_archive_copy_and_a_document_with_none_are_not(self):
        rows = self._rows()

        for number in ('TI-2', 'TI-3', 'TI-4'):
            self.assertIsNone(rows[number]['signed_original_id'], number)
            self.assertEqual(rows[number]['signed_at'], '', number)

    def test_a_partners_list_names_none(self):
        """The signed file is handed out to the office only."""
        from apps.core.models import Branch

        branch = Branch.objects.create(name='סניף של שותף')

        rows = list_ledger_documents(
            start_date=self.today - timedelta(days=1), end_date=self.today, local_only=True,
            branch_ids=[branch.id],
        )['documents']

        self.assertTrue(all('signed_original_id' not in row for row in rows))
