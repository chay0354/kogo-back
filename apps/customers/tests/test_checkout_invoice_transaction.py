"""
The family-checkout receipt commits whole or not at all (audit M1, 30.9.2026).

The IR number and the receipt used to commit on their own, and its lines, the
checkout log naming every charge it covers and its signed original's row after
them, outside any transaction: a failure there left an IR with no lines and no
original, and its other charges looked receipt-less to the missing-receipts
screen. Now they commit together, the mail goes after the commit, and a failure
gives the number back. The card is charged before any of this, by the callers.

No mail leaves: every exit's send_resend_email is patched.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.customers.checkout_invoice import CHECKOUT_LINES_ACTION, issue_widget_checkout_invoice
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import Payment, TranzilaTransaction
from apps.documents.models import SignedOriginal
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_delivery_never_final import DeliveryMixin


def run_number(number: str) -> int:
    return int(number.rsplit('-', 1)[-1])


@signing_on()
class CheckoutReceiptTransactionTests(DeliveryMixin, TestCase):
    def charged(self, txn: str, amount='236.00') -> Payment:
        payment = Payment.objects.create(
            child=self.kid, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal(amount), discount_amount=Decimal('0.00'), final_amount=Decimal(amount),
            payment_date=timezone.now(),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id=txn, confirmation_code=f'AUTH_{txn}', transaction_type='recurring_charge',
            is_successful=True, idempotency_key=f'checkout-{txn}',
        )
        payment.save(update_fields=['tranzila_transaction'])
        return payment

    def checkout(self, *payments, send_email=True):
        with self.captureOnCommitCallbacks(execute=True):
            return issue_widget_checkout_invoice(list(payments), send_email=send_email)

    def test_the_receipt_its_log_and_its_original_are_written_together_and_mailed_once(self):
        first, second = self.charged('TRX_A'), self.charged('TRX_B', '100.00')
        invoice = self.checkout(first, second)
        self.assertEqual(invoice.amount, Decimal('336.00'))
        self.assertEqual(invoice.children.count(), 2)
        log = InvoiceActivityLog.objects.get(invoice=invoice, action=CHECKOUT_LINES_ACTION)
        self.assertEqual(set(log.details['payment_ids']), {str(first.id), str(second.id)})
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertTrue(row.is_signed)
        self.assertIsNotNone(row.sent_at)
        self.lesson_mail.assert_called_once()

    def test_a_failure_after_the_number_leaves_no_receipt_no_original_and_gives_the_number_back(self):
        before = self.checkout(self.charged('TRX_1'))
        self.lesson_mail.reset_mock()

        broken = MagicMock()
        broken.objects.create.side_effect = RuntimeError('the log could not be written')
        failing = self.charged('TRX_2')
        with patch('apps.customers.checkout_invoice.InvoiceActivityLog', broken):
            with self.assertRaises(RuntimeError):
                self.checkout(failing)
        # Nothing of it is left: no receipt without lines, no original, no mail.
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(SignedOriginal.objects.count(), 1)
        self.assertFalse(Invoice.objects.filter(payment=failing).exists())
        self.lesson_mail.assert_not_called()

        # The charge is simply without a receipt; the next one takes the number the failure gave back.
        after = self.checkout(self.charged('TRX_3'))
        self.assertEqual(run_number(after.invoice_number), run_number(before.invoice_number) + 1)
        self.lesson_mail.assert_called_once()

    def test_without_its_mail_the_receipt_is_recorded_and_the_cron_mails_it(self):
        invoice = self.checkout(self.charged('TRX_Q'), send_email=False)
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertEqual(row.channel, SignedOriginal.CHANNEL_IR)
        self.assertEqual(invoice.children.count(), 1)
