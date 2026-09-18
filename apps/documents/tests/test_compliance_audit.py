"""
The invoicing compliance audit of 18.9.2026 (docs/COMPLIANCE-2026-09-18-INVOICING.md),
as tests: what each document must carry, and that every run kogo numbers reaches
the uniform-structure files.
"""
import io
import zipfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.customers.financial_models import InvoiceActivityLog
from apps.customers.models import BusinessCustomer, Parent
from apps.customers.subscription_invoice_pdf import (
    ORIGINAL_PRODUCED, build_subscription_invoice_layout, reproduce_subscription_invoice_pdf,
)
from apps.documents import service
from apps.documents.document_pdf import build_document_layout
from apps.documents.invoice_document import allocation_required
from apps.documents.issuer import COPY_MARK, ORIGINAL_MARK
from apps.documents.models import DocumentLineItem, DocumentPayment, FormalDocument
from apps.documents.serializers import FormalDocumentListSerializer
from apps.documents.tests.test_register import RegisterFixture
from apps.store.invoice_pdf import build_store_invoice_layout

CREATE = '/api/v1/documents/documents/create-document/'
EXPORT = '/api/v1/documents/documents/uniform-export/'


def fields(layout, block='document_fields'):
    return {field.label: field.value for field in getattr(layout, block)}


def notes(layout):
    return ' '.join(f'{note.lead} {note.text}' for note in layout.notes)


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CreditNoteNamesTheOriginalTests(RegisterFixture, APITestCase):
    def credit(self, **details):
        self.client.force_authenticate(self.manager)
        payload = {
            'document_type': 'credit_invoice',
            'client_type': 'existing',
            'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': '2026-09-18',
                'credit_reason': 'ביטול שיעור',
                'credit_amount_before_vat': '100.00',
                **details,
            },
        }
        return self.client.post(CREATE, payload, format='json')

    def test_a_credit_note_without_the_original_number_is_refused(self, _mail):
        response = self.credit()

        self.assertEqual(response.status_code, 400)
        self.assertIn('המסמך המקורי', str(response.data))
        self.assertFalse(FormalDocument.objects.filter(document_type='credit_invoice').exists())

    def test_a_lesson_receipt_credited_by_hand_prints_its_number_and_date(self, _mail):
        self.lesson_receipt('IR-2026-000007', day=9)

        response = self.credit(linked_invoice_id='IR-2026-000007')

        self.assertEqual(response.status_code, 201, response.data)
        doc = FormalDocument.objects.get(document_type='credit_invoice')
        self.assertEqual(doc.linked_document_date, date(2026, 8, 9))
        printed = fields(build_document_layout(doc))
        self.assertEqual(printed['זיכוי עבור מסמך'], 'IR-2026-000007')
        self.assertEqual(printed['תאריך המסמך המקורי'], '09/08/2026')

    def test_a_store_sale_credited_by_hand_prints_its_date(self, _mail):
        sale = self.store_sale(day=10)

        self.assertEqual(self.credit(linked_invoice_id=sale.invoice_number).status_code, 201)

        self.assertEqual(
            FormalDocument.objects.get(document_type='credit_invoice').linked_document_date, date(2026, 8, 10),
        )

    def test_a_number_from_the_previous_software_carries_the_date_typed_with_it(self, _mail):
        response = self.credit(linked_invoice_id='30112', linked_document_date='2026-08-30')

        self.assertEqual(response.status_code, 201, response.data)
        doc = FormalDocument.objects.get(document_type='credit_invoice')
        self.assertEqual((doc.linked_document_number, doc.linked_document_date), ('30112', date(2026, 8, 30)))


@override_settings(TRANZILA_BILLING_TERMINAL='')
class VatRoundsHalfUpToTheAgoraTests(RegisterFixture, TestCase):
    def test_half_an_agora_of_vat_rounds_up_as_on_every_other_document(self):
        # 100.25 × 18% = 18.045: half-even made it 18.04, the lesson receipts' rule 18.05.
        doc = service.create_invoice({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {
                'document_date': '2026-09-18',
                'line_items': [{'description': 'סדנה', 'quantity': 1, 'price': '100.25'}],
            },
        }, 'tax_invoice')

        self.assertEqual((doc.vat_amount, doc.total_amount), (Decimal('18.05'), Decimal('118.30')))

    @patch('apps.documents.service._email_credit_note')
    def test_a_credit_note_rounds_the_same_way(self, _mail):
        doc = service.create_credit_invoice({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': '2026-09-18', 'credit_reason': 'x', 'linked_invoice_id': 'TI-2026-000001',
                'credit_amount_before_vat': '100.25',
            },
        })

        self.assertEqual(doc.vat_amount, Decimal('18.05'))


class EveryRunReachesTheUniformFilesTests(RegisterFixture, APITestCase):
    """IR, ST, SD, RT and the five hand-issued runs, each as the type code it is reported under."""

    def formal(self, number, kind, **extra):
        doc = FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type=extra.pop('client_type', 'existing'),
            child=extra.pop('child', self.kid), document_date=date(2026, 8, 11),
            subtotal=Decimal('100.00'), vat_amount=Decimal('0.00') if kind in ('receipt', 'transaction_invoice') else Decimal('18.00'),
            total_amount=Decimal('100.00') if kind in ('receipt', 'transaction_invoice') else Decimal('118.00'),
            vat_exempt=kind in ('receipt', 'transaction_invoice'), **extra,
        )
        if kind != 'receipt':
            DocumentLineItem.objects.create(document=doc, description='שירות', quantity=1, unit_price=Decimal('100.00'))
        return doc

    def test_all_nine_runs_are_in_the_export(self):
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        sale = self.store_sale()
        on_account = self.store_sale(amount='120.00', method='monthly_billing', status='pending')
        tenant = BusinessCustomer.objects.create(first_name='שוכר', last_name='בע"מ', company_number='514000000')
        rental = self.formal('RT-2026-000001', 'combined', client_type='business', child=None, business_customer=tenant)
        DocumentPayment.objects.create(document=rental, payment_method='credit_card', amount=Decimal('118.00'), card_last_four='4242')
        self.formal('TI-2026-000001', 'tax_invoice')
        combined = self.formal('IRM-2026-000001', 'combined')
        DocumentPayment.objects.create(document=combined, payment_method='cash', amount=Decimal('118.00'))
        receipt = self.formal('RC-2026-000001', 'receipt')
        DocumentPayment.objects.create(
            document=receipt, payment_method='check', amount=Decimal('100.00'), reference='1234',
            check_date=date(2026, 10, 1), check_bank='12', check_branch='600', check_account='123456',
        )
        self.formal('TX-2026-000001', 'transaction_invoice')
        self.credit_note()
        self.client.force_authenticate(self.manager)

        response = self.client.get(EXPORT, {'month': '2026-08'})

        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        outer = zipfile.ZipFile(io.BytesIO(response.content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(next(n for n in outer.namelist() if n.endswith('BKMVDATA.zip')))))
        records = inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines()
        headers = {line[25:45].strip(): line[22:25] for line in records if line.startswith('C100')}
        self.assertEqual(headers, {
            'IR-2026-000001': '320',
            sale.invoice_number: '320',
            on_account.invoice_number: '300',
            'RT-2026-000001': '320',
            'TI-2026-000001': '305',
            'IRM-2026-000001': '320',
            'RC-2026-000001': '400',
            'TX-2026-000001': '300',
            'CR-2026-000001': '330',
        })


class ReceiptPrintsWhatHoraa5AsksTests(RegisterFixture, TestCase):
    """הוראה 5: the payer's name, and for a check its number, bank, branch and due date."""

    def test_a_check_receipt_prints_the_check(self):
        receipt = FormalDocument.objects.create(
            document_number='RC-2026-000001', document_type='receipt', client_type='existing',
            child=self.kid, document_date=date(2026, 9, 18), subtotal=Decimal('300'),
            total_amount=Decimal('300'), vat_exempt=True,
        )
        DocumentPayment.objects.create(
            document=receipt, payment_method='check', amount=Decimal('300'), reference='000123',
            check_date=date(2026, 10, 1), check_bank='12', check_branch='600', check_account='456789',
        )

        printed = fields(build_document_layout(receipt), 'payment_fields')

        self.assertEqual(
            printed["פרטי הצ'ק"], "מס' 000123 · בנק 12 · סניף 600 · חשבון 456789 · לפירעון 01/10/2026",
        )

    def test_a_document_for_a_child_names_the_parent_who_paid(self):
        Parent.objects.create(family=self.family, first_name='דנה', last_name='כהן', is_primary=True)
        doc = FormalDocument.objects.create(
            document_number='RC-2026-000002', document_type='receipt', client_type='existing',
            child=self.kid, document_date=date(2026, 9, 18), total_amount=Decimal('50'), vat_exempt=True,
        )

        printed = fields(build_document_layout(doc))

        self.assertEqual((printed['שם הלקוח'], printed['שם המשלם']), ('נועה כהן', 'דנה כהן'))


class AllocationNumberRulesTests(RegisterFixture, TestCase):
    """
    §38(א1) לחוק מע"מ: above ₪5,000 before VAT ("עולה על"), on a tax invoice to an
    עוסק מורשה; never on a credit note (the Tax Authority's FAQ).
    """

    def doc(self, number, kind='tax_invoice', subtotal='9000', business=True):
        customer = BusinessCustomer.objects.create(first_name='לקוח', last_name='עסקי', company_number='514000001') if business else None
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='business' if business else 'existing',
            business_customer=customer, child=None if business else self.kid, document_date=date(2026, 9, 18),
            subtotal=Decimal(subtotal), vat_amount=Decimal('0'), total_amount=Decimal(subtotal),
        )

    def test_exactly_the_threshold_needs_none(self):
        self.assertFalse(allocation_required(Decimal('5000.00')))
        self.assertTrue(allocation_required(Decimal('5000.01')))

    def test_a_business_tax_invoice_above_the_threshold_needs_one(self):
        doc = self.doc('TI-2026-000010')

        self.assertTrue(FormalDocumentListSerializer(doc).data['allocation_required'])
        self.assertIn('טרם הוזן', notes(build_document_layout(doc)))

    def test_a_credit_note_needs_none_and_says_nothing(self):
        doc = self.doc('CR-2026-000010', kind='credit_invoice')

        self.assertFalse(FormalDocumentListSerializer(doc).data['allocation_required'])
        self.assertNotIn('מספר הקצאה', notes(build_document_layout(doc)))

    def test_a_private_customer_above_the_threshold_is_told_none_is_needed(self):
        doc = self.doc('TI-2026-000011', business=False)

        self.assertFalse(FormalDocumentListSerializer(doc).data['allocation_required'])
        self.assertIn('אינו עוסק מורשה', notes(build_document_layout(doc)))
        self.assertNotIn('טרם הוזן', notes(build_document_layout(doc)))

    def test_a_lesson_receipt_above_the_threshold_does_not_ask_for_one(self):
        invoice = self.lesson_receipt('IR-2026-000050', amount='7080.00')

        self.assertNotIn('טרם הוזן', notes(build_subscription_invoice_layout(invoice)))


class OriginalOnceThenCopyTests(RegisterFixture, APITestCase):
    """נספח ה'(א)(4): "מקור" on one print only; every other print says "העתק"."""

    def test_the_first_download_of_an_unmailed_receipt_is_the_original_and_the_next_a_copy(self):
        invoice = self.lesson_receipt('IR-2026-000060')

        with patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf', side_effect=lambda layout: layout.copy_mark.encode()):
            first = reproduce_subscription_invoice_pdf(invoice, user=self.manager)
            second = reproduce_subscription_invoice_pdf(invoice, user=self.manager)

        self.assertEqual((first.decode(), second.decode()), (ORIGINAL_MARK, COPY_MARK))
        self.assertEqual(InvoiceActivityLog.objects.filter(invoice=invoice, action=ORIGINAL_PRODUCED).count(), 1)

    def test_a_mailed_receipt_downloads_as_a_copy(self):
        invoice = self.lesson_receipt('IR-2026-000061')
        type(invoice).objects.filter(pk=invoice.pk).update(email_sent_at=timezone.now())

        with patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf', side_effect=lambda layout: layout.copy_mark.encode()):
            self.assertEqual(reproduce_subscription_invoice_pdf(invoice).decode(), COPY_MARK)

    def test_the_download_endpoint_marks_the_second_print_a_copy(self):
        invoice = self.lesson_receipt('IR-2026-000062')
        self.client.force_authenticate(self.manager)
        url = f'/api/v1/customers/invoices/{invoice.id}/pdf/'

        with patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf', side_effect=lambda layout: layout.copy_mark.encode()):
            marks = [self.client.get(url).content.decode() for _ in range(2)]

        self.assertEqual(marks, [ORIGINAL_MARK, COPY_MARK])


class StoreReprintIsTheSaleAsIssuedTests(RegisterFixture, TestCase):
    def test_a_refunded_sale_reprints_as_paid_without_a_credit_on_its_face(self):
        sale = self.store_sale(amount='49.00')
        type(sale).objects.filter(pk=sale.pk).update(payment_status='refunded', refunded_amount=Decimal('49.00'))
        sale.refresh_from_db()

        layout = build_store_invoice_layout(sale, copy=True)
        printed = fields(layout, 'payment_fields')

        self.assertEqual(layout.copy_mark, COPY_MARK)
        self.assertEqual((printed['סטטוס'], printed['שולם'], printed['יתרה לתשלום']), ('שולם', '₪49.00', '₪0.00'))
        self.assertNotIn('זוכה', printed)

    def test_a_sale_on_monthly_billing_carries_no_allocation_line(self):
        sale = self.store_sale(amount='120.00', method='monthly_billing', status='pending')

        self.assertNotIn('מספר הקצאה', notes(build_store_invoice_layout(sale)))


@override_settings(TRANZILA_BILLING_TERMINAL='', TRANZILA_TERMINAL='payment-terminal')
class NoSecondDocumentFromTranzilaTests(RegisterFixture, TestCase):
    def test_a_store_sale_is_not_issued_again_through_the_payment_terminal(self):
        from apps.core.tranzila_service import TranzilaService
        from apps.store.tranzila_store_invoice import issue_store_tranzila_document

        sale = self.store_sale()
        with patch.object(TranzilaService, 'create_formal_document') as create:
            self.assertIsNone(issue_store_tranzila_document(sale))

        create.assert_not_called()


class UniformExportDatesTests(EveryRunReachesTheUniformFilesTests):
    def test_a_document_typed_with_an_earlier_date_reports_when_it_was_produced(self):
        doc = self.formal('TI-2026-000001', 'tax_invoice')  # dated 11.8.2026
        produced = timezone.make_aware(timezone.datetime(2026, 8, 20, 9, 30))
        FormalDocument.objects.filter(pk=doc.pk).update(created_at=produced)
        self.client.force_authenticate(self.manager)

        response = self.client.get(EXPORT, {'month': '2026-08'})

        outer = zipfile.ZipFile(io.BytesIO(response.content))
        inner = zipfile.ZipFile(io.BytesIO(outer.read(next(n for n in outer.namelist() if n.endswith('BKMVDATA.zip')))))
        header = next(line for line in inner.read('BKMVDATA.TXT').decode('iso-8859-8').splitlines() if line.startswith('C100'))
        # 1205 (position 46) is the day the system produced it; 1230 (position 401) the day printed on it.
        self.assertEqual((header[45:53], header[53:57], header[400:408]), ('20260820', '0930', '20260811'))

    def test_all_nine_runs_are_in_the_export(self):
        """Run once, in the parent class."""
