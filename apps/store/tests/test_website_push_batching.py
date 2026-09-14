"""One website call per save — not one per size row.

Saving a product with size rows fires post_save for the product, for every
size row, and for the retotal each size row triggers. Pushing on each of those
made a single "ערוך מוצר" save do 2N+3 HTTP calls to the shop from inside the
open transaction, and a slow shop ran the browser past its request timeout.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings

from apps.store.models import StoreProduct
from apps.store.serializers import StoreProductSerializer
from apps.store.website_integration import schedule_product_push


@override_settings(
    WEBSITE_INTEGRATION_URL='https://shop.example',
    WEBSITE_INTEGRATION_API_KEY='test-key',
)
class WebsitePushBatchingTest(TestCase):
    def setUp(self):
        # Drain the push this create schedules, so each test starts with no
        # callback of ours already queued on the connection.
        with self.captureOnCommitCallbacks(execute=False):
            self.product = StoreProduct.objects.create(
                name='חולצה',
                category='ביגוד',
                sale_price=Decimal('100.00'),
                cost_price=Decimal('10.00'),
                stock_quantity=10,
                website_legacy_id=4399,
            )

    def _save_with_sizes(self, sizes):
        payload = {
            'sale_price': '120.00',
            'size_stocks': [
                {'size': size, 'stock_quantity': 5, 'sort_order': i, 'branch': None}
                for i, size in enumerate(sizes)
            ],
        }
        serializer = StoreProductSerializer(self.product, data=payload, partial=True)
        serializer.is_valid(raise_exception=True)
        with patch('apps.store.website_integration.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.content = b'{"updated": 1}'
            post.return_value.json.return_value = {'updated': 1}
            with self.captureOnCommitCallbacks(execute=True):
                serializer.save()
        return post

    def test_one_outbound_call_however_many_size_rows(self):
        for sizes in (['S'], ['S', 'M', 'L'], ['S', 'M', 'L', 'XL', 'XXL']):
            with self.subTest(size_rows=len(sizes)):
                post = self._save_with_sizes(sizes)
                self.assertEqual(
                    post.call_count, 1,
                    f'{len(sizes)} size rows should still be one website call',
                )
                items = post.call_args.kwargs['json']['items']
                self.assertEqual([i['legacy_id'] for i in items], [4399])

    def test_push_carries_the_committed_values(self):
        post = self._save_with_sizes(['S', 'M'])
        item = post.call_args.kwargs['json']['items'][0]
        self.assertEqual(item['price'], 120.0)
        self.assertTrue(item['in_stock'])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    def test_push_happens_after_commit_not_inside_the_transaction(self):
        """The outbound call must not run while the save holds row locks."""
        with patch('apps.store.website_integration.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.content = b'{"updated": 1}'
            post.return_value.json.return_value = {'updated': 1}
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.product.sale_price = Decimal('130.00')
                    self.product.save()
                    self.assertEqual(post.call_count, 0, 'pushed before COMMIT')
        self.assertEqual(post.call_count, 1)

    def test_rolled_back_change_is_not_announced(self):
        with patch('apps.store.website_integration.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.content = b'{"updated": 1}'
            post.return_value.json.return_value = {'updated': 1}
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.product.sale_price = Decimal('999.00')
                        self.product.save()
                        raise RuntimeError('boom')
                except RuntimeError:
                    pass
            self.assertEqual(post.call_count, 0)

            # And the abandoned batch must not stop the next save from pushing.
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.product.refresh_from_db()
                    self.product.sale_price = Decimal('140.00')
                    self.product.save()
            self.assertEqual(post.call_count, 1)

    def test_products_saved_together_share_one_call(self):
        with self.captureOnCommitCallbacks(execute=False):
            other = StoreProduct.objects.create(
                name='כובע',
                category='ביגוד',
                sale_price=Decimal('50.00'),
                cost_price=Decimal('5.00'),
                stock_quantity=3,
                website_legacy_id=4400,
            )
        with patch('apps.store.website_integration.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.content = b'{"updated": 2}'
            post.return_value.json.return_value = {'updated': 2}
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    schedule_product_push(self.product)
                    schedule_product_push(other)
                    schedule_product_push(self.product)
        self.assertEqual(post.call_count, 1)
        items = post.call_args.kwargs['json']['items']
        self.assertEqual(sorted(i['legacy_id'] for i in items), [4399, 4400])

    def test_unlinked_product_is_never_pushed(self):
        with patch('apps.store.website_integration.requests.post') as post:
            with self.captureOnCommitCallbacks(execute=True):
                StoreProduct.objects.create(
                    name='מוצר מקומי',
                    category='ביגוד',
                    sale_price=Decimal('20.00'),
                    cost_price=Decimal('2.00'),
                    stock_quantity=1,
                )
        self.assertEqual(post.call_count, 0)
