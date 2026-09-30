"""
The cutover memo (apps/documents/cutover_memo.py): the facts it draws from the
database, the PDF it renders, and the signature `--sign` puts on it.
"""
import os
import tempfile
from datetime import date
from decimal import Decimal

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.customers.financial_models import Invoice
from apps.customers.models import BusinessCustomer
from apps.documents.cutover_memo import (
    MemoInputError,
    cutover_memo_pdf,
    gather_facts,
    parse_old_last,
    parse_switch_at,
)
from apps.documents.models import (
    CheckItem,
    CheckPlan,
    DocumentCounter,
    DocumentSeries,
    DocumentSeriesOpening,
    FormalDocument,
)
from apps.documents.tests.signing_support import pdf_text, signing_on
from apps.documents.tests.test_register import RegisterFixture
from apps.legacy_import.models import LegacyDocument

SWITCH = '2026-09-24 09:57'


class ParsingTests(TestCase):
    def test_old_last_numbers_are_read(self):
        self.assertEqual(
            parse_old_last(['combined=121882@2026-09-23', 'receipt=40413']),
            {'combined': (121882, date(2026, 9, 23)), 'receipt': (40413, None)},
        )
        for bad in ('invoice=1', 'combined=abc', 'combined=1@2026-13-40'):
            with self.assertRaises(MemoInputError, msg=bad):
                parse_old_last([bad])

    def test_the_switch_is_israel_time(self):
        moment = parse_switch_at(SWITCH)
        self.assertEqual(timezone.localtime(moment).strftime('%Y-%m-%d %H:%M'), SWITCH)
        self.assertIsNone(parse_switch_at(''))
        with self.assertRaises(MemoInputError):
            parse_switch_at('yesterday')


class MemoFixture(RegisterFixture):
    def runs(self):
        DocumentSeries.objects.create(series='IR', year=2026, counter=3)
        for number, day in ((1, 5), (2, 6), (3, 7)):
            self.lesson_receipt(f'IR-2026-{number:06d}', day=day)
        # IRM continues the old program's run: 121882 was the last one there.
        DocumentSeries.objects.create(series='IRM', year=2026, counter=121883, start=121883)
        DocumentSeriesOpening.objects.create(
            series='IRM', year=2026, start=121883, previous_last_number=121882,
            previous_type_label='חשבונית מס קבלה',
        )
        FormalDocument.objects.create(
            document_number='IRM-2026-121883', document_type='combined', client_type='existing', child=self.kid,
            document_date=date(2026, 9, 25), subtotal=Decimal('100'), vat_amount=Decimal('18'),
            total_amount=Decimal('118'),
        )


class FactsTests(MemoFixture, TestCase):
    def test_every_run_with_its_numbers_dates_and_opening(self):
        self.runs()
        DocumentSeries.objects.create(series='TI', year=2026, counter=2)  # 000002 was never issued
        FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='existing', child=self.kid,
            document_date=date(2026, 9, 10), subtotal=Decimal('100'), vat_amount=Decimal('18'),
            total_amount=Decimal('118'),
        )
        facts = gather_facts(switch_at=parse_switch_at(SWITCH))
        runs = {run.name: run for run in facts.runs}
        ir = runs['IR-2026']
        self.assertEqual((ir.first, ir.last, ir.issued), ('IR-2026-000001', 'IR-2026-000003', 3))
        self.assertEqual((ir.first_date, ir.last_date), (date(2026, 8, 5), date(2026, 8, 7)))
        self.assertEqual(runs['IRM-2026'].first, 'IRM-2026-121883')
        self.assertIn('121882', runs['IRM-2026'].continues)
        self.assertEqual([run.name for run in facts.gaps], ['TI-2026'])
        self.assertEqual(runs['TI-2026'].missing, ('TI-2026-000002',))
        # The TI invoice has no receipt against it: an open item.
        self.assertEqual([row['number'] for row in facts.open_invoices], ['TI-2026-000001'])

    def test_the_old_programs_last_numbers_come_from_its_imported_documents(self):
        LegacyDocument.objects.create(doc_type='combined', number=121880, original_type='חשבונית מס קבלה',
                                      document_date=date(2026, 9, 20))
        LegacyDocument.objects.create(doc_type='combined', number=121882, original_type='חשבונית מס קבלה',
                                      document_date=date(2026, 9, 23))
        facts = gather_facts(manual_old_last={'combined': (121881, None), 'receipt': (40413, date(2026, 9, 22))})
        lines = {line.doc_type: line for line in facts.old_last}
        self.assertEqual((lines['combined'].number, lines['combined'].on, lines['combined'].source),
                         (121882, date(2026, 9, 23), 'ייבוא'))
        self.assertIn('121881', lines['combined'].note)  # the typed value that disagrees is shown
        self.assertEqual((lines['receipt'].number, lines['receipt'].source), (40413, 'הוזן ידנית'))
        self.assertIsNone(lines['tax_invoice'].number)

    def test_the_closed_shared_run_the_old_numbers_and_the_open_items(self):
        DocumentCounter.objects.create(year=2026, counter=2)
        for number in (1, 2):
            FormalDocument.objects.create(
                document_number=f'2026-{number:04d}', document_type='receipt', client_type='existing',
                child=self.kid, document_date=date(2026, 8, number), subtotal=Decimal('50'),
                total_amount=Decimal('50'),
            )
        old = self.lesson_receipt('INV-20260801-ABCDEF12', day=1)
        Invoice.objects.filter(pk=old.pk).update(amount=Decimal('100'))
        plan = CheckPlan.objects.create(child=self.kid, branch=self.north)
        CheckItem.objects.create(plan=plan, due_date=date(2026, 11, 1), amount=Decimal('300'))
        FormalDocument.objects.create(document_number='D-ABCDEF12', document_type='draft', client_type='existing',
                                      child=self.kid, document_date=date(2026, 9, 1))

        facts = gather_facts()
        (shared,) = facts.shared_runs
        self.assertEqual((shared['count'], shared['first'], shared['last']), (2, '2026-0001', '2026-0002'))
        self.assertEqual(facts.old_lessons['count'], 1)
        self.assertEqual(facts.old_lessons['total'], Decimal('100'))
        self.assertEqual(facts.pending_checks['count'], 1)
        self.assertEqual(facts.pending_checks['total'], Decimal('300'))
        self.assertEqual(facts.drafts, 1)

    def test_a_receipt_naming_the_invoice_closes_it(self):
        merchant = BusinessCustomer.objects.create(first_name='עסק', last_name='בע"מ')
        FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='business',
            business_customer=merchant, document_date=date(2026, 9, 1), subtotal=Decimal('100'),
            vat_amount=Decimal('18'), total_amount=Decimal('118'),
        )
        FormalDocument.objects.create(
            document_number='RC-2026-000001', document_type='receipt', client_type='business',
            business_customer=merchant, document_date=date(2026, 9, 2), subtotal=Decimal('118'),
            total_amount=Decimal('118'), linked_document_number='TI-2026-000001',
        )
        self.assertEqual(gather_facts().open_invoices, [])


class RenderTests(MemoFixture, TestCase):
    def test_it_renders_with_no_data_at_all(self):
        pdf = cutover_memo_pdf()
        self.assertTrue(pdf.startswith(b'%PDF'))
        text = pdf_text(pdf)
        self.assertIn('______________', text)  # the switch and the old numbers are left to fill in

    def test_the_series_lines_are_in_the_pdf(self):
        self.runs()
        text = pdf_text(cutover_memo_pdf(switch_at=parse_switch_at(SWITCH), old_software='תוכנה ישנה',
                                         manual_old_last={'combined': (121882, date(2026, 9, 23)),
                                                          'receipt': (40413, date(2026, 9, 22))}))
        for number in ('IR-2026-000001', 'IR-2026-000003', 'IRM-2026-121883', '121882', '40413', '23/09/2026'):
            self.assertIn(number, text)

    def test_the_switch_and_the_statement_name_the_moment(self):
        from apps.documents.cutover_memo import build_story

        def texts(flowables):
            for item in flowables:
                yield getattr(item, 'text', '')
                yield from texts(getattr(item, '_content', []))

        story = ' '.join(texts(build_story(gather_facts(switch_at=parse_switch_at(SWITCH),
                                                        old_software='תוכנה ישנה'))))
        # The date and time sit inside a Hebrew line (bidi-reordered); each run stays whole.
        self.assertEqual(story.count('24/09/2026'), 2)
        self.assertEqual(story.count('09:57'), 2)

    def test_signed_with_the_configured_backend(self):
        from apps.documents.signing.backends import get_backend
        from apps.documents.signing.signer import check_signed_pdf

        with signing_on():
            pdf = cutover_memo_pdf(sign=True)
            check_signed_pdf(pdf, get_backend().certificate())

    def test_the_command_writes_the_file(self):
        self.runs()
        with tempfile.TemporaryDirectory() as folder:
            out = os.path.join(folder, 'memo.pdf')
            call_command('cutover_memo', '--switch-at', SWITCH, '--old-last', 'combined=121882@2026-09-23',
                         '--out', out, stdout=open(os.devnull, 'w'))
            with open(out, 'rb') as handle:
                self.assertIn('IR-2026-000003', pdf_text(handle.read()))
        with self.assertRaises(CommandError):
            call_command('cutover_memo', '--old-last', 'nonsense', '--out', os.devnull)

