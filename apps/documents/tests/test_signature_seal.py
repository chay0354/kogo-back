"""The round seal on a signed original (apps/documents/signature_seal.py)."""
import io
import re
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from pypdf import PdfReader

from apps.customers.subscription_invoice_pdf import build_subscription_invoice_layout
from apps.documents.invoice_document import business_fields, computerized_note, footer_line, signature_note
from apps.documents.invoice_layout import (
    FONT_BOLD, FONT_REGULAR, Field, InvoiceLayout, LineItem, ensure_fonts_registered, render_invoice_pdf,
)
from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_NAME, SIGNED_MARK
from apps.documents.signature_seal import CENTRE_TEXT, TOP_TEXT, _outlines
from apps.documents.tests.test_signing_delivery import LessonPaymentMixin
from apps.store.invoice_pdf import build_store_invoice_layout
from apps.store.models import StoreInvoice


def pdf_text(pdf: bytes) -> str:
    pages = PdfReader(io.BytesIO(pdf)).pages
    return re.sub(r'\s+', ' ', '\n'.join(page.extract_text() or '' for page in pages)).strip()


def sample_layout(seal: bool) -> InvoiceLayout:
    return InvoiceLayout(
        title='קבלה - SEAL-1',
        document_fields=[Field('מספר מסמך', 'SEAL-1')],
        business_fields=business_fields(),
        items=[LineItem(description='שיעור', quantity='1', unit_price='100.00',
                        line_net='84.75', vat_rate='18%', line_gross='100.00')],
        grand_value='100.00',
        notes=[computerized_note(), signature_note()],
        footer=footer_line(),
        signed_seal=seal,
    )


class SealDrawingTests(SimpleTestCase):
    def test_every_letter_on_the_seal_has_an_outline_in_the_font(self):
        ensure_fonts_registered()
        bold, regular = _outlines(FONT_BOLD), _outlines(FONT_REGULAR)
        for char in set(TOP_TEXT + CENTRE_TEXT + ISSUER_NAME) - {' '}:
            self.assertTrue(bold.contours(char), char)
        for char in set(f'ח.פ. {ISSUER_COMPANY_NUMBER}') - {' '}:
            self.assertTrue(regular.contours(char), char)

    def test_the_seal_is_drawn_as_shapes_and_adds_nothing_to_the_text(self):
        plain = render_invoice_pdf(sample_layout(seal=False))
        sealed = render_invoice_pdf(sample_layout(seal=True))
        # Letters round a circle would land in the text layer as loose characters.
        self.assertEqual(pdf_text(sealed), pdf_text(plain))
        self.assertGreater(len(sealed), len(plain) + 1000)

    def test_a_seal_that_cannot_be_drawn_never_costs_the_document(self):
        with patch('apps.documents.signature_seal._outlines', side_effect=RuntimeError('no font')):
            pdf = render_invoice_pdf(sample_layout(seal=True))
        self.assertIn(SIGNED_MARK, pdf_text(pdf))


class SealOnlyOnTheSignedOriginalTests(LessonPaymentMixin, TestCase):
    def test_a_lesson_receipt(self):
        invoice = self.lesson_receipt()
        self.assertTrue(build_subscription_invoice_layout(invoice, signed=True).signed_seal)
        self.assertFalse(build_subscription_invoice_layout(invoice, copy=True, signed=True).signed_seal)
        self.assertFalse(build_subscription_invoice_layout(invoice).signed_seal)

    def test_a_store_sale(self):
        invoice = StoreInvoice.objects.create(
            customer_name='קונה', total_amount=Decimal('49.00'),
            payment_method='credit_card', payment_status='completed',
        )
        self.assertTrue(build_store_invoice_layout(invoice, signed=True).signed_seal)
        self.assertFalse(build_store_invoice_layout(invoice, copy=True, signed=True).signed_seal)
        self.assertFalse(build_store_invoice_layout(invoice).signed_seal)
