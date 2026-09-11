"""A refund is corrected by a further, numbered document — never by editing the original.

סעיף 23(ב) and 23א להוראות ניהול פנקסי חשבונות. The credit note carries what
סעיף 9(ה) asks for, sits in a series of its own (סעיף 18(א)(1)), and shows up on
the child's card next to the receipt it credits.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, TranzilaTransaction
from apps.documents.models import FormalDocument
from apps.store.models import StoreInvoice

User = get_user_model()
REFUND_OK = {
    'success': True, 'transaction_id': 'REFUND_1', 'confirmation_code': 'R1',
    'response_code': '000', 'message': 'ok', 'raw_response': {},
}


@patch('apps.core.credit_note_email.send_credit_note_email')
@patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value=REFUND_OK)
class RefundCreditNoteTest(APITestCase):
    def setUp(self):
        user = User.objects.create_user(username='manager-credit@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)
        self.year = timezone.localdate().year

    def _charged_payment(self, key='1'):
        payment = Payment.objects.create(
            child=self.child, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('236.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('236.00'),
            payment_date=timezone.now() - timedelta(days=12),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id=f'TRX_{key}', confirmation_code=f'AUTH_{key}', transaction_type='recurring_charge',
            is_successful=True, idempotency_key=f'credit-note-{key}',
        )
        payment.save(update_fields=['tranzila_transaction'])
        receipt = PaymentService()._create_invoice_from_payment(
            payment, payment.tranzila_transaction, send_email=False, invoice_date=payment.payment_date,
        )
        return payment, receipt

    def test_a_refund_issues_a_numbered_credit_note_naming_the_receipt(self, _refund, _send):
        payment, receipt = self._charged_payment()

        self.assertTrue(PaymentService().refund_payment(str(payment.id), reason='ביטול מנוי')['success'])

        note = FormalDocument.objects.get(document_type='credit_invoice')
        self.assertEqual(note.document_number, f'CR-{self.year}-000001')
        self.assertEqual(note.linked_document_number, receipt.invoice_number)
        self.assertEqual(note.linked_document_date, timezone.localtime(receipt.invoice_date).date())
        self.assertEqual(note.credit_reason, 'ביטול מנוי')
        self.assertEqual((note.subtotal, note.vat_amount, note.total_amount),
                         (Decimal('200.00'), Decimal('36.00'), Decimal('236.00')))
        self.assertEqual(note.child, self.child)

    def test_credit_notes_run_in_their_own_series(self, _refund, _send):
        first, _ = self._charged_payment('1')
        second, _ = self._charged_payment('2')

        PaymentService().refund_payment(str(first.id), reason='א')
        PaymentService().refund_payment(str(second.id), reason='ב')

        numbers = list(
            FormalDocument.objects.filter(document_type='credit_invoice')
            .order_by('document_number').values_list('document_number', flat=True)
        )
        self.assertEqual(numbers, [f'CR-{self.year}-000001', f'CR-{self.year}-000002'])

    def test_the_credit_goes_to_the_name_and_address_on_the_receipt(self, _refund, send):
        payment, receipt = self._charged_payment()

        PaymentService().refund_payment(str(payment.id), reason='ביטול מנוי')

        mailed = send.call_args.args[0]
        self.assertEqual(mailed.email, 'parent@example.com')
        self.assertEqual(mailed.customer_name, receipt.payer_name)
        self.assertEqual(mailed.original_number, receipt.invoice_number)
        self.assertEqual(mailed.document_number, f'CR-{self.year}-000001')
        self.assertTrue(send.call_args.kwargs['pdf_bytes'])

    def test_a_gross_refund_splits_to_the_exact_agora(self, _refund, _send):
        sale = StoreInvoice.objects.create(
            customer_name='קונה מזדמן', customer_email='walkin@example.com', total_amount=Decimal('49.00'),
            payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='TRX_S', tranzila_confirmation_code='AUTH_S',
        )

        self.assertTrue(PaymentService().refund_store_invoice(str(sale.id), reason='החזרת מוצר')['success'])

        note = FormalDocument.objects.get(document_type='credit_invoice')
        self.assertEqual(note.total_amount, Decimal('49.00'))
        self.assertEqual(note.subtotal + note.vat_amount, Decimal('49.00'))
        self.assertIsNone(note.child)
        self.assertEqual(note.customer_name, 'קונה מזדמן')
        self.assertEqual(note.linked_document_number, sale.invoice_number)

    def test_the_credit_note_is_on_the_childs_card(self, _refund, _send):
        payment, _receipt = self._charged_payment()
        PaymentService().refund_payment(str(payment.id), reason='ביטול מנוי')

        rows = self.client.get(f'/api/v1/customers/children/{self.child.id}/documents/').data['documents']

        credit = next(row for row in rows if row['kind'] == 'formal')
        self.assertEqual(credit['status'], 'credit')
        self.assertTrue(credit['document_number'].startswith('CR-'))

    def test_a_family_receipt_covers_every_charge_of_the_checkout(self, _refund, _send):
        from apps.customers.checkout_invoice import issue_widget_checkout_invoice
        sibling = TestDataFactory.create_child(family=self.family)
        first, _ = self._charged_payment('1')
        second = Payment.objects.create(
            child=sibling, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('100.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('100.00'),
        )
        first.invoices.all().delete()  # a fresh checkout receipt below, not the per-charge one
        issue_widget_checkout_invoice([first, second], send_email=False)

        rows = self.client.get(f'/api/v1/customers/children/{self.child.id}/documents/').data['documents']

        receipt = next(row for row in rows if row['kind'] == 'receipt')
        self.assertEqual(set(receipt['payment_ids']), {str(first.id), str(second.id)})
