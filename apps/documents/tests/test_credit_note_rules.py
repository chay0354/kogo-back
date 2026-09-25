"""
What a credit note may credit (I): a tax invoice or invoice-receipt of the same
customer, for no more than is left of it; a number kogo never issued with its
date; and the customer's confirmation of receipt, recorded once (הוראה 23א(3)).
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.financial_models import Invoice
from apps.customers.models import Child, Family
from apps.documents.models import FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'
LIST = '/api/v1/documents/documents/'


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CreditNoteRulesTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('credit-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def original(self, kind='tax_invoice', number=None, child=None, net='200.00'):
        number = number or f'TI-{self.year}-000900'
        net = Decimal(net)
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='existing', child=child or self.kid,
            document_date=self.today, subtotal=net, vat_amount=net * Decimal('0.18'),
            total_amount=net * Decimal('1.18'),
        )

    def credit(self, number, amount='100.00', child=None, **details):
        return self.client.post(CREATE, {
            'document_type': 'credit_invoice',
            'client_type': 'existing',
            'child_id': str((child or self.kid).id),
            'credit_invoice_details': {
                'document_date': str(self.today), 'linked_invoice_id': number,
                'credit_reason': 'ביטול', 'credit_amount_before_vat': amount, **details,
            },
        }, format='json')

    def credits(self):
        return FormalDocument.objects.filter(document_type='credit_invoice')

    def test_a_tax_invoice_of_the_same_customer_is_credited_and_linked(self, _mail):
        original = self.original()
        res = self.credit(original.document_number)
        self.assertEqual(res.status_code, 201, res.data)
        doc = self.credits().get()
        self.assertEqual((doc.linked_document_id, doc.linked_document_date), (original.pk, original.document_date))

    def test_an_invoice_receipt_can_be_credited(self, _mail):
        original = self.original('combined', f'IRM-{self.year}-000900')
        self.assertEqual(self.credit(original.document_number).status_code, 201)

    def test_another_customers_document_is_refused(self, _mail):
        other = Child.objects.create(
            family=Family.objects.create(name='אחרת'), first_name='דן', last_name='לוי',
            birth_date=date(2014, 1, 1), gender='male', status='active',
        )
        original = self.original(child=other)
        res = self.credit(original.document_number)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('ללקוח אחר', res.data['error'])
        self.assertFalse(self.credits().exists())

    def test_what_is_not_a_tax_invoice_cannot_be_credited(self, _mail):
        cases = [
            ('receipt', f'RC-{self.year}-000900'),
            ('transaction_invoice', f'TX-{self.year}-000900'),
            ('credit_invoice', f'CR-{self.year}-000900'),
            ('draft', 'D-ABCDEF12'),
        ]
        for kind, number in cases:
            with self.subTest(kind):
                self.original(kind, number)
                res = self.credit(number)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn('בלבד', res.data['error'])
        self.assertFalse(self.credits().filter(linked_document_number__in=[n for _, n in cases]).exists())

    def test_a_store_transaction_invoice_cannot_be_credited(self, _mail):
        res = self.credit(f'SD-{self.year}-000001', linked_document_date=str(self.today))
        self.assertEqual(res.status_code, 400, res.data)

    def test_the_credit_may_not_pass_what_is_left_of_the_original(self, _mail):
        original = self.original(net='200.00')
        self.assertEqual(self.credit(original.document_number, '150.00').status_code, 201)

        over = self.credit(original.document_number, '50.01')
        self.assertEqual(over.status_code, 400, over.data)
        self.assertIn('50.00', over.data['error'])

        rest = self.credit(original.document_number, '50.00')
        self.assertEqual(rest.status_code, 201, rest.data)
        self.assertEqual(self.credits().count(), 2)

    def test_a_lesson_receipt_is_credited_up_to_its_amount_before_vat(self, _mail):
        family = self.kid.family
        Invoice.objects.create(
            invoice_number=f'IR-{self.year}-000900', family=family, amount=Decimal('236.00'), status='paid',
            payment_method='credit_card', payment_type='recurring', payer_name=family.name,
            invoice_date=timezone.now(),
        )
        self.assertEqual(self.credit(f'IR-{self.year}-000900', '200.01').status_code, 400)
        res = self.credit(f'IR-{self.year}-000900', '200.00')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNotNone(self.credits().get().linked_document_date)

    def test_a_number_kogo_never_issued_needs_its_date(self, _mail):
        res = self.credit('30112')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('תאריך המסמך המקורי', str(res.data))

        dated = self.credit('30112', linked_document_date=f'{self.year}-01-15')
        self.assertEqual(dated.status_code, 201, dated.data)
        self.assertEqual(self.credits().get().linked_document_date, date(self.year, 1, 15))

    def test_the_date_of_a_document_kogo_issued_is_its_own_not_the_typed_one(self, _mail):
        original = self.original()
        res = self.credit(original.document_number, linked_document_date=f'{self.year - 1}-12-01')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.credits().get().linked_document_date, original.document_date)

    def test_a_credit_of_nothing_is_refused(self, _mail):
        original = self.original()
        self.assertEqual(self.credit(original.document_number, '0.00').status_code, 400)


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CustomerAckTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('ack-manager@test', UserProfile.ROLE_MANAGER)
        self.note = self.issued(f'CR-{self.year}-000900', 'credit_invoice', self.today)

    def ack(self, doc, note='חתימה על העתק', user=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(f'{LIST}{doc.pk}/customer-ack/', {'note': note}, format='json')

    def test_the_customers_confirmation_is_recorded_once(self, _mail):
        res = self.ack(self.note)
        self.assertEqual(res.status_code, 200, res.data)
        self.note.refresh_from_db()
        self.assertIsNotNone(self.note.customer_ack_at)
        self.assertEqual(self.note.customer_ack_note, 'חתימה על העתק')

        again = self.ack(self.note, 'דואר רשום')
        self.assertEqual(again.status_code, 409, again.data)
        self.note.refresh_from_db()
        self.assertEqual(self.note.customer_ack_note, 'חתימה על העתק')

    def test_it_says_how_the_customer_confirmed(self, _mail):
        self.assertEqual(self.ack(self.note, '  ').status_code, 400)

    def test_only_a_credit_note_takes_one(self, _mail):
        invoice = self.issued(f'TI-{self.year}-000900', 'tax_invoice', self.today)
        self.assertEqual(self.ack(invoice).status_code, 400)

    def test_managers_only(self, _mail):
        partner = make_user('ack-partner@test', UserProfile.ROLE_PARTNER)
        self.assertEqual(self.ack(self.note, user=partner).status_code, 403)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.customer_ack_at)
