"""Tests for B2C website catalog sync — must not clobber CRM sale prices."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.store.models import StoreProduct
from apps.store.website_integration import (
    link_product_to_website,
    sync_products_from_website,
    unlink_product_from_website,
    update_product_from_website,
)


@override_settings(
    WEBSITE_INTEGRATION_URL='https://shop.example',
    WEBSITE_INTEGRATION_API_KEY='test-key',
)
class SyncProductsFromWebsiteTest(TestCase):
    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='בובה של -הפוך- מדברת ושרה',
            category='המוצרים של קוגומלו',
            sale_price=Decimal('119.98'),
            cost_price=Decimal('0.00'),
            stock_quantity=10,
            website_legacy_id=4399,
        )

    def test_metadata_save_with_update_fields_preserves_concurrent_price_edit(self):
        """Pattern used by sync: stale row + update_fields must not revert sale_price."""
        stale = StoreProduct.objects.get(pk=self.product.pk)
        StoreProduct.objects.filter(pk=self.product.pk).update(sale_price=Decimal('110.00'))

        stale.name = 'בובה של -הפוך- מדברת ושרה'
        stale.category = 'המוצרים של קוגומלו'
        stale.website_legacy_id = 4399
        stale.branch = None
        stale.is_active = True
        stale.branch_only = False
        stale.save(update_fields=[
            'name', 'website_legacy_id', 'category', 'branch',
            'is_active', 'branch_only', 'updated_at',
        ])

        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price, Decimal('110.00'))

    @patch('apps.store.website_integration.requests.get')
    @patch('apps.store.website_integration.push_products_batch_to_website', return_value=0)
    def test_sync_does_not_pull_website_price_for_existing_product(self, _push, mock_get):
        self.product.sale_price = Decimal('110.00')
        self.product.save(update_fields=['sale_price', 'updated_at'])

        mock_get.return_value.json.return_value = {
            'products': [{
                'id': 4399,
                'name': 'בובה של -הפוך- מדברת ושרה',
                'price': 119.98,
                'inStock': True,
                'images': ['/images/shop/p4399_0.webp'],
                'categories': ['המוצרים של קוגומלו'],
                'purchasable': True,
            }],
        }
        mock_get.return_value.raise_for_status = lambda: None

        sync_products_from_website()

        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price, Decimal('110.00'))


@override_settings(
    WEBSITE_INTEGRATION_URL='https://shop.example',
    WEBSITE_INTEGRATION_API_KEY='test-key',
)
@patch('apps.store.website_integration.push_product_to_website', return_value=True)
class LinkProductToWebsiteTest(TestCase):
    """website_legacy_id is unique — re-pointing a website product must not 500."""

    def setUp(self):
        self.first = StoreProduct.objects.create(
            name='חולצה א',
            category='ביגוד',
            sale_price=Decimal('100.00'),
            cost_price=Decimal('0.00'),
            stock_quantity=5,
            website_legacy_id=4399,
        )
        self.second = StoreProduct.objects.create(
            name='חולצה ב',
            category='ביגוד',
            sale_price=Decimal('120.00'),
            cost_price=Decimal('0.00'),
            stock_quantity=5,
        )

    def test_relink_moves_legacy_id_off_the_previous_product(self, _push):
        link_product_to_website(product_id=str(self.second.pk), website_legacy_id=4399)

        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertIsNone(self.first.website_legacy_id)
        self.assertEqual(self.second.website_legacy_id, 4399)

    def test_relinking_the_same_product_is_idempotent(self, _push):
        link_product_to_website(product_id=str(self.first.pk), website_legacy_id=4399)

        self.first.refresh_from_db()
        self.assertEqual(self.first.website_legacy_id, 4399)

    def test_unlink_clears_legacy_id(self, _push):
        unlink_product_from_website(product_id=str(self.first.pk))

        self.first.refresh_from_db()
        self.assertIsNone(self.first.website_legacy_id)

    def test_unlink_is_idempotent(self, _push):
        unlink_product_from_website(product_id=str(self.second.pk))

        self.second.refresh_from_db()
        self.assertIsNone(self.second.website_legacy_id)


@override_settings(
    WEBSITE_INTEGRATION_URL='https://shop.example',
    WEBSITE_INTEGRATION_API_KEY='test-key',
)
@patch('apps.store.website_integration.push_product_to_website', return_value=True)
class UpdateProductFromWebsiteTest(TestCase):
    def setUp(self):
        self.product = StoreProduct.objects.create(
            name='מארז לוחמת הקפוארה של קוגומלו',
            category='המוצרים של קוגומלו',
            sale_price=Decimal('149.00'),
            cost_price=Decimal('0.00'),
            stock_quantity=10,
            website_legacy_id=12570,
        )

    def test_updates_sale_price_by_legacy_id(self, _push):
        update_product_from_website(website_legacy_id=12570, sale_price=Decimal('169.00'))

        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price, Decimal('169.00'))

    def test_branch_only_and_zero_stock(self, _push):
        update_product_from_website(
            website_legacy_id=12570,
            branch_only=True,
            in_stock=False,
        )

        self.product.refresh_from_db()
        self.assertTrue(self.product.branch_only)
        self.assertEqual(self.product.stock_quantity, 0)
