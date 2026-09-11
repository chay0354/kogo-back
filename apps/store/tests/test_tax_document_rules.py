"""An issued invoice is a tax document, and the rules that follow from that.

Two of them are enforced here: it cannot be rewritten or deleted (סעיף 23(ב)),
and its face carries the registration line, "מקור" and "מסמך ממוחשב"
(תקנה 9א(א)(1)–(2), סעיף 18ב(א)).
"""
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from pypdf import PdfReader
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.documents.issuer import ISSUER_COMPANY_NUMBER, ISSUER_LINE, ISSUER_NAME
from apps.store.invoice_pdf import generate_store_invoice_pdf
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

User = get_user_model()
URL = '/api/v1/store/invoices/'


def _pdf_text(pdf_bytes: bytes) -> str:
    return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf_bytes)).pages)


class IssuedInvoiceIsImmutableTests(APITestCase):
    """סעיף 23(ב): a correction is a further document, never an edit."""

    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='אם המושבות', city=city)
        user = User.objects.create_user(username='manager-tax@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.invoice = StoreInvoice.objects.create(
            invoice_number='ST-TAX-1',
            customer_name='רותי ניסן',
            customer_email='ruti@example.com',
            total_amount=Decimal('149.00'),
            payment_method='credit_card',
            payment_status='completed',
        )

    def test_patch_is_refused(self):
        response = self.client.patch(f'{URL}{self.invoice.id}/', {'total_amount': '1.00'}, format='json')

        self.assertEqual(response.status_code, 405)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_amount, Decimal('149.00'))

    def test_put_is_refused(self):
        response = self.client.put(f'{URL}{self.invoice.id}/', {'total_amount': '1.00'}, format='json')

        self.assertEqual(response.status_code, 405)

    def test_delete_is_refused(self):
        response = self.client.delete(f'{URL}{self.invoice.id}/')

        self.assertEqual(response.status_code, 405)
        self.assertTrue(StoreInvoice.objects.filter(pk=self.invoice.pk).exists())

    def test_reading_an_invoice_still_works(self):
        response = self.client.get(f'{URL}{self.invoice.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['invoice_number'], 'ST-TAX-1')

    def test_refund_is_still_reachable(self):
        """Locking the verbs must not lock the one supported way to reverse an invoice."""
        response = self.client.post(f'{URL}{self.invoice.id}/refund/', {'reason': 'בדיקה'}, format='json')

        # 400 here (no Tranzila transaction on this fixture), never 405.
        self.assertNotEqual(response.status_code, 405)


class StoreInvoicePdfMarkingsTests(APITestCase):
    """תקנה 9א(א)(1)–(2) and סעיף 18ב(א) — printed on the document, not the letterhead."""

    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='חולצה', category='קוגומלו',
            sale_price=Decimal('49.00'), cost_price=Decimal('20.00'), stock_quantity=5,
        )
        self.invoice = StoreInvoice.objects.create(
            invoice_number='ST-PDF-1',
            customer_name='רוכש בדיקה',
            total_amount=Decimal('49.00'),
            payment_method='credit_card',
            payment_status='completed',
        )
        StoreSale.objects.create(
            invoice=self.invoice, product=self.product, quantity=1,
            unit_price=Decimal('49.00'), total_price=Decimal('49.00'),
            payment_method='credit_card',
        )

    def test_the_issuer_line_names_the_registration_number(self):
        self.assertIn(ISSUER_COMPANY_NUMBER, ISSUER_LINE)
        self.assertIn('עוסק מורשה', ISSUER_LINE)

    def test_pdf_carries_the_issuer_line_and_both_marks(self):
        text = _pdf_text(generate_store_invoice_pdf(self.invoice))

        self.assertIn('מקור', text)
        self.assertIn('מסמך ממוחשב', text)
        # pypdf truncates a mixed RTL + Latin-digit run after the Hebrew head, so
        # the issuer paragraph is matched by its opening — poppler on the same file
        # shows the full 'קוגומלו גרופ בע"מ · עוסק מורשה 516504412'. Its tail is
        # covered by the constant, above.
        self.assertIn(ISSUER_NAME, text)
