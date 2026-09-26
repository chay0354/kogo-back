"""
What a document from the dialog may carry (M): shekels only, no rounding of the
total to the shekel, no negative prices, a discount of 0–100%, a customer; a
transaction invoice shows the VAT it will charge; a receipt keeps its
withholding and the transfer's date.
"""
from decimal import Decimal

from django.test import override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.documents.document_pdf import build_document_layout
from apps.documents.models import FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'


@override_settings(TRANZILA_BILLING_TERMINAL='')
class DocumentValidationTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('valid-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def post(self, kind='tax_invoice', price='100.25', **details):
        payload = self.invoice_payload(kind, self.today)
        payload['invoice_details']['line_items'][0]['price'] = price
        payload['invoice_details'].update(details)
        return self.client.post(CREATE, payload, format='json')

    def test_a_foreign_currency_is_refused(self):
        res = self.post(currency='USD')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('בשקלים בלבד', str(res.data))

    def test_round_total_is_ignored_the_total_is_net_plus_vat(self):
        res = self.post(round_total=True)
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual((doc.vat_amount, doc.total_amount), (Decimal('18.05'), Decimal('118.30')))

    def test_a_negative_price_is_refused(self):
        res = self.post(price='-10.00')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('שלילי', str(res.data))

    def test_a_discount_outside_0_to_100_percent_is_refused(self):
        self.assertEqual(self.post(discount_percent='100.01').status_code, 400)
        self.assertEqual(self.post(discount_percent='-1').status_code, 400)
        self.assertEqual(self.post(discount_amount='-1').status_code, 400)

    def test_a_discount_above_the_lines_is_refused(self):
        res = self.post(discount_amount='200.00')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('ההנחה גדולה', res.data['error'])

    def test_a_percent_discount_is_to_the_agora(self):
        res = self.post(price='100.25', discount_percent='12.5')
        doc = FormalDocument.objects.get(pk=res.data['id'])
        # 12.5% of 100.25 = 12.53125 → 12.53; VAT on 87.72 = 15.7896 → 15.79.
        self.assertEqual((doc.discount_amount, doc.vat_amount, doc.total_amount),
                         (Decimal('12.53'), Decimal('15.79'), Decimal('103.51')))

    def test_a_customer_is_required(self):
        payload = self.invoice_payload('tax_invoice', self.today)
        payload['child_id'] = None
        res = self.client.post(CREATE, payload, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('child_id', res.data)
        payload.update(client_type='business')
        self.assertIn('business_customer_id', self.client.post(CREATE, payload, format='json').data)

    def test_a_transaction_invoice_prints_the_vat_it_will_charge(self):
        res = self.post('transaction_invoice', price='100.00')
        doc = FormalDocument.objects.get(pk=res.data['id'])
        totals = {f.label: f.value for f in build_document_layout(doc).totals}
        self.assertIn('מע"מ 18%', totals)
        self.assertNotIn('פטור', ' '.join(totals.values()))
        self.assertEqual(doc.total_amount, Decimal('118.00'))

    def test_a_receipt_keeps_its_withholding_and_the_transfers_date(self):
        res = self.client.post(CREATE, {
            'document_type': 'receipt', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'receipt_details': {'payment_method': 'העברה בנקאית', 'bank_amount': '900.00',
                                'bank_date': str(self.today), 'bank_reference': 'T-1', 'withholding': '100.00'},
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.withholding_amount, Decimal('100.00'))
        self.assertEqual(doc.payments.get().paid_on, self.today)
        printed = {f.label: f.value for f in build_document_layout(doc).payment_fields}
        self.assertIn('100.00', printed['ניכוי במקור'])
