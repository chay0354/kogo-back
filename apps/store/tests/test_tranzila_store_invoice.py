from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.core.tranzila_service import TranzilaService
from apps.store.tranzila_store_invoice import issue_store_tranzila_document
from apps.store.models import StoreInvoice, StoreProduct, StoreSale


class TranzilaStoreInvoiceTests(TestCase):
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

    def test_parse_billing_document_response(self):
        parsed = TranzilaService.parse_billing_document_response({
            'status_code': 0,
            'document': {
                'id': '12345',
                'number': '1000099',
                'retrieval_key': 'abc123',
            },
        })
        self.assertTrue(parsed['success'])
        self.assertEqual(parsed['doc_id'], '12345')
        self.assertEqual(parsed['document_number'], '1000099')
        self.assertIn('abc123', parsed['pdf_url'])

    @override_settings(TRANZILA_BILLING_TERMINAL='test-terminal')
    @patch.object(TranzilaService, 'create_formal_document')
    def test_issue_store_tranzila_document(self, mock_create):
        mock_create.return_value = {
            'status_code': 0,
            'document': {'id': '99', 'number': '1001', 'retrieval_key': 'rk'},
        }

        formal = issue_store_tranzila_document(self.invoice)
        self.assertIsNotNone(formal)
        self.assertTrue(formal.tranzila_issued)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.formal_document_id, formal.id)
