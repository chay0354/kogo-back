from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.core.tranzila_service import parse_create_document_response
from apps.store.models import StoreInvoice, StoreProduct, StoreSale
from apps.store.tranzila_document import issue_store_tranzila_document


class ParseCreateDocumentResponseTests(TestCase):
    def test_parses_nested_document(self):
        result = parse_create_document_response({
            'status_code': 0,
            'document': {
                'id': '6995',
                'number': '1000010',
                'retrieval_key': 'abc123',
            },
        })
        self.assertTrue(result['success'])
        self.assertEqual(result['doc_id'], '6995')
        self.assertEqual(result['document_number'], '1000010')
        self.assertIn('abc123', result['pdf_url'])


@override_settings(TRANZILA_BILLING_TERMINAL='billing-term')
class IssueStoreTranzilaDocumentTests(TestCase):
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
            customer_email='buyer@example.com',
            total_amount=Decimal('49.00'),
            payment_method='credit_card',
            payment_status='completed',
            website_order_number='CG-260811-TEST',
            tranzila_transaction_id='23255',
        )
        StoreSale.objects.create(
            invoice=self.invoice,
            product=self.product,
            quantity=1,
            unit_price=Decimal('49.00'),
            total_price=Decimal('49.00'),
            payment_method='credit_card',
        )

    @patch('apps.store.tranzila_document.TranzilaService.create_formal_document')
    def test_issues_and_persists_tranzila_fields(self, mock_create):
        mock_create.return_value = {
            'success': True,
            'doc_id': '123',
            'retrieval_key': 'rk',
            'document_number': '9001',
            'pdf_url': 'https://my.tranzila.com/api/get_financial_document/rk',
        }
        ok = issue_store_tranzila_document(self.invoice, card_last4='4242')
        self.assertTrue(ok)
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.tranzila_issued)
        self.assertEqual(self.invoice.tranzila_doc_id, '123')
        self.assertEqual(self.invoice.pdf_url, 'https://my.tranzila.com/api/get_financial_document/rk')
        mock_create.assert_called_once()
        payload = mock_create.call_args.kwargs
        self.assertEqual(payload['document_type'], 'IR')
        self.assertEqual(payload['client_email'], 'buyer@example.com')
        self.assertEqual(payload['payments'][0]['txnindex'], 23255)
