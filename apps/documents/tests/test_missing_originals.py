"""
The safety net for a document issued with no original (audit M1, 30.9.2026).

issue() writes the original's row in a savepoint and logs a failure rather than
fail the document; the cron used to walk only rows that exist, and the archive
covers only what was issued before signing went on. So a document issued
since then whose row was never written had no original, no delivery and no
trace. The sign-pending cron now finds it — by the register's own definition of
"issued" — and gives it its original through the normal row, signature and
delivery rules. Found more than three days after issue, it is not mailed by
itself: it goes on the hand-delivery list, and the office decides.

No mail leaves: every exit's send_resend_email is patched.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.customers.financial_models import Invoice
from apps.documents.models import FormalDocument, SignedOriginal
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.service import (
    REASON_FOUND_LATE, REASON_NO_EMAIL, _recovery_channel, missing_original_count, send_to_customer, sign_pending,
)
from apps.documents.signing.sources import REASON_CASH
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_delivery_never_final import DeliveryMixin
from apps.store.models import StoreInvoice

EMAIL, PAPER, HELD = 'email', 'paper', 'held'
STATUS = '/api/v1/documents/signing/status/'
ORIGINALS = '/api/v1/documents/signing/originals/'


def a_month_ago() -> str:
    return (timezone.now() - timedelta(days=30)).isoformat()


def with_cutoff(**extra):
    """Signing on since a month ago — the moment the safety net starts from."""
    return signing_on(SIGNING_ARCHIVE_ISSUED_BEFORE=a_month_ago(), **extra)


class MissingMixin(DeliveryMixin):
    def issued_ago(self, obj, delta: timedelta):
        """Move a document's issue back in time (created_at, and issued_at where it has one)."""
        moment = timezone.now() - delta
        fields = {'created_at': moment}
        if isinstance(obj, FormalDocument):
            fields['issued_at'] = moment
        type(obj).objects.filter(pk=obj.pk).update(**fields)
        obj.refresh_from_db()
        return obj

    def lesson_receipt_without_row(self, number='IR-2026-000501', ago=timedelta(minutes=30), **fields):
        invoice = Invoice.objects.create(
            invoice_number=number, family=self.family, branch=self.north, amount=Decimal('236.00'),
            status=fields.pop('status', 'paid'), payment_method='credit_card', payment_type='recurring',
            payer_name=self.family.name, payer_email=self.family.email, invoice_date=timezone.now(),
            tranzila_transaction_id='TRX-M1', **fields,
        )
        return self.issued_ago(invoice, ago)

    def till_sale_without_row(self, method='credit_card', status='completed', ago=timedelta(minutes=30)):
        sale = StoreInvoice.objects.create(
            child=self.kid, total_amount=Decimal('49.00'), payment_method=method, payment_status=status,
        )
        return self.issued_ago(sale, ago)

    def tax_invoice_without_row(self, ago=timedelta(minutes=30)):
        # What M1 is about: the document commits and its original's row does not.
        with patch('apps.documents.service._sign_at_issue'):
            doc = self.tax_invoice()
        self.assertFalse(SignedOriginal.objects.filter(number=doc.document_number).exists())
        return self.issued_ago(doc, ago)

    def run_cron(self) -> dict:
        with self.captureOnCommitCallbacks(execute=True):
            return sign_pending()


@with_cutoff()
class SafetyNetTests(MissingMixin, TestCase):
    def test_a_hand_issued_invoice_without_a_row_gets_its_original_and_is_mailed_once(self):
        doc = self.tax_invoice_without_row()
        self.assertEqual(missing_original_count(), {'count': 1, 'blocked': ''})

        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['late_to_office'], summary['missing_remaining']), (1, 0, 0))
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.assertEqual(row.sign_attempts, 1)
        self.assertEqual((row.channel, row.delivery), (SignedOriginal.CHANNEL_FORMAL, EMAIL))
        self.assertIsNotNone(row.sent_at)
        self.formal_mail.assert_called_once()

        # Idempotent: the next runs find nothing missing, sign nothing again and mail nothing again.
        for _ in range(2):
            again = self.run_cron()
            self.assertEqual((again['missing_found'], again['signed'], again['sent']), (0, 0, 0))
        self.formal_mail.assert_called_once()
        self.assertEqual(SignedOriginal.objects.filter(number=doc.document_number).count(), 1)
        self.row(doc).refresh_from_db()
        self.assertEqual(self.row(doc).sign_attempts, 1)
        self.assertEqual(missing_original_count()['count'], 0)

    def test_a_lesson_receipt_and_a_store_sale_without_a_row_are_found_too(self):
        receipt = self.lesson_receipt_without_row()
        sale = self.till_sale_without_row()
        summary = self.run_cron()
        self.assertEqual(summary['missing_found'], 2)
        ir = SignedOriginal.objects.get(number=receipt.invoice_number)
        st = SignedOriginal.objects.get(number=sale.invoice_number)
        self.assertEqual((ir.kind, ir.channel, ir.delivery), (SignedOriginal.KIND_IR, SignedOriginal.CHANNEL_IR, EMAIL))
        self.assertEqual((st.kind, st.channel, st.delivery), (SignedOriginal.KIND_STORE, SignedOriginal.CHANNEL_STORE, EMAIL))
        self.assertIsNotNone(ir.sent_at)
        self.assertIsNotNone(st.sent_at)
        self.lesson_mail.assert_called_once()
        self.store_mail.assert_called_once()
        self.run_cron()
        self.lesson_mail.assert_called_once()
        self.store_mail.assert_called_once()

    def test_a_document_that_already_has_its_original_is_never_touched(self):
        doc = self.tax_invoice()  # issued normally: signed and mailed at issue
        row = self.row(doc)
        self.issued_ago(doc, timedelta(days=5))
        self.formal_mail.reset_mock()
        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['late_to_office']), (0, 0))
        after = self.row(doc)
        self.assertEqual((after.delivery, after.sent_at, after.sha256), (row.delivery, row.sent_at, row.sha256))
        self.formal_mail.assert_not_called()

    def test_what_the_register_does_not_count_as_issued_is_left_alone(self):
        self.lesson_receipt_without_row('IR-2026-000601', status='pending')
        self.lesson_receipt_without_row('IR-2026-000602', status='failed')
        self.till_sale_without_row(status='pending')
        self.till_sale_without_row(status='failed')
        from apps.documents import service

        draft = service.create_draft({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {'document_date': '2026-09-18',
                                'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
        })
        self.issued_ago(draft, timedelta(hours=1))
        self.assertEqual(missing_original_count()['count'], 0)
        self.assertEqual(self.run_cron()['missing_found'], 0)
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_document_being_issued_right_now_is_left_to_its_request(self):
        self.lesson_receipt_without_row(ago=timedelta(minutes=1))
        self.assertEqual(self.run_cron()['missing_found'], 0)
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_document_issued_before_signing_went_on_is_the_archives(self):
        self.lesson_receipt_without_row(ago=timedelta(days=40))
        self.assertEqual(missing_original_count()['count'], 0)
        self.assertEqual(self.run_cron()['missing_found'], 0)
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_draft_approved_after_signing_went_on_counts_from_its_approval(self):
        from apps.documents import service

        draft = service.create_draft({
            'client_type': 'existing', 'child_id': str(self.kid.id), 'draft_target_type': 'tax_invoice',
            'invoice_details': {'document_date': '2026-09-18',
                                'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
        })
        # Typed before signing went on, approved a moment ago — and its row was never written.
        FormalDocument.objects.filter(pk=draft.pk).update(created_at=timezone.now() - timedelta(days=40))
        with patch('apps.documents.service._sign_at_issue'), self.captureOnCommitCallbacks(execute=True):
            doc = service.finalize_draft(draft, issued_by=self.manager)
        FormalDocument.objects.filter(pk=doc.pk).update(issued_at=timezone.now() - timedelta(minutes=30))
        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['late_to_office']), (1, 0))
        self.assertIsNotNone(self.row(doc).sent_at)

    def test_many_missing_documents_are_given_their_originals_a_few_a_run(self):
        numbers = [f'IR-2026-0007{n:02d}' for n in range(12)]
        for number in numbers:
            self.lesson_receipt_without_row(number)
        first = self.run_cron()
        self.assertEqual((first['missing_found'], first['missing_remaining']), (10, 2))
        second = self.run_cron()
        self.assertEqual((second['missing_found'], second['missing_remaining']), (2, 0))
        self.assertEqual(SignedOriginal.objects.filter(number__in=numbers).count(), 12)
        self.assertEqual(self.lesson_mail.call_count, 12)

    def test_a_number_held_by_another_documents_original_is_counted_not_forced(self):
        receipt = self.lesson_receipt_without_row()
        # Another document's archive copy under the same number (the passes leave archive rows alone).
        clash = SignedOriginal.objects.create(
            number=receipt.invoice_number, kind=SignedOriginal.KIND_STORE, source_id='someone-else',
            purpose=SignedOriginal.PURPOSE_ARCHIVE, delivery='none',
        )
        self.assertEqual(missing_original_count()['count'], 1)
        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['errors']), (0, 0))
        self.assertEqual(SignedOriginal.objects.get(pk=clash.pk).source_id, 'someone-else')

    def test_cash_found_late_stays_on_paper_for_its_own_reason(self):
        sale = self.till_sale_without_row(method='cash', ago=timedelta(days=5))
        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['late_to_office']), (1, 0))
        row = SignedOriginal.objects.get(number=sale.invoice_number)
        self.assertEqual((row.delivery, row.delivery_reason), (PAPER, REASON_CASH))
        self.store_mail.assert_not_called()


@with_cutoff()
class FoundLateTests(MissingMixin, TestCase):
    def test_found_more_than_three_days_after_issue_it_goes_to_the_office_not_the_customer(self):
        doc = self.tax_invoice_without_row(ago=timedelta(days=4))
        summary = self.run_cron()
        self.assertEqual((summary['missing_found'], summary['late_to_office'], summary['sent']), (1, 1, 0))
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.assertEqual((row.delivery, row.delivery_reason), (PAPER, REASON_FOUND_LATE))
        self.assertIsNone(row.sent_at)
        self.formal_mail.assert_not_called()

        # Nothing mails it by itself on later runs.
        for _ in range(2):
            self.run_cron()
        self.formal_mail.assert_not_called()

        # The office's "שלח" mails the original — once; the next send is a copy.
        first = send_to_customer(row.pk, user=self.manager)
        self.assertEqual(first.sent, 'original')
        self.formal_mail.assert_called_once()
        self.assertEqual(self.row(doc).delivery, EMAIL)
        second = send_to_customer(row.pk, user=self.manager)
        self.assertEqual(second.sent, 'copy')
        self.assertEqual(self.formal_mail.call_count, 2)
        self.assertIsNotNone(self.row(doc).sent_at)

    def test_found_late_without_an_address_is_not_mailed_when_an_address_appears(self):
        self.no_address()
        doc = self.tax_invoice_without_row(ago=timedelta(days=4))
        self.run_cron()
        self.assertEqual(self.row(doc).delivery_reason, REASON_FOUND_LATE)
        self.an_address()
        summary = self.run_cron()
        self.assertEqual((summary['paper_to_email'], summary['sent']), (0, 0))
        self.formal_mail.assert_not_called()

    def test_found_late_while_the_key_is_out_of_reach_is_still_never_mailed_by_itself(self):
        receipt = self.lesson_receipt_without_row(ago=timedelta(days=4))
        with patch('apps.documents.signing.signer.sign_pdf', side_effect=SigningUnavailable('KMS down')):
            summary = self.run_cron()
        self.assertEqual(summary['missing_found'], 1)
        row = SignedOriginal.objects.get(number=receipt.invoice_number)
        self.assertFalse(row.is_signed)
        self.assertEqual(row.delivery, HELD)

        # The key is back: the cron signs it, and puts it on the office's list instead of mailing it.
        first = self.run_cron()
        self.assertEqual(first['signed'], 1)
        with patch('apps.documents.signing.service.CRON_GRACE', timedelta(0)):
            second = self.run_cron()
        self.assertEqual(second['late_to_office'], 1)
        row.refresh_from_db()
        self.assertTrue(row.is_signed)
        self.assertEqual((row.delivery, row.delivery_reason), (PAPER, REASON_FOUND_LATE))
        self.lesson_mail.assert_not_called()

    def test_a_row_recorded_on_time_is_mailed_by_the_cron_as_before(self):
        # A row written with its document, whose mail failed: the cron retries it, however old it is now.
        doc = self.tax_invoice()
        row = self.row(doc)
        SignedOriginal.objects.filter(pk=row.pk).update(sent_at=None, delivery=EMAIL)
        moment = timezone.now() - timedelta(days=5)
        FormalDocument.objects.filter(pk=doc.pk).update(created_at=moment, issued_at=moment)
        SignedOriginal.objects.filter(pk=row.pk).update(created_at=moment)
        self.formal_mail.reset_mock()
        summary = self.run_cron()
        self.assertEqual((summary['sent'], summary['late_to_office']), (1, 0))
        self.formal_mail.assert_called_once()


class BlockedTests(MissingMixin, TestCase):
    @signing_on(SIGNING_ARCHIVE_ISSUED_BEFORE='')
    def test_without_the_moment_signing_went_on_nothing_is_guessed(self):
        receipt = self.lesson_receipt_without_row()
        summary = self.run_cron()
        self.assertEqual(summary['missing_found'], 0)
        self.assertTrue(summary['missing_blocked'])
        self.assertFalse(SignedOriginal.objects.filter(number=receipt.invoice_number).exists())
        self.assertEqual(missing_original_count()['count'], 0)

    def test_with_signing_off_the_cron_does_nothing(self):
        self.lesson_receipt_without_row()
        self.assertEqual(sign_pending(), {'disabled': True})
        self.assertFalse(SignedOriginal.objects.exists())


class RecoveryChannelTests(TestCase):
    def test_a_rent_receipt_and_michal_kagans_documents_keep_their_own_exits(self):
        def doc(number):
            return FormalDocument(document_number=number)

        self.assertEqual(_recovery_channel(SignedOriginal.KIND_FORMAL, doc('RT-2026-000001')),
                         SignedOriginal.CHANNEL_RENTAL)
        self.assertEqual(_recovery_channel(SignedOriginal.KIND_FORMAL, doc('MK-2026-000001')),
                         SignedOriginal.CHANNEL_MICHAL)
        self.assertEqual(_recovery_channel(SignedOriginal.KIND_FORMAL, doc('TI-2026-000001')), '')
        self.assertEqual(_recovery_channel(SignedOriginal.KIND_IR, Invoice(invoice_number='IR-2026-000001')), '')


@with_cutoff()
class StatusCountTests(MissingMixin, APITestCase):
    def test_the_status_counts_what_is_missing_and_what_was_found_late(self):
        self.client.force_authenticate(self.manager)
        self.tax_invoice_without_row(ago=timedelta(days=4))
        self.lesson_receipt_without_row()
        counts = self.client.get(STATUS).json()['counts']
        self.assertEqual((counts['missing_original'], counts['found_late']), (2, 0))

        self.run_cron()
        body = self.client.get(STATUS).json()
        self.assertEqual((body['counts']['missing_original'], body['counts']['found_late']), (0, 1))
        self.assertEqual(body['missing_original_blocked'], '')
        # On the hand-delivery list, where the office sees it.
        page = self.client.get(ORIGINALS, {'delivery': 'paper', 'printed': 'false'}).json()
        self.assertEqual([row['delivery_reason'] for row in page['results']], [REASON_FOUND_LATE])

    @signing_on(SIGNING_ARCHIVE_ISSUED_BEFORE='')
    def test_the_status_says_when_the_check_cannot_run(self):
        self.client.force_authenticate(self.manager)
        body = self.client.get(STATUS).json()
        self.assertEqual(body['counts']['missing_original'], 0)
        self.assertTrue(body['missing_original_blocked'])


class NoAddressReasonTests(TestCase):
    def test_the_reasons_differ(self):
        # The front tells a late row from a no-address row by these words.
        self.assertNotEqual(REASON_FOUND_LATE, REASON_NO_EMAIL)
