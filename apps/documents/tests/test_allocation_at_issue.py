"""
מספר הקצאה typed with the document (B): stored at creation, before the
original is signed, so the original carries it from its first print.
"""
from decimal import Decimal

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer
from apps.documents.document_pdf import build_document_layout
from apps.documents.models import FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'


@override_settings(TRANZILA_BILLING_TERMINAL='')
class AllocationAtIssueTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('alloc-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.customer = BusinessCustomer.objects.create(
            first_name='דנה', last_name='לוי', company_number='514123456',
        )

    def post(self, kind, allocation, price='6000.00'):
        payload = self.invoice_payload(kind, self.today)
        payload.update(client_type='business', child_id=None, business_customer_id=str(self.customer.pk))
        details = payload['invoice_details']
        details['line_items'][0]['price'] = price
        details['allocation_number'] = allocation
        if kind == 'combined':
            details['payments'] = [{'method': 'bank_transfer', 'amount': f"{Decimal(price) * Decimal('1.18'):.2f}"}]
        return self.client.post(CREATE, payload, format='json')

    def test_a_tax_invoice_above_the_threshold_is_issued_with_its_number(self):
        res = self.post('tax_invoice', '123-456-789')
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.allocation_number, '123456789')
        self.assertIsNotNone(doc.allocation_entered_at)
        self.assertEqual(doc.allocation_entered_by_id, self.manager.pk)
        notes = ' '.join(f'{n.lead} {n.text}' for n in build_document_layout(doc).notes)
        self.assertIn('מספר הקצאה: 123456789', notes)

    def test_an_invoice_receipt_takes_one_too(self):
        res = self.post('combined', '123456789')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=res.data['id']).allocation_number, '123456789')

    def test_a_number_that_is_not_nine_digits_is_refused(self):
        res = self.post('tax_invoice', '12345')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('9 ספרות', str(res.data))
        self.assertFalse(FormalDocument.objects.exists())

    def test_only_a_tax_invoice_carries_one(self):
        res = self.post('transaction_invoice', '123456789')
        self.assertEqual(res.status_code, 400, res.data)

    def test_without_one_nothing_changes(self):
        res = self.post('tax_invoice', '')
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual((doc.allocation_number, doc.allocation_entered_at), ('', None))
