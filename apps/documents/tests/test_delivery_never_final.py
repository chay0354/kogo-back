"""
Every original reaches its customer — mailed, on the hand-delivery list, or held with a reason.

The owner's decision D5 (25.9.2026). Until then an original issued without a
mail channel — a hand-issued TI/RC/IRM/TX, a till sale, a late lesson receipt
from the missing-receipts screen — or for a customer with no address was
'none': kept in the archive, never delivered. Now every original has its kind's
channel, a customer without an address puts it on the paper list (and the cron
mails it once an address appears), "שלח / שלח שוב" mails the original once and
copies after, the office's download is a copy, and reroute_undelivered moves the
old 'none' rows. An archive copy is the only 'none' left.

No mail leaves: every exit's send_resend_email is patched.
"""
import base64
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.payment_service import _sign_store_sale
from apps.customers.financial_models import Invoice
from apps.customers.models import Payment, TranzilaTransaction
from apps.documents import service
from apps.documents.issuer import COPY_MARK, ORIGINAL_MARK, SIGNED_MARK
from apps.documents.missing_receipts import issue_missing_receipts
from apps.documents.models import FormalDocument, SignedFileAccess, SignedOriginal
from apps.documents.signing.archive import sign_archive
from apps.documents.signing.service import (
    NO_ADDRESS, REASON_NO_EMAIL, reroute_undelivered, sign_pending,
)
from apps.documents.signing.sources import REASON_CASH
from apps.documents.tests.signing_support import archive_settings, pdf_text, signing_on
from apps.documents.tests.test_register import make_user
from apps.documents.tests.test_signing_delivery import ReceiptsMixin, past_the_grace_period
from apps.store.models import StoreInvoice

EMAIL, PAPER, HELD, NONE = 'email', 'paper', 'held', 'none'
ORIGINALS = '/api/v1/documents/signing/originals/'


def send_url(row) -> str:
    return f'{ORIGINALS}{row.pk}/send/'


def file_url(row) -> str:
    return f'{ORIGINALS}{row.pk}/file/'


def attached(call) -> bytes:
    return base64.b64decode(call.kwargs['attachments'][0]['content'])


def lines(pdf: bytes) -> list[str]:
    return [line.strip() for line in pdf_text(pdf).splitlines()]


class DeliveryMixin(ReceiptsMixin):
    """A family with an address (ReceiptsMixin), the formal exit patched there; the store and IR exits here."""

    def setUp(self):
        super().setUp()
        store = patch('apps.store.invoice_email.send_resend_email', return_value='msg-id')
        self.store_mail = store.start()
        self.addCleanup(store.stop)
        lessons = patch('apps.customers.subscription_invoice_email.send_resend_email', return_value='msg-id')
        self.lesson_mail = lessons.start()
        self.addCleanup(lessons.stop)

    def no_address(self):
        self.family.email = ''
        self.family.save(update_fields=['email'])

    def an_address(self, email='parent@example.com'):
        self.family.email = email
        self.family.save(update_fields=['email'])

    def tax_invoice(self, **invoice):
        with self.captureOnCommitCallbacks(execute=True):
            return service.create_invoice({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'invoice_details': {
                    'document_date': '2026-09-18',
                    'line_items': [{'description': 'חוג', 'quantity': 1, 'price': 200}],
                    **invoice,
                },
            }, 'tax_invoice')

    def till_sale(self, method='credit_card'):
        sale = StoreInvoice.objects.create(
            child=self.kid, total_amount=Decimal('49.00'), payment_method=method, payment_status='completed',
        )
        with self.captureOnCommitCallbacks(execute=True):
            _sign_store_sale(sale)
        return sale

    def late_receipt(self):
        payment = Payment.objects.create(
            child=self.kid, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('236.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('236.00'),
            payment_date=timezone.now() - timedelta(days=20),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id='TRX_LATE', confirmation_code='AUTH_LATE', transaction_type='recurring_charge',
            is_successful=True, idempotency_key='late-receipt-1',
        )
        payment.save(update_fields=['tranzila_transaction'])
        with self.captureOnCommitCallbacks(execute=True):
            result = issue_missing_receipts([payment.pk], user=self.manager)
        self.assertEqual(len(result['issued']), 1, result)
        return Invoice.objects.get(invoice_number=result['issued'][0]['number'])

    def row_of(self, number) -> SignedOriginal:
        return SignedOriginal.objects.get(number=number)


# ── every path that used to end 'none' ───────────────────────────────────────

@signing_on()
class FormerlyNoneTests(DeliveryMixin, TestCase):
    def test_a_hand_issued_tax_invoice_is_mailed_its_signed_original(self):
        doc = self.tax_invoice()
        row = self.row(doc)
        self.assertEqual((row.channel, row.delivery), (SignedOriginal.CHANNEL_FORMAL, EMAIL))
        self.assertIsNotNone(row.sent_at)
        self.formal_mail.assert_called_once()
        call = self.formal_mail.call_args
        self.assertEqual(call.kwargs['to'], ['parent@example.com'])
        self.assertIn(doc.document_number, call.kwargs['subject'])
        self.assertIn('חשבונית מס', call.kwargs['subject'])
        self.assertEqual(attached(call), bytes(row.pdf))
        self.assertTrue(row.pdf_intact())

    def test_a_hand_issued_card_receipt_for_a_family_without_an_address_goes_on_paper(self):
        self.no_address()
        doc = self.receipt('אשראי')
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.assertEqual((row.channel, row.delivery, row.delivery_reason),
                         (SignedOriginal.CHANNEL_FORMAL, PAPER, REASON_NO_EMAIL))
        self.formal_mail.assert_not_called()

    def test_a_transaction_invoice_is_mailed(self):
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_invoice({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'invoice_details': {'document_date': '2026-09-18',
                                    'line_items': [{'description': 'חוג', 'quantity': 1, 'price': 200}]},
            }, 'transaction_invoice')
        self.assertEqual(self.row(doc).delivery, EMAIL)
        self.formal_mail.assert_called_once()

    def test_a_till_card_sale_is_mailed_to_the_family(self):
        sale = self.till_sale()
        row = self.row_of(sale.invoice_number)
        self.assertEqual((row.channel, row.delivery), (SignedOriginal.CHANNEL_STORE, EMAIL))
        self.assertIsNotNone(row.sent_at)
        self.store_mail.assert_called_once()
        call = self.store_mail.call_args
        self.assertEqual(call.kwargs['to'], ['parent@example.com'])
        self.assertNotIn('הזמנה', call.kwargs['subject'])  # a till sale has no website order
        self.assertEqual(attached(call), bytes(row.pdf))
        self.assertIsNotNone(StoreInvoice.objects.get(pk=sale.pk).invoice_email_sent_at)

    def test_a_till_sale_without_an_address_goes_on_paper_and_in_cash_stays_paper(self):
        self.no_address()
        card = self.row_of(self.till_sale().invoice_number)
        self.assertEqual((card.delivery, card.delivery_reason), (PAPER, REASON_NO_EMAIL))
        self.an_address()
        cash = self.row_of(self.till_sale(method='cash').invoice_number)
        self.assertEqual((cash.delivery, cash.delivery_reason), (PAPER, REASON_CASH))
        self.store_mail.assert_not_called()

    def test_a_late_receipt_from_the_missing_receipts_screen_is_mailed(self):
        invoice = self.late_receipt()
        row = self.row_of(invoice.invoice_number)
        self.assertEqual((row.channel, row.delivery), (SignedOriginal.CHANNEL_IR, EMAIL))
        self.lesson_mail.assert_called_once()
        self.assertEqual(attached(self.lesson_mail.call_args), bytes(row.pdf))

    def test_a_late_receipt_for_a_family_without_an_address_goes_on_paper(self):
        self.no_address()
        invoice = self.late_receipt()
        row = self.row_of(invoice.invoice_number)
        self.assertEqual((row.delivery, row.delivery_reason), (PAPER, REASON_NO_EMAIL))
        self.lesson_mail.assert_not_called()

    def test_a_credit_note_for_a_family_without_an_address_goes_on_paper(self):
        self.no_address()
        with self.captureOnCommitCallbacks(execute=True), \
                patch('apps.core.credit_note_email.send_resend_email') as credit_mail:
            doc = service.create_credit_invoice({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'credit_invoice_details': {
                    'document_date': '2026-09-18', 'credit_reason': 'ביטול',
                    'credit_amount_before_vat': Decimal('50.00'), 'linked_invoice_id': 'IR-2026-000001',
                },
            })
        credit_mail.assert_not_called()
        self.assertEqual((self.row(doc).delivery, self.row(doc).delivery_reason), (PAPER, REASON_NO_EMAIL))

    def test_no_original_is_left_none(self):
        self.tax_invoice()
        self.receipt('מזומן')
        self.till_sale()
        self.late_receipt()
        self.no_address()
        self.receipt('אשראי')
        originals = SignedOriginal.objects.exclude(purpose=SignedOriginal.PURPOSE_ARCHIVE)
        self.assertEqual(originals.count(), 5)
        self.assertFalse(originals.filter(delivery=NONE).exists())
        self.assertFalse(originals.filter(channel='').exists())


# ── the cron: the paper list shrinks by itself ───────────────────────────────

@signing_on()
class PaperToEmailTests(DeliveryMixin, TestCase):
    def test_once_the_family_has_an_address_the_cron_mails_it_once(self):
        self.no_address()
        doc = self.receipt('אשראי')
        row = self.row(doc)
        self.assertEqual(row.delivery, PAPER)

        past_the_grace_period()
        self.assertEqual(sign_pending()['paper_to_email'], 0)  # still no address
        self.formal_mail.assert_not_called()

        self.an_address('later@example.com')
        summary = sign_pending()
        self.assertEqual((summary['paper_to_email'], summary['sent']), (1, 1))
        self.formal_mail.assert_called_once()
        self.assertEqual(self.formal_mail.call_args.kwargs['to'], ['later@example.com'])
        row.refresh_from_db()
        self.assertEqual((row.delivery, row.email_to), (EMAIL, 'later@example.com'))
        self.assertEqual(attached(self.formal_mail.call_args), bytes(row.pdf))

        again = sign_pending()
        self.assertEqual((again['paper_to_email'], again['sent']), (0, 0))
        self.formal_mail.assert_called_once()

    def test_cash_stays_on_paper_whatever_the_card_says(self):
        self.no_address()
        doc = self.receipt('מזומן')
        self.an_address()
        past_the_grace_period()
        sign_pending()
        self.formal_mail.assert_not_called()
        self.assertEqual((self.row(doc).delivery, self.row(doc).delivery_reason), (PAPER, REASON_CASH))

    def test_a_till_sale_is_mailed_once_the_family_has_an_address(self):
        self.no_address()
        sale = self.till_sale()
        self.an_address()
        past_the_grace_period()
        self.assertEqual(sign_pending()['paper_to_email'], 1)
        self.store_mail.assert_called_once()
        self.assertEqual(self.row_of(sale.invoice_number).delivery, EMAIL)

    def test_nothing_is_mailed_twice(self):
        doc = self.tax_invoice()
        self.formal_mail.assert_called_once()
        past_the_grace_period()
        sign_pending()
        sign_pending()
        self.formal_mail.assert_called_once()
        self.assertIsNone(service_claim(doc))

    def test_an_original_whose_mail_failed_is_sent_by_the_cron(self):
        self.formal_mail.side_effect = [RuntimeError('Resend failed (500)'), 'msg-id']
        doc = self.tax_invoice()
        row = self.row(doc)
        self.assertIsNone(row.sent_at)
        self.assertEqual(row.delivery, EMAIL)
        past_the_grace_period()
        self.assertEqual(sign_pending()['sent'], 1)
        self.assertEqual(self.formal_mail.call_count, 2)
        self.assertIsNotNone(self.row(doc).sent_at)


def service_claim(doc):
    from apps.documents.signing.service import KIND_FORMAL, claim_email

    return claim_email(KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_FORMAL, email_to='x@example.com')


# ── archive copies are untouched ─────────────────────────────────────────────

class ArchiveCopiesUntouchedTests(DeliveryMixin, APITestCase):
    def setUp(self):
        super().setUp()
        with self.settings(**archive_settings()):
            self.archive_row = sign_archive(SignedOriginal.KIND_IR, self.lesson_receipt('IR-2026-000001'))
        self.client.force_authenticate(self.manager)

    @signing_on()
    def test_the_cron_and_the_reroute_leave_it_none(self):
        past_the_grace_period()
        sign_pending()
        reroute_undelivered(apply=True)
        row = SignedOriginal.objects.get(pk=self.archive_row.pk)
        self.assertEqual((row.purpose, row.channel, row.delivery, row.sent_at),
                         (SignedOriginal.PURPOSE_ARCHIVE, '', NONE, None))
        self.lesson_mail.assert_not_called()

    @signing_on()
    def test_send_mails_a_copy_never_the_archive_bytes(self):
        response = self.client.post(send_url(self.archive_row), {}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['sent'], 'copy')
        # Drawn again as "העתק" and mailed through the document mail (document_email._deliver).
        self.formal_mail.assert_called_once()
        copy = attached(self.formal_mail.call_args)
        self.assertNotEqual(copy, bytes(self.archive_row.pdf))
        self.assertIn(COPY_MARK, lines(copy))
        row = SignedOriginal.objects.get(pk=self.archive_row.pk)
        self.assertEqual((row.sent_at, row.delivery), (None, NONE))


# ── "שלח / שלח שוב" ──────────────────────────────────────────────────────────

@signing_on()
class SendEndpointTests(DeliveryMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def post(self, row, **body):
        return self.client.post(send_url(row), body, format='json')

    def test_the_original_once_then_a_copy(self):
        self.no_address()
        doc = self.receipt('אשראי')
        row = self.row(doc)
        self.assertEqual(row.delivery, PAPER)

        first = self.post(row, email='typed@example.com')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()['sent'], 'original')
        self.assertEqual(first.json()['email'], 'typed@example.com')
        self.formal_mail.assert_called_once()
        row.refresh_from_db()
        self.assertEqual((row.delivery, row.email_to), (EMAIL, 'typed@example.com'))
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(attached(self.formal_mail.call_args), bytes(row.pdf))
        sent_at = row.sent_at

        again = self.post(row)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again.json()['sent'], 'copy')
        self.assertEqual(self.formal_mail.call_count, 2)
        copy = attached(self.formal_mail.call_args)
        self.assertNotEqual(copy, bytes(row.pdf))
        self.assertIn(COPY_MARK, lines(copy))
        self.assertNotIn(ORIGINAL_MARK, lines(copy))
        self.assertNotIn(SIGNED_MARK, ' '.join(lines(copy)))
        self.assertIn(COPY_MARK, self.formal_mail.call_args.kwargs['subject'])
        row.refresh_from_db()
        self.assertEqual((row.sent_at, row.send_attempts), (sent_at, 1))  # the original's record stands

    def test_a_printed_original_is_sent_as_a_copy(self):
        doc = self.receipt('מזומן')
        row = self.row(doc)
        self.assertEqual(self.client.post(f'{ORIGINALS}{row.pk}/print-original/').status_code, 200)
        response = self.post(row)
        self.assertEqual((response.status_code, response.json()['sent']), (200, 'copy'))
        self.assertIn(COPY_MARK, lines(attached(self.formal_mail.call_args)))

    def test_an_original_that_goes_on_paper_is_not_mailed(self):
        doc = self.receipt('מזומן')
        response = self.post(self.row(doc))
        self.assertEqual((response.status_code, response.json()), (409, {'error': REASON_CASH}))
        self.formal_mail.assert_not_called()
        self.assertIsNone(self.row(doc).sent_at)

    def test_no_address_and_a_bad_address(self):
        self.no_address()
        row = self.row(self.receipt('אשראי'))
        self.assertEqual((self.post(row).status_code, self.post(row).json()), (400, {'error': NO_ADDRESS}))
        self.assertEqual(self.post(row, email='not-an-address').status_code, 400)
        self.formal_mail.assert_not_called()

    def test_a_lesson_receipt_and_a_till_sale_through_their_own_exits(self):
        self.no_address()
        invoice = self.late_receipt()
        sale = self.till_sale()
        for number, mail in ((invoice.invoice_number, self.lesson_mail), (sale.invoice_number, self.store_mail)):
            response = self.post(self.row_of(number), email='typed@example.com')
            self.assertEqual((response.status_code, response.json()['sent']), (200, 'original'), number)
            mail.assert_called_once()
            self.assertEqual(mail.call_args.kwargs['to'], ['typed@example.com'])
            self.assertEqual(attached(mail.call_args), bytes(self.row_of(number).pdf))

    def test_an_original_mailed_at_issue_is_never_sent_again_as_an_original(self):
        doc = self.tax_invoice()
        self.formal_mail.assert_called_once()
        sent_at = self.row(doc).sent_at
        response = self.post(self.row(doc))
        self.assertEqual(response.json()['sent'], 'copy')
        self.assertEqual(self.formal_mail.call_count, 2)
        self.assertIn(COPY_MARK, lines(attached(self.formal_mail.call_args)))
        self.assertEqual(self.row(doc).sent_at, sent_at)

    def test_unknown_row_is_404(self):
        import uuid

        self.assertEqual(self.client.post(f'{ORIGINALS}{uuid.uuid4()}/send/').status_code, 404)

    def test_managers_only(self):
        row = self.row(self.receipt('מזומן'))
        self.client.force_authenticate(None)
        self.assertEqual(self.post(row).status_code, 401)
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            self.client.force_authenticate(make_user(f'{role}-send@test', role))
            self.assertEqual(self.post(row).status_code, 403, role)
        self.formal_mail.assert_not_called()


# ── the office's download is a copy ──────────────────────────────────────────

@signing_on()
class OfficeFileIsACopyTests(DeliveryMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_a_copy_of_an_original_on_request_and_the_stored_bytes_by_default(self):
        row = self.row(self.receipt('מזומן'))
        response = self.client.get(file_url(row), {'copy': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertNotEqual(response.content, bytes(row.pdf))
        self.assertIn(COPY_MARK, lines(response.content))
        self.assertNotIn(ORIGINAL_MARK, lines(response.content))
        self.assertFalse(response.has_header('X-Content-SHA256'))
        self.assertFalse(SignedFileAccess.objects.exists())  # the stored file did not leave

        # By default (a screen built before copies existed asks nothing) and with ?original=1.
        for params in ({}, {'original': '1'}):
            stored = self.client.get(file_url(row), params)
            self.assertEqual(stored.content, bytes(row.pdf))
            self.assertEqual(stored['X-Content-SHA256'], row.sha256)
        self.assertEqual(set(SignedFileAccess.objects.values_list('original_id', flat=True)), {row.pk})
        # Neither is the original's one print.
        self.assertIsNone(SignedOriginal.objects.get(pk=row.pk).paper_original_printed_at)

    def test_the_accountants_export_keeps_the_signed_originals(self):
        row = self.row(self.receipt('מזומן'))
        response = self.client.get(f'{ORIGINALS}export/')
        self.assertEqual(response.status_code, 200)
        import zipfile

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(archive.read(f'{row.number}.pdf'), bytes(row.pdf))


# ── the 'none' rows of before ────────────────────────────────────────────────

@signing_on()
class RerouteUndeliveredTests(DeliveryMixin, TestCase):
    def as_before(self, doc):
        """The row as the code before 25.9.2026 left it: no channel, no address of its own, 'none'."""
        SignedOriginal.objects.filter(number=doc.document_number).update(
            channel='', email_to='', delivery=NONE, delivery_reason='המערכת אינה שולחת מסמך זה במייל — המקור החתום שמור בארכיון',
            sent_at=None,
        )

    def test_dry_run_counts_and_apply_reroutes_then_the_cron_mails(self):
        self.formal_mail.side_effect = RuntimeError('not at issue')  # as if it was never mailed
        card = self.receipt('אשראי')
        self.formal_mail.side_effect = None
        cash = self.receipt('מזומן')
        self.no_address()
        orphan = self.receipt('העברה בנקאית')
        for doc in (card, cash, orphan):
            self.as_before(doc)
        self.formal_mail.reset_mock()

        # No address on the card yet: all three would go on the paper list.
        out = io.StringIO()
        call_command('reroute_undelivered', stdout=out)
        self.assertIn('הרצה יבשה', out.getvalue())
        self.assertIn('נבדקו 3', out.getvalue())
        self.assertEqual(SignedOriginal.objects.filter(delivery=NONE).count(), 3)  # nothing written
        counts = reroute_undelivered(apply=False)
        self.assertEqual((counts['examined'], counts[EMAIL], counts[PAPER]), (3, 0, 3))

        # With an address: the card and the transfer receipts by mail, cash on paper.
        self.an_address()
        counts = reroute_undelivered(apply=False)
        self.assertEqual((counts[EMAIL], counts[PAPER]), (2, 1))
        call_command('reroute_undelivered', '--apply', stdout=io.StringIO())
        self.assertFalse(SignedOriginal.objects.filter(delivery=NONE).exists())
        self.assertEqual(self.row(cash).delivery_reason, REASON_CASH)
        self.assertEqual((self.row(card).channel, self.row(card).delivery), (SignedOriginal.CHANNEL_FORMAL, EMAIL))
        self.formal_mail.assert_not_called()  # the command mails nothing itself

        past_the_grace_period()
        self.assertEqual(sign_pending()['sent'], 2)  # the card and the transfer receipt
        self.assertEqual(self.formal_mail.call_count, 2)

    def test_since_limits_it(self):
        doc = self.receipt('מזומן')
        self.as_before(doc)
        from datetime import date

        self.assertEqual(reroute_undelivered(since=date(2026, 9, 19))['examined'], 0)
        self.assertEqual(reroute_undelivered(since=date(2026, 9, 18))['examined'], 1)

    def test_apply_and_dry_run_together_are_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('reroute_undelivered', '--apply', '--dry-run', stdout=io.StringIO())


@signing_on()
class FormalMailBodyTests(DeliveryMixin, TestCase):
    def test_the_mail_names_the_type_and_number_and_escapes_the_name(self):
        from apps.documents.document_email import build_document_email

        subject, text, html = build_document_email(
            label='קבלה', number='RC-2026-000009', customer_name='<b>כהן</b>',
            document_date=timezone.localdate(), total=Decimal('100.00'),
        )
        self.assertEqual(subject, 'קבלה RC-2026-000009 — קוגומלו')
        self.assertIn('RC-2026-000009', text)
        self.assertIn('₪100.00', text)
        self.assertIn('מסמך ממוחשב', html)
        self.assertNotIn('<b>כהן</b>', html)
        copy_subject, _text, _html = build_document_email(label='קבלה', number='RC-2026-000009',
                                                          customer_name='', copy=True)
        self.assertTrue(copy_subject.startswith(COPY_MARK))

    def test_a_business_customers_document_goes_to_the_business_address(self):
        from apps.customers.models import BusinessCustomer

        customer = BusinessCustomer.objects.create(first_name='סטודיו', last_name='אור', email='or@example.com')
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_invoice({
                'client_type': 'business', 'business_customer_id': str(customer.pk),
                'invoice_details': {'document_date': '2026-09-18',
                                    'line_items': [{'description': 'ייעוץ', 'quantity': 1, 'price': 900}]},
            }, 'tax_invoice')
        self.formal_mail.assert_called_once()
        self.assertEqual(self.formal_mail.call_args.kwargs['to'], ['or@example.com'])
        self.assertEqual(FormalDocument.objects.get(pk=doc.pk).allocation_number, '')
