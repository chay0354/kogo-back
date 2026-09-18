"""
A customer holding an issued document is never deleted — through the API or the
admin — because CASCADE took the receipts with the family and SET_NULL left the
other documents with no customer on them. A customer with nothing issued still
deletes as before.
"""
from datetime import date
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.customers.admin import ChildAdmin, FamilyAdmin, InvoiceAdmin
from apps.customers.financial_models import Invoice, InvoiceChild
from apps.customers.models import BusinessCustomer, Child, Family, Payment
from apps.customers.tests.test_fixtures import create_test_branch, create_test_child, create_test_family
from apps.documents.models import FormalDocument
from apps.store.admin import StoreInvoiceAdmin
from apps.store.models import StoreInvoice


def manager_client():
    user = get_user_model().objects.create_user(
        username='retention-manager@test.com', email='retention-manager@test.com', password='x!12345678',
    )
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client, user


def issue_receipt(family, child=None, number='IR-2026-000001'):
    invoice = Invoice.objects.create(
        invoice_number=number, family=family, amount=Decimal('250.00'), status='paid',
        payment_method='credit_card', payment_type='recurring', payer_name=family.name,
        invoice_date=timezone.now(),
    )
    if child is not None:
        InvoiceChild.objects.create(invoice=invoice, child=child)
    return invoice


class ChildDeleteKeepsDocumentsTests(TestCase):
    def setUp(self):
        self.client, self.user = manager_client()
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)

    def delete(self, child):
        return self.client.delete(f'/api/v1/customers/children/{child.id}/')

    def test_a_child_named_on_a_receipt_is_not_deleted_and_the_receipt_stays_whole(self):
        invoice = issue_receipt(self.family, self.child)

        response = self.delete(self.child)

        self.assertEqual(response.status_code, 400)
        self.assertIn('7 שנים', response.data['error'])
        self.assertTrue(Child.objects.filter(pk=self.child.pk).exists())
        self.assertTrue(Invoice.objects.filter(pk=invoice.pk).exists())
        self.assertEqual(InvoiceChild.objects.filter(invoice=invoice, child=self.child).count(), 1)

    def test_a_child_with_a_completed_charge_is_not_deleted(self):
        Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, payment_type='one_time',
            status='completed', base_amount=Decimal('100'), final_amount=Decimal('100'),
        )

        self.assertEqual(self.delete(self.child).status_code, 400)
        self.assertTrue(Child.objects.filter(pk=self.child.pk).exists())

    def test_a_child_on_a_hand_issued_document_is_not_deleted(self):
        FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='existing',
            child=self.child, document_date=date(2026, 9, 1), total_amount=Decimal('118'),
        )

        self.assertEqual(self.delete(self.child).status_code, 400)
        self.assertEqual(FormalDocument.objects.get(document_number='TI-2026-000001').child_id, self.child.id)

    def test_a_child_on_a_store_sale_is_not_deleted(self):
        StoreInvoice.objects.create(
            invoice_number='ST-2026-000001', child=self.child, total_amount=Decimal('50'),
            payment_method='cash', payment_status='completed',
        )

        self.assertEqual(self.delete(self.child).status_code, 400)

    def test_a_draft_alone_does_not_hold_the_child(self):
        FormalDocument.objects.create(
            document_number='D-ABCDEF12', document_type='draft', client_type='existing',
            child=self.child, document_date=date(2026, 9, 1),
        )

        self.assertEqual(self.delete(self.child).status_code, 204)

    def test_a_child_with_nothing_issued_still_deletes(self):
        sibling = create_test_child(family=self.family, first_name='Sibling', id_number='111111118')

        self.assertEqual(self.delete(sibling).status_code, 204)
        self.assertFalse(Child.objects.filter(pk=sibling.pk).exists())

    def test_the_last_child_goes_but_a_family_holding_receipts_stays(self):
        # The receipt names another (already merged away) child's family, not this child.
        invoice = issue_receipt(self.family)

        self.assertEqual(self.delete(self.child).status_code, 204)

        self.assertTrue(Family.objects.filter(pk=self.family.pk).exists())
        self.assertTrue(Invoice.objects.filter(pk=invoice.pk).exists())


class FamilyAndMerchantDeleteKeepsDocumentsTests(TestCase):
    def setUp(self):
        self.client, self.user = manager_client()
        self.family = create_test_family(branch=create_test_branch())

    def test_a_family_with_receipts_is_not_deleted(self):
        invoice = issue_receipt(self.family)

        response = self.client.delete(f'/api/v1/customers/families/{self.family.id}/')

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Invoice.objects.filter(pk=invoice.pk).exists())

    def test_a_family_with_nothing_issued_still_deletes(self):
        response = self.client.delete(f'/api/v1/customers/families/{self.family.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Family.objects.filter(pk=self.family.pk).exists())

    def test_a_business_customer_with_documents_is_not_deleted(self):
        customer = BusinessCustomer.objects.create(first_name='סטודיו', last_name='בע"מ', company_number='514000000')
        FormalDocument.objects.create(
            document_number='TI-2026-000002', document_type='tax_invoice', client_type='business',
            business_customer=customer, document_date=date(2026, 9, 1), total_amount=Decimal('118'),
        )

        response = self.client.delete(f'/api/v1/customers/business-customers/{customer.id}/')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            FormalDocument.objects.get(document_number='TI-2026-000002').business_customer_id, customer.id,
        )


class AdminNeverEditsAnIssuedDocumentTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get('/admin/')
        self.request.user = get_user_model().objects.create_superuser('root', 'root@test.com', 'x!12345678')
        self.site = AdminSite()
        self.family = create_test_family(branch=create_test_branch())
        self.child = create_test_child(family=self.family)

    def test_receipts_and_store_sales_are_view_only(self):
        invoice = issue_receipt(self.family, self.child)
        sale = StoreInvoice.objects.create(
            invoice_number='ST-2026-000009', total_amount=Decimal('50'), payment_method='cash',
            payment_status='completed',
        )
        for admin_class, obj in ((InvoiceAdmin, invoice), (StoreInvoiceAdmin, sale)):
            model_admin = admin_class(type(obj), self.site)
            self.assertFalse(model_admin.has_add_permission(self.request))
            self.assertFalse(model_admin.has_change_permission(self.request, obj))
            self.assertFalse(model_admin.has_delete_permission(self.request, obj))
            self.assertTrue(model_admin.has_view_permission(self.request, obj))

    def test_a_family_or_child_holding_a_receipt_cannot_be_deleted_in_the_admin(self):
        issue_receipt(self.family, self.child)

        self.assertFalse(FamilyAdmin(Family, self.site).has_delete_permission(self.request, self.family))
        self.assertFalse(ChildAdmin(Child, self.site).has_delete_permission(self.request, self.child))

    def test_a_family_with_nothing_issued_can_still_be_deleted_in_the_admin(self):
        self.assertTrue(FamilyAdmin(Family, self.site).has_delete_permission(self.request, self.family))


class ChargesAreNotRewrittenTests(TestCase):
    """The lesson receipt is printed from its charge: PUT, PATCH and DELETE are gone."""

    def setUp(self):
        self.client, self.user = manager_client()
        branch = create_test_branch()
        self.family = create_test_family(branch=branch)
        self.child = create_test_child(family=self.family)
        self.payment = Payment.objects.create(
            child=self.child, family=self.family, branch=branch, payment_type='one_time',
            status='completed', base_amount=Decimal('250'), final_amount=Decimal('250'),
        )

    def test_a_charge_cannot_be_edited_or_deleted(self):
        url = f'/api/v1/customers/payments/{self.payment.id}/'

        self.assertEqual(self.client.patch(url, {'final_amount': '1.00'}, format='json').status_code, 405)
        self.assertEqual(self.client.put(url, {'final_amount': '1.00'}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.final_amount, Decimal('250'))

    def test_a_charge_can_still_be_read(self):
        self.assertEqual(self.client.get(f'/api/v1/customers/payments/{self.payment.id}/').status_code, 200)
