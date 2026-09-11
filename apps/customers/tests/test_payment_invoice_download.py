"""Downloading the חשבונית מס / קבלה for a charge.

It used to exist only as an attachment on the mail we sent at charge time, so a
parent or the office had no way back to it. This endpoint is that way back.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice
from apps.customers.models import Payment

User = get_user_model()


class PaymentInvoiceDownloadTests(APITestCase):
    def setUp(self):
        user = User.objects.create_user(username='manager-dl@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)
        self.payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('236.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('236.00'),
            description='מנוי חודשי',
            payment_date=timezone.now(),
        )

    def _url(self) -> str:
        return f'/api/v1/customers/payments/{self.payment.id}/invoice/'

    def test_downloads_the_pdf(self):
        Invoice.objects.create(
            invoice_number='INV-20260910-TEST',
            family=self.family,
            payment=self.payment,
            amount=Decimal('236.00'),
            status='paid',
            payer_name='משפחת כהן',
            invoice_date=timezone.now(),
        )

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('INV-20260910-TEST.pdf', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_says_so_when_no_invoice_was_issued(self):
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['error'], 'לא הופקה חשבונית לתשלום זה')
