"""Credit-note emails sent on refund.

The content assertions here are the checklist of סעיף 9(ה) להוראות ניהול פנקסי
חשבונות — if one of them starts failing, the document stopped being a valid
הודעת זיכוי, not just a prettier email.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.credit_note_email import (
    CreditNote, build_credit_note_email, send_credit_note_email,
)
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice
from apps.customers.models import Payment, TranzilaTransaction
from apps.store.models import StoreInvoice

REFUND_OK = {
    'success': True,
    'transaction_id': 'REFUND_1',
    'confirmation_code': 'R1',
    'response_code': '000',
    'message': 'ok',
    'raw_response': {},
}


@override_settings(EMAIL_HOST='smtp.test', DEFAULT_FROM_EMAIL='noreply@kogomalo.com')
class CreditNoteContentTests(TestCase):
    """Every detail סעיף 9(ה) requires has to reach the customer."""

    def setUp(self):
        self.note = CreditNote(
            customer_name='משפחת כהן',
            email='family@example.com',
            amount=Decimal('236.00'),
            reason='ביטול מנוי',
            original_number='INV-20260901-ABCD1234',
            original_date=date(2026, 9, 1),
            document_number='2026-0042',
            issued_at=date(2026, 9, 10),
        )

    def test_body_carries_every_mandatory_detail(self):
        _, text, html = build_credit_note_email(self.note)

        for body in (text, html):
            self.assertIn('משפחת כהן', body)                    # (3) שם הלקוח
            self.assertIn('INV-20260901-ABCD1234', body)        # (4) מספר החשבונית
            self.assertIn('01/09/2026', body)                   # (4) ותאריכה
            self.assertIn('ביטול מנוי', body)                   # (5) הסיבה
            self.assertIn('10/09/2026', body)                   # (2) תאריך ההודעה
            self.assertIn('מס ערך מוסף', body)                  # (7) במילים המלאות
            self.assertIn('516504412', body)                    # (1) מספר הרישום
            self.assertIn('עוסק מורשה', body)
            self.assertIn('מסמך ממוחשב', body)                  # סעיף 18ב(א)

    def test_vat_is_split_out_of_the_gross_amount(self):
        _, text, _ = build_credit_note_email(self.note)

        # 236.00 gross at 18% → 200.00 net, 36.00 VAT.
        self.assertIn('₪200.00', text)
        self.assertIn('₪36.00', text)
        self.assertIn('₪236.00', text)

    def test_subject_names_the_document(self):
        subject, _, _ = build_credit_note_email(self.note)

        self.assertIn('הודעת זיכוי', subject)
        self.assertIn('2026-0042', subject)

    def test_send_delivers_to_the_customer(self):
        self.assertTrue(send_credit_note_email(self.note))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['family@example.com'])

    def test_send_without_an_address_is_a_no_op(self):
        note = CreditNote(
            customer_name='ללא מייל',
            email='',
            amount=Decimal('100.00'),
            reason='זיכוי',
        )

        self.assertFalse(send_credit_note_email(note))
        self.assertEqual(mail.outbox, [])


class RefundSendsCreditNoteTests(TestCase):
    """Both automatic refund paths have to notify the customer."""

    def setUp(self):
        self.service = PaymentService()
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)

    def _completed_payment(self) -> Payment:
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('236.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('236.00'),
            description='מנוי חודשי',
            payment_date=timezone.now() - timedelta(days=30),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id='TRX_1',
            confirmation_code='AUTH_1',
            transaction_type='recurring_charge',
            is_successful=True,
            idempotency_key='credit-note-test-1',
        )
        payment.save(update_fields=['tranzila_transaction'])
        return payment

    @patch('apps.core.credit_note_email.send_credit_note_email')
    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value=REFUND_OK)
    def test_payment_refund_credits_the_family(self, _mock_refund, mock_send):
        payment = self._completed_payment()
        Invoice.objects.create(
            invoice_number='INV-20260810-DEADBEEF',
            family=self.family,
            payment=payment,
            amount=Decimal('236.00'),
            status='paid',
            payer_name='משפחת כהן',
            payer_email='payer@example.com',
            invoice_date=timezone.now() - timedelta(days=30),
        )

        result = self.service.refund_payment(str(payment.id), reason='ביטול מנוי')

        self.assertTrue(result['success'])
        note = mock_send.call_args.args[0]
        self.assertEqual(note.email, 'parent@example.com')
        self.assertEqual(note.customer_name, 'משפחת כהן')
        self.assertEqual(note.original_number, 'INV-20260810-DEADBEEF')
        self.assertEqual(note.reason, 'ביטול מנוי')
        self.assertEqual(note.amount, Decimal('236.00'))

    @patch('apps.core.credit_note_email.send_credit_note_email')
    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value=REFUND_OK)
    def test_store_refund_credits_the_buyer(self, _mock_refund, mock_send):
        invoice = StoreInvoice.objects.create(
            customer_name='רוכש בדיקה',
            customer_email='buyer@example.com',
            total_amount=Decimal('49.00'),
            payment_method='credit_card',
            payment_status='completed',
            tranzila_transaction_id='TRX_STORE',
            tranzila_confirmation_code='AUTH_STORE',
        )

        result = self.service.refund_store_invoice(str(invoice.id), reason='החזרת מוצר')

        self.assertTrue(result['success'])
        note = mock_send.call_args.args[0]
        self.assertEqual(note.email, 'buyer@example.com')
        self.assertEqual(note.original_number, invoice.invoice_number)
        self.assertEqual(note.reason, 'החזרת מוצר')

    @patch('apps.core.credit_note_email.send_credit_note_email', side_effect=RuntimeError('smtp down'))
    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value=REFUND_OK)
    def test_a_failed_email_does_not_fail_the_refund(self, _mock_refund, _mock_send):
        payment = self._completed_payment()

        result = self.service.refund_payment(str(payment.id), reason='בדיקה')

        self.assertTrue(result['success'])
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'refunded')


class ManualCreditNoteTests(TestCase):
    """A credit note issued from the documents module reaches the family too."""

    @patch('apps.core.credit_note_email.send_credit_note_email')
    def test_created_credit_invoice_is_emailed_with_its_pdf(self, mock_send):
        from apps.documents.service import create_credit_invoice

        family = TestDataFactory.create_family(email='parent@example.com')
        child = TestDataFactory.create_child(family=family)

        doc = create_credit_invoice({
            'client_type': 'existing',
            'child_id': child.id,
            'credit_invoice_details': {
                'document_date': date(2026, 9, 10),
                'credit_reason': 'ביטול הרשמה',
                'credit_amount_before_vat': Decimal('200.00'),
                'linked_invoice_id': '',
            },
        })

        note = mock_send.call_args.args[0]
        self.assertEqual(note.email, 'parent@example.com')
        self.assertEqual(note.customer_name, child.full_name)
        self.assertEqual(note.document_number, doc.document_number)
        self.assertEqual(note.reason, 'ביטול הרשמה')
        self.assertEqual(note.amount, Decimal('236.00'))
        self.assertTrue(mock_send.call_args.kwargs['pdf_bytes'])
