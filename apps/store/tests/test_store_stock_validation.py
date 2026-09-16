"""
Stock is held per size and per location. Every path that sells must ask the
row it is about to decrement — not the product total — and must ask before a
card is charged.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Branch
from apps.core.payment_service import PaymentService
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import Child, Family
from apps.store.models import StoreInvoice, StoreProduct, StoreProductSize, StoreSale

User = get_user_model()

APPROVED = {'success': True, 'transaction_id': '777', 'confirmation_code': '0000777', 'response_code': '000'}


@override_settings(
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_SUPPLIER='prod_supplier',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
)
class StoreStockValidationTest(TestCase):
    def setUp(self):
        branch = Branch.objects.create(name='ראשי')
        family = Family.objects.create(name='כהן', phone='0501111111', branch=branch)
        self.child = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        # A shirt with none left in S but plenty in XL: the total says 40.
        self.shirt = StoreProduct.objects.create(
            name='חולצה', category='ביגוד', cost_price=Decimal('20'), sale_price=Decimal('50'), stock_quantity=40,
        )
        StoreProductSize.objects.create(product=self.shirt, size='S', stock_quantity=0, sort_order=0)
        StoreProductSize.objects.create(product=self.shirt, size='XL', stock_quantity=40, sort_order=1)
        # A cap with no size rows and nothing left.
        self.cap = StoreProduct.objects.create(
            name='כובע', category='ביגוד', cost_price=Decimal('5'), sale_price=Decimal('30'), stock_quantity=0,
        )

    def _xl(self):
        return StoreProductSize.objects.get(product=self.shirt, size='XL').stock_quantity

    def _till(self):
        user = User.objects.create_user(username='till', password='x', is_staff=True)
        profile = getattr(user, 'profile', None)
        if profile is not None:
            profile.role = 'manager'
            profile.save()
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_cash_sale_of_a_size_that_ran_out_is_refused_before_anything_is_written(self):
        with self.assertRaisesMessage(ValueError, 'אין מספיק מלאי'):
            PaymentService().create_cash_invoice(
                product_items=[{'product_id': str(self.shirt.id), 'quantity': 1, 'size': 'S'}],
                child_id=str(self.child.id),
                payment_method='cash',
            )
        self.assertEqual(StoreInvoice.objects.count(), 0)
        self.assertEqual(StoreSale.objects.count(), 0)
        self.assertEqual(self._xl(), 40)

    def test_saved_card_is_not_charged_for_a_size_that_ran_out(self):
        invoice = StoreInvoice.objects.create(
            child=self.child, total_amount=Decimal('50'), payment_method='credit_card',
            payment_status='pending', charged_with_token=True,
        )
        with patch.object(TranzilaService, 'charge_with_token', return_value=APPROVED) as charge:
            result = PaymentService().charge_store_with_token(
                token='tok_saved', invoice=invoice,
                product_items=[{'product_id': str(self.shirt.id), 'quantity': 1, 'size': 'S'}],
            )
        charge.assert_not_called()
        self.assertFalse(result['success'])
        self.assertIn('אין מספיק מלאי', result['error'])
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertEqual(StoreSale.objects.count(), 0)
        self.assertEqual(self._xl(), 40)

    def _charge_card(self, client, product):
        return client.post('/api/v1/store/payment/charge-card/', {
            'items': [{'product_id': str(product.id), 'quantity': 1, 'size': 'S' if product is self.shirt else ''}],
            'customer_info': {'name': 'אורח', 'phone': '0500000000'},
            'card_details': {
                'card_number': '4580458045804580', 'expiry_month': 12, 'expiry_year': 2030,
                'cvv': '111', 'card_holder_id': '123456789',
            },
        }, format='json')

    def test_the_till_does_not_charge_a_card_for_a_size_that_ran_out(self):
        with patch.object(TranzilaService, 'charge_with_card', return_value=APPROVED) as charge:
            response = self._charge_card(self._till(), self.shirt)
        charge.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertIn('אין מספיק מלאי', response.data['error'])
        self.assertEqual(StoreInvoice.objects.count(), 0)
        self.assertEqual(StoreSale.objects.count(), 0)
        self.assertEqual(self._xl(), 40)

    def test_the_till_does_not_charge_a_card_when_the_product_is_out(self):
        with patch.object(TranzilaService, 'charge_with_card', return_value=APPROVED) as charge:
            response = self._charge_card(self._till(), self.cap)
        charge.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertIn('אין מספיק מלאי', response.data['error'])
        self.cap.refresh_from_db()
        self.assertEqual(self.cap.stock_quantity, 0)
        self.assertEqual(StoreSale.objects.count(), 0)
