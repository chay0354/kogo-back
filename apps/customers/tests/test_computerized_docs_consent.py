"""Consent to receive tax documents by email, and the markings on the subscription invoice.

סעיף 18ב(ג) permits sending a computerized document only to a customer who
consented and has not withdrawn that consent — so the record has to survive a
withdrawal and a later re-consent without guessing.
"""
import io
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from pypdf import PdfReader

from apps.core.computerized_docs import CONSENT_SOURCE_CRM, check_consent
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice
from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf
from apps.documents.issuer import ISSUER_NAME


class ComputerizedDocsConsentTests(TestCase):
    def setUp(self):
        self.family = TestDataFactory.create_family(email='parent@example.com')

    def test_a_family_starts_without_consent(self):
        self.assertFalse(self.family.accepts_computerized_documents)
        self.assertFalse(check_consent(self.family, 'INV-1'))

    def test_recorded_consent_counts(self):
        self.family.computerized_docs_consent_at = timezone.now()
        self.family.computerized_docs_consent_source = CONSENT_SOURCE_CRM
        self.family.save()

        self.assertTrue(self.family.accepts_computerized_documents)
        self.assertTrue(check_consent(self.family, 'INV-1'))

    def test_withdrawn_consent_stops_counting(self):
        now = timezone.now()
        self.family.computerized_docs_consent_at = now - timedelta(days=30)
        self.family.computerized_docs_consent_revoked_at = now
        self.family.save()

        self.assertFalse(self.family.accepts_computerized_documents)

    def test_consent_given_again_after_a_withdrawal_counts(self):
        now = timezone.now()
        self.family.computerized_docs_consent_revoked_at = now - timedelta(days=30)
        self.family.computerized_docs_consent_at = now
        self.family.save()

        self.assertTrue(self.family.accepts_computerized_documents)


class SubscriptionInvoicePdfMarkingsTests(TestCase):
    """תקנה 9א(א)(1)–(2) and סעיף 18ב(א) on the invoice every paying family receives."""

    def test_pdf_carries_the_issuer_line_and_both_marks(self):
        family = TestDataFactory.create_family(email='parent@example.com')
        invoice = Invoice.objects.create(
            invoice_number='INV-20260910-TEST',
            family=family,
            amount=Decimal('236.00'),
            status='paid',
            payer_name='משפחת כהן',
            invoice_date=timezone.now(),
        )

        pdf = generate_subscription_invoice_pdf(invoice)
        text = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf)).pages)

        self.assertIn('מקור', text)
        self.assertIn('מסמך ממוחשב', text)
        self.assertIn(ISSUER_NAME, text)
