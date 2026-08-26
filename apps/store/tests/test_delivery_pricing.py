from decimal import Decimal
from django.test import TestCase

from apps.store.models import StoreProduct
from apps.store.pricing import (
    cart_item_is_delivery,
    line_charge_amount,
    line_delivery_amount,
    order_delivery_amount,
)


class DeliveryPricingTests(TestCase):
    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='חולצה',
            sale_price=Decimal('100.00'),
            cost_price=Decimal('40.00'),
            delivery_price=Decimal('15.00'),
            branch=None,
        )

    def test_delivery_line_adds_per_unit_fee(self):
        item = {'product_id': str(self.product.id), 'quantity': 2, 'branch': 'delivery'}
        self.assertTrue(cart_item_is_delivery(item, self.product))
        self.assertEqual(line_delivery_amount(self.product, 2, item), Decimal('30.00'))
        self.assertEqual(line_charge_amount(self.product, 2, item), Decimal('230.00'))

    def test_branch_pickup_skips_delivery_fee(self):
        item = {'product_id': str(self.product.id), 'quantity': 2, 'branch': 'some-branch-id'}
        self.assertFalse(cart_item_is_delivery(item, self.product))
        self.assertEqual(line_delivery_amount(self.product, 2, item), Decimal('0.00'))
        self.assertEqual(line_charge_amount(self.product, 2, item), Decimal('200.00'))

    def test_zero_delivery_price_is_free(self):
        self.product.delivery_price = Decimal('0.00')
        item = {'quantity': 1, 'branch': 'delivery'}
        self.assertEqual(line_delivery_amount(self.product, 1, item), Decimal('0.00'))

    def test_website_shipping_is_once_per_order(self):
        other = StoreProduct.objects.create(
            name='מכנסיים',
            sale_price=Decimal('80.00'),
            cost_price=Decimal('20.00'),
            delivery_price=Decimal('15.00'),
        )
        cheaper = StoreProduct.objects.create(
            name='גרביים',
            sale_price=Decimal('20.00'),
            cost_price=Decimal('5.00'),
            delivery_price=Decimal('10.00'),
        )
        self.assertEqual(
            order_delivery_amount([self.product, other, cheaper], is_delivery=True),
            Decimal('15.00'),
        )
        self.assertEqual(
            order_delivery_amount([self.product, other, cheaper], is_delivery=False),
            Decimal('0.00'),
        )
