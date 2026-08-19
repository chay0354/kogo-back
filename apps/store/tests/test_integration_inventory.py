"""B2C integration inventory endpoints — same ops as the CRM store UI."""
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import Branch
from apps.store.models import InventoryAdjustment, StoreProduct, StoreProductSize


@override_settings(WEBSITE_INTEGRATION_API_KEY='test-key')
class IntegrationInventoryTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {'HTTP_X_INTEGRATION_KEY': 'test-key'}
        self.branch = Branch.objects.create(name='סניף בדיקה', is_active=True)
        self.product = StoreProduct.objects.create(
            name='חולצה',
            category='ביגוד',
            sale_price=Decimal('90.00'),
            cost_price=Decimal('10.00'),
            stock_quantity=5,
            min_stock_alert=2,
        )
        self.row_delivery = StoreProductSize.objects.create(
            product=self.product,
            size='M',
            stock_quantity=3,
            sort_order=0,
            branch=None,
        )
        self.row_branch = StoreProductSize.objects.create(
            product=self.product,
            size='M',
            stock_quantity=2,
            sort_order=1,
            branch=self.branch,
        )
        self.product.recalculate_total_stock()

    def test_unauthorized_without_key(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/inventory/'
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_get_inventory_includes_size_rows(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/inventory/'
        res = self.client.get(url, **self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body['stock_quantity'], 5)
        self.assertEqual(body['min_stock_alert'], 2)
        self.assertEqual(len(body['size_stocks']), 2)
        labels = {(r['size'], r['branch_name'], r['stock_quantity']) for r in body['size_stocks']}
        self.assertIn(('M', None, 3), labels)
        self.assertIn(('M', 'סניף בדיקה', 2), labels)

    def test_adjust_stock_receipt_on_size_row(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/adjust_stock/'
        res = self.client.post(
            url,
            {
                'quantity_delta': 4,
                'reason': 'receipt',
                'note': 'משלוח נכנס',
                'size_stock_id': str(self.row_delivery.id),
            },
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.row_delivery.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.row_delivery.stock_quantity, 7)
        self.assertEqual(self.product.stock_quantity, 9)
        adj = InventoryAdjustment.objects.get(product=self.product)
        self.assertEqual(adj.reason, 'receipt')
        self.assertEqual(adj.quantity_delta, 4)

    def test_transfer_stock_between_locations(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/transfer_stock/'
        res = self.client.post(
            url,
            {
                'quantity': 2,
                'from_size_stock_id': str(self.row_delivery.id),
                'to_size_stock_id': str(self.row_branch.id),
            },
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.row_delivery.refresh_from_db()
        self.row_branch.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.row_delivery.stock_quantity, 1)
        self.assertEqual(self.row_branch.stock_quantity, 4)
        self.assertEqual(self.product.stock_quantity, 5)

    def test_transfer_rejects_insufficient_stock(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/transfer_stock/'
        res = self.client.post(
            url,
            {
                'quantity': 99,
                'from_size_stock_id': str(self.row_delivery.id),
                'to_size_stock_id': str(self.row_branch.id),
            },
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('מלאי לא מספיק', res.json()['error'])

    def test_save_min_alert_and_replace_size_rows(self):
        url = f'/api/v1/store/integration/products/{self.product.id}/inventory/'
        res = self.client.patch(
            url,
            {
                'min_stock_alert': 1,
                'size_stocks': [
                    {'size': 'S', 'stock_quantity': 8, 'branch': None, 'sort_order': 0},
                    {'size': 'S', 'stock_quantity': 1, 'branch': str(self.branch.id), 'sort_order': 1},
                ],
            },
            format='json',
            **self.headers,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.min_stock_alert, 1)
        self.assertEqual(self.product.stock_quantity, 9)
        rows = list(self.product.size_stocks.order_by('sort_order'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].size, 'S')
        self.assertIsNone(rows[0].branch_id)
        self.assertEqual(rows[0].stock_quantity, 8)

    def test_catalog_list_includes_inventory_fields(self):
        res = self.client.get('/api/v1/store/integration/products/', **self.headers)
        self.assertEqual(res.status_code, 200)
        row = next(p for p in res.json() if p['id'] == str(self.product.id))
        self.assertIn('size_stocks', row)
        self.assertIn('min_stock_alert', row)
        self.assertEqual(len(row['sizes']), 2)

    def test_branches_list(self):
        res = self.client.get('/api/v1/store/integration/branches/', **self.headers)
        self.assertEqual(res.status_code, 200)
        names = [b['name'] for b in res.json()]
        self.assertIn('סניף בדיקה', names)

    def test_get_inventory_without_sizes_includes_place_and_low_stock(self):
        product = StoreProduct.objects.create(
            name='כובע',
            category='ביגוד',
            sale_price=Decimal('40.00'),
            cost_price=Decimal('8.00'),
            stock_quantity=10,
            min_stock_alert=12,
            branch=self.branch,
        )
        url = f'/api/v1/store/integration/products/{product.id}/inventory/'
        res = self.client.get(url, **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body['stock_quantity'], 10)
        self.assertEqual(body['min_stock_alert'], 12)
        self.assertTrue(body['is_low_stock'])
        self.assertEqual(body['branch_name'], 'סניף בדיקה')
        self.assertEqual(body['size_stocks'], [])

    def test_inventory_of_inactive_linked_product(self):
        self.product.is_active = False
        self.product.website_legacy_id = 1638
        self.product.save(update_fields=['is_active', 'website_legacy_id', 'updated_at'])
        url = f'/api/v1/store/integration/products/{self.product.id}/inventory/'
        res = self.client.get(url, **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['id'], str(self.product.id))
