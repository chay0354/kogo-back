"""Every customer-facing PDF still carries what the law requires it to carry.

The three generators now share one drawing (``apps.documents.invoice_layout``),
so a change to the design can quietly drop a field. These tests read the
rendered PDF back and look for the markings and the numbers, on the documents
the business actually issues — including the shapes an old record has: a number
from before the per-type series, a sale with no line items, a customer with no
phone or email, a zero total, a credit.

Set ``INVOICE_DESIGN_SAMPLES`` to a directory to have the same fixtures written
out as PDFs to look at:

    INVOICE_DESIGN_SAMPLES=/tmp/out manage.py test apps.documents.tests.test_invoice_design
"""
import io
import os
import re
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from pypdf import PdfReader

from apps.core.models import Branch, City
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import BusinessCustomer, Child, Family
from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf
from apps.documents.document_pdf import generate_document_pdf
from apps.documents.issuer import (
    COMPUTERIZED_MARK, ISSUER_ADDRESS, ISSUER_COMPANY_NUMBER, ISSUER_NAME, ORIGINAL_MARK,
)
from apps.documents.models import DocumentLineItem, DocumentPayment, FormalDocument
from apps.store.invoice_pdf import generate_store_invoice_pdf
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

SAMPLES_DIR = os.environ.get('INVOICE_DESIGN_SAMPLES')
# What survives pypdf's extraction of the address (it stops at the first digit).
ADDRESS_HEAD = ISSUER_ADDRESS.split(' 5,')[0]


def pdf_text(pdf_bytes: bytes) -> str:
    """
    The rendered text, with the whitespace that line-breaking put in taken out.

    A value can land on a line of its own or be split by the column it sits in,
    so a search for a phrase has to survive that.
    """
    pages = PdfReader(io.BytesIO(pdf_bytes)).pages
    return re.sub(r'\s+', ' ', '\n'.join(page.extract_text() or '' for page in pages))


def squashed(pdf_bytes: bytes) -> str:
    """The same text with every space removed — for a phrase broken across lines."""
    return re.sub(r'\s+', '', pdf_text(pdf_bytes))


def page_count(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def save_sample(name: str, pdf_bytes: bytes) -> None:
    if not SAMPLES_DIR:
        return
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    with open(os.path.join(SAMPLES_DIR, f'{name}.pdf'), 'wb') as handle:
        handle.write(pdf_bytes)


class MandatoryMarkingsMixin:
    """The markings every tax document carries, whatever produced it."""

    def assert_statutory_markings(self, pdf_bytes: bytes, number: str, type_name: str):
        text = pdf_text(pdf_bytes)
        tight = squashed(pdf_bytes)
        # The number exactly as it was issued, never reshaped.
        self.assertIn(number, text, 'the document number is missing')
        self.assertIn(re.sub(r'\s+', '', type_name), tight, 'the document type is missing')
        self.assertIn(ORIGINAL_MARK, text, 'מקור is missing')
        self.assertIn(COMPUTERIZED_MARK, text, 'מסמך ממוחשב is missing')
        self.assertIn('עוסק מורשה', text, 'the registration wording is missing')
        self.assertIn(ISSUER_COMPANY_NUMBER, text, 'the registration number is missing')
        self.assertIn(ISSUER_NAME, text, "the business's name is missing")
        # pypdf gives up on a Hebrew run once a Latin digit interrupts it, so
        # the address is matched by its head here; poppler on the same file
        # shows the whole of ISSUER_ADDRESS, and IssuerBlockTests checks that
        # the full string is what the document was given to print.
        self.assertIn(ADDRESS_HEAD, text, "the business's address is missing")
        self.assertIn('מע"מ', text, 'the VAT wording is missing')

    def assert_money(self, pdf_bytes: bytes, *amounts: str):
        # The ₪ does not always survive extraction; the number always does.
        tight = squashed(pdf_bytes).replace('₪', '')
        for amount in amounts:
            self.assertIn(amount.replace('₪', ''), tight,
                          f'{amount} is missing from the document')


class LessonReceiptDesignTests(MandatoryMarkingsMixin, TestCase):
    """The receipt for a lesson charge — the most common document."""

    def setUp(self):
        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='אם המושבות', city=city)
        self.family = Family.objects.create(
            name='משפחת כהן', branch=self.branch,
            phone='052-4611511', email='cohen@example.com',
        )

    def make_invoice(self, **extra) -> Invoice:
        fields = {
            'invoice_number': 'IR-2026-000123',
            'family': self.family,
            'branch': self.branch,
            'amount': Decimal('236.00'),
            'status': 'paid',
            'payer_name': 'שחר כהן',
            'payer_phone': '052-4611511',
            'payer_email': 'shahar@example.com',
            'payment_method': 'credit_card',
            'tranzila_transaction_id': '4411220',
            'invoice_date': timezone.now(),
        }
        fields.update(extra)
        return Invoice.objects.create(**fields)

    def test_a_paid_lesson_receipt_carries_every_marking(self):
        pdf = generate_subscription_invoice_pdf(self.make_invoice())
        save_sample('01-lesson-receipt', pdf)

        self.assert_statutory_markings(pdf, 'IR-2026-000123', 'חשבונית מס / קבלה')
        text = pdf_text(pdf)
        self.assertIn('שחר כהן', text)
        # Before VAT, the VAT itself and the total: 236.00 / 1.18 = 200.00.
        self.assert_money(pdf, '₪200.00', '₪36.00', '₪236.00')
        self.assertIn('18%', text)
        self.assertIn('4411220', text, 'the confirmation number is missing')
        self.assertIn(timezone.localtime(timezone.now()).strftime('%d/%m/%Y'), text)

    def test_a_receipt_issued_late_says_so_with_both_dates(self):
        invoice = self.make_invoice(invoice_number='IR-2026-000124')
        InvoiceActivityLog.objects.create(
            invoice=invoice,
            action='issued_late',
            details={
                'money_received_at': '2026-08-19T20:36:00+03:00',
                'document_issued_at': '2026-09-02T09:15:00+03:00',
            },
        )

        pdf = generate_subscription_invoice_pdf(invoice)
        save_sample('05-lesson-receipt-late', pdf)

        self.assert_statutory_markings(pdf, 'IR-2026-000124', 'חשבונית מס / קבלה')
        tight = squashed(pdf)
        self.assertIn('הופקבאיחור', tight, 'the late-issue note is missing')
        self.assertIn('תאריךהפקתהמסמך', tight)
        self.assertIn('02/09/2026', tight, 'the date it was issued is missing')
        self.assertIn('תאריךקבלתהתשלום', tight)
        self.assertIn('19/08/2026', tight, 'the date the money came in is missing')

    def test_an_old_number_prints_exactly_as_it_was_issued(self):
        """A receipt from before the per-type series carries a UUID-shaped number."""
        invoice = self.make_invoice(
            invoice_number='INV-20260815-A1B2C3D4',
            payer_phone='', payer_email='', payment_method='',
            tranzila_transaction_id='', branch=None,
        )
        self.family.phone = ''
        self.family.email = ''
        self.family.save(update_fields=['phone', 'email'])

        pdf = generate_subscription_invoice_pdf(invoice)
        save_sample('06-lesson-receipt-old-number', pdf)

        self.assert_statutory_markings(pdf, 'INV-20260815-A1B2C3D4', 'חשבונית מס / קבלה')
        text = pdf_text(pdf)
        # A row with nothing to say is left out, never printed as a bare label.
        self.assertNotIn('אימייל', text)
        self.assertNotIn('אישור תשלום', text)
        self.assertNotIn('סניף', text)

    def test_a_zero_amount_receipt_renders(self):
        invoice = self.make_invoice(invoice_number='IR-2026-000125', amount=Decimal('0.00'))

        pdf = generate_subscription_invoice_pdf(invoice)
        save_sample('07-lesson-receipt-zero', pdf)

        self.assert_statutory_markings(pdf, 'IR-2026-000125', 'חשבונית מס / קבלה')
        self.assert_money(pdf, '₪0.00')

    def test_a_long_name_wraps_instead_of_spilling(self):
        invoice = self.make_invoice(
            invoice_number='IR-2026-000126',
            payer_name='משפחת אברמוביץ-רוזנצוויג בן שושן הכהן מפתח תקווה והסביבה',
        )

        pdf = generate_subscription_invoice_pdf(invoice)
        save_sample('08-lesson-receipt-long-name', pdf)

        self.assertIn('אברמוביץ', squashed(pdf))
        self.assertEqual(page_count(pdf), 1)


class StoreSaleDesignTests(MandatoryMarkingsMixin, TestCase):
    """A store sale."""

    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='חולצת קוגומלו', category='קוגומלו',
            sale_price=Decimal('49.00'), cost_price=Decimal('20.00'), stock_quantity=50,
        )

    def make_invoice(self, **extra) -> StoreInvoice:
        fields = {
            'invoice_number': 'ST-2026-000045',
            'customer_name': 'רותי ניסן',
            'customer_phone': '050-1234567',
            'customer_email': 'ruti@example.com',
            'website_order_number': 'CG-260819-OJS2',
            'total_amount': Decimal('149.00'),
            'amount_paid': Decimal('149.00'),
            'payment_method': 'credit_card',
            'payment_status': 'completed',
            'tranzila_confirmation_code': '0091234',
        }
        fields.update(extra)
        return StoreInvoice.objects.create(**fields)

    def test_a_store_sale_carries_every_marking(self):
        invoice = self.make_invoice()
        StoreSale.objects.create(
            invoice=invoice, product=self.product, quantity=1,
            unit_price=Decimal('49.00'), total_price=Decimal('49.00'),
            payment_method='credit_card',
        )
        StoreSale.objects.create(
            invoice=invoice, product=self.product, quantity=2, size='M',
            unit_price=Decimal('50.00'), total_price=Decimal('100.00'),
            payment_method='credit_card',
        )

        pdf = generate_store_invoice_pdf(invoice)
        save_sample('02-store-sale', pdf)

        self.assert_statutory_markings(pdf, 'ST-2026-000045', 'חשבונית מס / קבלה')
        text = pdf_text(pdf)
        self.assertIn('רותי ניסן', text)
        self.assertIn('CG-260819-OJS2', text, 'the order number is missing')
        self.assertIn('0091234', text, 'the confirmation number is missing')
        self.assertIn('חולצת קוגומלו', text)
        # 149.00 inclusive -> 126.27 + 22.73.
        self.assert_money(pdf, '₪126.27', '₪22.73', '₪149.00')

    def test_an_old_sale_with_no_lines_and_no_payment_details_renders(self):
        invoice = self.make_invoice(
            invoice_number='INV-202608-00020',
            customer_phone='', customer_email='', website_order_number=None,
            payment_method='', payment_status='pending',
            tranzila_confirmation_code='', amount_paid=Decimal('0.00'),
        )

        pdf = generate_store_invoice_pdf(invoice)
        save_sample('09-store-sale-old', pdf)

        self.assert_statutory_markings(pdf, 'INV-202608-00020', 'חשבונית מס / קבלה')
        text = pdf_text(pdf)
        self.assertNotIn('מספר הזמנה', text)
        self.assertNotIn('אישור תשלום', text)
        self.assert_money(pdf, '₪149.00')

    def test_a_refunded_sale_still_reads_correctly(self):
        invoice = self.make_invoice(
            invoice_number='ST-2026-000046',
            payment_status='refunded', refunded_amount=Decimal('149.00'),
        )

        pdf = generate_store_invoice_pdf(invoice)
        save_sample('10-store-sale-refunded', pdf)

        self.assert_statutory_markings(pdf, 'ST-2026-000046', 'חשבונית מס / קבלה')
        self.assertIn('זוכה', pdf_text(pdf))

    def test_a_sale_on_the_standing_order_is_a_transaction_invoice(self):
        invoice = self.make_invoice(
            invoice_number='ST-2026-000047', payment_method='monthly_billing',
        )

        pdf = generate_store_invoice_pdf(invoice)

        self.assertIn('חשבונית עסקה', pdf_text(pdf))
        self.assertIn('ST-2026-000047', pdf_text(pdf))

    def test_many_lines_flow_onto_a_second_page_with_the_header_repeated(self):
        invoice = self.make_invoice(
            invoice_number='ST-2026-000048', total_amount=Decimal('1960.00'),
            amount_paid=Decimal('1960.00'),
        )
        for index in range(40):
            StoreSale.objects.create(
                invoice=invoice, product=self.product, quantity=1,
                unit_price=Decimal('49.00'), total_price=Decimal('49.00'),
                payment_method='credit_card',
            )

        pdf = generate_store_invoice_pdf(invoice)
        save_sample('11-store-sale-many-lines', pdf)

        self.assertGreater(page_count(pdf), 1, 'forty lines should run onto a second page')
        pages = PdfReader(io.BytesIO(pdf)).pages
        for number, page in enumerate(pages, start=1):
            page_text = re.sub(r'\s+', '', page.extract_text() or '')
            self.assertIn('כמות', page_text, f'the table header is missing on page {number}')
            self.assertIn(re.sub(r'\s+', '', ISSUER_NAME), page_text,
                          f'the footer is missing on page {number}')
        self.assert_statutory_markings(pdf, 'ST-2026-000048', 'חשבונית מס / קבלה')


class HandIssuedDocumentDesignTests(MandatoryMarkingsMixin, TestCase):
    """The documents the office issues by hand."""

    def setUp(self):
        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='אם המושבות', city=city)
        self.customer = BusinessCustomer.objects.create(
            first_name='דנה', last_name='לוי',
            company_number='514123456', id_number='039123456',
            phone='050-7654321', email='dana@example.com',
            address='הרצל 12, תל אביב',
        )

    def make_document(self, **extra) -> FormalDocument:
        fields = {
            'document_number': 'TI-2026-000077',
            'document_type': 'combined',
            'client_type': 'business',
            'business_customer': self.customer,
            'document_date': date(2026, 9, 2),
            'subtotal': Decimal('1000.00'),
            'vat_amount': Decimal('180.00'),
            'total_amount': Decimal('1180.00'),
            'vat_percent': Decimal('18'),
            'branch': self.branch,
        }
        fields.update(extra)
        return FormalDocument.objects.create(**fields)

    def test_a_hand_issued_tax_invoice_receipt_carries_every_marking(self):
        doc = self.make_document()
        DocumentLineItem.objects.create(
            document=doc, description='ליווי והפקה', sku='SRV-1',
            quantity=Decimal('2'), unit_price=Decimal('500.00'),
        )
        DocumentPayment.objects.create(
            document=doc, payment_method='credit_card', amount=Decimal('1180.00'),
            card_last_four='4242', card_installments=3, reference='0091234',
        )

        pdf = generate_document_pdf(doc)
        save_sample('03-hand-issued-tax-invoice-receipt', pdf)

        self.assert_statutory_markings(pdf, 'TI-2026-000077', 'חשבונית מס/קבלה')
        text = pdf_text(pdf)
        self.assertIn('דנה לוי', text)
        self.assertIn('514123456', text, "the customer's company number is missing")
        self.assertIn('039123456', text, "the customer's ID number is missing")
        self.assertIn('4242', text, 'the card digits are missing')
        self.assertIn('0091234', text, 'the confirmation number is missing')
        self.assertIn('02/09/2026', squashed(pdf))
        self.assert_money(pdf, '₪1000.00', '₪180.00', '₪1180.00')

    def test_a_credit_note_names_the_document_it_credits_and_why(self):
        original = self.make_document(document_number='TI-2026-000077')
        credit = self.make_document(
            document_number='CR-2026-000012',
            document_type='credit_invoice',
            linked_document=original,
            linked_document_date=date(2026, 9, 2),
            credit_reason='ביטול הרשמה לחוג',
            subtotal=Decimal('1000.00'),
            vat_amount=Decimal('180.00'),
            total_amount=Decimal('1180.00'),
        )
        DocumentLineItem.objects.create(
            document=credit, description='זיכוי בגין ביטול',
            quantity=Decimal('1'), unit_price=Decimal('1000.00'),
        )

        pdf = generate_document_pdf(credit)
        save_sample('04-credit-note', pdf)

        self.assert_statutory_markings(pdf, 'CR-2026-000012', 'חשבונית מס זיכוי')
        tight = squashed(pdf)
        self.assertIn('TI-2026-000077', pdf_text(pdf),
                      'the credited document is not named')
        self.assertIn('02/09/2026', tight, "the credited document's date is missing")
        self.assertIn('ביטולהרשמהלחוג', tight, 'the reason for the credit is missing')
        self.assertIn('סה"כזיכוי', tight.replace('״', '"'))

    def test_a_credit_note_with_only_a_typed_original_number_still_names_it(self):
        credit = self.make_document(
            document_number='CR-2026-000013',
            document_type='credit_invoice',
            business_customer=None,
            client_type='business',
            customer_name='קונה מזדמן',
            linked_document_number='ST-2025-000900',
            linked_document_date=None,
            credit_reason='החזרת מוצר',
        )

        pdf = generate_document_pdf(credit)
        save_sample('12-credit-note-old-original', pdf)

        self.assert_statutory_markings(pdf, 'CR-2026-000013', 'חשבונית מס זיכוי')
        self.assertIn('ST-2025-000900', pdf_text(pdf))
        self.assertIn('קונה מזדמן', pdf_text(pdf))

    def test_a_document_priced_before_vat_shows_both_sides(self):
        doc = self.make_document(
            document_number='TI-2026-000078',
            document_type='tax_invoice',
            prices_include_vat=False,
        )
        DocumentLineItem.objects.create(
            document=doc, description='שירות', quantity=Decimal('1'),
            unit_price=Decimal('1000.00'),
        )

        pdf = generate_document_pdf(doc)

        self.assert_statutory_markings(pdf, 'TI-2026-000078', 'חשבונית מס')
        self.assert_money(pdf, '₪1000.00', '₪1180.00')

    def test_a_document_above_the_allocation_threshold_says_a_number_is_needed(self):
        doc = self.make_document(
            document_number='TI-2026-000079',
            subtotal=Decimal('9000.00'), vat_amount=Decimal('1620.00'),
            total_amount=Decimal('10620.00'),
        )

        pdf = generate_document_pdf(doc)
        save_sample('13-hand-issued-above-threshold', pdf)

        tight = squashed(pdf)
        self.assertIn('מספרהקצאה', tight)
        self.assertIn('נדרשלעסקהזו', tight)
        # pypdf cannot read past the digits inside the sentence; the sentence
        # itself is checked on the data the document was given.
        from apps.documents.document_pdf import build_document_layout
        note = ' '.join(f'{n.lead} {n.text}' for n in build_document_layout(doc).notes)
        self.assertIn('5,000', note)

    def test_a_document_for_a_child_with_no_family_details_renders(self):
        family = Family.objects.create(name='משפחה', branch=self.branch)
        child = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        doc = self.make_document(
            document_number='RC-2026-000005', document_type='receipt',
            client_type='existing', business_customer=None, child=child,
        )

        pdf = generate_document_pdf(doc)
        save_sample('14-hand-issued-receipt-child', pdf)

        self.assert_statutory_markings(pdf, 'RC-2026-000005', 'קבלה')
        self.assertIn('נועה כהן', pdf_text(pdf))

    def test_a_draft_is_marked_as_one_and_never_claims_to_be_a_tax_document(self):
        doc = self.make_document(
            document_number='D-1A2B3C4D', document_type='draft',
            draft_target_type='tax_invoice',
        )

        pdf = generate_document_pdf(doc)
        save_sample('15-draft', pdf)

        text = pdf_text(pdf)
        self.assertIn('טיוטה', text)
        self.assertNotIn(ORIGINAL_MARK, text, 'a draft must not be marked מקור')

    def test_a_document_with_many_lines_repeats_the_table_header(self):
        doc = self.make_document(
            document_number='TI-2026-000080',
            subtotal=Decimal('4000.00'), vat_amount=Decimal('720.00'),
            total_amount=Decimal('4720.00'),
        )
        for index in range(35):
            DocumentLineItem.objects.create(
                document=doc,
                description=f'שורת שירות מספר {index + 1} עם תיאור ארוך במיוחד '
                            f'שנועד לבדוק גלישה נכונה של טקסט עברי בעמודה',
                sku=f'SKU-{index + 1}', quantity=Decimal('1'), unit_price=Decimal('114.28'),
            )

        pdf = generate_document_pdf(doc)
        save_sample('16-hand-issued-many-lines', pdf)

        self.assertGreater(page_count(pdf), 1)
        for number, page in enumerate(PdfReader(io.BytesIO(pdf)).pages, start=1):
            page_text = re.sub(r'\s+', '', page.extract_text() or '')
            self.assertIn('כמות', page_text, f'the table header is missing on page {number}')

    def test_a_vat_exempt_document_says_so(self):
        doc = self.make_document(
            document_number='TI-2026-000081', vat_exempt=True,
            vat_amount=Decimal('0.00'), total_amount=Decimal('1000.00'),
        )

        pdf = generate_document_pdf(doc)

        self.assertIn('פטור', pdf_text(pdf))

    def test_a_document_with_a_due_date_and_a_discount_renders(self):
        doc = self.make_document(
            document_number='TI-2026-000082',
            document_type='transaction_invoice',
            discount_amount=Decimal('100.00'),
            due_date=date(2026, 9, 2) + timedelta(days=30),
            payment_terms='שוטף + 30',
            customer_notes='התשלום יבוצע בהעברה בנקאית.',
        )

        pdf = generate_document_pdf(doc)
        save_sample('17-transaction-invoice', pdf)

        tight = squashed(pdf)
        self.assertIn('הנחה', tight)
        self.assertIn('02/10/2026', tight)


class IssuerBlockTests(TestCase):
    """The business block every document prints, checked on the data itself."""

    def test_all_three_generators_print_the_same_issuer_details(self):
        from apps.documents.invoice_document import business_fields

        printed = {field.label: field.value for field in business_fields()}

        self.assertEqual(printed['שם העסק'], ISSUER_NAME)
        self.assertEqual(printed['עוסק מורשה / ח.פ.'], ISSUER_COMPANY_NUMBER)
        self.assertEqual(printed['כתובת'], ISSUER_ADDRESS)
        self.assertIn('עוסק מורשה', ' '.join(printed))
        self.assertEqual(ISSUER_COMPANY_NUMBER, '516504412')

    def test_every_generator_hands_the_layout_the_full_address(self):
        from apps.customers.subscription_invoice_pdf import build_subscription_invoice_layout
        from apps.documents.document_pdf import build_document_layout
        from apps.store.invoice_pdf import build_store_invoice_layout

        family = Family.objects.create(name='משפחה')
        lesson = build_subscription_invoice_layout(Invoice.objects.create(
            invoice_number='IR-2026-000300', family=family,
            amount=Decimal('100.00'), status='paid', invoice_date=timezone.now(),
        ))
        store = build_store_invoice_layout(StoreInvoice.objects.create(
            invoice_number='ST-2026-000300', customer_name='לקוח',
            total_amount=Decimal('100.00'), payment_method='cash', payment_status='completed',
        ))
        hand = build_document_layout(FormalDocument.objects.create(
            document_number='TI-2026-000300', document_type='tax_invoice',
            client_type='business', customer_name='לקוח', document_date=date(2026, 9, 2),
            subtotal=Decimal('100.00'), vat_amount=Decimal('18.00'),
            total_amount=Decimal('118.00'),
        ))

        for layout in (lesson, store, hand):
            values = [field.value for field in layout.business_fields]
            self.assertIn(ISSUER_ADDRESS, values, layout.title)
            self.assertIn(ISSUER_COMPANY_NUMBER, values, layout.title)
            self.assertIn(ISSUER_NAME, values, layout.title)
            self.assertIn('עוסק מורשה', ' '.join(f.label for f in layout.business_fields))
            self.assertTrue(layout.footer.endswith('cogo.co.il') or '@' in layout.footer)


class LayoutRobustnessTests(TestCase):
    """The drawing itself, away from any model."""

    def test_a_missing_logo_file_does_not_break_a_document(self):
        from unittest.mock import patch

        from apps.documents import invoice_layout

        family = Family.objects.create(name='משפחה')
        invoice = Invoice.objects.create(
            invoice_number='IR-2026-000200', family=family,
            amount=Decimal('100.00'), status='paid', invoice_date=timezone.now(),
        )
        with patch.object(invoice_layout, 'LOGO_PATH', '/no/such/file.png'):
            pdf = generate_subscription_invoice_pdf(invoice)

        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertIn('IR-2026-000200', pdf_text(pdf))

    def test_a_long_unbroken_value_is_split_rather_than_spilling(self):
        from apps.documents.invoice_layout import ensure_fonts_registered, wrapped_lines

        ensure_fonts_registered()
        lines = wrapped_lines('a' * 400, 'Heebo', 8.25, 100)

        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) < 400 for line in lines))
