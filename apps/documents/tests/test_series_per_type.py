"""A document issued by hand draws from the run of its own type (סעיף 5(ג)), and
every run can be checked for gaps (נספח ה׳(א)(5))."""
from datetime import date
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Branch, City
from apps.customers.models import Child, Family
from apps.documents import service
from apps.documents.models import DocumentCounter, DocumentSeries, FormalDocument
from apps.documents.numbering import continuity, formal_document_number


def invoice_details():
    return {
        'document_date': '2026-09-02',
        'line_items': [{'description': 'חוג ספטמבר', 'quantity': 1, 'price': '100.00'}],
    }


class IssuingMixin:
    def setUp(self):
        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        family = Family.objects.create(name='משפחה', branch=branch)
        self.kid = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.year = timezone.localdate().year

    def issue(self, kind):
        base = {'client_type': 'existing', 'child_id': str(self.kid.id)}
        if kind in ('tax_invoice', 'transaction_invoice'):
            return service.create_invoice({**base, 'invoice_details': invoice_details()}, kind)
        if kind == 'combined':
            return service.create_combined({**base, 'invoice_details': invoice_details()})
        if kind == 'receipt':
            return service.create_receipt(
                {**base, 'receipt_details': {'payment_method': 'מזומן', 'cash_amount': 100}},
            )
        if kind == 'credit_invoice':
            return service.create_credit_invoice({**base, 'credit_invoice_details': {
                'credit_amount_before_vat': '100.00', 'document_date': '2026-09-02', 'credit_reason': 'ביטול',
            }})
        raise AssertionError(kind)


# No Tranzila issuance and no mail: the credit note would otherwise be sent.
@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class SeriesPerTypeTests(IssuingMixin, TestCase):
    def test_each_type_starts_a_run_of_its_own(self, _mail):
        runs = {
            'tax_invoice': 'TI',
            'combined': 'IRM',
            'receipt': 'RC',
            'transaction_invoice': 'TX',
            'credit_invoice': 'CR',
        }
        for kind, series in runs.items():
            with self.subTest(kind=kind):
                self.assertEqual(self.issue(kind).document_number, f'{series}-{self.year}-000001')

    def test_a_type_only_continues_its_own_run(self, _mail):
        self.issue('tax_invoice')
        self.issue('combined')
        self.assertEqual(self.issue('tax_invoice').document_number, f'TI-{self.year}-000002')
        self.assertEqual(self.issue('combined').document_number, f'IRM-{self.year}-000002')

    def test_the_closed_shared_run_is_never_drawn_from(self, _mail):
        DocumentCounter.objects.create(year=self.year, counter=41)
        self.issue('tax_invoice')
        self.issue('receipt')
        self.assertEqual(DocumentCounter.objects.get(year=self.year).counter, 41)

    def test_a_type_without_a_run_is_refused_rather_than_numbered_in_another(self, _mail):
        with self.assertRaises(ValueError):
            formal_document_number('draft')


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ContinuityTests(IssuingMixin, TestCase):
    def test_a_complete_run_says_what_it_handed_out(self, _mail):
        self.issue('tax_invoice')
        self.issue('tax_invoice')
        self.issue('receipt')
        runs = {run.name: run for run in continuity(self.year)}
        tax = runs[f'TI-{self.year}']
        self.assertTrue(tax.complete)
        self.assertEqual(
            (tax.issued, tax.first, tax.last),
            (2, f'TI-{self.year}-000001', f'TI-{self.year}-000002'),
        )
        self.assertTrue(runs[f'RC-{self.year}'].complete)

    def test_a_number_no_document_carries_is_named(self, _mail):
        first = self.issue('tax_invoice')
        self.issue('tax_invoice')
        FormalDocument.objects.filter(pk=first.pk).delete()
        run = next(run for run in continuity(self.year) if run.series == 'TI')
        self.assertEqual(run.missing, (f'TI-{self.year}-000001',))

    def test_one_runs_numbers_never_fill_anothers_gap(self, _mail):
        self.issue('combined')
        # The lesson run handed out a number that no receipt carries. The manual
        # invoice-receipt 'IRM-…-000001' shares the first letters, not the run.
        DocumentSeries.objects.create(series='IR', year=self.year, counter=1)
        runs = {run.series: run for run in continuity(self.year)}
        self.assertEqual(runs['IR'].missing, (f'IR-{self.year}-000001',))
        self.assertTrue(runs['IRM'].complete)

    def test_the_closed_shared_run_is_still_checked(self, _mail):
        DocumentCounter.objects.create(year=self.year, counter=3)
        for n in (1, 3):
            FormalDocument.objects.create(
                document_number=f'{self.year}-{n:04d}', document_type='tax_invoice',
                client_type='existing', document_date=date(2026, 9, 1),
            )
        shared = next(run for run in continuity(self.year) if run.series == '')
        self.assertEqual(shared.missing, (f'{self.year}-0002',))
        self.assertEqual(shared.last, f'{self.year}-0003')

    def test_check_invoices_names_the_gap(self, _mail):
        first = self.issue('receipt')
        self.issue('receipt')
        FormalDocument.objects.filter(pk=first.pk).delete()
        out = StringIO()
        call_command('check_invoices', stdout=out)
        self.assertIn(f'חור בסדרה RC-{self.year}: RC-{self.year}-000001', out.getvalue())
