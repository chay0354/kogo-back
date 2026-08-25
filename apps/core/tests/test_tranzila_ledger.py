"""Tranzila documents + transactions ledger for the invoices page."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.core.tranzila_ledger import (
    list_ledger_documents,
    list_ledger_payments,
    normalize_tranzila_document,
    normalize_tranzila_transaction,
)
from apps.customers.models import Payment, TranzilaTransaction
from apps.documents.models import FormalDocument


class TranzilaNormalizeTest(TestCase):
    def test_invoice_receipt_is_paid(self):
        row = normalize_tranzila_document({
            'id': '6252',
            'number': '1001956',
            'type': 'IR',
            'action': '1',
            'total_charge_amount': 10,
            'created_at': '2026-08-24 14:14:31',
            'retrieval_key': 'abc',
        }, customer_name='דני כהן')
        self.assertEqual(row['document_number'], '1001956')
        self.assertEqual(row['customer_name'], 'דני כהן')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['total_amount'], 10.0)
        self.assertIn('abc', row['pdf_url'])

    def test_approved_transaction(self):
        row = normalize_tranzila_transaction({
            'index': '41044',
            'transaction_date': '2026-08-24',
            'transaction_time': '15:00:01',
            'amount': '5.00',
            'processor_response_code': '000',
            'authorization_number': 'CONF1',
            'contact': 'הורה בדיקה',
        })
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['amount'], 5.0)
        self.assertEqual(row['customer_name'], 'הורה בדיקה')
        self.assertEqual(row['transaction_reference'], 'CONF1')

    def test_declined_transaction(self):
        row = normalize_tranzila_transaction({
            'index': '41045',
            'transaction_date': '2026-08-24',
            'amount': '5.00',
            'processor_response_code': '033',
        })
        self.assertEqual(row['status'], 'failed')


class TranzilaLedgerTest(TestCase):
    def setUp(self):
        self.child = TestDataFactory.create_child()
        self.parent = TestDataFactory.create_parent(family=self.child.family)
        FormalDocument.objects.create(
            document_number='1001956',
            document_type='combined',
            client_type='existing',
            child=self.child,
            document_date=date.today(),
            total_amount=Decimal('10.00'),
            tranzila_issued=True,
            tranzila_doc_id='6252',
        )

    @patch('apps.core.tranzila_ledger._tranzila_client')
    def test_documents_include_tranzila_and_local_customer(self, mock_production):
        mock_production.return_value.list_documents.return_value = {
            'success': True,
            'documents': [{
                'id': '6252',
                'number': '1001956',
                'type': 'IR',
                'action': '1',
                'total_charge_amount': 10,
                'created_at': f'{date.today().isoformat()} 14:14:31',
                'retrieval_key': 'abc',
            }],
        }

        result = list_ledger_documents(date.today(), date.today())
        self.assertEqual(result['source'], 'tranzila')
        numbers = [row['document_number'] for row in result['documents']]
        self.assertEqual(numbers.count('1001956'), 1)
        match = result['documents'][0]
        self.assertEqual(match['customer_name'], self.child.full_name)

    @patch('apps.core.tranzila_ledger._tranzila_client')
    def test_documents_fallback_to_local(self, mock_production):
        mock_production.return_value.list_documents.return_value = {
            'success': False,
            'error': 'credentials missing',
            'documents': [],
        }
        result = list_ledger_documents(date.today(), date.today())
        self.assertTrue(any(row['document_number'] == '1001956' for row in result['documents']))

    def test_payments_include_widget_and_store_only(self):
        from apps.store.models import StoreInvoice

        unmatched = TranzilaTransaction.objects.create(
            transaction_id='99999',
            confirmation_code='ORPHAN',
            transaction_type='charge',
            response_code='000',
            idempotency_key='ledger-orphan-99999',
            is_successful=True,
            response_timestamp=timezone.now(),
        )
        txn = TranzilaTransaction.objects.create(
            transaction_id='41044',
            confirmation_code='CONF1',
            transaction_type='charge',
            response_code='000',
            idempotency_key='ledger-test-41044',
            is_successful=True,
            response_timestamp=timezone.now(),
        )
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            parent=self.parent,
            branch=self.child.family.branch,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('5.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('5.00'),
            tranzila_transaction=txn,
            payment_date=timezone.now(),
        )
        StoreInvoice.objects.create(
            invoice_number='INV-LEDGER-STORE-1',
            child=self.child,
            total_amount=Decimal('40.00'),
            amount_paid=Decimal('40.00'),
            payment_method='credit_card',
            payment_status='completed',
            tranzila_confirmation_code='STORE1',
        )
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            parent=self.parent,
            branch=self.child.family.branch,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('0.00'),
            description='card verify only',
        )

        result = list_ledger_payments(date.today(), date.today())
        refs = [row['transaction_reference'] for row in result['payments']]
        amounts = [row['amount'] for row in result['payments']]
        sources = {row['source'] for row in result['payments']}
        self.assertIn('CONF1', refs)
        self.assertIn('STORE1', refs)
        self.assertNotIn('ORPHAN', refs)
        self.assertTrue(all(amount > 0 for amount in amounts))
        self.assertEqual(sources, {'widget', 'store'})
        self.assertTrue(any(self.child.full_name in row['customer_name'] for row in result['payments']))
        self.assertEqual(unmatched.confirmation_code, 'ORPHAN')


class TranzilaLedgerApiTest(BaseAPITestCase):
    @patch('apps.core.tranzila_ledger._tranzila_client')
    def test_documents_endpoint(self, mock_production):
        mock_production.return_value.list_documents.return_value = {
            'success': True,
            'documents': [{
                'id': '9',
                'number': '2001',
                'type': 'RE',
                'total_charge_amount': 12,
                'created_at': f'{date.today().isoformat()} 10:00:00',
                'retrieval_key': 'rk',
            }],
        }
        response = self.client.get('/api/v1/documents/documents/tranzila/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(row['document_number'] == '2001' for row in response.data['documents']))

    def test_transactions_endpoint(self):
        child = TestDataFactory.create_child()
        parent = TestDataFactory.create_parent(family=child.family)
        txn = TranzilaTransaction.objects.create(
            transaction_id='77',
            confirmation_code='ZZ',
            transaction_type='charge',
            response_code='000',
            idempotency_key='ledger-api-77',
            is_successful=True,
            response_timestamp=timezone.now(),
        )
        Payment.objects.create(
            child=child,
            family=child.family,
            parent=parent,
            branch=child.family.branch,
            payment_type='one_time',
            status='completed',
            base_amount=Decimal('8.50'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('8.50'),
            tranzila_transaction=txn,
            payment_date=timezone.now(),
        )
        response = self.client.get('/api/v1/customers/payments/tranzila-transactions/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['payments']), 1)
        self.assertEqual(response.data['payments'][0]['transaction_reference'], 'ZZ')
        self.assertEqual(response.data['payments'][0]['source'], 'widget')
