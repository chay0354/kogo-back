"""
Where a signed original goes, and that every mail carries exactly the stored bytes.

הוראה 18ב(ד): with a secured (not approved) signature, a document is mailed only
for a payment by the customer's card, a check crossed "לא סחיר" in the
customer's name, or a transfer from the customer's account — cash and an
unmarked check send the original on paper. 18ב(ג): consent, reported or
enforced. And the five mail exits: lesson receipt (IR), website sale (ST),
manual credit note, refund credit note, rental receipt (RT).
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.computerized_docs import CONSENT_SOURCE_CRM, record_consent
from apps.core.models import UserProfile
from apps.core.payment_service import PaymentService, _sign_store_sale
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import BusinessCustomer, Payment, TranzilaTransaction
from apps.customers.recurring_billing import process_due_recurring_charges
from apps.customers.subscription_invoice_email import send_subscription_invoice_email
from apps.customers.subscription_invoice_pdf import ORIGINAL_PRODUCED
from apps.customers.tests.test_charge_survives_receipt_failure import TOKEN_CHARGE_OK, _due_standing_order
from apps.documents import service
from apps.documents.cash_plans import register_cash_plan
from apps.documents.check_plans import register_check_plan
from apps.documents.issuer import COPY_MARK, ORIGINAL_MARK, SIGNED_MARK
from apps.documents.models import FormalDocument, SignedOriginal
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import LocalKeyBackend
from apps.documents.signing.service import (
    ALREADY_MAILED, ALREADY_PRINTED, NOT_SIGNED_YET, REASON_ARCHIVE, REASON_NO_CONSENT,
    REASON_NO_CONSENT_REPORTED, REASON_PENDING, REASON_PRINTED, REASON_SENT, STORED_FILE_BROKEN,
    claim_email, clear_inline_budget, delivery_decision, reset_inline_budget, sign_pending,
)
from apps.documents.signing.sources import (
    REASON_CASH, REASON_CASH_PLAN, REASON_CHECK_NOT_CROSSED, REASON_UNKNOWN_METHOD,
)
from apps.documents.tests.signing_support import attachment_bytes, pdf_text, signing_on
from apps.documents.tests.test_register import RegisterFixture, make_user
from apps.rental_billing.billing import charge_due
from apps.rental_billing.models import TenantCharge
from apps.rental_billing.tests.factories import BillingFixture
from apps.store.invoice_email import send_store_invoice_email
from apps.store.models import StoreInvoice

User = get_user_model()
FORMAL = SignedOriginal.KIND_FORMAL
CRON = '/api/v1/documents/cron/sign-pending/'
STATUS = '/api/v1/documents/signing/status/'
ORIGINALS = '/api/v1/documents/signing/originals/'
CERTIFICATE = '/api/v1/documents/signing/certificate/'


def print_url(row) -> str:
    return f'{ORIGINALS}{row.pk}/print-original/'


def past_the_grace_period():
    """The cron leaves a fresh row to the request that issued it; the tests do not wait."""
    SignedOriginal.objects.update(created_at=timezone.now() - timedelta(minutes=10))


class ReceiptsMixin(RegisterFixture):
    def setUp(self):
        super().setUp()
        self.family.email = 'parent@example.com'
        self.family.save(update_fields=['email'])

    def receipt(self, method, **details):
        amounts = {'מזומן': 'cash_amount', 'אשראי': 'card_amount', 'העברה בנקאית': 'bank_amount'}
        body = {'payment_method': method, **details}
        if method in amounts:
            body.setdefault(amounts[method], '100.00')
        with self.captureOnCommitCallbacks(execute=True):
            return service.create_receipt({
                'client_type': 'existing', 'child_id': str(self.kid.id), 'document_date': '2026-09-18',
                'receipt_details': body,
            })

    def check_receipt(self, crossed):
        check = {'amount': 100, 'confirmed': True, 'date': '2026-09-20', 'check_number': '000123', 'bank': '12'}
        if crossed is not None:
            check['check_crossed'] = crossed
        return self.receipt("צ'ק", checks=[check])

    def row(self, doc) -> SignedOriginal:
        return SignedOriginal.objects.get(number=doc.document_number)


# ── 18ב(ד): how it was paid decides where the original goes ──────────────────

@signing_on()
class PaymentMeansTests(ReceiptsMixin, APITestCase):
    def test_cash_goes_on_paper(self):
        doc = self.receipt('מזומן')
        row = self.row(doc)
        self.assertTrue(row.is_signed)  # the original exists — it is only never mailed
        self.assertEqual((row.delivery, row.delivery_reason), (SignedOriginal.DELIVERY_PAPER, REASON_CASH))
        self.assertEqual(delivery_decision(FORMAL, doc, channel=SignedOriginal.CHANNEL_CREDIT_NOTE)[0], 'paper')
        self.assertIsNone(claim_email(FORMAL, doc, channel=SignedOriginal.CHANNEL_CREDIT_NOTE, email_to='p@example.com'))

    def test_a_check_not_marked_crossed_goes_on_paper(self):
        for crossed in (None, False):
            doc = self.check_receipt(crossed)
            self.assertFalse(doc.payments.get().check_crossed)
            row = self.row(doc)
            self.assertEqual((row.delivery, row.delivery_reason),
                             (SignedOriginal.DELIVERY_PAPER, REASON_CHECK_NOT_CROSSED))

    def test_a_crossed_check_a_card_and_a_transfer_may_be_mailed_the_stored_bytes(self):
        docs = [self.check_receipt(True), self.receipt('אשראי'), self.receipt('העברה בנקאית')]
        self.assertTrue(docs[0].payments.get().check_crossed)
        for doc in docs:
            row = self.row(doc)
            # kogo does not mail a receipt issued by hand: its original is archived …
            self.assertEqual((row.delivery, row.delivery_reason), (SignedOriginal.DELIVERY_NONE, REASON_ARCHIVE))
            # … and were it mailed, the mail would carry exactly the stored original.
            self.assertEqual(delivery_decision(FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL)[0], 'email')
            claim = claim_email(FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL, email_to='p@example.com')
            self.assertEqual(claim.pdf, bytes(SignedOriginal.objects.get(pk=row.pk).pdf))

    def test_a_combined_document_paid_by_a_crossed_check(self):
        payload = {
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {
                'document_date': '2026-09-18', 'line_items': [{'description': 'חוג', 'quantity': 1, 'price': 100}],
                'payment_methods': ["צ'ק"], 'check_crossed': True,
            },
        }
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_combined(payload)
        self.assertTrue(doc.payments.get().check_crossed)
        self.assertEqual(delivery_decision(FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL)[0], 'email')

    def test_a_cash_plans_monthly_document_counts_as_cash(self):
        with self.captureOnCommitCallbacks(execute=True):
            plan = register_cash_plan(
                child_id=str(self.kid.id), total_amount='480.00', monthly_amount='240.00',
                start_month=timezone.localdate().replace(day=1),
            )
        month = plan.months.filter(status='invoiced').first()
        self.assertEqual(month.document.payments.count(), 0)  # no payment line on it
        self.assertEqual((self.row(month.document).delivery, self.row(month.document).delivery_reason),
                         (SignedOriginal.DELIVERY_PAPER, REASON_CASH_PLAN))
        self.assertEqual(self.row(plan.receipt).delivery_reason, REASON_CASH)

    def test_a_check_plans_monthly_invoice_follows_its_checks(self):
        today = timezone.localdate()
        for crossed, expected in ((False, SignedOriginal.DELIVERY_PAPER), (True, SignedOriginal.DELIVERY_NONE)):
            with self.captureOnCommitCallbacks(execute=True):
                plan = register_check_plan(child_id=str(self.kid.id), checks=[
                    {'date': str(today), 'amount': '240', 'check_number': '1', 'check_crossed': crossed},
                ])
            invoice = plan.items.get().tax_invoice
            self.assertEqual(invoice.document_type, 'tax_invoice')
            self.assertEqual(self.row(invoice).delivery, expected)

    def test_a_receipt_that_names_no_means_goes_on_paper(self):
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_combined({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'invoice_details': {'document_date': '2026-09-18',
                                    'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
            })
        self.assertEqual(self.row(doc).delivery_reason, REASON_UNKNOWN_METHOD)

    def dialog_receipt(self, checks):
        """A receipt exactly as the document dialog sends it (NewDocumentDialog/utils.ts receiptDetailsPayload)."""
        return {
            'document_type': 'receipt',
            'client_type': 'existing',
            'child_id': str(self.kid.id),
            'business_customer_id': None,
            'branch_id': None,
            'receipt_details': {
                'payment_method': "צ'ק",
                'linked_invoice_id': '',
                'cash_amount': 0,
                'cash_notes': '',
                'checks': checks,
                'withholding': 0,
                'check_notes': '',
                'card_last_four': '',
                'card_expiry': '',
                'card_amount': 0,
                'card_installments': 1,
                'card_notes': '',
                'bank_date': None,
                'bank_reference': '',
                'bank_amount': 0,
                'bank_notes': '',
            },
        }

    @staticmethod
    def dialog_check(number, amount, crossed, date='2026-09-20'):
        return {
            'date': date, 'bank': '12', 'branch': '600', 'account_number': '456789',
            'check_number': number, 'amount': amount, 'confirmed': True, 'check_crossed': crossed,
        }

    def post_document(self, payload):
        self.client.force_authenticate(self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/v1/documents/documents/create-document/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_the_dialogs_receipt_payload_takes_check_crossed_per_check(self):
        body = self.post_document(self.dialog_receipt([
            self.dialog_check('000123', 150, True),
            self.dialog_check('000124', 150, False, date='2026-10-20'),
        ]))
        payments = sorted(body['payments'], key=lambda p: p['reference'])
        self.assertEqual([(p['reference'], p['amount'], p['check_crossed']) for p in payments],
                         [('000123', '150.00', True), ('000124', '150.00', False)])
        self.assertEqual(body['total_amount'], '300.00')
        # One unmarked check is enough to send the original on paper …
        doc = FormalDocument.objects.get(pk=body['id'])
        self.assertEqual(self.row(doc).delivery_reason, REASON_CHECK_NOT_CROSSED)

    def test_the_dialogs_receipt_with_every_check_crossed(self):
        body = self.post_document(self.dialog_receipt([self.dialog_check('000200', 236, True)]))
        doc = FormalDocument.objects.get(pk=body['id'])
        # … and with every check crossed it may go by mail (kogo archives a hand-issued receipt).
        self.assertEqual(self.row(doc).delivery, SignedOriginal.DELIVERY_NONE)
        self.assertEqual(delivery_decision(FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL)[0], 'email')

    def test_the_dialogs_receipt_with_signing_off_is_issued_as_before(self):
        with override_settings(DOCUMENT_SIGNING_ENABLED=False):
            body = self.post_document(self.dialog_receipt([self.dialog_check('000300', 100, True)]))
        self.assertEqual(body['payments'][0]['check_crossed'], True)
        self.assertFalse(SignedOriginal.objects.exists())

    def combined_payload(self, **top):
        return {
            'document_type': 'combined', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'business_customer_id': None, 'branch_id': None,
            'invoice_details': {
                'document_date': '2026-09-18', 'due_date': None, 'description': '', 'currency': 'ILS',
                'prices_include_vat': True,
                'line_items': [{'sku': '', 'description': 'חוג', 'quantity': 1, 'price': 236}],
                'discount_amount': 0, 'discount_percent': 0, 'vat_exempt': False, 'round_total': False,
                'payment_terms': '', 'customer_notes': '', 'internal_notes': '',
                'payment_methods': ["צ'ק"],
            },
            **top,
        }

    def test_a_combined_payload_says_its_check_is_crossed_at_the_top(self):
        crossed = FormalDocument.objects.get(pk=self.post_document(self.combined_payload(check_crossed=True))['id'])
        self.assertTrue(crossed.payments.get().check_crossed)
        self.assertEqual(delivery_decision(FORMAL, crossed, channel=SignedOriginal.CHANNEL_RENTAL)[0], 'email')

        plain = FormalDocument.objects.get(pk=self.post_document(self.combined_payload())['id'])
        self.assertFalse(plain.payments.get().check_crossed)
        self.assertEqual(self.row(plain).delivery_reason, REASON_CHECK_NOT_CROSSED)


# ── the mail exits ───────────────────────────────────────────────────────────

class LessonPaymentMixin:
    def setUp(self):
        super().setUp()
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)

    def charged_payment(self, key='1', transaction_id=True):
        payment = Payment.objects.create(
            child=self.child, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('236.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('236.00'),
            payment_date=timezone.now(),
        )
        if transaction_id:
            payment.tranzila_transaction = TranzilaTransaction.objects.create(
                transaction_id=f'TRX_{key}', confirmation_code=f'AUTH_{key}', transaction_type='recurring_charge',
                is_successful=True, idempotency_key=f'signing-{key}',
            )
            payment.save(update_fields=['tranzila_transaction'])
        return payment

    def lesson_receipt(self, payment=None, **kwargs):
        payment = payment or self.charged_payment()
        with self.captureOnCommitCallbacks(execute=True):
            return PaymentService()._create_invoice_from_payment(payment, payment.tranzila_transaction, **kwargs)


@signing_on()
@patch('apps.customers.subscription_invoice_email.send_resend_email')
class LessonReceiptMailTests(LessonPaymentMixin, TestCase):
    def test_a_card_receipt_is_mailed_as_the_stored_signed_original(self, resend):
        invoice = self.lesson_receipt()
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        resend.assert_called_once()
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertTrue(row.pdf_intact())
        self.assertEqual((row.kind, row.channel, row.delivery), ('ir', 'ir', 'email'))
        self.assertIsNotNone(row.sent_at)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.email_sent_at)
        # Mailed again by nobody.
        Invoice.objects.filter(pk=invoice.pk).update(email_sent_at=None)
        self.assertFalse(send_subscription_invoice_email(Invoice.objects.get(pk=invoice.pk)))
        resend.assert_called_once()

    def test_consent_is_reported_while_not_enforced(self, resend):
        with self.assertLogs('apps.core.computerized_docs', 'WARNING'):
            invoice = self.lesson_receipt()
        resend.assert_called_once()
        self.assertEqual(SignedOriginal.objects.get(number=invoice.invoice_number).delivery_reason,
                         REASON_NO_CONSENT_REPORTED)

    def test_with_consent_on_record_it_just_goes(self, resend):
        record_consent(self.family, CONSENT_SOURCE_CRM)
        invoice = self.lesson_receipt()
        resend.assert_called_once()
        self.assertEqual(SignedOriginal.objects.get(number=invoice.invoice_number).delivery_reason, REASON_SENT)

    @signing_on(COMPUTERIZED_CONSENT_ENFORCED=True, CRON_TOKEN='cron-secret')
    def test_enforced_consent_holds_the_mail_until_consent_is_recorded(self, resend):
        invoice = self.lesson_receipt()
        resend.assert_not_called()
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertTrue(row.is_signed)
        self.assertEqual((row.delivery, row.delivery_reason), ('held', REASON_NO_CONSENT))

        past_the_grace_period()
        self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret')
        resend.assert_not_called()  # still no consent

        record_consent(self.family, CONSENT_SOURCE_CRM)
        response = self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(response.json()['summary']['sent'], 1)
        resend.assert_called_once()
        self.assertEqual(attachment_bytes(resend), bytes(SignedOriginal.objects.get(pk=row.pk).pdf))

    def test_a_receipt_paid_in_cash_is_never_mailed(self, resend):
        invoice = Invoice.objects.create(
            invoice_number='IR-2026-000900', family=self.family, amount=Decimal('236.00'), status='paid',
            payment_method='cash', payment_type='manual', payer_email='parent@example.com',
            invoice_date=timezone.now(),
        )
        self.assertFalse(send_subscription_invoice_email(invoice))
        resend.assert_not_called()
        row = SignedOriginal.objects.get(number='IR-2026-000900')
        self.assertTrue(row.is_signed)
        self.assertEqual((row.delivery, row.delivery_reason), ('paper', REASON_CASH))

    def test_a_receipt_with_no_recorded_means_goes_on_paper(self, resend):
        invoice = self.lesson_receipt(self.charged_payment(transaction_id=False))
        resend.assert_not_called()
        self.assertEqual(SignedOriginal.objects.get(number=invoice.invoice_number).delivery_reason,
                         REASON_UNKNOWN_METHOD)

    def test_a_receipt_issued_without_mail_is_archived(self, resend):
        invoice = self.lesson_receipt(send_email=False)
        resend.assert_not_called()
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertEqual((row.channel, row.delivery), ('', 'none'))


@signing_on(CRON_TOKEN='cron-secret')
@patch('apps.customers.subscription_invoice_email.send_resend_email')
class HeldThenSignedByTheCronTests(TestCase):
    def test_a_key_out_of_reach_never_touches_the_charge_and_the_cron_signs_and_mails_later(self, resend):
        recurring = _due_standing_order()
        family = recurring.child.family
        family.email = 'parent@example.com'
        family.save(update_fields=['email'])

        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge, \
                patch.object(LocalKeyBackend, 'sign_digest',
                             side_effect=SigningUnavailable('KMS asymmetricSign: ReadTimeout')), \
                self.captureOnCommitCallbacks(execute=True):
            summary = process_due_recurring_charges()

        # The charge and its receipt stand; nothing unsigned went out.
        self.assertEqual((summary['charged'], summary['errors']), (1, []))
        self.assertEqual(charge.call_count, 1)
        monthly = Payment.objects.filter(child=recurring.child).exclude(id=recurring.initial_payment_id).get()
        self.assertEqual(monthly.status, 'completed')
        invoice = Invoice.objects.get(payment=monthly)
        resend.assert_not_called()
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertFalse(row.is_signed)
        self.assertEqual(row.delivery, 'held')
        self.assertIsNone(Invoice.objects.get(pk=invoice.pk).email_sent_at)

        past_the_grace_period()
        response = self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['summary']['signed'], 1)
        self.assertEqual(response.json()['summary']['sent'], 1)
        row.refresh_from_db()
        self.assertTrue(row.is_signed)
        resend.assert_called_once()
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertIsNotNone(Invoice.objects.get(pk=invoice.pk).email_sent_at)

        again = self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret').json()['summary']
        self.assertEqual((again['signed'], again['sent']), (0, 0))
        resend.assert_called_once()

    def test_the_cron_wants_its_token_and_is_idle_while_signing_is_off(self, resend):
        self.assertEqual(self.client.get(CRON).status_code, 401)
        with override_settings(DOCUMENT_SIGNING_ENABLED=False):
            response = self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(response.json()['summary'], {'disabled': True})


@signing_on()
@patch('apps.store.invoice_email.send_resend_email')
class WebsiteSaleMailTests(TestCase):
    def sale(self, **fields):
        values = dict(
            customer_name='קונה', customer_email='buyer@example.com', total_amount=Decimal('49.00'),
            payment_method='credit_card', payment_status='completed', website_order_number='W-1001',
        )
        values.update(fields)
        return StoreInvoice.objects.create(**values)

    def test_a_paid_website_sale_is_mailed_as_its_stored_original(self, resend):
        invoice = self.sale()
        with self.captureOnCommitCallbacks(execute=True):
            _sign_store_sale(invoice)
        self.assertTrue(send_store_invoice_email(invoice))
        row = SignedOriginal.objects.get(number=invoice.invoice_number)
        self.assertEqual((row.kind, row.channel, row.email_to), ('store', 'store', 'buyer@example.com'))
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertIsNotNone(StoreInvoice.objects.get(pk=invoice.pk).invoice_email_sent_at)

    def test_a_till_sale_is_archived_and_a_pending_one_not_recorded(self, resend):
        till = self.sale(website_order_number=None, customer_email='', payment_method='cash')
        pending = self.sale(website_order_number=None, payment_status='pending')
        with self.captureOnCommitCallbacks(execute=True):
            _sign_store_sale(till)
            _sign_store_sale(pending)
        row = SignedOriginal.objects.get(number=till.invoice_number)
        self.assertEqual((row.channel, row.delivery, row.delivery_reason), ('', 'paper', REASON_CASH))
        self.assertFalse(SignedOriginal.objects.filter(number=pending.invoice_number).exists())
        resend.assert_not_called()


@signing_on()
@patch('apps.core.credit_note_email.send_resend_email')
class CreditNoteMailTests(ReceiptsMixin, TestCase):
    def credit_payload(self):
        return {
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': date(2026, 9, 18), 'credit_reason': 'ביטול הרשמה',
                'credit_amount_before_vat': Decimal('100.00'), 'linked_invoice_id': 'IR-2026-000001',
            },
        }

    def test_a_manual_credit_note_is_mailed_after_commit_and_only_once(self, resend):
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_credit_invoice(self.credit_payload())
            # Still inside the transaction that numbered it: nothing has gone out.
            resend.assert_not_called()
        resend.assert_called_once()
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertEqual((row.channel, row.email_to, row.delivery), ('credit_note', 'parent@example.com', 'email'))

        # Neither the exit again nor the cron mails it a second time.
        self.assertFalse(service._email_credit_note(doc))
        past_the_grace_period()
        sign_pending()
        resend.assert_called_once()

    def test_a_credit_note_whose_transaction_rolls_back_is_never_mailed(self, resend):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    service.create_credit_invoice(self.credit_payload())
                    raise RuntimeError('the request failed after the document')
            except RuntimeError:
                pass
        resend.assert_not_called()
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_refunds_credit_note_goes_to_the_address_its_caller_gave(self, resend):
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.issue_refund_credit_note(
                gross_amount=Decimal('49.00'), reason='החזר מוצר', original_number='ST-2026-000001',
                customer_name='קונה מזדמן', email='walkin@example.com',
            )
        resend.assert_called_once()
        self.assertEqual(resend.call_args.kwargs['to'], ['walkin@example.com'])
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertEqual((row.email_to, row.customer_name), ('walkin@example.com', 'קונה מזדמן'))
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))

    def test_a_failed_mail_is_given_back_and_the_cron_sends_it(self, resend):
        resend.side_effect = [RuntimeError('Resend failed (500)'), 'msg-id']
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_credit_invoice(self.credit_payload())
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertIsNone(row.sent_at)
        self.assertIn('RuntimeError', row.last_error)
        past_the_grace_period()
        self.assertEqual(sign_pending()['sent'], 1)
        self.assertEqual(resend.call_count, 2)
        self.assertIsNotNone(SignedOriginal.objects.get(pk=row.pk).sent_at)


@signing_on(RENTAL_BILLING_ENABLED=True)
class RentalReceiptMailTests(BillingFixture, APITestCase):
    def test_the_rental_receipt_is_mailed_as_its_stored_original(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        with patch('apps.rental_billing.receipt_email.send_resend_email') as resend, \
                self.captureOnCommitCallbacks(execute=True):
            charge_due(today=date(2026, 10, 10))
        charge = TenantCharge.objects.get()
        self.assertEqual(charge.status, TenantCharge.STATUS_CHARGED)
        row = SignedOriginal.objects.get(number=charge.receipt.document_number)
        self.assertEqual((row.channel, row.delivery), ('rental', 'email'))
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertIsNotNone(charge.receipt_emailed_at)

    def test_a_tenant_with_consent_on_record(self):
        record_consent(self.tenancy.tenant, CONSENT_SOURCE_CRM)
        self.active_order(next_charge_date=date(2026, 10, 10))
        with patch('apps.rental_billing.receipt_email.send_resend_email'), \
                self.captureOnCommitCallbacks(execute=True):
            charge_due(today=date(2026, 10, 10))
        row = SignedOriginal.objects.get(number=TenantCharge.objects.get().receipt.document_number)
        self.assertEqual(row.delivery_reason, REASON_SENT)


# ── Tranzila is not handed the address once kogo mails signed originals ─────

class TranzilaGetsNoEmailTests(ReceiptsMixin, TestCase):
    def issue(self):
        with patch('apps.core.tranzila_service.TranzilaService.create_formal_document', return_value={}) as create, \
                patch('apps.core.tranzila_service.TranzilaService.parse_billing_document_response',
                      return_value={'success': False}):
            with self.captureOnCommitCallbacks(execute=True):
                self.receipt('אשראי')
        return create.call_args.kwargs['client_email']

    @signing_on(TRANZILA_BILLING_TERMINAL='billing-terminal')
    def test_signing_on(self):
        self.assertEqual(self.issue(), '')

    @override_settings(TRANZILA_BILLING_TERMINAL='billing-terminal', DOCUMENT_SIGNING_ENABLED=False)
    def test_signing_off_as_before(self):
        self.assertEqual(self.issue(), 'parent@example.com')


# ── the inline budget ───────────────────────────────────────────────────────

@signing_on(SIGNING_INLINE_BUDGET=1)
class InlineBudgetTests(ReceiptsMixin, TestCase):
    def test_a_request_signs_its_share_of_archive_originals_and_the_cron_the_rest(self):
        reset_inline_budget()
        try:
            first = self.receipt('אשראי')
            second = self.receipt('אשראי')
        finally:
            clear_inline_budget()
        self.assertTrue(self.row(first).is_signed)
        self.assertFalse(self.row(second).is_signed)
        self.assertEqual(self.row(second).delivery_reason, REASON_PENDING)
        self.assertEqual(sign_pending()['signed'], 1)
        self.assertTrue(self.row(second).is_signed)


# ── the office: copies, the original on paper, the lists ─────────────────────

@signing_on()
class OfficeDownloadsAreCopiesTests(LessonPaymentMixin, ReceiptsMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def assert_copy(self, response):
        self.assertEqual(response.status_code, 200)
        # The mark stands on a line of its own ("המקורי" in a sentence is not it).
        lines = [line.strip() for line in pdf_text(response.content).splitlines()]
        self.assertIn(COPY_MARK, lines)
        self.assertNotIn(ORIGINAL_MARK, lines)
        self.assertNotIn(SIGNED_MARK, ' '.join(lines))

    def test_a_hand_issued_document(self):
        doc = self.receipt('אשראי')
        self.assert_copy(self.client.get(f'/api/v1/documents/documents/{doc.pk}/pdf/'))

    @patch('apps.customers.subscription_invoice_email.send_resend_email')
    def test_a_lesson_receipt_by_either_door(self, _resend):
        invoice = self.lesson_receipt(send_email=False)
        self.assert_copy(self.client.get(f'/api/v1/customers/invoices/{invoice.pk}/pdf/'))
        self.assert_copy(self.client.get(f'/api/v1/customers/payments/{invoice.payment_id}/invoice/'))
        # The download no longer stands in for the original.
        self.assertFalse(InvoiceActivityLog.objects.filter(invoice=invoice, action=ORIGINAL_PRODUCED).exists())

    def test_a_till_sale_never_mailed(self):
        sale = StoreInvoice.objects.create(
            customer_name='קונה', total_amount=Decimal('49.00'), payment_method='credit_card',
            payment_status='completed',
        )
        self.assert_copy(self.client.get(f'/api/v1/store/invoices/{sale.pk}/download/'))

    @override_settings(DOCUMENT_SIGNING_ENABLED=False)
    def test_signing_off_prints_as_before(self):
        doc = self.receipt('אשראי')
        response = self.client.get(f'/api/v1/documents/documents/{doc.pk}/pdf/')
        self.assertIn(ORIGINAL_MARK, [line.strip() for line in pdf_text(response.content).splitlines()])


@signing_on()
class PrintOriginalTests(ReceiptsMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)

    def test_the_stored_original_is_printed_once_and_then_refused(self):
        doc = self.receipt('מזומן')
        row = self.row(doc)
        response = self.client.post(print_url(row))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response.content, bytes(row.pdf))
        row.refresh_from_db()
        self.assertIsNotNone(row.paper_original_printed_at)
        self.assertEqual(row.paper_original_printed_by, self.manager)
        self.assertEqual(row.delivery_reason, REASON_CASH)  # why it went on paper stays

        again = self.client.post(print_url(row))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {'error': ALREADY_PRINTED})
        self.assertEqual(ALREADY_PRINTED, 'המקור כבר הודפס — כל הדפסה נוספת היא העתק')

    def test_an_original_printed_is_never_mailed_after(self):
        doc = self.receipt('אשראי')
        row = self.row(doc)
        self.client.post(print_url(row))
        row.refresh_from_db()
        self.assertEqual((row.delivery, row.delivery_reason), ('paper', REASON_PRINTED))
        self.assertIsNone(claim_email(FORMAL, doc, channel=SignedOriginal.CHANNEL_RENTAL, email_to='p@example.com'))

    def test_unsigned_mailed_and_broken_originals_are_refused(self):
        with patch.object(LocalKeyBackend, 'sign_digest', side_effect=SigningUnavailable('down')):
            held = self.row(self.receipt('אשראי'))
        self.assertEqual(self.client.post(print_url(held)).json(), {'error': NOT_SIGNED_YET})

        mailed = self.row(self.receipt('אשראי'))
        SignedOriginal.objects.filter(pk=mailed.pk).update(sent_at=timezone.now())
        response = self.client.post(print_url(mailed))
        self.assertEqual((response.status_code, response.json()), (409, {'error': ALREADY_MAILED}))

        broken = self.row(self.receipt('אשראי'))
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('UPDATE signed_originals SET pdf = %s WHERE id = %s', [b'%PDF-broken', broken.pk])
        response = self.client.post(print_url(broken))
        self.assertEqual((response.status_code, response.json()), (500, {'error': STORED_FILE_BROKEN}))
        self.assertIsNone(SignedOriginal.objects.get(pk=broken.pk).paper_original_printed_at)
        # Nor is it mailed.
        self.assertIsNone(claim_email(FORMAL, FormalDocument.objects.get(pk=broken.source_id),
                                      channel=SignedOriginal.CHANNEL_RENTAL, email_to='p@example.com'))

    def test_the_hand_delivery_list(self):
        cash = self.receipt('מזומן')
        self.receipt('אשראי')
        response = self.client.get(ORIGINALS, {'delivery': 'paper', 'printed': 'false'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        item = body['results'][0]
        self.assertEqual(set(item), {
            'id', 'number', 'kind', 'document_type_label', 'customer_name', 'document_date', 'total',
            'delivery', 'delivery_reason', 'signed_at', 'sent_at', 'paper_original_printed_at',
            # Added with the signed archive (test_signed_archive.py); the fields above are unchanged.
            'purpose', 'sha256', 'size',
        })
        self.assertEqual(item['purpose'], 'original')
        self.assertEqual((item['number'], item['kind'], item['document_type_label'], item['total']),
                         (cash.document_number, 'formal', 'קבלה', '100.00'))
        self.assertEqual((item['delivery'], item['delivery_reason']), ('paper', REASON_CASH))
        self.assertEqual(item['customer_name'], self.kid.full_name)
        self.assertEqual(item['document_date'], '2026-09-18')

        self.client.post(print_url(self.row(cash)))
        self.assertEqual(self.client.get(ORIGINALS, {'delivery': 'paper', 'printed': 'false'}).json()['count'], 0)
        self.assertEqual(self.client.get(ORIGINALS, {'printed': 'true'}).json()['count'], 1)
        self.assertEqual(self.client.get(ORIGINALS, {'limit': 1, 'offset': 1}).json()['count'], 2)
        self.assertEqual(len(self.client.get(ORIGINALS, {'limit': 1, 'offset': 1}).json()['results']), 1)
        self.assertEqual(self.client.get(ORIGINALS, {'delivery': 'fax'}).status_code, 400)

    def test_the_status_panel(self):
        self.receipt('מזומן')
        self.receipt('אשראי')
        body = self.client.get(STATUS).json()
        self.assertEqual(set(body), {
            'enabled', 'consent_enforced', 'backend', 'key_id', 'cert_fingerprint', 'cert_subject',
            'last_signed_at', 'counts',
        })
        self.assertEqual((body['enabled'], body['consent_enforced'], body['backend']), (True, False, 'local'))
        self.assertTrue(body['key_id'].startswith('local:'))
        self.assertEqual(len(body['cert_fingerprint']), 64)
        self.assertIn('516504412', body['cert_subject'])
        self.assertIsNotNone(body['last_signed_at'])
        self.assertEqual(body['counts'], {'held': 0, 'paper_pending': 1, 'signed_today': 2})


class SigningPermissionsTests(RegisterFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.partner = make_user('partner-signing@test', UserProfile.ROLE_PARTNER)

    @signing_on()
    def test_the_office_endpoints_are_for_managers(self):
        row = SignedOriginal.objects.create(number='RC-2026-000777', kind='formal', source_id='x')
        for method, url in (('get', STATUS), ('get', ORIGINALS), ('post', print_url(row))):
            self.client.force_authenticate(None)
            self.assertEqual(getattr(self.client, method)(url).status_code, 401, url)
            self.client.force_authenticate(self.partner)
            self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)

    @signing_on()
    def test_the_certificate_is_public(self):
        response = self.client.get(CERTIFICATE)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['configured'])
        self.assertTrue(body['pem'].startswith('-----BEGIN CERTIFICATE-----'))
        self.assertEqual(len(body['fingerprint_sha256']), 64)
        self.assertIn('קוגומלו גרופ בע"מ', body['subject'])
        self.assertTrue(body['not_before'] < body['not_after'])
        # A stale token in the browser does not lock anyone out of a public page.
        self.assertEqual(self.client.get(CERTIFICATE, HTTP_AUTHORIZATION='Token stale').status_code, 200)

    @override_settings(SIGNING_CERT_PEM='')
    def test_no_certificate_yet(self):
        with patch('apps.documents.signing.certificate.CERT_PATH') as path:
            path.exists.return_value = False
            body = self.client.get(CERTIFICATE).json()
        self.assertEqual(body, {
            'configured': False, 'pem': '', 'fingerprint_sha256': '', 'subject': '',
            'not_before': None, 'not_after': None,
        })


# ── consent for business customers ──────────────────────────────────────────

class BusinessCustomerConsentTests(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-consent@test', UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner-consent@test', UserProfile.ROLE_PARTNER)
        self.customer = BusinessCustomer.objects.create(first_name='סטודיו', last_name='אור', email='or@example.com')

    def url(self):
        return f'/api/v1/customers/business-customers/{self.customer.pk}/computerized-consent/'

    def test_the_office_records_and_withdraws_consent(self):
        self.client.force_authenticate(self.manager)
        self.assertFalse(self.client.get(f'/api/v1/customers/business-customers/{self.customer.pk}/').json()[
            'accepts_computerized_documents'])

        given = self.client.post(self.url(), {'consent': True}, format='json')
        self.assertEqual(given.status_code, 200)
        self.assertTrue(given.json()['accepts_computerized_documents'])
        self.assertIsNotNone(given.json()['computerized_docs_consent_at'])
        self.assertEqual(given.json()['id'], str(self.customer.pk))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.computerized_docs_consent_source, CONSENT_SOURCE_CRM)

        withdrawn = self.client.post(self.url(), {'consent': False}, format='json')
        self.assertFalse(withdrawn.json()['accepts_computerized_documents'])
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.computerized_docs_consent_revoked_at)

    def test_only_a_json_boolean_and_only_a_manager(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(self.url(), {'consent': 'yes'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url(), {}, format='json').status_code, 400)
        self.client.force_authenticate(self.partner)
        self.assertEqual(self.client.post(self.url(), {'consent': True}, format='json').status_code, 403)

    def test_the_card_cannot_write_consent_by_editing(self):
        self.client.force_authenticate(self.manager)
        self.client.patch(f'/api/v1/customers/business-customers/{self.customer.pk}/',
                          {'computerized_docs_consent_at': '2026-01-01T00:00:00Z'}, format='json')
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.computerized_docs_consent_at)

    @signing_on(COMPUTERIZED_CONSENT_ENFORCED=True)
    def test_enforced_consent_holds_a_business_customers_credit_note(self):
        with patch('apps.core.credit_note_email.send_resend_email') as resend:
            with self.captureOnCommitCallbacks(execute=True):
                doc = service.create_credit_invoice({
                    'client_type': 'business', 'business_customer_id': str(self.customer.pk),
                    'credit_invoice_details': {
                        'document_date': date(2026, 9, 18), 'credit_reason': 'זיכוי',
                        'credit_amount_before_vat': Decimal('100.00'), 'linked_invoice_id': 'TI-2026-000001',
                    },
                })
        resend.assert_not_called()
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertEqual((row.delivery, row.delivery_reason), ('held', REASON_NO_CONSENT))
