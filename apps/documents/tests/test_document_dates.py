"""
The date a document issued by hand may carry (H, owner decision D3): not in the
future, in the tax year it is issued in, and not before the latest date already
in its run — so every run's numbers stay in the order of their dates. And a
date the server fills in is Israel's day, not UTC's.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family
from apps.documents import service
from apps.documents.models import DocumentSeries, FormalDocument
from apps.documents.numbering import DocumentDateError, israel_today, validate_document_date
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'
LIST = '/api/v1/documents/documents/'


class Fixture:
    def setUp(self):
        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        family = Family.objects.create(name='משפחה', branch=branch)
        self.kid = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.today = israel_today()
        self.year = self.today.year

    def earlier_this_year(self):
        if self.today == date(self.year, 1, 1):
            self.skipTest('no earlier day in this tax year on 1 January')
        return self.today - timedelta(days=1)

    def issued(self, number, kind, day):
        """A document already in a run, dated `day` (a number far from the counter's, so they never clash)."""
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='existing', child=self.kid,
            document_date=day, subtotal=Decimal('100'), vat_amount=Decimal('18'), total_amount=Decimal('118'),
        )

    def counter(self, series):
        row = DocumentSeries.objects.filter(series=series, year=self.year).first()
        return row.counter if row else 0

    def invoice_payload(self, kind, day):
        payload = {
            'document_type': kind,
            'client_type': 'existing',
            'child_id': str(self.kid.id),
            'invoice_details': {
                'document_date': str(day),
                'line_items': [{'description': 'סדנה', 'quantity': 1, 'price': '100.00'}],
            },
        }
        if kind == 'combined':
            # ₪100 + 18%, paid in cash (G: an invoice-receipt names its payments).
            payload['invoice_details']['payments'] = [{'method': 'מזומן', 'amount': '118.00'}]
        return payload


class ValidateDocumentDateTests(Fixture, TestCase):
    def test_today_is_fine(self):
        self.assertEqual(validate_document_date('tax_invoice', self.today), self.today)

    def test_a_date_as_text_is_read(self):
        self.assertEqual(validate_document_date('TI', self.today.isoformat()), self.today)

    def test_tomorrow_is_refused(self):
        with self.assertRaisesMessage(DocumentDateError, 'בעתיד'):
            validate_document_date('tax_invoice', self.today + timedelta(days=1))

    def test_a_date_in_the_previous_tax_year_is_refused(self):
        with self.assertRaisesMessage(DocumentDateError, f'אינו בשנת המס {self.year}'):
            validate_document_date('receipt', date(self.year - 1, 12, 31))

    def test_a_date_before_the_latest_in_the_run_is_refused(self):
        earlier = self.earlier_this_year()
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        with self.assertRaisesMessage(DocumentDateError, 'לפי סדר התאריכים'):
            validate_document_date('tax_invoice', earlier)
        # Named by its run's code, the same.
        with self.assertRaises(DocumentDateError):
            validate_document_date('TI', earlier)

    def test_the_same_day_as_the_latest_is_fine(self):
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        self.assertEqual(validate_document_date('tax_invoice', self.today), self.today)

    def test_another_run_does_not_hold_a_date_back(self):
        earlier = self.earlier_this_year()
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        self.assertEqual(validate_document_date('receipt', earlier), earlier)

    def test_the_issue_day_can_be_given(self):
        # Issued on 2.3 of some year: 1.3 of that year is fine, 3.3 is in its future.
        self.assertEqual(validate_document_date('TX', date(2030, 3, 1), date(2030, 3, 2)), date(2030, 3, 1))
        with self.assertRaises(DocumentDateError):
            validate_document_date('TX', date(2030, 3, 3), date(2030, 3, 2))


@override_settings(TRANZILA_BILLING_TERMINAL='')
@mock.patch('apps.documents.service._email_credit_note')
class DatesThroughTheApiTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('dates-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def test_a_future_dated_invoice_is_refused_in_hebrew_and_uses_no_number(self, _mail):
        before = self.counter('TI')
        res = self.client.post(CREATE, self.invoice_payload('tax_invoice', self.today + timedelta(days=1)), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('בעתיד', res.data['error'])
        self.assertEqual(self.counter('TI'), before)
        self.assertFalse(FormalDocument.objects.filter(document_type='tax_invoice').exists())

    def test_every_hand_issued_type_is_checked(self, _mail):
        tomorrow = str(self.today + timedelta(days=1))
        payloads = [
            self.invoice_payload('tax_invoice', tomorrow),
            self.invoice_payload('transaction_invoice', tomorrow),
            self.invoice_payload('combined', tomorrow),
            {
                'document_type': 'receipt', 'client_type': 'existing', 'child_id': str(self.kid.id),
                'document_date': tomorrow,
                'receipt_details': {'payment_method': 'מזומן', 'cash_amount': '50.00'},
            },
            {
                'document_type': 'credit_invoice', 'client_type': 'existing', 'child_id': str(self.kid.id),
                'credit_invoice_details': {
                    'document_date': tomorrow, 'linked_invoice_id': '30112',
                    'linked_document_date': str(self.today), 'credit_reason': 'ביטול',
                    'credit_amount_before_vat': '10.00',
                },
            },
        ]
        for payload in payloads:
            with self.subTest(payload['document_type']):
                res = self.client.post(CREATE, payload, format='json')
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn('בעתיד', res.data['error'])
        self.assertEqual(FormalDocument.objects.count(), 0)

    def test_an_invoice_dated_before_the_runs_latest_is_refused(self, _mail):
        earlier = self.earlier_this_year()
        self.issued(f'IRM-{self.year}-000900', 'combined', self.today)
        res = self.client.post(CREATE, self.invoice_payload('combined', earlier), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('לפי סדר התאריכים', res.data['error'])

    def test_a_receipt_with_no_date_is_dated_on_israels_calendar(self, _mail):
        # 22:30 UTC on 25.9 is already 01:30 on 26.9 in Israel.
        late = datetime(self.year, 9, 25, 22, 30, tzinfo=dt_timezone.utc)
        payload = {
            'document_type': 'receipt', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'receipt_details': {'payment_method': 'מזומן', 'cash_amount': '50.00'},
        }
        with mock.patch('django.utils.timezone.now', return_value=late):
            res = self.client.post(CREATE, payload, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['document_date'], f'{self.year}-09-26')

    def test_a_draft_is_not_approved_into_a_run_that_already_holds_a_later_date(self, _mail):
        drafted = self.client.post(
            CREATE, {**self.invoice_payload('draft', self.today), 'draft_target_type': 'tax_invoice'}, format='json',
        )
        self.assertEqual(drafted.status_code, 201, drafted.data)
        # A later date already in the run — typed before this rule existed.
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today + timedelta(days=2))
        before = self.counter('TI')

        res = self.client.post(f"{LIST}{drafted.data['id']}/finalize/")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('לפי סדר התאריכים', res.data['error'])
        self.assertEqual(FormalDocument.objects.get(pk=drafted.data['id']).document_type, 'draft')
        self.assertEqual(self.counter('TI'), before)


@override_settings(TRANZILA_BILLING_TERMINAL='')
class PlanPathsSkipTheRulesTests(Fixture, TestCase):
    """The check and cash plans date a month's document on its own day (WS-3 fixes that); they are let through."""

    def test_a_plan_invoice_may_be_dated_before_the_runs_latest(self):
        earlier = self.earlier_this_year()
        self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        payload = self.invoice_payload('tax_invoice', earlier)

        with self.assertRaises(DocumentDateError):
            service.create_invoice(payload, 'tax_invoice')
        doc = service.create_invoice(payload, 'tax_invoice', skip_date_rules=True)

        doc.refresh_from_db()
        self.assertEqual(doc.document_date, earlier)
