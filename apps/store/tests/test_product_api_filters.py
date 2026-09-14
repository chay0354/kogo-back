"""
Store product API: where a product is, and what a save is allowed to change.

Stock lives in size rows. A product's own `branch` only holds the branch of its
FIRST row, and website-synced products carry none at all, so a location filter
that asks only that field answers with the wrong products.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, City
from apps.store.models import StoreProduct, StoreProductSize

User = get_user_model()


def rows(response):
    data = response.data
    return data if isinstance(data, list) else data.get('results', [])


def names(response):
    return sorted(row['name'] for row in rows(response))


class StoreProductLocationFilterTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='store-mgr', password='x', is_staff=True)
        profile = getattr(self.user, 'profile', None)
        if profile is not None:
            profile.role = 'manager'
            profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        city = City.objects.create(name='חיפה')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)

        # Stock in two branches; `branch` holds only the first row's branch —
        # exactly what the serializer leaves behind on a real save.
        self.split = StoreProduct.objects.create(
            name='מוצר בשני סניפים', category='ביגוד',
            sale_price=Decimal('100'), cost_price=Decimal('10'),
            stock_quantity=9, branch=self.north,
        )
        StoreProductSize.objects.create(product=self.split, size='S', stock_quantity=2, branch=self.north)
        StoreProductSize.objects.create(product=self.split, size='M', stock_quantity=7, branch=self.south)

        # Synced from the website: no branch of its own, all stock in rows.
        self.synced = StoreProduct.objects.create(
            name='מוצר מהאתר', category='ביגוד',
            sale_price=Decimal('80'), cost_price=Decimal('8'),
            stock_quantity=4, branch=None, website_legacy_id=999,
        )
        StoreProductSize.objects.create(product=self.synced, size='L', stock_quantity=4, branch=self.south)

        # Sold by delivery only, no rows at all.
        self.delivery_only = StoreProduct.objects.create(
            name='מוצר במשלוח', category='אביזרים',
            sale_price=Decimal('50'), cost_price=Decimal('5'),
            stock_quantity=40, branch=None,
        )

        # Has both: a branch row and a delivery row.
        self.mixed = StoreProduct.objects.create(
            name='מוצר מעורב', category='אביזרים',
            sale_price=Decimal('60'), cost_price=Decimal('6'),
            stock_quantity=5, branch=self.north,
        )
        StoreProductSize.objects.create(product=self.mixed, size='', stock_quantity=3, branch=self.north)
        StoreProductSize.objects.create(product=self.mixed, size='', stock_quantity=2, branch=None)

    def get(self, **params):
        return self.client.get('/api/v1/store/products/', params)

    def test_branch_filter_finds_stock_held_in_size_rows(self):
        """The south branch holds rows of two products and is no product's `branch`."""
        self.assertEqual(names(self.get(branch=str(self.south.id))), ['מוצר בשני סניפים', 'מוצר מהאתר'])

    def test_branch_filter_still_finds_the_products_own_branch(self):
        self.assertEqual(names(self.get(branch=str(self.north.id))), ['מוצר בשני סניפים', 'מוצר מעורב'])

    def test_delivery_filter_is_not_everything_without_a_branch(self):
        """Only real delivery stock — not every product whose `branch` is empty."""
        self.assertEqual(names(self.get(branch='delivery')), ['מוצר במשלוח', 'מוצר מעורב'])

    def test_delivery_filter_excludes_a_synced_product_stocked_in_a_branch(self):
        self.assertNotIn('מוצר מהאתר', names(self.get(branch='delivery')))

    def test_each_product_is_listed_once(self):
        """Rows are matched with EXISTS, so two matching rows must not duplicate."""
        listed = [row['name'] for row in rows(self.get(branch=str(self.north.id)))]
        self.assertEqual(len(listed), len(set(listed)))

    def test_all_still_returns_everything(self):
        self.assertEqual(len(names(self.get(branch='all'))), 4)

    def test_unknown_sort_field_falls_back_instead_of_500(self):
        response = self.get(sort_by='bogus; drop table')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(names(response), sorted(names(response)))

    def test_known_sort_field_still_sorts(self):
        response = self.get(sort_by='sale_price', sort_order='desc')
        self.assertEqual(response.status_code, 200)
        prices = [float(row['sale_price']) for row in rows(response)]
        self.assertEqual(prices, sorted(prices, reverse=True))


class StoreProductSaveRulesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='store-mgr2', password='x', is_staff=True)
        profile = getattr(self.user, 'profile', None)
        if profile is not None:
            profile.role = 'manager'
            profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def create(self, **over):
        payload = {
            'name': 'מוצר חדש', 'category': '', 'size': '',
            'cost_price': 10, 'sale_price': 100, 'delivery_price': 0,
            'branch': None, 'stock_quantity': 5, 'min_stock_alert': 3,
            'image_url': '', 'notes': '', 'branch_only': False, 'size_stocks': [],
        }
        payload.update(over)
        return self.client.post('/api/v1/store/products/', payload, format='json')

    def test_blank_category_falls_back_to_the_default(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['category'], 'כללי')

    def test_whitespace_category_falls_back_to_the_default(self):
        response = self.create(category='   ')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['category'], 'כללי')

    def test_a_real_category_is_kept(self):
        response = self.create(category='ביגוד')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['category'], 'ביגוד')

    def test_blank_category_on_edit_does_not_fail_the_save(self):
        product = StoreProduct.objects.create(
            name='קיים', category='ביגוד',
            sale_price=Decimal('100'), cost_price=Decimal('10'), stock_quantity=1)
        response = self.client.patch(
            f'/api/v1/store/products/{product.id}/', {'category': ''}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['category'], 'כללי')

    def test_a_product_priced_at_or_below_cost_can_still_be_edited(self):
        """Renaming it must not be refused because of prices it already had."""
        product = StoreProduct.objects.create(
            name='ישן', category='ביגוד',
            sale_price=Decimal('50'), cost_price=Decimal('50'), stock_quantity=1)

        rename = self.client.patch(
            f'/api/v1/store/products/{product.id}/', {'name': 'שם חדש'}, format='json')
        self.assertEqual(rename.status_code, 200, rename.data)

        # The dialog re-sends the unchanged prices with every save.
        resend = self.client.patch(
            f'/api/v1/store/products/{product.id}/',
            {'name': 'שם אחר', 'cost_price': 50, 'sale_price': 50},
            format='json')
        self.assertEqual(resend.status_code, 200, resend.data)

        product.refresh_from_db()
        self.assertEqual(product.name, 'שם אחר')

    def test_such_a_product_can_be_repaired(self):
        product = StoreProduct.objects.create(
            name='ישן', category='ביגוד',
            sale_price=Decimal('50'), cost_price=Decimal('50'), stock_quantity=1)
        response = self.client.patch(
            f'/api/v1/store/products/{product.id}/', {'sale_price': 80}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_moving_a_price_into_that_state_is_still_refused(self):
        product = StoreProduct.objects.create(
            name='תקין', category='ביגוד',
            sale_price=Decimal('100'), cost_price=Decimal('10'), stock_quantity=1)
        response = self.client.patch(
            f'/api/v1/store/products/{product.id}/', {'cost_price': 120}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_creating_one_in_that_state_is_still_refused(self):
        self.assertEqual(self.create(cost_price=100, sale_price=100).status_code, 400)
