"""
Invisible direction marks in what a person pasted (7.10.2026).

Two tax invoices could not be opened or downloaded — a 500 — because their
customers' phones had been pasted with a direction mark (U+2069) nobody sees,
and the PDF's reordering stops on one. A document is drawn without them, and a
business customer's card is kept without them.
"""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from apps.core.direction_marks import DIRECTION_MARKS, strip_direction_marks, visual_order
from apps.core.models import Business, BusinessCategory
from apps.customers.models import BusinessCustomer
from apps.documents.document_pdf import build_document_layout, generate_document_pdf
from apps.documents.invoice_layout import wrapped_lines
from apps.documents.models import FormalDocument

PDI = '⁩'
LRI = '⁦'
PASTED_PHONE = f'050-123-4567{PDI}'


class DirectionMarksTests(SimpleTestCase):
    def test_every_mark_is_taken_out_and_nothing_else(self):
        self.assertEqual(strip_direction_marks(f'{LRI}050-123-4567{PDI}'), '050-123-4567')
        self.assertEqual(strip_direction_marks(''.join(DIRECTION_MARKS)), '')
        self.assertEqual(strip_direction_marks('גן הילדים "אור" 12-ב'), 'גן הילדים "אור" 12-ב')
        self.assertEqual(strip_direction_marks(None), '')

    def test_a_stray_mark_does_not_stop_the_reordering(self):
        # python-bidi itself refuses this line: "PDI not allowed here".
        self.assertEqual(visual_order(PASTED_PHONE), '050-123-4567')
        self.assertEqual(visual_order(f'טלפון {LRI}050{PDI}'), visual_order('טלפון 050'))

    def test_a_line_is_measured_and_broken_without_them(self):
        lines = wrapped_lines(f'{LRI}050-123-4567{PDI} שלוחה 3', 'Helvetica', 9, 400)
        self.assertEqual(lines, ['050-123-4567 שלוחה 3'])


class DocumentOfAPastedPhoneTests(TestCase):
    def setUp(self):
        self.business = Business.objects.create(name='עסק בדיקה')
        self.category = BusinessCategory.objects.create(business=self.business, name='כללי')

    def card(self, **extra):
        values = {'first_name': 'גן', 'last_name': 'הדגמה', 'company_number': '515151515',
                  'business': self.business, 'business_category': self.category}
        values.update(extra)
        return BusinessCustomer.objects.create(**values)

    def invoice(self, customer):
        doc = FormalDocument.objects.create(
            document_number='TI-2026-000007', document_type='tax_invoice', client_type='business',
            business_customer=customer, business=self.business, business_category=self.category,
            document_date=date(2026, 10, 7), subtotal=Decimal('15000.00'), vat_percent=Decimal('18'),
            vat_amount=Decimal('2700.00'), total_amount=Decimal('17700.00'),
        )
        doc.line_items.create(description='שירות', quantity=Decimal('1'), unit_price=Decimal('15000'),
                              line_total=Decimal('15000'))
        return doc

    def test_a_card_is_kept_without_the_marks(self):
        card = self.card(phone=PASTED_PHONE, first_name=f'{LRI}גן{PDI}', address=f'הרצל 1{PDI}')
        card.refresh_from_db()
        self.assertEqual((card.phone, card.first_name, card.address), ('050-123-4567', 'גן', 'הרצל 1'))

    def test_a_card_that_already_holds_one_still_prints(self):
        """The cards saved before today: their documents open as they are."""
        card = self.card()
        # Written past save(), as the rows already in the table were.
        BusinessCustomer.objects.filter(pk=card.pk).update(phone=PASTED_PHONE, address=f'{LRI}הרצל 1, תל אביב')
        card.refresh_from_db()
        self.assertIn(PDI, card.phone)
        doc = self.invoice(card)

        for copy in (True, False):
            pdf = generate_document_pdf(doc, copy=copy)
            self.assertTrue(pdf.startswith(b'%PDF'), copy)
        self.assertTrue(generate_document_pdf(doc, signed=True).startswith(b'%PDF'))

    def test_the_document_still_shows_the_phone(self):
        card = self.card()
        BusinessCustomer.objects.filter(pk=card.pk).update(phone=PASTED_PHONE)
        card.refresh_from_db()
        layout = build_document_layout(self.invoice(card), copy=True)
        phone = next(field.value for field in layout.document_fields if field.label == 'טלפון')
        self.assertEqual(strip_direction_marks(phone), '050-123-4567')
