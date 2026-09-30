"""Michal Kagan's documents: the key, her run (MK), her clients as business customers of her
Business (never families), one document per payment however often it is asked for, credit
notes that never credit more than was paid, the mail in her name, and the PDF endpoint that
serves her documents only."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import Business, BusinessCategory
from apps.customers.models import BusinessCustomer, Family
from apps.documents.models import DocumentSeries, FormalDocument, SignedOriginal
from apps.documents.numbering import SERIES_LABELS, SERIES_MICHAL, continuity
from apps.documents.tests.signing_support import signing_on

KEY = 'michal-test-key'
MAIL = {
    'MICHAL_INTEGRATION_API_KEY': KEY,
    'DOCUMENT_SIGNING_ENABLED': False,
    'RESEND_API_KEY': '',
    'EMAIL_HOST': 'smtp.test',
    'EMAIL_BACKEND': 'django.core.mail.backends.locmem.EmailBackend',
    'TRANZILA_BILLING_TERMINAL': '',
}


def payment_request(external_id='pay-1', client_id='client-1', amount='400.00', **payment):
    return {
        'kind': 'payment',
        'external_id': external_id,
        'customer': {
            'external_id': client_id,
            'full_name': 'דנה לוי',
            'email': 'dana@example.com',
            'phone': '+972501234567',
        },
        'payment': {
            'amount': amount,
            'paid_at': timezone.now().isoformat(),
            'card_last_four': '4242',
            'transaction_id': '17',
            'confirmation_code': '0037569',
            'description': 'טיפול קונדליני אקטיביישן',
            **payment,
        },
    }


def refund_request(external_id='ref-1', payment_id='pay-1', amount='400.00'):
    return {
        'kind': 'refund',
        'external_id': external_id,
        'refund': {'payment_external_id': payment_id, 'amount': amount, 'reason': 'ביטול 72 שעות מראש'},
    }


@override_settings(**MAIL)
class MichalDocumentsTests(APITestCase):
    url = '/api/v1/documents/integrations/michal/documents/'

    def setUp(self):
        self.business = Business.objects.create(name='מיכל קגן')
        self.category = BusinessCategory.objects.create(business=self.business, name='כללי')
        self.year = timezone.localdate().year

    def post(self, body, key=KEY):
        headers = {'HTTP_X_INTEGRATION_KEY': key} if key else {}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, body, format='json', **headers)

    # The key

    def test_every_request_is_refused_without_the_right_key(self):
        self.assertEqual(self.post(payment_request(), key=None).status_code, 401)
        self.assertEqual(self.post(payment_request(), key='wrong').status_code, 401)
        with override_settings(MICHAL_INTEGRATION_API_KEY=''):
            self.assertEqual(self.post(payment_request()).status_code, 401)
        self.assertFalse(FormalDocument.objects.exists())

    def test_the_store_key_does_not_open_her_endpoint(self):
        with override_settings(WEBSITE_INTEGRATION_API_KEY='store-key'):
            self.assertEqual(self.post(payment_request(), key='store-key').status_code, 401)

    def test_a_bearer_token_is_accepted_too(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(self.url, payment_request(), format='json', HTTP_AUTHORIZATION=f'Bearer {KEY}')
        self.assertEqual(res.status_code, 201)

    # Payments

    def test_a_payment_gets_a_combined_document_in_her_own_run(self):
        res = self.post(payment_request())
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['document_number'], f'MK-{self.year}-000001')
        doc = FormalDocument.objects.prefetch_related('line_items', 'payments').get()
        self.assertEqual(doc.document_type, 'combined')
        self.assertEqual(doc.client_type, 'business')
        self.assertEqual((doc.business, doc.business_category), (self.business, self.category))
        self.assertIsNone(doc.branch_id)
        # ₪400 paid, VAT taken out of it: exactly ₪400 on the document.
        self.assertEqual(
            (doc.subtotal, doc.vat_amount, doc.total_amount), (Decimal('338.98'), Decimal('61.02'), Decimal('400.00')),
        )
        self.assertFalse(doc.tranzila_issued)
        payment = doc.payments.get()
        self.assertEqual((payment.payment_method, payment.card_last_four), ('credit_card', '4242'))
        self.assertEqual(payment.reference, 'אישור 0037569')
        self.assertIn('מיכל קגן', doc.line_items.get().description)
        # The office's own run is not drawn from.
        self.assertEqual(DocumentSeries.objects.get(series=SERIES_MICHAL, year=self.year).counter, 1)
        self.assertFalse(DocumentSeries.objects.filter(series='IRM').exists())

    def test_her_client_is_a_business_customer_of_her_business_never_a_family(self):
        self.post(payment_request(external_id='pay-1'))
        self.post(payment_request(external_id='pay-2', client_id='client-1'))
        customer = BusinessCustomer.objects.get()
        self.assertEqual((customer.first_name, customer.last_name), ('דנה', 'לוי'))
        self.assertEqual(customer.business, self.business)
        self.assertEqual(customer.email, 'dana@example.com')
        self.assertFalse(Family.objects.exists())
        self.assertEqual(FormalDocument.objects.filter(business_customer=customer).count(), 2)

    def test_asking_twice_gives_the_first_document_back(self):
        first = self.post(payment_request())
        again = self.post(payment_request())
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(first.json()['document_number'], again.json()['document_number'])
        self.assertEqual(FormalDocument.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_her_business_missing_refuses_rather_than_filing_elsewhere(self):
        self.business.delete()
        res = self.post(payment_request())
        self.assertEqual(res.status_code, 503)
        self.assertFalse(FormalDocument.objects.exists())
        self.assertFalse(BusinessCustomer.objects.exists())

    def test_consent_from_her_site_is_recorded(self):
        body = payment_request()
        body['customer']['computerized_docs_consent'] = True
        self.post(body)
        customer = BusinessCustomer.objects.get()
        self.assertTrue(customer.accepts_computerized_documents)
        self.assertEqual(customer.computerized_docs_consent_source, 'website')

    def test_a_late_document_says_so(self):
        self.post(payment_request(paid_at=(timezone.now() - timedelta(days=3)).isoformat()))
        self.assertIn('הופק באיחור', FormalDocument.objects.get().customer_notes)

    def test_a_payment_dated_later_than_today_is_not_called_late(self):
        # Clocks differ a little around midnight; only a document issued after the payment day is late.
        self.post(payment_request(paid_at=(timezone.now() + timedelta(days=1)).isoformat()))
        self.assertEqual(FormalDocument.objects.get().customer_notes, '')

    def test_invalid_requests_are_refused(self):
        self.assertEqual(self.post({'kind': 'payment', 'external_id': 'x'}).status_code, 400)
        self.assertEqual(self.post(payment_request(amount='0')).status_code, 400)
        self.assertEqual(self.post(payment_request(card_last_four='12ab')).status_code, 400)

    def test_the_mail_is_in_her_name_with_the_pdf(self):
        self.post(payment_request())
        message = mail.outbox[0]
        self.assertEqual(message.to, ['dana@example.com'])
        self.assertIn(f'MK-{self.year}-000001', message.subject)
        self.assertIn('מיכל קגן', message.subject)
        self.assertNotIn('קוגומלו', message.subject)
        self.assertIn('מיכל קגן', message.body)
        (filename, content, mimetype) = message.attachments[0]
        self.assertEqual((filename, mimetype), (f'MK-{self.year}-000001.pdf', 'application/pdf'))
        self.assertTrue(content.startswith(b'%PDF'))

    def test_a_client_with_no_email_gets_the_document_but_no_mail(self):
        body = payment_request()
        body['customer']['email'] = ''
        self.assertEqual(self.post(body).status_code, 201)
        self.assertEqual(len(mail.outbox), 0)

    # Refunds

    def test_a_refund_gets_a_credit_note_pointing_at_the_document(self):
        self.post(payment_request())
        res = self.post(refund_request(amount='150.00'))
        self.assertEqual(res.status_code, 201, res.content)
        credit = FormalDocument.objects.get(document_type='credit_invoice')
        original = FormalDocument.objects.get(document_type='combined')
        self.assertEqual(credit.document_number, f'CR-{self.year}-000001')
        self.assertEqual(credit.linked_document, original)
        self.assertEqual(credit.linked_document_number, original.document_number)
        self.assertEqual((credit.business_customer, credit.business), (original.business_customer, self.business))
        self.assertEqual(credit.total_amount, Decimal('150.00'))
        self.assertEqual(res.json()['linked_document_number'], original.document_number)
        # Both mails in her name: the document, then the credit note.
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('זיכוי', mail.outbox[1].subject)
        self.assertNotIn('קוגומלו', mail.outbox[1].body.split('\n\n')[1])

    def test_credits_never_exceed_what_was_paid(self):
        self.post(payment_request())
        self.assertEqual(self.post(refund_request(external_id='ref-1', amount='300.00')).status_code, 201)
        res = self.post(refund_request(external_id='ref-2', amount='150.00'))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.post(refund_request(external_id='ref-3', amount='100.00')).status_code, 201)
        self.assertEqual(FormalDocument.objects.filter(document_type='credit_invoice').count(), 2)

    def test_a_refund_asked_twice_gives_the_first_credit_back(self):
        self.post(payment_request())
        first = self.post(refund_request())
        again = self.post(refund_request())
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(FormalDocument.objects.filter(document_type='credit_invoice').count(), 1)

    def test_a_refund_of_a_payment_with_no_document_yet_is_asked_again_later(self):
        res = self.post(refund_request(payment_id='unknown'))
        self.assertEqual(res.status_code, 409)
        self.assertFalse(FormalDocument.objects.exists())

    # Reports

    def test_her_run_is_checked_for_gaps_with_the_others(self):
        self.post(payment_request(external_id='pay-1'))
        self.post(payment_request(external_id='pay-2'))
        run = next(run for run in continuity(self.year) if run.series == SERIES_MICHAL)
        self.assertEqual((run.issued, run.complete), (2, True))
        self.assertEqual(run.label, SERIES_LABELS[SERIES_MICHAL])


@override_settings(**MAIL)
class MichalDocumentPdfTests(APITestCase):
    def setUp(self):
        business = Business.objects.create(name='מיכל קגן')
        BusinessCategory.objects.create(business=business, name='כללי')
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                '/api/v1/documents/integrations/michal/documents/', payment_request(), format='json',
                HTTP_X_INTEGRATION_KEY=KEY,
            )
        self.number = res.json()['document_number']

    def pdf(self, number, key=KEY):
        headers = {'HTTP_X_INTEGRATION_KEY': key} if key else {}
        return self.client.get(reverse('michal-document-pdf', args=[number]), **headers)

    def test_her_document_is_served_as_a_pdf(self):
        res = self.pdf(self.number)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_the_key_is_required(self):
        self.assertEqual(self.pdf(self.number, key=None).status_code, 401)

    def test_a_document_of_another_business_line_is_not_served(self):
        other = FormalDocument.objects.get(document_number=self.number)
        other.pk = None
        other.document_number = f'IRM-{timezone.localdate().year}-000001'
        other.internal_notes = 'הופק ידנית'
        other.save()
        self.assertEqual(self.pdf(other.document_number).status_code, 404)


@signing_on(MICHAL_INTEGRATION_API_KEY=KEY)
class MichalSignedDocumentTests(APITestCase):
    def setUp(self):
        business = Business.objects.create(name='מיכל קגן')
        BusinessCategory.objects.create(business=business, name='כללי')

    @patch('apps.documents.michal.email.send_resend_email')
    def test_the_signed_original_goes_out_through_her_channel(self, resend):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                '/api/v1/documents/integrations/michal/documents/', payment_request(), format='json',
                HTTP_X_INTEGRATION_KEY=KEY,
            )
        self.assertEqual(res.status_code, 201, res.content)
        row = SignedOriginal.objects.get(number=res.json()['document_number'])
        self.assertEqual(row.channel, SignedOriginal.CHANNEL_MICHAL)
        self.assertEqual(row.email_to, 'dana@example.com')
        if row.pdf:
            # Signed in time for the mail: the mail carried the stored original, once.
            self.assertEqual(resend.call_count, 1)
            self.assertIsNotNone(row.sent_at)
