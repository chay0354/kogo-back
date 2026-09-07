"""
The owner's question about a store order is "who is this" — a phone number,
an address, an order number — and the answer has to be the order, with its
PDF. These hold the list's search to every field the buyer typed.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.store.invoice_pdf import generate_store_invoice_pdf
from apps.store.models import StoreInvoice

User = get_user_model()
URL = '/api/v1/store/invoices/'


class StoreInvoiceSearchTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='אם המושבות', city=city)
        user = User.objects.create_user(username='manager-search@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.web = StoreInvoice.objects.create(
            invoice_number='ST-WEB-1', customer_name='רותי ניסן', customer_phone='0521234567',
            customer_email='ruti@example.com', shipping_address='הרצל 12, כפר סבא',
            customer_notes='להשאיר אצל השכן', website_order_number='CG-260830-ABCD',
            total_amount=Decimal('149.00'), payment_method='credit_card', payment_status='completed',
        )
        self.other = StoreInvoice.objects.create(
            invoice_number='ST-WEB-2', customer_name='דלית זפרני', customer_phone='0549876543',
            total_amount=Decimal('319.00'), payment_method='credit_card', payment_status='completed',
            branch=self.branch,
        )

    def _numbers(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json().get('results', res.json())
        return sorted(row['invoice_number'] for row in rows)

    def test_finds_an_order_by_phone(self):
        self.assertEqual(self._numbers(search='0521234567'), ['ST-WEB-1'])

    def test_finds_an_order_by_part_of_the_address(self):
        self.assertEqual(self._numbers(search='הרצל'), ['ST-WEB-1'])

    def test_finds_an_order_by_website_order_number_and_by_email(self):
        self.assertEqual(self._numbers(search='CG-260830'), ['ST-WEB-1'])
        self.assertEqual(self._numbers(search='ruti@'), ['ST-WEB-1'])

    def test_no_search_returns_everything(self):
        self.assertEqual(self._numbers(), ['ST-WEB-1', 'ST-WEB-2'])

    def test_the_list_carries_what_the_buyer_typed(self):
        res = self.client.get(URL, {'search': '0521234567'})
        row = res.json()['results'][0]
        self.assertEqual(row['shipping_address'], 'הרצל 12, כפר סבא')
        self.assertEqual(row['customer_notes'], 'להשאיר אצל השכן')
        self.assertEqual(row['customer_email'], 'ruti@example.com')
        self.assertEqual(row['website_order_number'], 'CG-260830-ABCD')

    def test_the_pdf_still_renders_with_an_address_and_notes(self):
        pdf = generate_store_invoice_pdf(self.web)
        self.assertTrue(pdf.startswith(b'%PDF'))
