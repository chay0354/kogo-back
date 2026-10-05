"""A document is one A4 page, whatever it holds (the owner's rule).

``invoice_layout.render_invoice_pdf`` draws a document the sample's way first.
When that is one page it is the result, untouched. Only a document that runs
over is drawn again with its body pressed: the white space closes first (every
letter keeps its size), and only then is the body scaled down evenly.

These tests read the rendered PDF back: how many pages, that every line is
still in the text, where the head and the foot landed, and how big the type is.

Set ``INVOICE_DESIGN_SAMPLES`` to a directory to have the long documents written
out as PDFs to look at (see test_invoice_design).
"""
import io
import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from pypdf import PdfReader
from reportlab import rl_config

from apps.core.models import Branch, City
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import BusinessCustomer, Family
from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf
from apps.documents import invoice_layout
from apps.documents.document_pdf import generate_document_pdf
from apps.documents.invoice_layout import (
    NATURAL, Field, Fit, InvoiceLayout, LineItem, Note, build_story, render_invoice_pdf,
)
from apps.documents.issuer import ARCHIVE_MARK, COPY_MARK, ORIGINAL_MARK, SIGNED_MARK
from apps.documents.models import DocumentLineItem, DocumentPayment, FormalDocument
from apps.documents.signing.selftest import sample_pdf
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_invoice_design import page_count, pdf_text, save_sample, squashed
from apps.store.invoice_pdf import generate_store_invoice_pdf
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

LONG_NOTE = (
    'הערה ארוכה מאוד שנכתבה כדי לבדוק שהמסמך נשאר בעמוד אחד גם כשהמשרד מוסיף '
    'הסבר מפורט ללקוח על תנאי התשלום, מועדי האספקה ופרטי ההתקשרות. '
)
FOOTER = 'קוגומלו - רחוב הדוגמה, פתח תקווה  |  office@example.com'
ROW_TYPE_SIZE = 7.9           # the table's type in the sample (invoice_layout 'td')


def line_code(index: int) -> str:
    return f'LINE-{index:03d}'


def sample_layout(rows: int = 1, *, seal: bool = False, payments: int = 1, long_notes: bool = False,
                  watermark: str = '') -> InvoiceLayout:
    """A document away from any model: `rows` lines, each with a code and an amount of its own."""
    items = [
        LineItem(
            description=f'{line_code(index)} שירות', quantity='1', unit_price='₪100.00',
            line_net='₪100.00', vat_rate='18%', line_gross=f'₪{1000 + index}.00',
        )
        for index in range(1, rows + 1)
    ]
    payment_fields = [Field('סטטוס', 'שולם')]
    for number in range(1, payments + 1):
        suffix = f' ({number})' if payments > 1 else ''
        payment_fields += [
            Field(f'אמצעי תשלום{suffix}', 'כרטיס אשראי'),
            Field(f'4 ספרות אחרונות{suffix}', f'42{number:02d}'),
            Field(f'אסמכתא / אישור{suffix}', f'77000{number:02d}'),
            Field(f'סכום ששולם{suffix}', '₪118.00'),
        ]
    # The one-line note comes last, so the foot of the body is a line whose place can be read back.
    notes = []
    if long_notes:
        notes.append(Note('הערה:', LONG_NOTE * 6))
    if seal:
        notes.append(Note('חתימה אלקטרונית:', f'{SIGNED_MARK}.'))
    notes.append(Note('מסמך ממוחשב:', 'מסמך זה הופק באופן דיגיטלי.'))
    return InvoiceLayout(
        title='חשבונית מס/קבלה - TI-2026-000900',
        copy_mark=ORIGINAL_MARK,
        document_fields=[
            Field('מספר מסמך', 'TI-2026-000900'),
            Field('תאריך המסמך', '02/09/2026'),
            Field('שם הלקוח', 'דנה לוי'),
            Field('טלפון', '050-7654321'),
            Field('פרטים', LONG_NOTE * 6 if long_notes else ''),
        ],
        business_fields=[Field('שם העסק', 'קוגומלו'), Field('עוסק מורשה / ח.פ.', '516504412')],
        items=items,
        payment_fields=payment_fields,
        payment_note=LONG_NOTE * 6 if long_notes else '',
        totals=[Field('סה"כ לפני מע"מ', '₪1000.00'), Field('מע"מ 18%', '₪180.00')],
        grand_value='₪1180.00',
        notes=notes,
        footer=FOOTER,
        watermark=watermark,
        signed_seal=seal,
        pdf_title='TI-2026-000900',
    )


def spans(pdf_bytes: bytes) -> list[tuple[str, float, float]]:
    """(text, height on the page, size as drawn) for every run of text on page one."""
    found: list[tuple[str, float, float]] = []

    def visit(text, cm, tm, _font, size):
        if text.strip():
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            found.append((text.strip(), round(y, 1), round(size * tm[3] * cm[3], 2)))

    PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text(visitor_text=visit)
    return found


def span(pdf_bytes: bytes, needle: str) -> tuple[str, float, float]:
    return next(item for item in spans(pdf_bytes) if needle in item[0])


def runs(pdf_bytes: bytes) -> list[str]:
    """
    Every run of text as it was drawn, on every page.

    ``extract_text`` drops a Latin code that sits in a Hebrew line (LINE-001,
    an email); the runs themselves are all there, so a search for a line of the
    table reads them.
    """
    found: list[str] = []
    for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
        page.extract_text(visitor_text=lambda text, *_rest: found.append(text.strip()) if text.strip() else None)
    return found


class OrdinaryDocumentUnchangedTests(SimpleTestCase):
    """A document that fits its page is the sample's drawing, to the byte."""

    def test_nothing_is_pressed_by_default(self):
        for normal, tightest in ((13, 10.5), (2.6, 0.75), (10.5, 3), (26, 10), (12.1, 9.6)):
            self.assertEqual(NATURAL.gap(normal, tightest), normal)
        self.assertEqual(NATURAL.wide(invoice_layout.CONTENT_WIDTH), invoice_layout.CONTENT_WIDTH)
        self.assertEqual(Fit(tight=1.0).gap(10.5, 3), 3)

    def test_a_short_document_is_the_flowing_drawing_itself(self):
        layout = sample_layout(rows=2, seal=True)
        with patch.object(rl_config, 'invariant', 1), \
                patch.object(invoice_layout, 'build_pressed_story', side_effect=AssertionError('pressed')):
            pdf = render_invoice_pdf(layout)
            flowing, pages = invoice_layout._draw(layout, build_story(layout))
        self.assertEqual(pages, 1)
        self.assertEqual(pdf, flowing)
        self.assertEqual(page_count(pdf), 1)

    def test_a_short_document_lands_where_it_did_before_the_rule(self):
        """
        The heights and sizes below were read off the same layout drawn by the
        code as it was before a document was ever pressed. A change here is a
        change to how every ordinary document looks.
        """
        pdf = render_invoice_pdf(sample_layout(rows=2, seal=True))
        for needle, height, size in PINNED_SHORT_DOCUMENT:
            _text, y, drawn = span(pdf, needle)
            self.assertAlmostEqual(y, height, delta=0.05, msg=needle)
            self.assertAlmostEqual(drawn, size, delta=0.01, msg=needle)

    def test_a_draft_that_fits_keeps_its_page(self):
        layout = sample_layout(rows=2, watermark='טיוטה')
        with patch.object(invoice_layout, 'build_pressed_story', side_effect=AssertionError('pressed')):
            pdf = render_invoice_pdf(layout)
        self.assertEqual(page_count(pdf), 1)
        self.assertIn('טיוטה', pdf_text(pdf))

    def test_the_signing_self_test_sample_is_one_page(self):
        self.assertEqual(page_count(sample_pdf()), 1)


# (text on the page, its height from the foot of the page, its size) — see the test above.
PINNED_SHORT_DOCUMENT = (
    ('office@example.com', 12.6, 12.0),          # the footer line
    ('חשבונית מס/קבלה', 685.8, 18.5),            # the document's name
    ('מקור', 663.5, 9.75),
    ('פרטי המסמך והלקוח', 616.3, 9.0),
    ('050-7654321', 541.3, 8.25),                # the last row of the details card
    ('פירוט העסקה', 487.0, 9.0),
    ('תיאור פריט / שירות', 452.3, 7.9),          # the table's header
    ('LINE-001', 416.6, 7.9),
    ('LINE-002', 384.6, 7.9),                    # 32pt a row
    ('פרטי תשלום', 335.0, 9.0),
    ('7700001', 258.5, 8.25),
    ('1180.00', 274.4, 17.25),                   # the amount due
    ('חתימה אלקטרונית:', 167.2, 7.1),
    ('מסמך ממוחשב:', 155.1, 7.1),
)


class LongDocumentOnePageTests(SimpleTestCase):
    """The drawing itself, on documents that used to run over."""

    def assert_every_line_is_there(self, pdf: bytes, rows: int):
        drawn = runs(pdf)
        for index in range(1, rows + 1):
            self.assertEqual(drawn.count(line_code(index)), 1, f'line {index} is missing')
            self.assertEqual(drawn.count(f'{1000 + index}.00'), 1, f"line {index}'s amount is missing")
        self.assertEqual(sum(1 for run in drawn if run.startswith('LINE-')), rows)

    def assert_head_and_foot_in_place(self, pdf: bytes):
        """The document's name, its mark and the footer sit where they do on every document."""
        for needle, height, size in PINNED_SHORT_DOCUMENT[:3]:
            _text, y, drawn = span(pdf, needle)
            self.assertAlmostEqual(y, height, delta=0.05, msg=needle)
            self.assertAlmostEqual(drawn, size, delta=0.01, msg=needle)

    def assert_inside_the_page(self, pdf: bytes):
        """The body starts under the מקור mark and its last line ends above the footer's room."""
        first = span(pdf, 'פרטי המסמך והלקוח')[1]
        last = span(pdf, 'מסמך ממוחשב:')[1]
        self.assertLess(first, 663.5 - 10)
        self.assertGreater(last, invoice_layout.PAGE_HEIGHT - invoice_layout.BODY_BOTTOM)
        self.assertLess(last, first)

    def test_fifteen_lines_fit_by_closing_white_space_alone(self):
        pdf = render_invoice_pdf(sample_layout(rows=15))
        save_sample('20-fifteen-lines', pdf)

        self.assertEqual(page_count(pdf), 1)
        self.assert_every_line_is_there(pdf, 15)
        self.assert_head_and_foot_in_place(pdf)
        self.assert_inside_the_page(pdf)
        # Spacing first: every letter keeps its size.
        self.assertEqual(span(pdf, line_code(15))[2], ROW_TYPE_SIZE)

    def test_thirty_lines_fit_on_one_page(self):
        pdf = render_invoice_pdf(sample_layout(rows=30))
        save_sample('21-thirty-lines', pdf)

        self.assertEqual(page_count(pdf), 1)
        self.assert_every_line_is_there(pdf, 30)
        self.assert_head_and_foot_in_place(pdf)
        self.assert_inside_the_page(pdf)
        # Scaled, but still type a person reads on paper.
        self.assertGreaterEqual(span(pdf, line_code(30))[2], 6.0)
        self.assertLess(span(pdf, line_code(30))[2], ROW_TYPE_SIZE)

    def test_forty_five_lines_fit_on_one_page(self):
        with self.assertLogs('apps.documents.invoice_layout', level='WARNING') as logged:
            pdf = render_invoice_pdf(sample_layout(rows=45))
        save_sample('22-forty-five-lines', pdf)

        self.assertEqual(page_count(pdf), 1)
        self.assert_every_line_is_there(pdf, 45)
        self.assert_head_and_foot_in_place(pdf)
        self.assert_inside_the_page(pdf)
        # Small enough to be worth a line in the log, and the log says how small.
        self.assertIn('TI-2026-000900', logged.output[0])
        self.assertGreaterEqual(span(pdf, line_code(45))[2], 4.5)

    def test_the_readability_limits_are_what_the_handover_says(self):
        """
        How many lines a signed original like the sample holds at which size
        of type (the sample's is 7.9pt). The handover to the owner quotes
        these; if the spacing changes, so must the numbers he was given.
        """
        def row_size(rows: int) -> float:
            return span(render_invoice_pdf(sample_layout(rows=rows, seal=True)), line_code(rows))[2]

        with self.assertLogs('apps.documents.invoice_layout', level='WARNING'):
            sizes = {rows: row_size(rows) for rows in (17, 18, 28, 39, 46, 60)}
        self.assertEqual(sizes[17], ROW_TYPE_SIZE)          # full size up to here
        self.assertLess(sizes[18], ROW_TYPE_SIZE)
        self.assertGreaterEqual(sizes[28], 6.0)             # comfortable on paper
        self.assertGreaterEqual(sizes[39], 5.0)             # small, still readable on paper
        self.assertGreaterEqual(sizes[46], 4.5)             # the edge of readable on paper
        self.assertLess(sizes[60], 4.0)                     # one page, but read on a screen, zoomed

    def test_the_body_is_scaled_evenly(self):
        """One scale for the table, the cards and the small print — not a size of its own for each."""
        pdf = render_invoice_pdf(sample_layout(rows=30))
        scale = span(pdf, line_code(1))[2] / ROW_TYPE_SIZE
        self.assertAlmostEqual(span(pdf, '050-7654321')[2] / 8.25, scale, delta=0.01)
        self.assertAlmostEqual(span(pdf, '1180.00')[2] / 17.25, scale, delta=0.01)
        self.assertAlmostEqual(span(pdf, 'מסמך ממוחשב:')[2] / 7.1, scale, delta=0.01)

    def test_long_notes_and_several_payments_fit_on_one_page(self):
        pdf = render_invoice_pdf(sample_layout(rows=3, payments=3, long_notes=True))
        save_sample('23-long-notes-three-payments', pdf)

        self.assertEqual(page_count(pdf), 1)
        self.assert_head_and_foot_in_place(pdf)
        self.assert_inside_the_page(pdf)
        tight = squashed(pdf)
        # The note is printed three times (details, payment note, small print), six repeats each.
        self.assertEqual(tight.count('הערהארוכהמאוד'), 18)
        for number in (1, 2, 3):
            self.assertIn(f'77000{number:02d}', tight, f'payment {number} is missing')
            self.assertIn(f'42{number:02d}', tight)
        self.assert_every_line_is_there(pdf, 3)

    def test_a_long_signed_document_keeps_its_seal(self):
        sealed = render_invoice_pdf(sample_layout(rows=40, seal=True))
        save_sample('24-forty-lines-signed', sealed)
        plain_layout = sample_layout(rows=40, seal=True)
        plain_layout.signed_seal = False
        plain = render_invoice_pdf(plain_layout)

        self.assertEqual(page_count(sealed), 1)
        self.assert_every_line_is_there(sealed, 40)
        self.assert_head_and_foot_in_place(sealed)
        self.assert_inside_the_page(sealed)
        self.assertIn(SIGNED_MARK, pdf_text(sealed), 'the signature line is missing')
        # The seal is drawn as shapes: it adds weight to the file and nothing to its text.
        self.assertEqual(pdf_text(sealed), pdf_text(plain))
        self.assertGreater(len(sealed), len(plain) + 1000)

    @signing_on()
    def test_a_pressed_document_signs_and_verifies(self):
        """The signing itself is untouched; this only shows a pressed page goes through it whole."""
        from apps.documents.signing.backends import get_backend
        from apps.documents.signing.signer import check_signed_pdf, sign_pdf

        backend = get_backend()
        certificate = backend.certificate()
        signed = sign_pdf(render_invoice_pdf(sample_layout(rows=40, seal=True)),
                          backend=backend, certificate=certificate)
        check_signed_pdf(signed, certificate)
        self.assertEqual(page_count(signed), 1)
        self.assert_every_line_is_there(signed, 40)

    def test_the_seal_is_drawn_once_on_the_pressed_page(self):
        from apps.documents.signature_seal import SignatureSeal

        invoice_layout.ensure_fonts_registered()
        layout = sample_layout(rows=40, seal=True)
        story, _body = invoice_layout.build_pressed_story(layout)
        with patch.object(SignatureSeal, '_draw_on_grid', autospec=True) as drawn:
            _pdf, pages = invoice_layout._draw(layout, story)
        self.assertEqual(pages, 1)
        self.assertEqual(drawn.call_count, 1)

    def test_hebrew_and_numbers_read_the_same_after_pressing(self):
        """
        The text layer of a pressed document is the flowing drawing's own: the
        same runs, each in the same order of letters — only the table's header
        and the footer, which the flowing drawing repeats on every page, differ
        in how often they appear.
        """
        layout = sample_layout(rows=45)
        pressed = render_invoice_pdf(layout)
        invoice_layout.ensure_fonts_registered()
        flowing, pages = invoice_layout._draw(layout, build_story(layout))
        self.assertGreater(pages, 1)

        # "עמוד 2", "עמוד 3" — the word and the number are runs of their own.
        page_marks = {'עמוד'} | {str(number) for number in range(2, pages + 1)}
        self.assertNotIn('עמוד', runs(pressed))
        self.assertEqual(set(runs(pressed)) - page_marks, set(runs(flowing)) - page_marks)
        text = pdf_text(pressed)
        for phrase in ('חשבונית מס/קבלה', 'פרטי המסמך והלקוח', 'דנה לוי', '050-7654321', '02/09/2026',
                       'תיאור פריט / שירות', 'סה"כ לתשלום', '1180.00', 'מסמך זה הופק באופן דיגיטלי.',
                       '516504412'):
            self.assertIn(phrase, text, f'{phrase} did not survive the pressing')

    def test_a_long_draft_is_one_page_and_still_marked(self):
        pdf = render_invoice_pdf(sample_layout(rows=25, watermark='טיוטה'))
        self.assertEqual(page_count(pdf), 1)
        self.assertIn('טיוטה', pdf_text(pdf))
        self.assert_every_line_is_there(pdf, 25)

    def test_a_block_taller_than_a_page_now_renders(self):
        """A details card taller than the page was a LayoutError — no document at all."""
        layout = sample_layout(rows=1)
        layout.document_fields.append(Field('פרטים', 'מילה ' * 900))
        pdf = render_invoice_pdf(layout)
        self.assertEqual(page_count(pdf), 1)
        self.assertEqual(squashed(pdf).count('מילה'), 900)

    def test_a_failed_pressing_never_costs_the_document(self):
        with patch.object(invoice_layout, '_press', side_effect=RuntimeError('boom')), \
                self.assertLogs('apps.documents.invoice_layout', level='ERROR'):
            pdf = render_invoice_pdf(sample_layout(rows=30))
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertGreater(page_count(pdf), 1)
        self.assertEqual(sum(1 for run in runs(pdf) if run.startswith('LINE-')), 30)


class EveryGeneratorOnePageTests(TestCase):
    """Each generator that draws through the shared layout, with more than a page of content."""

    def setUp(self):
        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='אם המושבות', city=city)

    def assert_one_page(self, pdf: bytes, number: str, mark: str):
        self.assertEqual(page_count(pdf), 1, f'{number} is not one page')
        text = pdf_text(pdf)
        self.assertIn(number, text, 'the document number is missing')
        self.assertIn(mark, text, f'{mark} is missing')

    # --- the lesson receipt (apps.customers.subscription_invoice_pdf) -----------------

    def lesson_receipt(self, lines: int) -> Invoice:
        family = Family.objects.create(
            name='משפחת כהן', branch=self.branch, phone='052-4611511', email='cohen@example.com',
        )
        invoice = Invoice.objects.create(
            invoice_number='IR-2026-000900', family=family, branch=self.branch,
            amount=Decimal('100.00') * lines, status='paid', payer_name='שחר כהן',
            payment_method='credit_card', tranzila_transaction_id='4411220', invoice_date=timezone.now(),
        )
        InvoiceActivityLog.objects.create(
            invoice=invoice, action='checkout_lines',
            details={'lines': [
                {'child_name': f'ילד {index}', 'description': f'CLASS-{index:03d} קפוארה', 'amount': '100.00'}
                for index in range(1, lines + 1)
            ]},
        )
        return invoice

    def test_a_lesson_receipt_with_thirty_lines(self):
        invoice = self.lesson_receipt(30)
        for kwargs, mark in (({}, ORIGINAL_MARK), ({'signed': True}, ORIGINAL_MARK),
                             ({'copy': True}, COPY_MARK), ({'archive': True}, ARCHIVE_MARK)):
            pdf = generate_subscription_invoice_pdf(invoice, **kwargs)
            self.assert_one_page(pdf, 'IR-2026-000900', mark)
            drawn = runs(pdf)
            for index in range(1, 31):
                self.assertEqual(drawn.count(f'CLASS-{index:03d}'), 1, f'line {index} is missing')
        save_sample('25-lesson-receipt-thirty-lines-signed', generate_subscription_invoice_pdf(invoice, signed=True))

    def test_an_ordinary_lesson_receipt_is_not_pressed(self):
        invoice = self.lesson_receipt(1)
        with patch.object(invoice_layout, 'build_pressed_story', side_effect=AssertionError('pressed')):
            for kwargs in ({}, {'signed': True}, {'copy': True}, {'archive': True}):
                self.assertEqual(page_count(generate_subscription_invoice_pdf(invoice, **kwargs)), 1)

    # --- the store sale (apps.store.invoice_pdf) --------------------------------------

    def test_a_store_sale_with_forty_five_lines(self):
        invoice = StoreInvoice.objects.create(
            invoice_number='ST-2026-000900', customer_name='רותי ניסן', customer_phone='050-1234567',
            customer_email='ruti@example.com', website_order_number='CG-260819-OJS2',
            shipping_address='הרצל 12, תל אביב', customer_notes=LONG_NOTE,
            total_amount=Decimal('2205.00'), amount_paid=Decimal('2205.00'),
            payment_method='credit_card', payment_status='completed', tranzila_confirmation_code='0091234',
        )
        for index in range(1, 46):
            product = StoreProduct.objects.create(
                name=f'PROD-{index:03d} חולצה', category='קוגומלו',
                sale_price=Decimal('49.00'), cost_price=Decimal('20.00'), stock_quantity=50,
            )
            StoreSale.objects.create(
                invoice=invoice, product=product, quantity=1, size='M',
                unit_price=Decimal('49.00'), total_price=Decimal('49.00'), payment_method='credit_card',
            )
        with self.assertLogs('apps.documents.invoice_layout', level='WARNING'):
            for kwargs, mark in (({}, ORIGINAL_MARK), ({'signed': True}, ORIGINAL_MARK),
                                 ({'copy': True}, COPY_MARK), ({'archive': True}, ARCHIVE_MARK)):
                pdf = generate_store_invoice_pdf(invoice, **kwargs)
                self.assert_one_page(pdf, 'ST-2026-000900', mark)
                drawn = runs(pdf)
                for index in range(1, 46):
                    self.assertEqual(drawn.count(f'PROD-{index:03d}'), 1, f'line {index} is missing')
                self.assertIn('2205.00', squashed(pdf))
            save_sample('26-store-sale-forty-five-lines-signed', generate_store_invoice_pdf(invoice, signed=True))

    # --- the hand-issued document (apps.documents.document_pdf): also the rental
    # --- receipt and Michal's documents, which are FormalDocuments drawn by it ----------

    def hand_issued(self, **extra) -> FormalDocument:
        customer = BusinessCustomer.objects.create(
            first_name='דנה', last_name='לוי', company_number='514123456', id_number='039123456',
            phone='050-7654321', email='dana@example.com', address='הרצל 12, תל אביב',
        )
        fields = {
            'document_number': 'TI-2026-000900', 'document_type': 'combined', 'client_type': 'business',
            'business_customer': customer, 'document_date': date(2026, 9, 2),
            'subtotal': Decimal('4500.00'), 'vat_amount': Decimal('810.00'), 'total_amount': Decimal('5310.00'),
            'vat_percent': Decimal('18'), 'branch': self.branch,
        }
        fields.update(extra)
        return FormalDocument.objects.create(**fields)

    def test_a_hand_issued_document_with_lines_payments_and_long_notes(self):
        doc = self.hand_issued(description=LONG_NOTE * 3, customer_notes=LONG_NOTE * 4)
        for index in range(1, 46):
            DocumentLineItem.objects.create(
                document=doc, description=f'SRV-{index:03d} ליווי והפקה', sku=f'SKU-{index:03d}',
                quantity=Decimal('1'), unit_price=Decimal('100.00'),
            )
        for number in (1, 2, 3):
            DocumentPayment.objects.create(
                document=doc, payment_method='credit_card', amount=Decimal('1770.00'),
                card_last_four=f'42{number:02d}', card_installments=3, reference=f'77000{number:02d}',
            )
        with self.assertLogs('apps.documents.invoice_layout', level='WARNING'):
            for kwargs, mark in (({}, ORIGINAL_MARK), ({'signed': True}, ORIGINAL_MARK),
                                 ({'copy': True}, COPY_MARK), ({'archive': True}, ARCHIVE_MARK)):
                pdf = generate_document_pdf(doc, **kwargs)
                self.assert_one_page(pdf, 'TI-2026-000900', mark)
                drawn = runs(pdf)
                tight = squashed(pdf)
                for index in range(1, 46):
                    self.assertEqual(drawn.count(f'SRV-{index:03d}'), 1, f'line {index} is missing')
                    self.assertEqual(drawn.count(f'SKU-{index:03d}'), 1, f"line {index}'s SKU is missing")
                for number in (1, 2, 3):
                    self.assertIn(f'77000{number:02d}', tight, f'payment {number} is missing')
                # The description once (3 repeats) and the customer's note once (4).
                self.assertEqual(tight.count('הערהארוכהמאוד'), 7)
            save_sample('27-hand-issued-forty-five-lines-signed', generate_document_pdf(doc, signed=True))

    def test_a_receipt_for_a_dozen_checks_is_one_page(self):
        doc = self.hand_issued(
            document_number='RC-2026-000900', document_type='receipt',
            subtotal=Decimal('14400.00'), vat_amount=Decimal('0.00'), total_amount=Decimal('14400.00'),
        )
        for month in range(1, 13):
            DocumentPayment.objects.create(
                document=doc, payment_method='check', amount=Decimal('1200.00'),
                reference=f'{9000000 + month}', check_date=date(2026, month, 10),
                check_bank='12', check_branch='345', check_account='678901',
            )
        pdf = generate_document_pdf(doc, signed=True)
        save_sample('28-receipt-for-twelve-checks-signed', pdf)

        self.assert_one_page(pdf, 'RC-2026-000900', ORIGINAL_MARK)
        self.assertEqual(squashed(pdf).count("פרטיהצ'ק"), 12)
        drawn = ' '.join(runs(pdf))
        for month in range(1, 13):
            self.assertIn(f'{9000000 + month}', drawn, f'check {month} is missing')

    def test_a_long_credit_note_and_a_long_draft_are_one_page(self):
        original = self.hand_issued()
        credit = self.hand_issued(
            document_number='CR-2026-000900', document_type='credit_invoice', business_customer=None,
            customer_name='דנה לוי', linked_document=original, linked_document_date=date(2026, 9, 2),
            credit_reason=LONG_NOTE,
        )
        draft = self.hand_issued(
            document_number='D-1A2B3C4D', document_type='draft', draft_target_type='tax_invoice',
            business_customer=None, customer_name='דנה לוי',
        )
        for doc, code in ((credit, 'CRD'), (draft, 'DRF')):
            for index in range(1, 31):
                DocumentLineItem.objects.create(
                    document=doc, description=f'{code}-{index:03d} שירות',
                    quantity=Decimal('1'), unit_price=Decimal('100.00'),
                )

        credit_pdf = generate_document_pdf(credit, signed=True)
        self.assert_one_page(credit_pdf, 'CR-2026-000900', ORIGINAL_MARK)
        self.assertIn('TI-2026-000900', pdf_text(credit_pdf), 'the credited document is not named')
        self.assertEqual(sum(1 for run in runs(credit_pdf) if run.startswith('CRD-')), 30)

        draft_pdf = generate_document_pdf(draft, signed=True)
        self.assertEqual(page_count(draft_pdf), 1)
        self.assertIn('טיוטה', pdf_text(draft_pdf))
        self.assertNotIn(ORIGINAL_MARK, pdf_text(draft_pdf), 'a draft must not be marked מקור')
        self.assertEqual(sum(1 for run in runs(draft_pdf) if run.startswith('DRF-')), 30)
