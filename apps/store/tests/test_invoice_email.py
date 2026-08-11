from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.store.invoice_email import build_store_invoice_email, send_store_invoice_email
from apps.store.models import StoreInvoice, StoreProduct, StoreSale


@override_settings(
    EMAIL_HOST='smtp.test',
    DEFAULT_FROM_EMAIL='noreply@kogomalo.com',
)
class StoreInvoiceEmailTests(TestCase):
    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='חולצה',
            category='קוגומלו',
            sale_price=Decimal('49.00'),
            cost_price=Decimal('20.00'),
            stock_quantity=5,
        )
        self.invoice = StoreInvoice.objects.create(
            customer_name='Test User',
            customer_phone='0500000000',
            customer_email='buyer@example.com',
            total_amount=Decimal('49.00'),
            payment_method='credit_card',
            payment_status='completed',
            website_order_number='CG-260811-TEST',
        )
        StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('49.00'),
            total_price=Decimal('49.00'),
            payment_method='credit_card',
        )

    def test_build_email_contains_invoice_and_order(self):
        subject, text, html = build_store_invoice_email(self.invoice)
        self.assertIn(self.invoice.invoice_number, subject)
        self.assertIn('CG-260811-TEST', text)
        self.assertIn('חולצה', html)

    @patch('apps.store.invoice_email.send_mail')
    def test_send_marks_sent_at(self, mock_send):
        ok = send_store_invoice_email(self.invoice)
        self.assertTrue(ok)
        mock_send.assert_called_once()
        self.invoice.refresh_from_db()
        self.assertIsNotNone(self.invoice.invoice_email_sent_at)

    @patch('apps.store.invoice_email.send_mail')
    def test_send_is_idempotent(self, mock_send):
        self.invoice.invoice_email_sent_at = timezone.now()
        self.invoice.save(update_fields=['invoice_email_sent_at'])
        ok = send_store_invoice_email(self.invoice)
        self.assertTrue(ok)
        mock_send.assert_not_called()
