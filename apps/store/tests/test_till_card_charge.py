"""
The till's typed-card charge (store/payment/charge-card/) sells from the store
and nothing else, and never charges one checkout twice.

Owner's decisions, 23.9.2026:
* a store purchase does not save the card on the child and does not touch the
  standing order — it used to write the card over the standing order's own, or
  open a new "active" standing order at 0 ₪;
* when Tranzila does not answer, the sale is not called a decline: it stays
  pending, marked, and the same checkout cannot be charged again until someone
  has looked in Tranzila.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import Child, Family, Payment, RecurringPayment
from apps.store.models import StoreInvoice, StoreProduct, StoreSale
from apps.store.views import TILL_CHARGE_UNCERTAIN_MARK

APPROVED = {'success': True, 'transaction_id': '469194', 'confirmation_code': '0000777',
            'response_code': '000', 'token': 'tok-from-the-card'}
DECLINED = {'success': False, 'error': 'העסקה נדחתה', 'response_code': '033'}
NO_ANSWER = {'success': False, 'error': 'Request timed out', 'uncertain': True}

PROD = dict(
    TRANZILA_PROD_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='prod_rest_terminal',
    TRANZILA_PROD_SUPPLIER='prod_supplier',
    TRANZILA_PROD_PUBLIC_KEY='prod_pk',
    TRANZILA_PROD_SECRET_KEY='prod_sk',
)


@override_settings(**PROD)
class TillCardCharge(TestCase):
    def setUp(self):
        manager = TestDataFactory.create_user(username='till@test.com', role=UserProfile.ROLE_MANAGER)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')
        branch = Branch.objects.create(name='ראשי')
        family = Family.objects.create(name='כהן', phone='0501111111', branch=branch)
        self.child = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        self.pants = StoreProduct.objects.create(
            name='מכנס קפוארה', category='ביגוד', cost_price=Decimal('30'), sale_price=Decimal('90'),
            stock_quantity=10,
        )

    def charge(self, *, key=None, walk_in=True, gateway=APPROVED):
        body = {
            'items': [{'product_id': str(self.pants.id), 'quantity': 1}],
            'card_details': {
                'card_number': '4580458045804580', 'expiry_month': 12, 'expiry_year': 2030,
                'cvv': '111', 'card_holder_id': '123456789',
            },
        }
        if walk_in:
            body['customer_info'] = {'name': 'לקוח מזדמן', 'phone': '0500000000'}
        else:
            body['child_id'] = str(self.child.id)
        if key:
            body['idempotency_key'] = key
        with patch.object(TranzilaService, 'charge_with_card', return_value=gateway) as charge:
            res = self.client.post('/api/v1/store/payment/charge-card/', body, format='json')
        return res, charge

    # --- a store purchase stays a store purchase -------------------------

    def test_a_walk_in_is_sold_to_and_charged_on_the_business_terminal(self):
        res, charge = self.charge(key='till-1')

        self.assertEqual(res.status_code, 200, res.data)
        charge.assert_called_once()
        invoice = StoreInvoice.objects.get()
        self.assertEqual(invoice.payment_status, 'completed')
        self.assertEqual((invoice.customer_name, invoice.customer_phone), ('לקוח מזדמן', '0500000000'))
        self.assertIsNone(invoice.child_id)
        self.assertEqual(StoreSale.objects.get().product, self.pants)
        self.pants.refresh_from_db()
        self.assertEqual(self.pants.stock_quantity, 9)

    def test_a_child_charged_by_card_gets_no_standing_order(self):
        res, _ = self.charge(walk_in=False)

        self.assertTrue(res.data['success'], res.data)
        self.assertFalse(res.data['token_saved'])
        self.assertFalse(RecurringPayment.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_a_child_charged_by_card_keeps_the_standing_order_card(self):
        standing = RecurringPayment.objects.create(
            child=self.child, tranzila_token='the-standing-order-card', status='active', amount=Decimal('235'),
            start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1),
        )

        self.charge(walk_in=False)

        standing.refresh_from_db()
        self.assertEqual(standing.tranzila_token, 'the-standing-order-card')
        self.assertEqual(RecurringPayment.objects.count(), 1)

    # --- one checkout, one charge -----------------------------------------

    def test_repeating_a_checkout_that_went_through_charges_nothing(self):
        self.charge(key='till-2')
        res, charge = self.charge(key='till-2')

        charge.assert_not_called()
        self.assertTrue(res.data['already_paid'])
        self.assertEqual(StoreInvoice.objects.count(), 1)
        self.assertEqual(StoreSale.objects.count(), 1)

    def test_no_answer_is_not_a_decline(self):
        res, _ = self.charge(key='till-3', gateway=NO_ANSWER)

        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.data['uncertain'])
        invoice = StoreInvoice.objects.get()
        self.assertEqual(invoice.payment_status, 'pending')
        self.assertEqual(invoice.tranzila_confirmation_code, TILL_CHARGE_UNCERTAIN_MARK)
        self.assertFalse(StoreSale.objects.exists())

    def test_after_no_answer_the_same_checkout_is_not_charged_again(self):
        self.charge(key='till-4', gateway=NO_ANSWER)
        res, charge = self.charge(key='till-4', gateway=APPROVED)

        charge.assert_not_called()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.data['uncertain'])

    def test_after_a_decline_a_new_checkout_may_try_again(self):
        first, _ = self.charge(key='till-5', gateway=DECLINED)
        self.assertEqual(first.status_code, 400)
        self.assertEqual(StoreInvoice.objects.get().payment_status, 'failed')

        second, charge = self.charge(key='till-6', gateway=APPROVED)

        charge.assert_called_once()
        self.assertTrue(second.data['success'])

    def test_the_settings_screen_without_a_key_charges_as_before(self):
        res, charge = self.charge()

        charge.assert_called_once()
        self.assertTrue(res.data['success'])
