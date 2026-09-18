"""Continuing the previous software's numbering: a run opened at the old run's last number plus one.

An opening only ever moves a run that has handed out nothing, never renumbers
what was issued (סעיף 23(ב)), gives each old run one continuation a year, and
leaves every run checkable for gaps from its own start (נספח ה׳(א)(5)).
"""
import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents.missing_receipts import missing_receipts_report, next_receipt_number
from apps.documents.models import DocumentSeries, DocumentSeriesOpening, FormalDocument
from apps.documents.numbering import (
    LESSON_RUN_REGEX,
    RENTAL_RUN_REGEX,
    STORE_RUN_REGEX,
    continuity,
    format_document_number,
    is_rental_number,
    next_document_number,
)
from apps.documents.register import series_of
from apps.documents.series_opening import (
    OpeningRefused,
    open_series,
    series_overview,
    suggested_series,
)
from apps.documents.tests.test_register import RegisterFixture, make_user
from apps.documents.tests.test_series_per_type import IssuingMixin

URL = '/api/v1/documents/series/'
OPEN = f'{URL}open/'


def this_year():
    return timezone.localdate().year


def untouched(series, year):
    """The run was left as it was: no row, or one that still starts at 1 and handed out nothing."""
    row = DocumentSeries.objects.filter(series=series, year=year).first()
    return row is None or (row.start, row.counter) == (1, 0)


def open_run(series='TI', label='חשבונית מס', last=40413, year=None, **extra):
    return open_series(
        series=series, year=year or this_year(), previous_last_number=last,
        previous_type_label=label, **extra,
    )


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class OpeningTests(IssuingMixin, TestCase):
    def test_the_next_number_is_the_old_last_number_plus_one(self, _mail):
        opening = open_run('TI', 'חשבונית מס', 40413, note='נבדק מול התוכנה הקודמת')

        self.assertEqual(opening.start, 40414)
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-040414')
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-040415')
        row = DocumentSeries.objects.get(series='TI', year=self.year)
        self.assertEqual((row.start, row.counter, row.issued), (40414, 40415, 2))

    def test_every_suggested_old_run_continues_in_its_own_type(self, _mail):
        # IR has issued in a live system; here it has not, so IRM is chosen by hand.
        for series, label, last, kind in (
            ('TI', 'חשבונית מס', 40413, 'tax_invoice'),
            ('RC', 'קבלה', 33403, 'receipt'),
            ('CR', 'חשבונית מס זיכוי', 41047, 'credit_invoice'),
            ('TX', 'חשבון עיסקה', 60012, 'transaction_invoice'),
            ('IRM', 'חשבונית מס קבלה', 121882, 'combined'),
        ):
            with self.subTest(series=series):
                open_run(series, label, last)
                self.assertEqual(
                    self.issue(kind).document_number, format_document_number(series, self.year, last + 1),
                )

    def test_a_run_that_issued_this_year_is_refused_and_keeps_its_numbers(self, _mail):
        first = self.issue('tax_invoice')

        with self.assertRaises(OpeningRefused) as refused:
            open_run('TI', 'חשבונית מס', 40413)

        self.assertTrue(refused.exception.conflict)
        self.assertIn('כבר הונפק מסמך אחד', refused.exception.message)
        row = DocumentSeries.objects.get(series='TI', year=self.year)
        self.assertEqual((row.start, row.counter), (1, 1))
        self.assertFalse(DocumentSeriesOpening.objects.exists())
        first.refresh_from_db()
        self.assertEqual(first.document_number, f'TI-{self.year}-000001')
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-000002')

    def test_a_run_whose_numbers_are_on_documents_is_refused_whatever_its_counter_says(self, _mail):
        FormalDocument.objects.create(
            document_number=f'TI-{self.year}-000001', document_type='tax_invoice', client_type='existing',
            child=self.kid, document_date=date(self.year, 1, 5), total_amount=Decimal('100.00'),
        )
        with self.assertRaises(OpeningRefused) as refused:
            open_run('TI', 'חשבונית מס', 40413)
        self.assertIn('כבר קיימים מסמכים', refused.exception.message)
        self.assertTrue(untouched('TI', self.year))

    def test_a_run_is_opened_once(self, _mail):
        open_run('TI', 'חשבונית מס', 40413)
        with self.assertRaises(OpeningRefused) as again:
            open_run('TI', 'חשבונית מס', 40500)
        self.assertTrue(again.exception.conflict)
        row = DocumentSeries.objects.get(series='TI', year=self.year)
        self.assertEqual((row.start, row.counter), (40414, 40413))
        self.assertEqual(DocumentSeriesOpening.objects.count(), 1)

    def test_an_opening_is_never_lowered_below_what_the_run_issued(self, _mail):
        open_run('TI', 'חשבונית מס', 40413)
        self.issue('tax_invoice')
        with self.assertRaises(OpeningRefused):
            open_run('TI', 'חשבונית מס', 100)
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-040415')

    def test_next_year_may_be_opened_while_this_year_runs_on(self, _mail):
        self.issue('tax_invoice')
        next_year = self.year + 1

        open_run('TI', 'חשבונית מס', 40413, year=next_year)

        self.assertEqual(
            next_document_number('TI', date(next_year, 1, 2)), f'TI-{next_year}-040414',
        )
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-000002')

    def test_only_this_year_and_the_next(self, _mail):
        for year in (self.year - 1, self.year + 2):
            with self.subTest(year=year), self.assertRaises(OpeningRefused):
                open_run('TI', 'חשבונית מס', 40413, year=year)
        self.assertFalse(DocumentSeries.objects.exists())

    def test_one_old_run_is_continued_by_one_kogo_run_a_year(self, _mail):
        open_run('IRM', 'חשבונית מס קבלה', 121882)

        with self.assertRaises(OpeningRefused) as refused:
            open_run('IR', 'חשבונית מס קבלה', 121882)

        self.assertTrue(refused.exception.conflict)
        self.assertIn('IRM', refused.exception.message)
        self.assertTrue(untouched('IR', self.year))
        # The next tax year is a year of its own.
        open_run('IR', 'חשבונית מס קבלה', 121882, year=self.year + 1)

    def test_an_old_run_continues_only_in_a_run_of_its_type(self, _mail):
        with self.assertRaises(OpeningRefused) as refused:
            open_run('RC', 'חשבונית מס', 40413)
        self.assertFalse(refused.exception.conflict)
        with self.assertRaises(OpeningRefused):
            open_run('TI', 'הצעת מחיר', 40413)
        self.assertFalse(DocumentSeries.objects.exists())

    def test_the_start_is_exactly_the_old_last_number_plus_one(self, _mail):
        with self.assertRaises(OpeningRefused):
            open_run('TI', 'חשבונית מס', 40413, start=40420)
        with self.assertRaises(OpeningRefused):
            open_run('TI', 'חשבונית מס', 0)
        with self.assertRaises(OpeningRefused):
            open_run('TI', 'חשבונית מס', 'abc')
        opening = open_run('TI', 'חשבונית מס', '40,413', start='40414')
        self.assertEqual(opening.start, 40414)

    def test_the_database_refuses_what_passed_the_checks_together(self, _mail):
        """Two requests opening one old run at once: the constraint lets one through, and the loser leaves no trace."""
        with patch.object(DocumentSeriesOpening.objects, 'create', side_effect=IntegrityError('duplicate')):
            with self.assertRaises(OpeningRefused) as refused:
                open_run('TI', 'חשבונית מס', 40413)
        self.assertTrue(refused.exception.conflict)
        self.assertTrue(untouched('TI', self.year))

        open_run('IRM', 'חשבונית מס קבלה', 121882)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentSeriesOpening.objects.create(
                series='IR', year=self.year, start=121883, previous_last_number=121882,
                previous_type_label='חשבונית מס קבלה',
            )
        # A start that is not the old last number plus one is refused by the database too.
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentSeriesOpening.objects.create(
                series='TI', year=self.year, start=5, previous_last_number=121882,
                previous_type_label='חשבונית מס',
            )

    def test_the_record_is_never_edited_or_deleted(self, _mail):
        opening = open_run('TI', 'חשבונית מס', 40413)
        opening.note = 'שונה'
        with self.assertRaises(ValueError):
            opening.save()
        with self.assertRaises(ValueError):
            opening.delete()
        self.assertEqual(DocumentSeriesOpening.objects.get().note, '')


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ContinuityFromTheStartTests(IssuingMixin, TestCase):
    def test_a_continued_run_is_checked_from_its_start(self, _mail):
        open_run('TI', 'חשבונית מס', 40413)
        for _ in range(3):
            self.issue('tax_invoice')

        run = next(run for run in continuity(self.year) if run.series == 'TI')

        self.assertEqual(run.start, 40414)
        self.assertEqual(run.issued, 3)
        self.assertEqual((run.first, run.last), (f'TI-{self.year}-040414', f'TI-{self.year}-040416'))
        self.assertTrue(run.complete)
        self.assertEqual(run.continues, 'ממשיך את הסדרה של התוכנה הקודמת (אחרון 40413)')

    def test_a_gap_above_the_start_is_named_and_nothing_below_it(self, _mail):
        open_run('TI', 'חשבונית מס', 40413)
        self.issue('tax_invoice')
        DocumentSeries.next_number('TI', self.year)  # handed out, and no document carries it
        self.issue('tax_invoice')

        run = next(run for run in continuity(self.year) if run.series == 'TI')

        self.assertEqual(run.missing, (f'TI-{self.year}-040415',))
        self.assertEqual(run.issued, 3)

    def test_an_opened_run_that_issued_nothing_is_whole(self, _mail):
        open_run('TI', 'חשבונית מס', 40413)
        run = next(run for run in continuity(self.year) if run.series == 'TI')
        self.assertEqual((run.issued, run.first, run.last, run.missing), (0, '', '', ()))

    def test_a_run_from_1_is_checked_as_before(self, _mail):
        self.issue('receipt')
        DocumentSeries.next_number('RC', self.year)
        run = next(run for run in continuity(self.year) if run.series == 'RC')
        self.assertEqual((run.start, run.issued, run.missing, run.continues), (1, 2, (f'RC-{self.year}-000002',), ''))

    def test_the_missing_receipts_screen_carries_the_opening(self, _mail):
        open_run('IR', 'חשבונית מס קבלה', 121882)

        self.assertEqual(next_receipt_number(), f'IR-{self.year}-121883')
        report = missing_receipts_report(self.year)
        lessons = next(run for run in report['continuity'] if run['series'] == 'IR')
        self.assertEqual(lessons['start'], 121883)
        self.assertEqual(lessons['previous_last_number'], 121882)
        self.assertEqual(lessons['continues'], 'ממשיך את הסדרה של התוכנה הקודמת (אחרון 121882)')


class PeriodReportTests(RegisterFixture, TestCase):
    def test_the_report_names_the_opening_and_still_renders(self):
        from apps.documents.period_report import build_report
        from apps.documents.period_report_pdf import generate_period_report_pdf

        year = this_year()
        open_run('TI', 'חשבונית מס', 40413)
        FormalDocument.objects.create(
            document_number=next_document_number('TI', date(year, 1, 10)), document_type='tax_invoice',
            client_type='existing', child=self.kid, document_date=date(year, 1, 10),
            subtotal=Decimal('100.00'), vat_amount=Decimal('18.00'), total_amount=Decimal('118.00'),
        )

        report = build_report(self.manager, date(year, 1, 1), date(year, 1, 31), f'ינואר {year}')

        run = next(run for run in report.continuity if run.series == 'TI')
        self.assertEqual((run.first, run.issued, run.missing), (f'TI-{year}-040414', 1, ()))
        self.assertEqual(run.continues, 'ממשיך את הסדרה של התוכנה הקודמת (אחרון 40413)')
        self.assertTrue(generate_period_report_pdf(report).startswith(b'%PDF'))


class OverviewTests(IssuingMixin, TestCase):
    def test_every_run_of_this_year_and_the_next(self):
        overview = series_overview()
        self.assertEqual(overview['years'], [self.year, self.year + 1])
        self.assertEqual(len(overview['runs']), 18)
        ti = next(run for run in overview['runs'] if run['name'] == f'TI-{self.year}')
        self.assertEqual(
            (ti['issued'], ti['start'], ti['next_number'], ti['can_open'], ti['opening']),
            (0, 1, f'TI-{self.year}-000001', True, None),
        )

    def test_a_run_that_issued_says_so_and_when_it_can_be_opened(self):
        DocumentSeries.objects.create(series='IR', year=self.year, counter=250)
        overview = series_overview()
        ir = next(run for run in overview['runs'] if run['name'] == f'IR-{self.year}')
        self.assertFalse(ir['can_open'])
        self.assertIn('הונפקו 250 מסמכים', ir['reason'])
        self.assertIn(str(self.year + 1), ir['reason'])
        self.assertEqual(ir['last'], f'IR-{self.year}-000250')
        ir_next = next(run for run in overview['runs'] if run['name'] == f'IR-{self.year + 1}')
        self.assertTrue(ir_next['can_open'])

    def test_the_invoice_receipt_run_is_suggested_to_ir_only_while_ir_issued_nothing(self):
        self.assertEqual(suggested_series('חשבונית מס קבלה', self.year), 'IR')
        DocumentSeries.objects.create(series='IR', year=self.year, counter=1)
        self.assertEqual(suggested_series('חשבונית מס קבלה', self.year), 'IRM')
        self.assertEqual(suggested_series('חשבונית מס קבלה', self.year + 1), 'IR')
        self.assertEqual(
            {label: suggested_series(label, self.year) for label in ('חשבונית מס', 'קבלה', 'חשבונית מס זיכוי', 'חשבון עיסקה')},
            {'חשבונית מס': 'TI', 'קבלה': 'RC', 'חשבונית מס זיכוי': 'CR', 'חשבון עיסקה': 'TX'},
        )
        types = {row['label']: row for row in series_overview()['previous_types']}
        self.assertEqual(types['חשבונית מס קבלה']['suggested'], {str(self.year): 'IRM', str(self.year + 1): 'IR'})
        self.assertEqual(types['חשבונית מס קבלה']['series_options'], ['IR', 'ST', 'RT', 'IRM'])

    def test_an_opened_run_carries_its_record_and_its_siblings_are_closed_to_that_old_run(self):
        open_run('IRM', 'חשבונית מס קבלה', 121882, note='אומת מול הייצוא')
        overview = series_overview()
        irm = next(run for run in overview['runs'] if run['name'] == f'IRM-{self.year}')
        self.assertFalse(irm['can_open'])
        self.assertEqual(irm['next_number'], f'IRM-{self.year}-121883')
        self.assertEqual(irm['opening']['previous_last_number'], 121882)
        self.assertEqual(irm['opening']['note'], 'אומת מול הייצוא')
        # Every other combined run this year could only continue the same old run.
        ir = next(run for run in overview['runs'] if run['name'] == f'IR-{self.year}')
        self.assertFalse(ir['can_open'])
        self.assertIn('IRM', ir['reason'])
        types = {row['label']: row for row in overview['previous_types']}
        self.assertEqual(types['חשבונית מס קבלה']['continued_by'], {str(self.year): 'IRM'})


class LongNumbersTests(RegisterFixture, APITestCase):
    """A run continued past 999999 prints seven digits, and every reader still knows its run."""

    def test_numbers_past_six_digits_format_and_match_their_runs(self):
        self.assertEqual(format_document_number('TI', 2026, 1_000_000), 'TI-2026-1000000')
        self.assertEqual(format_document_number('TI', 2026, 7), 'TI-2026-000007')
        self.assertTrue(re.match(LESSON_RUN_REGEX, 'IR-2026-1000000'))
        self.assertTrue(re.match(STORE_RUN_REGEX, 'ST-2026-1000000'))
        self.assertTrue(re.match(STORE_RUN_REGEX, 'SD-2026-12345678'))
        self.assertTrue(re.match(RENTAL_RUN_REGEX, 'RT-2026-1000000'))
        self.assertTrue(is_rental_number('RT-2026-1000000'))
        # Still no fewer than six digits, and still not the old UUID numbers.
        self.assertFalse(re.match(LESSON_RUN_REGEX, 'IR-2026-00001'))
        self.assertFalse(re.match(LESSON_RUN_REGEX, 'INV-20260809-A1B2C3D4'))
        self.assertEqual(series_of('IRM-2026-1000000'), 'IRM')

    def test_a_run_opened_at_999999_hands_out_1000000(self):
        open_run('TI', 'חשבונית מס', 999_999)
        self.assertEqual(next_document_number('TI'), f'TI-{this_year()}-1000000')
        run = next(run for run in continuity(this_year()) if run.series == 'TI')
        self.assertEqual(run.missing, (f'TI-{this_year()}-1000000',))

    def test_the_register_and_the_export_still_classify_long_numbers(self):
        self.lesson_receipt('IR-2026-1000000', child=self.kid)
        DocumentSeries.objects.create(series='ST', year=this_year(), counter=999_999)
        sale = self.store_sale()
        self.assertEqual(sale.invoice_number, f'ST-{this_year()}-1000000')
        self.credit_note(number='CR-2026-1000000', credits='IR-2026-1000000')

        rows = self.rows(self.report())
        self.assertEqual(rows['IR-2026-1000000'].channel, 'lessons')
        self.assertEqual(rows[sale.invoice_number].channel, 'store')
        self.assertTrue(rows['CR-2026-1000000'].is_credit)
        self.assertFalse({'IR-2026-1000000', sale.invoice_number} & self.undocumented_references())

        self.client.force_authenticate(self.manager)
        res = self.client.get('/api/v1/documents/documents/register-export/', {'month': '2026-08'})
        body = res.content.decode('utf-8-sig')
        self.assertIn('IR-2026-1000000,IR,', body)
        self.assertIn(f'{sale.invoice_number},ST,', body)

        import io
        import zipfile

        res = self.client.get('/api/v1/documents/documents/uniform-export/', {'month': '2026-08'})
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
        outer = zipfile.ZipFile(io.BytesIO(res.content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(next(n for n in outer.namelist() if n.endswith('BKMVDATA.zip')))))
        records = inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines()
        headers = {line[25:45].strip() for line in records if line.startswith('C100')}
        self.assertTrue({'IR-2026-1000000', sale.invoice_number, 'CR-2026-1000000'} <= headers)
        credit_line = next(line for line in records if line.startswith('D110') and 'CR-2026-1000000' in line)
        self.assertIn('IR-2026-1000000', credit_line)


class SeriesEndpointTests(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-series@test', UserProfile.ROLE_MANAGER)
        self.year = this_year()

    def payload(self, **over):
        return {
            'series': 'TI', 'year': self.year, 'start': 40414, 'previous_last_number': 40413,
            'previous_type_label': 'חשבונית מס', 'note': 'נבדק בתוכנה הקודמת ב-18.9', **over,
        }

    def test_a_manager_opens_a_run_and_sees_it(self):
        self.client.force_authenticate(self.manager)

        res = self.client.post(OPEN, self.payload(), format='json')

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['run']['next_number'], f'TI-{self.year}-040414')
        self.assertEqual(res.data['run']['opening']['created_by'], 'manager-series@test')
        self.assertFalse(res.data['run']['can_open'])
        opening = DocumentSeriesOpening.objects.get()
        self.assertEqual(
            (opening.series, opening.year, opening.start, opening.previous_last_number,
             opening.previous_type_label, opening.created_by, opening.note),
            ('TI', self.year, 40414, 40413, 'חשבונית מס', self.manager, 'נבדק בתוכנה הקודמת ב-18.9'),
        )
        listed = self.client.get(URL)
        self.assertEqual(listed.status_code, 200)
        ti = next(run for run in listed.data['runs'] if run['name'] == f'TI-{self.year}')
        self.assertEqual(ti['opening']['continues'], 'ממשיך את הסדרה של התוכנה הקודמת (אחרון 40413)')

    def test_bad_input_is_a_400_and_a_second_opening_a_409(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(OPEN, self.payload(series='ZZ'), format='json').status_code, 400)
        self.assertEqual(self.client.post(OPEN, self.payload(start=40500), format='json').status_code, 400)
        self.assertEqual(self.client.post(OPEN, self.payload(year=self.year + 2), format='json').status_code, 400)
        self.assertEqual(self.client.post(OPEN, self.payload(), format='json').status_code, 201)
        again = self.client.post(OPEN, self.payload(), format='json')
        self.assertEqual(again.status_code, 409)
        self.assertIn('error', again.data)

    def test_only_a_manager(self):
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            with self.subTest(role=role):
                self.client.force_authenticate(make_user(f'{role}-series@test', role))
                self.assertEqual(self.client.get(URL).status_code, 403)
                self.assertEqual(self.client.post(OPEN, self.payload(), format='json').status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertIn(self.client.post(OPEN, self.payload(), format='json').status_code, (401, 403))
        self.assertFalse(DocumentSeries.objects.exists())
        self.assertFalse(DocumentSeriesOpening.objects.exists())
