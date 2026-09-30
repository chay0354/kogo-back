"""
A חשבונית מס/קבלה issued by hand records how much was paid each way (G).

Each method used to be written for the whole total — paid half in cash and half
by check, the document reported twice its money in the uniform file. Its rows
now come to its total exactly, and carry what identifies each payment.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents.document_pdf import build_document_layout
from apps.documents.models import DocumentPayment, FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'


def fields(layout, block='payment_fields'):
    return {field.label: field.value for field in getattr(layout, block)}


@override_settings(TRANZILA_BILLING_TERMINAL='')
class InvoiceReceiptPaymentsTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('irm-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def post(self, price='200.00', **details):
        payload = self.invoice_payload('combined', self.today)
        payload['invoice_details']['line_items'][0]['price'] = price
        payload['invoice_details'].pop('payments')  # each test says how it was paid
        payload['invoice_details'].update(details)
        return self.client.post(CREATE, payload, format='json')

    def test_two_methods_are_two_rows_that_add_up_to_the_total(self):
        # ₪200 + 18% = ₪236: ₪100 in cash and a ₪136 check.
        check_day = self.today + timedelta(days=30)
        res = self.post(payments=[
            {'method': 'מזומן', 'amount': '100.00'},
            {'method': "צ'ק", 'amount': '136.00', 'check_number': '000123', 'check_bank': '12',
             'check_branch': '600', 'check_account': '456789', 'check_date': str(check_day),
             'check_crossed': True},
        ])

        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.total_amount, Decimal('236.00'))
        rows = {p.payment_method: p for p in doc.payments.all()}
        self.assertEqual(set(rows), {'cash', 'check'})
        self.assertEqual(sum(p.amount for p in rows.values()), doc.total_amount)
        check = rows['check']
        self.assertEqual(
            (check.amount, check.reference, check.check_bank, check.check_branch, check.check_account,
             check.check_date, check.check_crossed),
            (Decimal('136.00'), '000123', '12', '600', '456789', check_day, True),
        )
        self.assertFalse(rows['cash'].check_crossed)

    def test_a_card_and_a_transfer_keep_what_identifies_them(self):
        paid_on = self.today
        res = self.post(payments=[
            {'method': 'credit_card', 'amount': '200.00', 'card_last_four': '4242', 'card_brand': 'ויזה',
             'installments': 3, 'reference': '0091234', 'paid_on': str(paid_on)},
            {'method': 'bank_transfer', 'amount': '36.00', 'reference': 'TRF-77', 'paid_on': str(paid_on)},
        ])

        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        card = doc.payments.get(payment_method='credit_card')
        self.assertEqual(
            (card.card_last_four, card.card_brand, card.card_installments, card.reference, card.paid_on),
            ('4242', 'ויזה', 3, '0091234', paid_on),
        )
        transfer = doc.payments.get(payment_method='bank_transfer')
        self.assertEqual((transfer.amount, transfer.reference, transfer.paid_on), (Decimal('36.00'), 'TRF-77', paid_on))
        printed = fields(build_document_layout(doc))
        self.assertEqual(printed['סוג כרטיס (1)'], 'ויזה')
        self.assertIn('0.00', printed['יתרה לתשלום'])

    def test_rows_that_do_not_add_up_are_refused_and_use_no_number(self):
        before = self.counter('IRM')
        res = self.post(payments=[
            {'method': 'מזומן', 'amount': '236.00'},
            {'method': "צ'ק", 'amount': '236.00'},
        ])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('אינם שווים לסכום החשבונית', res.data['error'])
        self.assertEqual(self.counter('IRM'), before)
        self.assertFalse(FormalDocument.objects.filter(document_type='combined').exists())

    def test_an_invoice_receipt_without_any_payment_is_refused(self):
        res = self.post()
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('אמצעי תשלום', str(res.data))

    def test_a_row_of_zero_is_refused(self):
        res = self.post(payments=[{'method': 'מזומן', 'amount': '0.00'}])
        self.assertEqual(res.status_code, 400, res.data)

    def test_the_older_payload_with_one_method_still_pays_it_all(self):
        res = self.post(payment_methods=["צ'ק"], check_crossed=True)
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        row = doc.payments.get()
        self.assertEqual((row.payment_method, row.amount, row.check_crossed), ('check', Decimal('236.00'), True))

    def test_the_older_payload_with_several_methods_is_refused(self):
        res = self.post(payment_methods=['מזומן', "צ'ק"])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('הסכום של כל אחד', str(res.data))
        self.assertFalse(FormalDocument.objects.filter(document_type='combined').exists())

    def test_withholding_and_the_rows_come_to_the_total(self):
        res = self.post(withholding_amount='36.00', payments=[{'method': 'bank_transfer', 'amount': '200.00'}])

        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.withholding_amount, Decimal('36.00'))
        printed = fields(build_document_layout(doc))
        self.assertIn('36.00', printed['ניכוי במקור'])
        self.assertIn('0.00', printed['יתרה לתשלום'])

        refused = self.post(withholding_amount='36.00', payments=[{'method': 'bank_transfer', 'amount': '236.00'}])
        self.assertEqual(refused.status_code, 400, refused.data)

    def test_the_uniform_file_reports_each_payment_once(self):
        from apps.documents.period_report import _row_from
        from apps.documents.uniform_export import _manual

        res = self.post(withholding_amount='6.00', payments=[
            {'method': 'מזומן', 'amount': '100.00'},
            {'method': "צ'ק", 'amount': '130.00', 'check_number': '55'},
        ])
        self.assertEqual(res.status_code, 201, res.data)
        doc = (
            FormalDocument.objects.prefetch_related('line_items', 'payments', 'store_invoices')
            .select_related('business', 'business_category').get(pk=res.data['id'])
        )

        uniform = _manual(_row_from(doc), doc, {})

        self.assertEqual(sorted(p.amount for p in uniform.payments), [Decimal('100.00'), Decimal('130.00')])
        self.assertEqual(uniform.withholding_tax, Decimal('6.00'))
        self.assertEqual(sum(p.amount for p in uniform.payments) + uniform.withholding_tax, doc.total_amount)


class DoubledPaymentsReportTests(Fixture, TestCase):
    def doubled(self, number, total, *methods):
        doc = self.issued(number, 'combined', date(self.year, 1, 5))
        FormalDocument.objects.filter(pk=doc.pk).update(total_amount=Decimal(total))
        for method in methods:
            DocumentPayment.objects.create(document=doc, payment_method=method, amount=Decimal(total))
        return doc

    def test_it_lists_the_invoice_receipts_whose_rows_exceed_their_total(self):
        self.doubled(f'IRM-{self.year}-000001', '236.00', 'cash', 'check')
        self.doubled(f'IRM-{self.year}-000002', '118.00', 'cash')
        out = StringIO()

        call_command('report_doubled_irm_payments', year=self.year, stdout=out)

        text = out.getvalue()
        self.assertIn('checked dated in', text)
        self.assertIn('Payment rows above the document total: 1', text)
        self.assertIn(f'IRM-{self.year}-000001,{self.year}-01-05,236.00,472.00,2,236.00', text)
        self.assertNotIn(f'IRM-{self.year}-000002', text)
        # Read-only: nothing it looked at changed.
        self.assertEqual(DocumentPayment.objects.count(), 3)
        self.assertNotIn('נועה', text)

    def test_nothing_to_report(self):
        out = StringIO()
        call_command('report_doubled_irm_payments', stdout=out)
        self.assertIn('Payment rows above the document total: 0', out.getvalue())


@override_settings(TRANZILA_BILLING_TERMINAL='')
class ReceiptCardBrandTests(Fixture, TestCase):
    def test_a_receipt_by_card_keeps_the_cards_brand(self):
        from apps.documents import service

        doc = service.create_receipt({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'receipt_details': {'payment_method': 'אשראי', 'card_amount': '50.00', 'card_last_four': '1111',
                                'card_brand': 'מאסטרקארד'},
        })
        self.assertEqual(doc.payments.get().card_brand, 'מאסטרקארד')


class PaymentDateRowTests(TestCase):
    """The day a payment was made is printed only when it is not the document's own day."""

    def document(self, paid_on):
        doc = FormalDocument.objects.create(
            document_number='RC-2026-000321', document_type='receipt', client_type='existing',
            document_date=date(2026, 9, 10), subtotal=Decimal('100.00'), total_amount=Decimal('100.00'),
        )
        DocumentPayment.objects.create(document=doc, payment_method='bank_transfer', amount=Decimal('100.00'),
                                       reference='TRF-1', paid_on=paid_on)
        return doc

    def test_a_payment_made_on_the_documents_day_adds_no_row(self):
        printed = fields(build_document_layout(self.document(date(2026, 9, 10))))
        self.assertEqual(printed.get('תאריך התשלום', ''), '')

    def test_a_payment_made_on_another_day_names_it(self):
        printed = fields(build_document_layout(self.document(date(2026, 9, 3))))
        self.assertEqual(printed['תאריך התשלום'], '03/09/2026')
