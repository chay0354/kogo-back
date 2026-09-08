"""The till sends each cart line's branch as an id string; the saved-card path must accept it."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.core.models import Branch
from apps.core.payment_service import PaymentService
from apps.customers.models import Child, Family, RecurringPayment
from apps.store.models import StoreInvoice, StoreProduct


class TokenChargeBranchTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        family = Family.objects.create(name='Cohen', phone='0501111111', branch=self.branch)
        self.child = Child.objects.create(
            family=family, first_name='Noa', last_name='Cohen', birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        RecurringPayment.objects.create(
            child=self.child, tranzila_token='tok_saved', status='active',
            base_amount=Decimal('300'), discount_amount=Decimal('0'), amount=Decimal('300'),
            billing_day=1, start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1),
        )
        self.product = StoreProduct.objects.create(
            name='חולצה', category='clothing', size='', cost_price=Decimal('20'), sale_price=Decimal('50'),
            branch=self.branch, stock_quantity=10,
        )

    def _charge(self, item):
        with patch.object(PaymentService, 'charge_store_with_token', return_value={'success': True}) as charge:
            result = PaymentService().initiate_store_purchase(product_items=[item], child_id=str(self.child.id))
        charge.assert_called_once()
        return result, StoreInvoice.objects.get(child=self.child)

    def test_branch_id_string_from_the_till_is_accepted(self):
        _, invoice = self._charge({'product_id': str(self.product.id), 'quantity': 1, 'size': '', 'branch': str(self.branch.id)})
        self.assertEqual(invoice.branch_id, self.branch.id)
        self.assertTrue(invoice.charged_with_token)

    def test_delivery_line_falls_back_to_the_product_branch(self):
        _, invoice = self._charge({'product_id': str(self.product.id), 'quantity': 1, 'size': '', 'branch': 'delivery'})
        self.assertEqual(invoice.branch_id, self.branch.id)

    def test_no_branch_on_the_line_uses_the_product_branch(self):
        _, invoice = self._charge({'product_id': str(self.product.id), 'quantity': 1})
        self.assertEqual(invoice.branch_id, self.branch.id)
